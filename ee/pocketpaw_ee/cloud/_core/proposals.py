# ee/pocketpaw_ee/cloud/_core/proposals.py — the shared Decision-Graph chain
# helpers every gated proposal kind uses after ``InstinctStore.propose``.
#
# Created: 2026-10-01 (refactor/canon-proposal-helpers, CN-5) — replaces the ten
#   per-module copies of ``_emit_agent_proposed`` + ``_persist_chain_ids`` (admin,
#   pocket, fabric, instinct-rule, external, site-plan, fabric-conflict, ship,
#   growth proposals and the belt MCP server). The copies differed only in the
#   blob's parameters key and the payload's intent/proposal, so those are the
#   arguments. Blob writes go through ``update_action_blob`` →
#   ``InstinctStore.update_parameters`` — no raw SQL on ``instinct_actions``
#   outside the store. ``persist_chain_ids`` takes each kind's historical log
#   ``label`` so warning greps keep matching.

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def emit_agent_proposed(
    *,
    correlation_id: UUID,
    action_id: str,
    kind: str,
    intent: str,
    proposal: dict[str, Any],
    workspace_id: str,
    user_id: str,
    pocket_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> UUID | None:
    """Emit the chain-opening ``agent.proposed`` event for a gated proposal.

    The proposing caller is the actor (``kind="agent"``, the user on its id, the
    workspace on its scope_context). ``pocket_id`` on the payload defaults to the
    workspace — most gated kinds are tenant-scoped, not pocket-bound. ``extra``
    adds kind-specific payload fields (belt's ``summary``).

    Returns the event id for the blob's ``proposed_event_id`` (the
    ``human.corrected`` causation link), or ``None`` when the emit raised —
    best-effort per RFC 09; the reconciler picks up orphans.
    """
    try:
        # Lazy: tests patch ``journal_writer.record_agent_proposed``.
        from soul_protocol.spec.journal import Actor

        from pocketpaw_ee.cloud.decisions.journal_writer import record_agent_proposed

        actor = Actor(
            kind="agent",
            id=f"user:{user_id or 'unknown'}",
            scope_context=[f"workspace:{workspace_id}"],
        )
        payload: dict[str, Any] = {
            # Fields the projection's ``_fold_proposed`` consumes.
            "intent": intent,
            "action": kind,
            "pocket_id": workspace_id if pocket_id is None else pocket_id,
            "inputs": [],
            # Richer fields for the explain narrator.
            "proposal_kind": kind,
            **(extra or {}),
            "proposal": proposal,
            "action_id": action_id,
        }
        entry = record_agent_proposed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
        )
        return entry.id
    except Exception:  # noqa: BLE001 — chain emit is best-effort
        logger.warning(
            "%s agent.proposed emit failed for correlation_id=%s (action_id=%s) "
            "— reconciler will catch up",
            kind,
            correlation_id,
            action_id,
            exc_info=True,
        )
        return None


async def update_action_blob(
    *, store: Any, action_id: str, param_key: str, updates: dict[str, Any]
) -> bool:
    """Merge ``updates`` into the dict blob at ``parameters[param_key]`` and
    persist it through ``InstinctStore.update_parameters``.

    Returns ``False`` (writes nothing) when the action or a dict blob under
    ``param_key`` is missing. Raises on a store failure — callers decide whether
    the write is best-effort.
    """
    action = await store.get_action(action_id)
    if action is None:
        return False
    params = dict(getattr(action, "parameters", None) or {})
    blob = params.get(param_key)
    if not isinstance(blob, dict):
        return False
    params[param_key] = {**blob, **updates}
    # no-event: content back-write onto an existing proposal; the proposal's own
    # lifecycle events (agent.proposed / human.corrected) carry the change.
    await store.update_parameters(action_id, params)
    return True


async def persist_chain_ids(
    *,
    store: Any,
    action_id: str,
    param_key: str,
    correlation_id: str,
    proposed_event_id: str | None,
    label: str,
) -> None:
    """Write ``correlation_id`` + ``proposed_event_id`` onto the stored blob
    after ``agent.proposed`` fired.

    Best-effort: a failure leaves ``proposed_event_id`` None and the eventual
    ``human.corrected`` emits without a causation_id (the chain still folds).
    ``label`` prefixes the warning (the kind's historical log prefix, e.g.
    ``admin_action`` / ``ship``) so existing log greps keep matching.
    """
    try:
        await update_action_blob(
            store=store,
            action_id=action_id,
            param_key=param_key,
            updates={"correlation_id": correlation_id, "proposed_event_id": proposed_event_id},
        )
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "%s: failed to persist chain ids onto action %s — the chain's "
            "human.corrected will emit without causation_id",
            label,
            action_id,
            exc_info=True,
        )


__all__ = ["emit_agent_proposed", "persist_chain_ids", "update_action_blob"]
