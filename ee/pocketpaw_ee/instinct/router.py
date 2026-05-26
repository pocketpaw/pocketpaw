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
#
# Updated: 2026-05-26 (RFC 09 Slice 3 — Instinct emits + reject security fix) —
#   * Decision-Graph chain emits — ``approve_action`` /
#     ``bulk_approve_actions`` now emit ``human.corrected(disposition=
#     accepted|edited)`` per item, chained off the parked
#     ``policy.evaluated`` (the bridge populated the parked blob's
#     ``parked_policy_event_id`` in Slice 3). ``reject_action`` /
#     ``bulk_reject_actions`` emit ``human.corrected(disposition=
#     rejected)`` followed by ``decision.completed(passed=False,
#     action_outcome="rejected")`` to close the chain — the bridge is
#     never invoked on the reject paths, so the router owns the close.
#     The approve paths DO NOT emit ``decision.completed`` — the
#     bridge's ``execute_approved_write`` owns the chain close after
#     the post-approval HTTP call lands (success / re-validation
#     rejection / executor crash all close via
#     ``instinct_bridge._emit_bridge_chain_close``). All emits are
#     best-effort (``record_*`` helpers swallow projection failures
#     internally + the local try/except guards the journal-side
#     failure path so a Decision-Graph wire never breaks an approval /
#     rejection).
#   * Touch-time security fix on reject endpoints —
#     ``reject_action`` and ``bulk_reject_actions`` previously lacked
#     ``current_user`` / ``current_workspace_id`` deps, which meant
#     ``_assert_pocket_write_workspace`` could not run on reject paths.
#     A workspace-A approver could therefore reject a workspace-B
#     ``_pocket_write`` Action — a cross-tenant rejection escalation
#     mirror of the BLOCKER 1 gap closed for approvals in PR #1183. The
#     two deps are added and the assertion runs before any state
#     mutation. Same partial-failure-fails-whole-batch semantics as
#     ``bulk_approve_actions``.
#   * ``bulk_reject_actions`` per-item emit loop — the underlying
#     ``store.bulk_reject`` already iterates per item internally; the
#     router now also loops over the returned ``rejected`` list to fire
#     the per-item chain emits. No semantic change to the bulk-reject
#     response shape (``BulkActionResponse`` with shared ``bulk_id``).
#
# Updated: 2026-05-26 (RFC 09 Slice 4 — approve-side policy.evaluated emit) —
#   * Captain Decision 12 (chain symmetry) follow-up — ``approve_action``
#     and ``bulk_approve_actions`` now emit a second
#     ``policy.evaluated(passed=True, policy_name="approve_per_row")``
#     AFTER ``human.corrected`` and BEFORE the bridge call. Today's
#     chain on an approved write reads ``instinct_policy_passed=False``
#     because the only ``policy.evaluated`` event seen by the projection
#     is the parked ``passed=False`` emit from
#     ``instinct_bridge.propose_pocket_write``. The projection's
#     ``_fold_policy`` uses the LAST policy.evaluated seen before the
#     terminal — adding the ``passed=True`` emit on the approve path
#     flips ``Decision.instinct_policy_passed`` to True and replaces the
#     placeholder policy name with the real approval-gate label
#     ("approve_per_row"). The synthetic policy name keeps approved
#     chains queryable as policy gates rather than confusing them with
#     auto-approve chains (the ``"auto"`` synthetic name from the
#     direct-success path). Reject chains keep the last-seen ``False``
#     emit so ``instinct_policy_passed`` stays False on rejection. Best-
#     effort with the same log-and-continue pattern as the other Slice 3
#     helpers — a Decision-Graph wiring failure must never break an
#     approval.

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

    Slice 3 (RFC 09) — the reject paths now invoke this check too
    (previously only approve paths did). Same error code so the
    frontend's existing 403 handler covers both.
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


# ---------------------------------------------------------------------------
# RFC 09 Slice 3 — Decision-Graph chain emit helpers
# ---------------------------------------------------------------------------
# The approve / reject endpoints emit ``human.corrected`` per item; the
# reject endpoints additionally emit ``decision.completed(rejected)`` to
# close the chain. The bridge owns the chain close on the approve path
# (``instinct_bridge._emit_bridge_chain_close`` fires from
# ``execute_approved_write`` after the post-approval HTTP call). Both
# helpers below are best-effort — a Decision-Graph wiring failure must
# never break an approval or rejection (the journal write is the source
# of truth; the Slice 4 reconciler is the safety net).
#
# ``_chain_actor_human`` shape: ``kind="user"`` (this is the human
# approver acting, not the agent that proposed). ``id`` is the
# authenticated user id with a ``user:`` prefix so the projection's
# ``_fold_corrected`` can attribute the ApproverRef to the human.
# ``scope_context`` carries the approver's active workspace + the
# action's pocket so visibility filters narrow correctly.


