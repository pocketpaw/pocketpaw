# ee/instinct/router.py — FastAPI router for the Instinct decision pipeline API.
# Created: 2026-03-28 — Propose, approve/reject, list pending, query audit.
# Updated: 2026-03-30 — Added GET /instinct/actions (list all with status filter),
#   GET /instinct/audit/export (JSON export), switched to singleton from ee.api.
# Updated: 2026-04-12 (Move 1 PR-A) — /approve now accepts optional edited fields.
#   When present, the server diffs the stored proposal against the edits, persists
#   a Correction, then approves. GET /instinct/corrections exposes corrections
#   scoped to a pocket or an action so the UI and agents can read them back.
# Updated: 2026-04-13 (Move 2 PR-B) — POST /instinct/actions accepts an optional
#   reasoning_trace + fabric_snapshots body so callers (and the agent tool) can
#   attach decision inputs at propose time. GET /instinct/audit/{id}?hydrate=1
#   returns the audit entry with the trace's referenced IDs expanded into Fabric
#   object snapshots, making the "Why?" drawer possible in the UI.
# Updated: 2026-05-07 (fix/rbac-guards-fabric-instinct-agent-knowledge) — all
#   endpoints now require a valid license + workspace membership. Read/propose
#   endpoints require ``instinct.read``/``instinct.propose`` (MEMBER). Approve,
#   reject, and all audit endpoints require ``instinct.approve``/``instinct.audit``
#   (ADMIN) — governance actions that trigger automations or record corrections.
#   Previously the router had zero auth.
# Updated: 2026-05-07 (feat/rbac-plan-feature-gate) — added router-level
#   ``require_plan_feature("instinct")`` so the entire Instinct API is gated to
#   business-tier (or higher) plans. Closes the plan-tier bypass where a
#   team-plan member who passed the workspace RBAC check still hit Instinct for
#   free.
# Updated: 2026-05-13 (feat/mission-control-facade) — added ``assignee`` query
#   param to GET /instinct/actions/pending (filter The Tray to a single human's
#   queue) plus POST /instinct/actions/bulk-approve and
#   POST /instinct/actions/bulk-reject. Bulk endpoints write N audit rows with
#   a shared ``bulk_id`` UUID so the bulk transaction is replay-able per item
#   and query-able as a unit.
# Updated: 2026-05-22 (RFC 05 M2b.1) — ``approve_action`` now fires a parked
#   pocket write. When the approved Action's ``parameters`` carries a
#   ``_pocket_write`` blob, the route lazy-imports
#   ``ee.cloud.pockets.instinct_bridge`` and calls ``execute_approved_write``
#   — best-effort, failures recorded on the Action, never breaking the
#   approve response. A lazy import avoids an instinct→pockets module-top
#   dependency.
# Updated: 2026-05-22 (security-review fixes for PR #1183) —
#   * BLOCKER 1: ``approve_action`` and ``bulk_approve_actions`` now
#     verify a parked ``_pocket_write`` belongs to the approver's active
#     workspace. ``require_action_any_workspace`` only checks the caller
#     holds the role somewhere; it does NOT bind the action to a
#     workspace. ``_assert_pocket_write_workspace`` raises ``Forbidden``
#     (403) when ``blob["workspace_id"]`` differs from the caller's
#     active workspace, closing a cross-tenant approval-escalation gap.
#   * BLOCKER 2: ``bulk_approve_actions`` now mirrors the single-approve
#     hook — every bulk-approved Action carrying a ``_pocket_write`` blob
#     fires ``execute_approved_write`` best-effort, so bulk-approved
#     pocket writes actually execute instead of silently stalling at
#     ``approved``.
#   * SHOULD-FIX 1: the audit ``approved_by``/``actor`` and the outcome
#     actor are now the AUTHENTICATED user id, not the free-text
#     ``approver`` request field — a caller can no longer forge the
#     audit actor. The request field stays for display only.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from pocketpaw.instinct.correction import (
    Correction,
    compute_patches,
    summarize_correction,
)
from pocketpaw.instinct.models import (
    Action,
    ActionCategory,
    ActionPriority,
    ActionStatus,
    ActionTrigger,
    AuditEntry,
)
from pocketpaw.instinct.trace import FabricObjectSnapshot, ReasoningTrace
from pocketpaw_ee.cloud._core.deps import (
    current_user,
    current_workspace_id,
    require_plan_feature,
)
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import require_action_any_workspace

