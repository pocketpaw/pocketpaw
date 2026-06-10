# ee/instinct/chain_emitters.py — shared Decision-Graph chain-emit helpers.
# Created: 2026-06-10 (sov/r2a FIX 3) — extracted from
#   ``ee.instinct.router`` so the Mission Control service no longer couples to
#   the router's private internals. Both the Instinct router and the Mission
#   Control façade now import the SAME helpers from here:
#     - ``_pocket_write_blob`` — read the parked ``_pocket_write`` blob off an
#       Action;
#     - ``_chain_actor_human`` — build the human Actor for an emit;
#     - ``_parked_policy_event_id`` / ``_parked_correlation_id`` — pull the
#       schema-2 chain ids off the blob;
#     - ``_emit_human_corrected`` — best-effort ``human.corrected`` emit;
#     - ``_emit_decision_completed_rejected`` — best-effort reject-path close;
#     - ``_emit_policy_evaluated_approved`` — best-effort approve-side
#       ``policy.evaluated(passed=True)`` emit.
#   Behavior is identical to the previous router-local definitions (this is a
#   pure move + re-home, no logic change). The router re-imports these names so
#   its callers and existing tests keep the same call sites; the service imports
#   the three it needs directly from this module. Best-effort posture is
#   unchanged — a Decision-Graph wiring failure must never break an approval or
#   rejection (the journal write is the source of truth; the Slice 4 reconciler
#   is the safety net).

from __future__ import annotations

import logging
from typing import Any

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
