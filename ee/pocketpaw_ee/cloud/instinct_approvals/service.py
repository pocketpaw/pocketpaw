# ee/pocketpaw_ee/cloud/instinct_approvals/service.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — sole Beanie
# writer for the ``InstinctApproval`` collection (RFC 03 v2). Module-
# level ``async def`` API per EE cloud rule 5. Every state-mutating
# function:
#
# Updated: 2026-08-06 (feat/coupling-template-approvals, T-5) — a
# template-level approval is now VISIBLE IN THE GOVERNANCE RECORD. It is the
# highest-leverage governance act in the product (a human authorising a whole
# CLASS of future writes) and until now it left no trace anywhere: the row
# carried no chain id, ``_decide`` only saved the doc and emitted an ephemeral
# realtime event, and an owner who was offline when the approval was requested
# was never told. Three additions, all modelled on the row-level path in
# ``ee/pocketpaw_ee/instinct/router.py`` + ``instinct/chain_emitters.py``:
#
#   1. CREATE mints a ``correlation_id`` onto the row and OPENS the
#      Decision-Graph chain with ``agent.proposed``. The mint alone is not
#      enough: ``decisions.projection`` only materialises a Decision row on a
#      TERMINAL event, and ``_close_chain`` DROPS any chain that never saw
#      ``agent.proposed`` (``chain.decided_by is None`` → "closed without
#      proposed event — skipping"). Without the open, /decisions stays empty
#      no matter what the decide path emits.
#   2. DECIDE closes that same chain — ``human.corrected(accepted|rejected)``
#      then ``decision.completed`` — and writes a workspace audit row
#      (``instinct_approval.approved`` / ``.rejected``) so the decision also
#      lands on /activity. Same pair the row-level path fires, plus the
#      terminal the row-level path owns on its reject side. There is no double
#      close: the correlation is minted HERE and known only to this row (the
#      post-approval re-entry is still unwired — see ``approve``), so no other
#      producer can close it.
#   3. CREATE publishes ``instinct.approval.created`` on
#      ``shared.events.event_bus``, where ``bridges/notifications.py`` turns it
#      into a PERSISTED notification for the workspace owner + admins. The
#      pre-existing realtime ``emit`` is a websocket fan-out only — an offline
#      owner saw nothing.
#
# Every one of those is best-effort and cannot break the approval flow: the
# chain emits go through ``_safe_chain_emit``, the audit write through
# ``_record_audit_safe``, and the notification fan-out through
# ``_publish_created_safe``. The Mongo row is the source of truth; the Slice 4
# reconciler catches up any chain event that failed to land.
#
# Updated: 2026-06-19 (feat/instinct-gate-integration, security-review FIX 3) —
# ``list_approvals`` now honors optional ``action_name`` + ``row_id`` query
# filters. The gate's BATCH dedup pushes them into the Mongo query so a pocket
# with more pending rows than the page ``limit`` can no longer bury a duplicate
# match past the page boundary (the old code listed + matched in Python and
# could miss the row, stacking a duplicate pending approval).
#
# Updated: 2026-06-18 (feat/instinct-gate-foundation, T3) — added
# ``auto_approve``, the layered/learning gate's AUTO-lane writer. It
# inserts a row ALREADY-DECIDED (``status="auto_approved"``,
# ``decided_by="system:triager"``, ``decided_at=now``) in a single write
# and emits ``InstinctApprovalAutoApproved``. This is how the AUTO lane
# keeps a complete approval-level audit trail in the SAME Mongo collection
# as every human decision without ever touching the OSS SQLite store
# (design MF-1/MF-2). The human queue filters on ``status="pending"``, so
# an auto-approved row never appears in the human tray.
#   * validates at entry via ``<Request>.model_validate(body)`` (rule 6)
#   * filters reads by ``workspace=workspace_id`` (rule 7)
#   * raises ``CloudError`` subclasses, never ``HTTPException`` (rule 10)
#   * emits an event on the way out (rule 9)
#
# Errors:
#   * unknown approval id → ``NotFound("instinct_approval", id)``
#   * tenant mismatch on read → returns ``None`` (no oracle); on
#     decision attempt → ``NotFound`` (treat as if it does not exist)
#   * already-decided approval → ``ConflictError("instinct_approval.already_decided", ...)``