def _chain_actor_human(*, user_id: str, workspace_id: str, pocket_id: str) -> Any:
    """Build the Actor recorded on a ``human.corrected`` / reject-path
    ``decision.completed`` chain event."""
    from soul_protocol.spec.journal import Actor

    return Actor(
        kind="user",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
    )


def _parked_policy_event_id(blob: dict[str, Any]) -> Any:
    """Pull the ``parked_policy_event_id`` UUID off a schema-2 blob, or
    ``None`` if missing / malformed. The Slice 3 bridge writes this back
    onto the Action after ``store.propose`` succeeds; using it as the
    ``causation_id`` on the next ``human.corrected`` event gives the
    chain a clean policy → human cause-arrow."""
    from uuid import UUID

    raw = blob.get("parked_policy_event_id")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _parked_correlation_id(blob: dict[str, Any]) -> Any:
    """Pull the chain ``correlation_id`` off a schema-2 blob, or
    ``None`` if missing / malformed. Without a correlation_id the emit
    is skipped — there's no chain to fold into."""
    from uuid import UUID

    raw = blob.get("correlation_id")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _emit_human_corrected(
    *,
    blob: dict[str, Any],
    action: Any,
    user_id: str,
    workspace_id: str,
    disposition: str,
    note: str | None,
) -> Any | None:
    """Best-effort ``human.corrected`` emit for an approve / reject /
    bulk-approve / bulk-reject item.

    ``disposition`` is one of ``accepted`` / ``edited`` / ``rejected``.
    ``note`` is the operator-supplied reason text (reject path) or
    correction note (edit path); ``None`` for a plain approve.

    Skipped silently when the blob carries no ``correlation_id`` — a
    blob-without-chain-id is a defensive guard (Slice 2 always populates
    it from the executor's mint; a None means a future code path parked
    a write without minting one). The Slice 4 reconciler / abandon
    sweeper will deal with the orphan.

    Returns the emitted event id (``UUID``) on success, or ``None`` when
    the emit was skipped (missing correlation_id) or raised. Slice 4's
    approve-side ``policy.evaluated`` emit uses this as its
    ``causation_id`` so the chain ``policy(fail) → human → policy(pass)
    → completed`` walks a clean causal arrow.
    """
    from pocketpaw_ee.cloud.decisions.journal_writer import record_human_corrected

    correlation_id = _parked_correlation_id(blob)
    if correlation_id is None:
        return None

    pocket_id = str(getattr(action, "pocket_id", "") or "")
    causation = _parked_policy_event_id(blob)
    payload: dict[str, Any] = {
        "disposition": disposition,
        "action_id": str(getattr(action, "id", "") or ""),
    }
    if note:
        payload["note"] = note

    try:
        entry = record_human_corrected(
            correlation_id=correlation_id,
            actor=_chain_actor_human(
                user_id=user_id, workspace_id=workspace_id, pocket_id=pocket_id
            ),
            scope=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
            payload=payload,
            causation_id=causation,
        )
    except Exception:  # noqa: BLE001 — chain emit is best-effort
        logger.warning(
            "instinct human.corrected emit failed for correlation_id=%s "
            "(disposition=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            disposition,
            exc_info=True,
        )
        return None
    return entry.id


def _emit_decision_completed_rejected(
    *,
    blob: dict[str, Any],
    action: Any,
    user_id: str,
    workspace_id: str,
    reason: str,
) -> None:
    """Best-effort ``decision.completed(passed=False, action_outcome=
    "rejected")`` chain-close for a reject / bulk-reject item.

    Same skip-on-missing-correlation-id semantics as
    ``_emit_human_corrected``. The reject path owns the close because
    the bridge is never invoked on rejection — for the approve path the
    bridge's ``_emit_bridge_chain_close`` owns the close instead.
    """
    from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

    correlation_id = _parked_correlation_id(blob)
    if correlation_id is None:
        return

    pocket_id = str(getattr(action, "pocket_id", "") or "")
    payload: dict[str, Any] = {
        "passed": False,
        "action_outcome": "rejected",
    }
    if reason:
        payload["reason"] = reason

    try:
        record_decision_completed(
            correlation_id=correlation_id,
            actor=_chain_actor_human(
                user_id=user_id, workspace_id=workspace_id, pocket_id=pocket_id
            ),
            scope=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
            payload=payload,
        )
    except Exception:  # noqa: BLE001 — chain close is best-effort
        logger.warning(
            "instinct decision.completed(rejected) emit failed for "
            "correlation_id=%s — Slice 4 reconciler will catch up",
            correlation_id,
            exc_info=True,
        )