logger = logging.getLogger(__name__)


def _pocket_write_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_pocket_write`` blob on an Action, or ``None``.

    The blob is the parked-write payload ``instinct_bridge`` stores under
    ``Action.parameters._pocket_write`` (method/path/params/idempotency/
    outcome + the originating ``workspace_id``). Anything that is not a
    dict-of-dict shape is treated as "no parked write".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get("_pocket_write")
    return blob if isinstance(blob, dict) else None


def _assert_pocket_write_workspace(action: Any, current_workspace: str) -> None:
    """Reject approving a parked pocket write from another workspace.

    ``require_action_any_workspace("instinct.approve")`` only proves the
    caller holds ``instinct.approve`` in SOME workspace — it does not bind
    the Action being approved to that workspace, and the
    ``instinct_actions`` table has no ``workspace_id`` column. Without this
    check a caller with ``instinct.approve`` in workspace A could approve
    a workspace-B parked write and trigger a cross-tenant backend write.

    When the Action carries a ``_pocket_write`` blob whose ``workspace_id``
    differs from the caller's active workspace, raise ``Forbidden`` (403).
    A non-pocket-write Action (no blob) is unaffected — instinct's other
    Action kinds are not tenant-bound by this column.
    """
    blob = _pocket_write_blob(action)
    if blob is None:
        return
    blob_workspace = str(blob.get("workspace_id") or "")
    if blob_workspace and blob_workspace != current_workspace:
        raise Forbidden(
            "instinct.cross_workspace_approval",
            "This action's pocket write belongs to a different workspace",
        )


router = APIRouter(
    tags=["Instinct"],
    dependencies=[Depends(require_license), Depends(require_plan_feature("instinct"))],
)


def _store():
    from pocketpaw_ee.api import get_instinct_store

    return get_instinct_store()


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ProposeRequest(BaseModel):
    pocket_id: str
    title: str
    description: str = ""
    recommendation: str = ""
    trigger: ActionTrigger
    category: ActionCategory = ActionCategory.WORKFLOW
    priority: ActionPriority = ActionPriority.MEDIUM
    parameters: dict[str, Any] = {}
    reasoning_trace: ReasoningTrace | None = Field(
        default=None,
        description=(
            "Optional decision trace: which Fabric objects / soul memories / "
            "KB articles / tool calls the agent consumed to produce this proposal. "
            "Persisted into the audit entry so the Why? drawer can expand it."
        ),
    )
    fabric_snapshots: list[FabricObjectSnapshot] = Field(
        default_factory=list,
        description=(
            "Optional snapshots of the Fabric objects referenced in the trace, "
            "captured at decision time so later live mutations don't erase the reasoning."
        ),
    )


class RejectRequest(BaseModel):
    reason: str = ""


class ApproveRequest(BaseModel):
    """Optional edits and approver metadata for an approval.

    When any of `title`, `description`, `recommendation`, `category`, `priority`,
    or `parameters` differ from the stored proposal, the server computes a
    Correction before approving. Omit the fields to approve unchanged.
    """

    approver: str = "user"
    title: str | None = None
    description: str | None = None
    recommendation: str | None = None
    category: ActionCategory | None = None
    priority: ActionPriority | None = None
    parameters: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ActionsListResponse(BaseModel):
    actions: list[Action]
    total: int


class AuditListResponse(BaseModel):
    entries: list[AuditEntry]
    total: int


class ApproveResponse(BaseModel):
    action: Action
    correction: Correction | None = Field(
        default=None,
        description="Present when the approver edited the proposal before approving.",
    )


class CorrectionsListResponse(BaseModel):
    corrections: list[Correction]
    total: int


class BulkApproveRequest(BaseModel):
    """Body for POST /instinct/actions/bulk-approve.

    ``ids`` is the list of pending action ids to flip to approved. ``note``
    is an optional operator-supplied note tagged onto every audit row in
    the bulk transaction (also surfaced in the shared ``bulk_id`` group).
    """

    ids: list[str] = Field(min_length=1)
    note: str | None = None
    approver: str = "user"


class BulkRejectRequest(BaseModel):
    """Body for POST /instinct/actions/bulk-reject.

    ``reason`` is required — the UI gates the bulk-reject button behind a
    typed reason. The server enforces non-empty so we don't end up with
    silently rejected items that confuse a later audit review.
    """

    ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)
    rejector: str = "user"