"""Service for ``instinct_approvals``."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    InstinctApprovalApproved,
    InstinctApprovalAutoApproved,
    InstinctApprovalCreated,
    InstinctApprovalRejected,
)
from pocketpaw_ee.cloud.instinct_approvals.domain import InstinctApproval
from pocketpaw_ee.cloud.instinct_approvals.dto import (
    ApprovalDecisionRequest,
    CreateApprovalRequest,
    ListApprovalsRequest,
    approval_to_wire_dict,
)
from pocketpaw_ee.cloud.models.instinct_approval import InstinctApproval as _ApprovalDoc
from pocketpaw_ee.cloud.shared.events import event_bus

logger = logging.getLogger(__name__)

# Bus topic the notification bridge subscribes to. Deliberately the same
# string as ``InstinctApprovalCreated.EVENT_TYPE`` — the two buses are
# different objects (realtime websocket fan-out vs. the cross-domain
# side-effect bus), and one name for one fact keeps them readable together.
CREATED_TOPIC = "instinct.approval.created"

# ---------------------------------------------------------------------------
# Private mapping helper — Beanie doc → domain
# ---------------------------------------------------------------------------


def _to_domain(doc: _ApprovalDoc) -> InstinctApproval:
    return InstinctApproval(
        id=str(doc.id),
        workspace_id=doc.workspace,
        pocket_id=doc.pocket_id,
        action_name=doc.action_name,
        row_id=doc.row_id,
        row_data=dict(doc.row_data or {}),
        verdict=doc.verdict,
        reason=doc.reason,
        matched_rules=list(doc.matched_rules or []),
        requested_at=doc.requested_at,
        requested_by=doc.requested_by,
        status=doc.status,
        decided_at=doc.decided_at,
        decided_by=doc.decided_by,
        park=dict(doc.park) if doc.park else None,
        created_at=getattr(doc, "createdAt", None),
        correlation_id=getattr(doc, "correlation_id", "") or "",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_approval(
    workspace_id: str, user_id: str, body: dict | CreateApprovalRequest
) -> dict:
    """Persist a new pending approval row.

    Called by ``pockets.instinct_dispatch.gate_action`` when the
    template-level composer returns ``ESCALATE_APPROVAL``. Re-validates
    the body (FastAPI parsed it; internal callers re-parse so the
    schema is enforced uniformly — rule 6).

    T-5 — this is the chain-OPEN moment. A fresh ``correlation_id`` is minted
    onto the row and ``agent.proposed`` is emitted under it, so the eventual
    human decision has a chain to close and /decisions has a row to show.
    The executor mints its own correlation for the direct-write path, but on
    the ``instinct_pending`` branch it returns BEFORE emitting
    ``agent.proposed`` and never threads its id onto ``park`` — so there is no
    existing chain to join here, and joining one would be joining an empty
    chain. Minting is the correct move, and it also guarantees this row's
    correlation is known to no other producer, so nothing else can close it.

    The owner/admin notification rides ``shared.events.event_bus``, NOT the
    return value — the caller (the gate) must not be able to fail because a
    notification recipient lookup did.
    """
    body = CreateApprovalRequest.model_validate(body)

    if not workspace_id:
        raise ValidationError(
            "instinct_approval.workspace_required",
            "workspace_id is required to create an approval row",
        )
    if not user_id:
        raise ValidationError(
            "instinct_approval.user_required",
            "user_id is required to create an approval row",
        )

    now = datetime.now(UTC)
    correlation_id = uuid4()
    doc = _ApprovalDoc(
        workspace=workspace_id,
        pocket_id=body.pocket_id,
        action_name=body.action_name,
        row_id=body.row_id,
        row_data=body.row_data,
        verdict=body.verdict,
        reason=body.reason,
        matched_rules=body.matched_rules,
        requested_at=now,
        requested_by=user_id,
        status="pending",
        correlation_id=str(correlation_id),
        park=body.park,
    )
    await doc.insert()
    domain = _to_domain(doc)
    wire = approval_to_wire_dict(domain)

    _open_chain(
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        pocket_id=body.pocket_id,
        user_id=user_id,
        approval_id=domain.id,
        action_name=body.action_name,
        row_id=body.row_id,
        reason=body.reason,
    )

    await emit(InstinctApprovalCreated(data=dict(wire)))
    await _publish_created_safe(dict(wire))
    return wire


async def auto_approve(
    workspace_id: str,
    user_id: str,
    body: dict | CreateApprovalRequest,
    trust_score: float,
    triager_reasoning: str,
    lane: str = "AUTO",
) -> dict:
    """Create a decided (``auto_approved``) approval row in one write.

    The layered/learning gate's AUTO and OPTIMISTIC lanes call this instead
    of ``create_approval`` when ``classify_lane`` clears the write for an
    auto decision. The row is inserted ALREADY-DECIDED:
    ``status="auto_approved"``, ``decided_by="system:triager"``,
    ``decided_at=now`` — there is no intermediate pending state and the OSS
    SQLite store is never touched (design MF-1). Emits
    ``InstinctApprovalAutoApproved`` carrying the triager's ``trust_score``
    + ``triager_reasoning`` + ``lane`` so the audit trail and Decision-Graph
    join have a backing row in the same collection as every human decision
    (design MF-2).

    ``lane`` is the triage lane that produced the decision (``"AUTO"`` or
    ``"OPTIMISTIC"``) — it rides on the emitted event so the UI can render
    an optimistic (reversible, fired-now) decision distinctly from a fully
    auto-approved one. Both share ``status="auto_approved"``; the lane is
    the discriminator.

    ``user_id`` is the human who TRIGGERED the action (recorded as
    ``requested_by``); the DECIDER is the system triager, not the user.
    """
    body = CreateApprovalRequest.model_validate(body)

    if not workspace_id:
        raise ValidationError(
            "instinct_approval.workspace_required",
            "workspace_id is required to auto-approve an approval row",
        )
    if not user_id:
        raise ValidationError(
            "instinct_approval.user_required",
            "user_id is required to auto-approve an approval row",
        )

    now = datetime.now(UTC)
    doc = _ApprovalDoc(
        workspace=workspace_id,
        pocket_id=body.pocket_id,
        action_name=body.action_name,
        row_id=body.row_id,
        row_data=body.row_data,
        verdict=body.verdict,
        reason=body.reason,
        matched_rules=body.matched_rules,
        requested_at=now,
        requested_by=user_id,
        status="auto_approved",
        decided_at=now,
        decided_by="system:triager",
        park=body.park,
    )
    await doc.insert()
    domain = _to_domain(doc)
    wire = approval_to_wire_dict(domain)
    # The event payload carries the triager rationale alongside the wire
    # row. ``actor`` is the system triager (UI renders system: with a robot
    # icon, not in the human-pending queue).
    payload = dict(wire)
    payload["actor"] = "system:triager"
    payload["trust_score"] = trust_score
    payload["triager_reasoning"] = triager_reasoning
    payload["lane"] = lane
    await emit(InstinctApprovalAutoApproved(data=payload))
    return wire


async def list_approvals(
    workspace_id: str, user_id: str, body: dict | ListApprovalsRequest
) -> list[dict]:
    """List approvals scoped to ``workspace_id``. ``user_id`` is the
    viewer; current behaviour is workspace-wide read (no per-user
    filtering) — a future PR adds approver-scoped filtering."""
    body = ListApprovalsRequest.model_validate(body)
    # `user_id` carries viewer context for future per-approver filtering.
    _ = user_id

    query: dict[str, Any] = {"workspace": workspace_id}
    if body.status:
        query["status"] = body.status
    if body.pocket_id:
        query["pocket_id"] = body.pocket_id
    # Server-side dedup filters (security-review FIX 3). When the gate's BATCH
    # dedup passes action_name + row_id, the DB does the matching so a pocket
    # with more pending rows than ``limit`` can't bury the match past the page.
    if body.action_name:
        query["action_name"] = body.action_name
    if body.row_id:
        query["row_id"] = body.row_id
    cursor = (
        _ApprovalDoc.find(query).sort(-_ApprovalDoc.createdAt).limit(body.limit)  # type: ignore[operator]
    )
    return [approval_to_wire_dict(_to_domain(doc)) async for doc in cursor]


async def get_approval(workspace_id: str, user_id: str, approval_id: str) -> dict:
    """Return one approval row by id, scoped to ``workspace_id``.

    Raises ``NotFound`` when the id does not resolve in the caller's
    workspace — treating a foreign-workspace hit as a 404 keeps the
    endpoint from being a cross-tenant existence oracle.
    """
    _ = user_id  # viewer context unused on the read path today
    try:
        oid = PydanticObjectId(approval_id)
    except Exception as exc:
        raise NotFound("instinct_approval", approval_id) from exc

    doc = await _ApprovalDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("instinct_approval", approval_id)
    return approval_to_wire_dict(_to_domain(doc))


async def approve(
    workspace_id: str,
    user_id: str,
    approval_id: str,
    body: dict | ApprovalDecisionRequest | None = None,
) -> dict:
    """Mark a pending approval as ``approved``. Emits ``InstinctApprovalApproved``.

    Out of scope for Wave 3a: this PR persists the decision only. The
    follow-up wave wires the post-approval re-entry into
    ``action_executor.run_action(from_instinct=True)``.
    """
    body = ApprovalDecisionRequest.model_validate(body or {})
    return await _decide(
        workspace_id=workspace_id,
        user_id=user_id,
        approval_id=approval_id,
        new_status="approved",
        event_cls=InstinctApprovalApproved,
        note=body.note,
    )


async def reject(
    workspace_id: str,
    user_id: str,
    approval_id: str,
    body: dict | ApprovalDecisionRequest | None = None,
) -> dict:
    """Mark a pending approval as ``rejected``. Emits ``InstinctApprovalRejected``."""
    body = ApprovalDecisionRequest.model_validate(body or {})
    return await _decide(
        workspace_id=workspace_id,
        user_id=user_id,
        approval_id=approval_id,
        new_status="rejected",
        event_cls=InstinctApprovalRejected,
        note=body.note,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _decide(
    *,
    workspace_id: str,
    user_id: str,
    approval_id: str,
    new_status: str,
    event_cls: type,
    note: str | None,
) -> dict:
    if not user_id:
        raise ValidationError(
            "instinct_approval.user_required",
            "user_id is required to decide an approval",
        )
    try:
        oid = PydanticObjectId(approval_id)
    except Exception as exc:
        raise NotFound("instinct_approval", approval_id) from exc

    doc = await _ApprovalDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("instinct_approval", approval_id)
    if doc.status != "pending":
        raise ConflictError(
            "instinct_approval.already_decided",
            f"approval {approval_id} is already {doc.status!r}",
        )

    doc.status = new_status  # type: ignore[assignment]
    doc.decided_at = datetime.now(UTC)
    doc.decided_by = user_id
    await doc.save()
    domain = _to_domain(doc)
    wire = approval_to_wire_dict(domain)

    # T-5 — the governance record. The Mongo row above is the source of
    # truth and is already committed; everything below is best-effort and
    # cannot fail the decision.
    _close_chain(
        correlation_id=_chain_id(domain.correlation_id),
        workspace_id=workspace_id,
        pocket_id=domain.pocket_id,
        user_id=user_id,
        approval_id=domain.id,
        new_status=new_status,
        note=note,
    )
    await _record_audit_safe(
        workspace_id=workspace_id,
        user_id=user_id,
        approval=domain,
        new_status=new_status,
        note=note,
    )

    payload = dict(wire)
    if note:
        payload["note"] = note
    await emit(event_cls(data=payload))
    return wire


# ---------------------------------------------------------------------------
# T-5 — governance-record helpers (chain emits, audit, notification publish)
# ---------------------------------------------------------------------------


def _chain_id(raw: str) -> UUID | None:
    """Parse a stored ``correlation_id``, or ``None`` when absent/malformed.

    Rows written before T-5 carry ``""``; an ``auto_approve`` row carries ""
    too (the AUTO lane has no human chain). Both mean "no chain" — the emits
    are skipped rather than fabricating a chain id that joins to nothing.
    """
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        logger.warning("instinct_approval: malformed correlation_id %r — skipping chain", raw)
        return None


def _chain_actor(*, user_id: str, workspace_id: str, pocket_id: str) -> Any:
    """Actor stamped on this approval's chain events.

    ``kind="user"`` — mirrors ``instinct.chain_emitters._chain_actor_human``.
    A template-level approval decision is a HUMAN act; the projection's
    ``_fold_corrected`` attributes the ApproverRef from this actor, and
    ``scope_context`` narrows ``DecisionStore``'s visibility filter to the
    deciding workspace, which is also what keeps workspace B from seeing
    workspace A's chain.
    """
    from soul_protocol.spec.journal import Actor

    return Actor(
        kind="user",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
    )


def _chain_scope(*, workspace_id: str, pocket_id: str) -> list[str]:
    """Tenancy tags on every chain event — the projection intersects these
    with the requester's scopes, so they ARE the cross-tenant boundary."""
    return [f"workspace:{workspace_id}", f"pocket:{pocket_id}"]