def _emit_policy_evaluated_approved(
    *,
    blob: dict[str, Any],
    action: Any,
    user_id: str,
    workspace_id: str,
    causation_event_id: Any | None,
) -> None:
    """Best-effort ``policy.evaluated(passed=True, policy="approve_per_row")``
    emit after a human approval lands (Slice 4 — Captain Decision 12 follow-up).

    The projection's ``_fold_policy`` keeps the LAST observed
    ``policy.evaluated`` event for the chain. Without this emit, an
    approved chain still reads ``Decision.instinct_policy_passed=False``
    because the only policy event seen is the parked ``passed=False``
    from ``instinct_bridge.propose_pocket_write``. Firing this AFTER the
    ``human.corrected`` event and BEFORE the bridge's chain close gives
    the projection a fresh policy-evaluated to fold into the closed
    Decision row — chain symmetry with auto-approve chains, which carry
    ``policy="auto", passed=True`` from the direct-success path in
    ``action_executor``.

    Causation: the natural cause is the ``human.corrected`` event that
    just landed. The caller threads its emitted event id through
    ``causation_event_id`` so the projection's edge graph can chain
    policy → human → policy as a single causal sequence.

    Same skip-on-missing-correlation-id semantics as the sibling helpers.
    """
    from pocketpaw_ee.cloud.decisions.journal_writer import record_policy_evaluated

    correlation_id = _parked_correlation_id(blob)
    if correlation_id is None:
        return

    pocket_id = str(getattr(action, "pocket_id", "") or "")
    payload: dict[str, Any] = {
        "policy": "approve_per_row",
        "passed": True,
        "reason": f"approved by user:{user_id or 'unknown'}",
        "action_id": str(getattr(action, "id", "") or ""),
        "evaluator": "instinct",
    }
    try:
        record_policy_evaluated(
            correlation_id=correlation_id,
            actor=_chain_actor_human(
                user_id=user_id, workspace_id=workspace_id, pocket_id=pocket_id
            ),
            scope=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
            payload=payload,
            causation_id=causation_event_id,
        )
    except Exception:  # noqa: BLE001 — chain emit is best-effort
        logger.warning(
            "instinct policy.evaluated(passed=True) emit failed for "
            "correlation_id=%s — Slice 4 reconciler will catch up",
            correlation_id,
            exc_info=True,
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
    #
    # RFC 09 Slice 3 — per-item ``human.corrected`` emit slots into the
    # same loop. Disposition is always ``accepted`` for bulk-approve —
    # the endpoint doesn't support edits (the UI doesn't expose them on
    # the bulk bar). The bridge owns the chain close on the approve
    # path so we do NOT emit ``decision.completed`` here.
    for action in approved:
        action_blob = _pocket_write_blob(action)
        if action_blob is None:
            continue
        human_event_id = _emit_human_corrected(
            blob=action_blob,
            action=action,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition="accepted",
            note=req.note,
        )
        # Slice 4 — chain symmetry: a second ``policy.evaluated`` event
        # with ``passed=True`` flips ``Decision.instinct_policy_passed``
        # from the parked ``False`` to ``True``. ``causation_id`` points
        # at the just-emitted ``human.corrected`` so the projection's
        # edge graph carries the human → policy causal arrow.
        _emit_policy_evaluated_approved(
            blob=action_blob,
            action=action,
            user_id=approver_id,
            workspace_id=workspace_id,
            causation_event_id=human_event_id,
        )
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
async def bulk_reject_actions(
    req: BulkRejectRequest,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> BulkActionResponse:
    """Reject N pending actions in one call. ``reason`` is required.

    Mirrors ``bulk_approve_actions``: shared ``bulk_id``, per-item audit
    rows, partial-success surface via ``missing``. The reason text lands
    on every audit row's ``context.reason`` and on each Action's
    ``rejected_reason`` so the soul-bridge correction pipeline still
    sees the same shape it sees on single-item rejects.

    Slice 3 (RFC 09) — endpoint signature grew ``current_user`` and
    ``current_workspace_id`` deps for the same two reasons as
    ``reject_action``: (a) the touch-time cross-workspace security fix,
    and (b) per-item ``human.corrected`` + ``decision.completed
    (rejected)`` chain emits. Cross-workspace check fails the whole
    batch with 403 — a partial bulk that silently dropped a foreign
    item would hide a cross-tenant rejection-escalation attempt
    (mirror of bulk-approve's BLOCKER 1 behaviour).
    """
    store = _store()
    rejector_id = str(user.id)

    # Touch-time security fix — verify tenancy on every requested
    # action up front, same shape as ``bulk_approve_actions``. A
    # missing id has no blob to check; it falls through to
    # ``bulk_reject`` and lands in ``missing``.
    for action_id in req.ids:
        action = await store.get_action(action_id)
        if action is not None:
            _assert_pocket_write_workspace(action, workspace_id)

    rejected, missing, bulk_id = await store.bulk_reject(
        list(req.ids), reason=req.reason, rejector=rejector_id
    )

    # RFC 09 Slice 3 — per-item ``human.corrected`` + ``decision.
    # completed(rejected)`` emit loop. The store's bulk_reject already
    # iterates per item internally for the audit log; this loop adds
    # the chain emits. Non-pocket-write Actions (no blob) skip both
    # emits — there's no chain to close.
    for action in rejected:
        action_blob = _pocket_write_blob(action)
        if action_blob is None:
            continue
        _emit_human_corrected(
            blob=action_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=req.reason or None,
        )
        _emit_decision_completed_rejected(
            blob=action_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=req.reason,
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

    # RFC 09 Slice 3 — emit the ``human.corrected`` chain event BEFORE
    # the bridge fires. Disposition is ``edited`` when the approver
    # adjusted fields (``edited_fields`` is non-empty), ``accepted``
    # otherwise. The bridge owns the chain close on the approve path
    # (``_emit_bridge_chain_close`` from ``execute_approved_write``),
    # so we do NOT emit ``decision.completed`` here — emitting it would
    # double-fire the chain terminal.
    approved_blob = _pocket_write_blob(approved)
    if approved_blob is not None:
        disposition = "edited" if edited_fields else "accepted"
        # ``note`` is the correction's free-text summary when the
        # approver edited; None on a plain approve.
        note = correction.context_summary if correction is not None else None
        human_event_id = _emit_human_corrected(
            blob=approved_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition=disposition,
            note=note,
        )
        # Slice 4 — chain symmetry: a second ``policy.evaluated`` event
        # with ``passed=True`` flips ``Decision.instinct_policy_passed``
        # to True on the approved chain. ``causation_id`` points at the
        # just-emitted ``human.corrected`` so the chain reads policy
        # (fail) → human → policy(pass) → completed as one causal walk.
        _emit_policy_evaluated_approved(
            blob=approved_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            causation_event_id=human_event_id,
        )

    # RFC 05 M2b.1 — when the approved Action carries a parked pocket
    # write (``parameters._pocket_write``), fire it. Best-effort: a
    # lazy import keeps the instinct package free of a module-top
    # dependency on ee.cloud.pockets, and any failure is recorded on the
    # Action by the bridge itself — it must NEVER break this approve
    # response. A non-pocket-write Action (the common case) skips this.
    if approved_blob is not None:
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
async def reject_action(
    action_id: str,
    req: RejectRequest | None = None,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Reject a pending action with an optional reason.

    Slice 3 (RFC 09) — endpoint signature grew ``current_user`` and
    ``current_workspace_id`` deps for two reasons:

      1. **Touch-time security fix** — ``require_action_any_workspace``
         only proves the caller holds ``instinct.approve`` SOMEWHERE; it
         does not bind the rejected Action to the caller's workspace.
         Without the workspace dep, ``_assert_pocket_write_workspace``
         could not run on the reject path — a workspace-A approver
         could reject a workspace-B parked write, the mirror of the
         BLOCKER 1 approval-escalation gap closed for approvals in PR
         #1183. Now the same 403 + ``instinct.cross_workspace_approval``
         error code fires on cross-tenant rejections.
      2. **Decision-Graph chain emits** — the rejection emits
         ``human.corrected(disposition=rejected)`` then closes the
         chain with ``decision.completed(passed=False, action_outcome=
         "rejected")``. The actor on both events is the authenticated
         user id (same forge-resistance as ``approve_action``'s
         SHOULD-FIX 1); the workspace + action's pocket form the
         scope. The bridge is NOT invoked on reject so the router owns
         the chain close.
    """
    store = _store()
    before = await store.get_action(action_id)
    if not before:
        raise HTTPException(404, "Action not found")

    # Touch-time security fix — same gate the approve path runs.
    _assert_pocket_write_workspace(before, workspace_id)

    reason = req.reason if req else ""
    rejector_id = str(user.id)
    action = await store.reject(action_id, reason=reason, rejector=rejector_id)
    if not action:
        raise HTTPException(404, "Action not found")

    # RFC 09 Slice 3 — emit ``human.corrected`` then ``decision.completed``
    # to close the chain. Order matters for the narrator: the human
    # action lands before the chain terminal, mirroring the approve
    # path's "human.corrected → execute → decision.completed" ordering.
    rejected_blob = _pocket_write_blob(action)
    if rejected_blob is not None:
        _emit_human_corrected(
            blob=rejected_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=reason or None,
        )
        _emit_decision_completed_rejected(
            blob=rejected_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=reason,
        )

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