class BulkActionResponse(BaseModel):
    """Response shape for both bulk-approve and bulk-reject."""

    bulk_id: str = Field(
        description=(
            "UUID4 hex tag stamped onto every audit row written for this "
            "bulk transaction. Query ``GET /instinct/audit`` and filter "
            "client-side on ``context.bulk_id`` to recover the group."
        ),
    )
    affected: list[Action]
    missing: list[str] = Field(
        default_factory=list,
        description=(
            "IDs that did not flip — either the row didn't exist or it was "
            "not in ``pending`` state. The frontend can surface these "
            "individually so the operator knows which items still need "
            "manual attention."
        ),
    )


# ---------------------------------------------------------------------------
# Action endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/instinct/actions",
    response_model=Action,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("instinct.propose"))],
)
async def propose_action(req: ProposeRequest):
    """Propose a new action for human approval.

    Optional `reasoning_trace` and `fabric_snapshots` let callers attach the
    agent's decision inputs at propose time. They are persisted into the
    resulting audit row for later hydration via `/audit/{id}?hydrate=1`.
    """
    return await _store().propose(
        pocket_id=req.pocket_id,
        title=req.title,
        description=req.description,
        recommendation=req.recommendation,
        trigger=req.trigger,
        category=req.category,
        priority=req.priority,
        parameters=req.parameters,
        reasoning_trace=req.reasoning_trace,
        fabric_snapshots=list(req.fabric_snapshots) if req.fabric_snapshots else None,
    )


@router.get(
    "/instinct/actions/pending",
    response_model=list[Action],
    dependencies=[Depends(require_action_any_workspace("instinct.read"))],
)
async def pending_actions(
    pocket_id: str | None = Query(None),
    assignee: str | None = Query(
        None,
        description=(
            "Filter to actions awaiting approval from a specific human "
            "(user id). Drives The Tray in Mission Control so an operator "
            "only sees the items they own. When omitted, behavior is "
            "unchanged from before — every pending item is returned."
        ),
    ),
):
    """List actions waiting for human approval."""
    return await _store().pending(pocket_id=pocket_id, assignee=assignee)


@router.get(
    "/instinct/actions",
    response_model=ActionsListResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.read"))],
)
async def list_actions(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    status: str | None = Query(
        None, description="Filter by status: pending|approved|rejected|executed|failed"
    ),
    limit: int = Query(50, ge=1, le=500, description="Max actions to return"),
):
    """List all actions with optional status and pocket filters."""
    store = _store()
    status_enum = ActionStatus(status) if status else None
    actions = await store.list_actions(
        pocket_id=pocket_id,
        status=status_enum,
        limit=limit,
    )
    return ActionsListResponse(actions=actions, total=len(actions))