def _safe_chain_emit(record_fn, *, correlation_id: UUID, **kwargs) -> Any | None:
    """Best-effort Decision-Graph emit — peer of ``action_executor._safe_record``.

    A journal/projection failure must never break an approval; the journal row
    is the source of truth and the Slice 4 reconciler replays from the cursor.
    Returns the emitted event id so the next event in the chain can cite it as
    ``causation_id``, or ``None`` when the emit raised.
    """
    try:
        entry = record_fn(correlation_id=correlation_id, **kwargs)
        return entry.id
    except Exception:  # noqa: BLE001 — chain emit is best-effort by contract
        logger.warning(
            "instinct_approval chain emit failed for %s correlation_id=%s",
            getattr(record_fn, "__name__", "record_*"),
            correlation_id,
            exc_info=True,
        )
        return None


def _open_chain(
    *,
    correlation_id: UUID,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    approval_id: str,
    action_name: str,
    row_id: str,
    reason: str,
) -> None:
    """Emit ``agent.proposed`` — the chain-opening event for this approval.

    Load-bearing, not decorative: ``projection._close_chain`` drops any chain
    whose ``decided_by`` is unset, and only ``_fold_proposed`` sets it. Skip
    this and the decide path's terminal is discarded with a "closed without
    proposed event" warning, leaving /decisions empty.

    ``policy.evaluated(passed=False)`` rides along because that IS what
    happened: the template's Instinct rules adjudicated this write and
    escalated it. It gives the projection the pre-human policy state the
    row-level path gets from ``instinct_bridge.propose_pocket_write``.
    """
    from pocketpaw_ee.cloud.decisions.journal_writer import (
        record_agent_proposed,
        record_policy_evaluated,
    )

    actor = _chain_actor(user_id=user_id, workspace_id=workspace_id, pocket_id=pocket_id)
    scope = _chain_scope(workspace_id=workspace_id, pocket_id=pocket_id)

    proposed_id = _safe_chain_emit(
        record_agent_proposed,
        correlation_id=correlation_id,
        actor=actor,
        scope=scope,
        payload={
            # Fields ``projection._fold_proposed`` consumes.
            "intent": f"template action {action_name} on row {row_id or '-'}",
            "action": action_name,
            "pocket_id": pocket_id,
            "inputs": [],
            # Ride-alongs for the explain narrator / future AgentProposal swap.
            "proposal_kind": "instinct_approval",
            "summary": f"template action {action_name} escalated for approval",
            "approval_id": approval_id,
            "row_id": row_id,
        },
    )
    _safe_chain_emit(
        record_policy_evaluated,
        correlation_id=correlation_id,
        actor=actor,
        scope=scope,
        payload={
            "policy": "template_instinct_gate",
            "passed": False,
            "reason": reason or "escalated for human approval",
            "approval_id": approval_id,
            "evaluator": "instinct",
        },
        causation_id=proposed_id,
    )


def _close_chain(
    *,
    correlation_id: UUID | None,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    approval_id: str,
    new_status: str,
    note: str | None,
) -> None:
    """Emit ``human.corrected`` then ``decision.completed`` for a decision.

    Both events are needed. ``human.corrected`` is what attributes the
    decision to the human; ``decision.completed`` is the ONLY action in
    ``projection._TERMINAL_ACTIONS``, and until a terminal lands the chain
    accumulates in the projection's in-memory pending dict and never becomes
    a queryable Decision row.

    Unlike the row-level approve path — where the post-approval bridge owns
    the close — nothing runs after a template-level approval today (see
    ``approve``: the re-entry into ``run_action(from_instinct=True)`` is still
    unwired), so this path owns the close on BOTH approve and reject. The
    correlation was minted in ``create_approval`` and is on no other producer's
    blob, so there is no second closer to race.
    """
    if correlation_id is None:
        return

    from pocketpaw_ee.cloud.decisions.journal_writer import (
        record_decision_completed,
        record_human_corrected,
    )

    approved = new_status == "approved"
    disposition = "accepted" if approved else "rejected"
    actor = _chain_actor(user_id=user_id, workspace_id=workspace_id, pocket_id=pocket_id)
    scope = _chain_scope(workspace_id=workspace_id, pocket_id=pocket_id)

    corrected_payload: dict[str, Any] = {
        "disposition": disposition,
        "approval_id": approval_id,
    }
    if note:
        corrected_payload["note"] = note

    human_event_id = _safe_chain_emit(
        record_human_corrected,
        correlation_id=correlation_id,
        actor=actor,
        scope=scope,
        payload=corrected_payload,
    )

    terminal_payload: dict[str, Any] = {
        "passed": approved,
        "action_outcome": new_status,
        "approval_id": approval_id,
    }
    if note:
        terminal_payload["reason"] = note

    _safe_chain_emit(
        record_decision_completed,
        correlation_id=correlation_id,
        actor=actor,
        scope=scope,
        payload=terminal_payload,
        causation_id=human_event_id,
    )