# Bulk endpoints must be registered BEFORE the parameterised
# ``/instinct/actions/{action_id}/approve`` and ``.../reject`` routes:
# FastAPI matches in registration order and ``bulk-approve`` would
# otherwise be eaten by ``{action_id}`` and fail validation.
@router.post(
    "/instinct/actions/bulk-approve",
    response_model=BulkActionResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.approve"))],
)
async def bulk_approve_actions(
    req: BulkApproveRequest,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> BulkActionResponse:
    """Approve N pending actions in one call.

    Each item is flipped individually (so per-item audit replay still
    works) but every audit row carries a shared ``bulk_id`` UUID under
    ``context.bulk_id``. The operator can query the audit log filtered
    by that key to recover the bulk transaction as a unit. Items that
    are missing or already resolved come back in ``missing`` rather than
    raising — a partial-success surface beats a single all-or-nothing
    failure on the operator console.

    Security (PR #1183):
      * BLOCKER 1 — before flipping anything, every requested Action is
        loaded and any parked ``_pocket_write`` is checked against the
        caller's active workspace. A single cross-workspace item fails
        the whole call with 403 — a partial bulk that silently dropped
        the foreign item would hide the escalation attempt.
      * BLOCKER 2 — after the flip, each approved Action carrying a
        ``_pocket_write`` blob fires ``execute_approved_write`` so
        bulk-approved pocket writes actually execute (the single-approve
        hook is the template).
      * SHOULD-FIX 1 — the audit actor is the authenticated user id, not
        the free-text ``req.approver`` field.
    """
    store = _store()
    approver_id = str(user.id)

    # BLOCKER 1 — verify tenancy on every requested action up front. A
    # missing id simply has no blob to check; it falls through to
    # ``bulk_approve`` and lands in ``missing``.
    for action_id in req.ids:
        action = await store.get_action(action_id)
        if action is not None:
            _assert_pocket_write_workspace(action, workspace_id)

    approved, missing, bulk_id = await store.bulk_approve(
        list(req.ids), approver=approver_id, note=req.note
    )

    # BLOCKER 2 — bulk-approved pocket writes must fire, exactly like the
    # single-approve hook. Best-effort per item: a lazy import keeps the
    # instinct package free of a module-top dependency on ee.cloud.pockets,
    # and any failure is recorded on the Action by the bridge — it must
    # never break the bulk response.
    for action in approved:
        if _pocket_write_blob(action) is not None:
            try:
                from pocketpaw_ee.cloud.pockets import instinct_bridge

                await instinct_bridge.execute_approved_write(action)
            except Exception:
                logger.exception(
                    "bulk-approve pocket-write execution failed for %s (non-fatal)",
                    action.id,
                )

    return BulkActionResponse(bulk_id=bulk_id, affected=approved, missing=missing)


@router.post(
    "/instinct/actions/bulk-reject",
    response_model=BulkActionResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.approve"))],
)
async def bulk_reject_actions(req: BulkRejectRequest) -> BulkActionResponse:
    """Reject N pending actions in one call. ``reason`` is required.

    Mirrors ``bulk_approve_actions``: shared ``bulk_id``, per-item audit
    rows, partial-success surface via ``missing``. The reason text lands
    on every audit row's ``context.reason`` and on each Action's
    ``rejected_reason`` so the soul-bridge correction pipeline still
    sees the same shape it sees on single-item rejects.
    """
    rejected, missing, bulk_id = await _store().bulk_reject(
        list(req.ids), reason=req.reason, rejector=req.rejector
    )
    return BulkActionResponse(bulk_id=bulk_id, affected=rejected, missing=missing)


@router.post(
    "/instinct/actions/{action_id}/approve",
    response_model=ApproveResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.approve"))],
)
async def approve_action(
    action_id: str,
    req: ApproveRequest | None = None,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Approve a pending action, optionally with edits.

    If the request body carries edits, the server diffs the stored proposal
    against the incoming shape and persists a Correction alongside the
    approval. Callers that want to approve unchanged can POST with no body.

    Security (PR #1183):
      * BLOCKER 1 — a parked ``_pocket_write`` must belong to the
        approver's active workspace, else 403.
      * SHOULD-FIX 1 — the audit actor + outcome actor are the
        authenticated user id, never the free-text ``req.approver``.
    """
    store = _store()
    before = await store.get_action(action_id)
    if not before:
        raise HTTPException(404, "Action not found")

    # BLOCKER 1 — reject a cross-workspace parked-write approval before
    # any state mutation. ``require_action_any_workspace`` only proved the
    # caller holds ``instinct.approve`` somewhere; this binds the Action
    # to the caller's workspace.
    _assert_pocket_write_workspace(before, workspace_id)

    req = req or ApproveRequest()
    # SHOULD-FIX 1 — the audit actor is the authenticated identity, not
    # the request body's free-text ``approver``. The body field may still
    # carry a display label, but it can never forge the audit trail.
    approver_id = str(user.id)
    after, edited_fields = _apply_edits(before, req)

    correction: Correction | None = None
    if edited_fields:
        patches = compute_patches(before, after)
        if patches:
            correction = Correction(
                action_id=before.id,
                pocket_id=before.pocket_id,
                actor=approver_id,
                patches=patches,
                context_summary=summarize_correction(before, patches),
                action_title=before.title,
            )
            await store.record_correction(correction)
            await _persist_edits(store, after, edited_fields)
            await _forward_to_soul(correction, after)

    approved = await store.approve(action_id, approver=approver_id)
    if not approved:
        raise HTTPException(404, "Action not found")

    # RFC 05 M2b.1 — when the approved Action carries a parked pocket
    # write (``parameters._pocket_write``), fire it. Best-effort: a
    # lazy import keeps the instinct package free of a module-top
    # dependency on ee.cloud.pockets, and any failure is recorded on the
    # Action by the bridge itself — it must NEVER break this approve
    # response. A non-pocket-write Action (the common case) skips this.
    if _pocket_write_blob(approved) is not None:
        try:
            from pocketpaw_ee.cloud.pockets import instinct_bridge

            await instinct_bridge.execute_approved_write(approved)
        except Exception:
            logger.exception("pocket-write execution after approval failed (non-fatal)")

    return ApproveResponse(action=approved, correction=correction)


async def _forward_to_soul(correction: Correction, action: Action) -> None:
    """Hand off to the soul bridge — always best-effort, never breaks approval."""
    try:
        from pocketpaw.instinct.correction_soul_bridge import CorrectionSoulBridge
        from pocketpaw.soul import get_soul_manager

        manager = get_soul_manager()
        if manager is None:
            return
        bridge = CorrectionSoulBridge(soul_manager=manager, store=_store())
        await bridge.record(correction, action)
    except Exception:
        logger.exception("Correction soul-bridge failed (non-fatal)")


@router.post(
    "/instinct/actions/{action_id}/reject",
    response_model=Action,
    dependencies=[Depends(require_action_any_workspace("instinct.approve"))],
)
async def reject_action(action_id: str, req: RejectRequest | None = None):
    """Reject a pending action with an optional reason."""
    reason = req.reason if req else ""
    action = await _store().reject(action_id, reason=reason)
    if not action:
        raise HTTPException(404, "Action not found")
    return action


def _apply_edits(before: Action, req: ApproveRequest) -> tuple[Action, set[str]]:
    """Return a copy of `before` with any non-null fields from `req` applied.

    Also returns the set of field names that were actually changed so the
    caller can decide whether to persist them back to the store.
    """
    edited: set[str] = set()
    update: dict[str, Any] = {}
    for field in ("title", "description", "recommendation", "category", "priority"):
        incoming = getattr(req, field)
        if incoming is not None and incoming != getattr(before, field):
            update[field] = incoming
            edited.add(field)
    if req.parameters is not None and req.parameters != before.parameters:
        update["parameters"] = req.parameters
        edited.add("parameters")
    return before.model_copy(update=update), edited


async def _persist_edits(store: Any, action: Action, edited: set[str]) -> None:
    """Persist the human edits back to the store before the approve update.

    Approval itself touches `status` and `approved_*` so we only write the
    content fields that actually changed — no redundant updates.
    """
    import aiosqlite

    assignments: list[str] = []
    params: list[Any] = []
    if "title" in edited:
        assignments.append("title = ?")
        params.append(action.title)
    if "description" in edited:
        assignments.append("description = ?")
        params.append(action.description)
    if "recommendation" in edited:
        assignments.append("recommendation = ?")
        params.append(action.recommendation)
    if "category" in edited:
        assignments.append("category = ?")
        params.append(action.category.value)
    if "priority" in edited:
        assignments.append("priority = ?")
        params.append(action.priority.value)
    if "parameters" in edited:
        import json as _json

        assignments.append("parameters = ?")
        params.append(_json.dumps(action.parameters))

    if not assignments:
        return

    assignments.append("updated_at = datetime('now')")
    params.append(action.id)
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            f"UPDATE instinct_actions SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Correction endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/instinct/corrections",
    response_model=CorrectionsListResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.read"))],
)
async def list_corrections(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    action_id: str | None = Query(None, description="Filter by action ID"),
    limit: int = Query(100, ge=1, le=500),
):
    """List corrections captured when humans edited proposed actions."""
    store = _store()
    if action_id:
        corrections = await store.get_corrections_for_action(action_id)
    elif pocket_id:
        corrections = await store.get_corrections_for_pocket(pocket_id, limit=limit)
    else:
        raise HTTPException(400, "Provide pocket_id or action_id")
    return CorrectionsListResponse(corrections=corrections, total=len(corrections))


# ---------------------------------------------------------------------------
# Audit endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/instinct/audit",
    response_model=AuditListResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.audit"))],
)
async def query_audit(
    response: Response,
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    category: str | None = Query(
        None, description="Filter by category: decision|data|config|security"
    ),
    event: str | None = Query(None, description="Filter by event type"),
    actor: str | None = Query(
        None,
        description=(
            "Filter by fully-qualified actor string (e.g. ``agent:abc123`` "
            "or ``user:maya``). Exact match — added 2026-04-19 for the "
            "AgentReasoningTab's per-agent reasoning-trace view."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max entries to return"),
):
    """Query instinct audit log entries with optional filters.

    DEPRECATED: Cluster C / PR4 made ``/api/v1/runtime/audit`` the canonical
    audit surface with workspace rollup + FTS. This endpoint stays as the
    decision-trace fetch path (it carries instinct-specific fields that
    haven't been merged into the unified view yet) but new callers should
    prefer /runtime/audit for basic queries. We emit Deprecation + Link
    headers for discoverability.
    """
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</api/v1/runtime/audit>; rel="successor-version"'
    entries = await _store().query_audit(
        pocket_id=pocket_id,
        category=category,
        event=event,
        actor=actor,
        limit=limit,
    )
    return AuditListResponse(entries=entries, total=len(entries))


class HydratedAuditEntry(BaseModel):
    """Audit entry with referenced IDs expanded for the Why? drawer."""

    entry: AuditEntry
    reasoning_trace: ReasoningTrace | None = None
    fabric_snapshots: list[FabricObjectSnapshot] = Field(default_factory=list)
    fabric_current: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Live Fabric objects referenced in the trace (current state).",
    )


# /instinct/audit/export must be declared BEFORE the parameterised
# /instinct/audit/{audit_id} below — FastAPI routes match in registration
# order, and a literal-vs-parameter collision would otherwise route
# /audit/export to the {audit_id} handler and 404.
@router.get(
    "/instinct/audit/export",
    dependencies=[Depends(require_action_any_workspace("instinct.audit"))],
)
async def export_audit(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
):
    """Export the full instinct audit log as JSON for compliance."""
    data = await _store().export_audit(pocket_id=pocket_id)
    return Response(
        content=data,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="instinct_audit.json"'},
    )


@router.get(
    "/instinct/audit/{audit_id}",
    response_model=HydratedAuditEntry,
    dependencies=[Depends(require_action_any_workspace("instinct.audit"))],
)
async def get_audit_entry(
    audit_id: str,
    hydrate: int = Query(0, description="Pass 1 to expand referenced IDs"),
):
    """Fetch a single audit entry, optionally hydrated with referenced content.

    When `hydrate=1`, the response carries:
    - the decoded `reasoning_trace` (if stored)
    - `fabric_snapshots` — immutable snapshots captured at decision time
    - `fabric_current` — live state of the referenced objects (so a reviewer
      can compare what the agent saw against what the object is now)
    """
    store = _store()
    entries = await store.query_audit(limit=1000)
    entry = next((e for e in entries if e.id == audit_id), None)
    if entry is None:
        raise HTTPException(404, "Audit entry not found")

    trace = _decode_trace(entry)
    if not hydrate:
        return HydratedAuditEntry(entry=entry, reasoning_trace=trace)

    snapshots: list[FabricObjectSnapshot] = []
    current: list[dict[str, Any]] = []
    if trace is not None:
        snapshots = await store.get_snapshots_for_audit(audit_id)
        current = await _fetch_current_fabric(trace.fabric_queries)

    return HydratedAuditEntry(
        entry=entry,
        reasoning_trace=trace,
        fabric_snapshots=snapshots,
        fabric_current=current,
    )


def _decode_trace(entry: AuditEntry) -> ReasoningTrace | None:
    raw = (entry.context or {}).get("reasoning_trace")
    if not raw:
        return None
    try:
        return ReasoningTrace.model_validate(raw)
    except Exception:
        logger.debug("Failed to decode reasoning_trace on audit %s", entry.id)
        return None


async def _fetch_current_fabric(object_ids: list[str]) -> list[dict[str, Any]]:
    """Look up live Fabric objects by ID, tolerating a missing ee module."""
    if not object_ids:
        return []
    try:
        from pocketpaw_ee.api import get_fabric_store

        fabric = get_fabric_store()
    except ImportError:
        return []

    results: list[dict[str, Any]] = []
    for oid in object_ids:
        try:
            obj = await fabric.get_object(oid)
        except Exception:
            obj = None
        if obj is None:
            continue
        results.append(
            {
                "object_id": oid,
                "type_name": getattr(obj, "type_name", ""),
                "properties": getattr(obj, "properties", {}),
            },
        )
    return results