async def _record_audit_safe(
    *,
    workspace_id: str,
    user_id: str,
    approval: InstinctApproval,
    new_status: str,
    note: str | None,
) -> None:
    """Write the workspace audit row for a template-approval decision.

    ``audit.service.record`` already swallows its own insert failures, but its
    SIEM hand-off sits outside that guard — so the call is wrapped here too.
    An audit sink must never be able to undo a decision the operator made.

    ``metadata.category`` is ``pocket_router``, matching what the row-level
    path's ``record_decision`` stamps, so a decided template approval lands in
    the same /activity filter bucket as a decided row-level one.
    """
    try:
        from pocketpaw_ee.cloud.audit import service as audit_service

        await audit_service.record(
            workspace_id=workspace_id,
            actor_id=user_id or "unknown",
            action=f"instinct_approval.{new_status}",
            target_type="instinct_approval",
            target_id=approval.id,
            metadata={
                "category": "pocket_router",
                "pocket_id": approval.pocket_id,
                "action_name": approval.action_name,
                "row_id": approval.row_id,
                "correlation_id": approval.correlation_id,
                "note": note or "",
            },
        )
    except Exception:  # noqa: BLE001 — audit must never break the decision
        logger.warning(
            "instinct_approval audit write failed for approval=%s status=%s",
            approval.id,
            new_status,
            exc_info=True,
        )


async def _publish_created_safe(wire: dict) -> None:
    """Publish ``instinct.approval.created`` for the notification bridge.

    ``event_bus.emit`` already guards each handler, so a broken subscriber is
    contained there; this wrapper covers the bus call itself. The approval row
    is committed before we get here — no fan-out failure may unwind it.
    """
    try:
        await event_bus.emit(CREATED_TOPIC, wire)
    except Exception:  # noqa: BLE001 — fan-out must never break create
        logger.warning(
            "instinct_approval created fan-out failed for approval=%s",
            wire.get("id"),
            exc_info=True,
        )


__all__ = [
    "CREATED_TOPIC",
    "approve",
    "auto_approve",
    "create_approval",
    "get_approval",
    "list_approvals",
    "reject",
]
