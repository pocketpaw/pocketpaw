# ee/pocketpaw_ee/cloud/growth/propose.py — file a gated /growth outbound send
# through Instinct (the human approve/reject layer, "sudo for agents").
#
# The propose half of the /growth send gate. An outreach draft NEVER goes out
# from a tool call or an HTTP request: ``POST /growth/drafts/{id}/propose``
# flips the draft to ``proposed`` and files an Instinct ``Action`` carrying a
# ``_growth_send`` blob. A human approves in The Tray, and only then does
# ``growth.executor.execute_approved_growth_send`` flip the draft to
# ``approved`` and enqueue the ``growth.dispatch`` job.
#
# A peer gated proposal kind alongside ``_ship_action`` (the reference this
# mirrors), ``_external_action``, ``_admin_action`` et al. — same blob
# discipline: schema version so a stale pending Action fails LOUD, the
# originating ``workspace_id`` as a top-level field for the router's tenancy
# gate, an idempotency key so a retry never double-dispatches, and the
# Decision-Graph chain fields (``correlation_id`` / ``proposed_event_id``).
#
# The blob carries the RENDERED PREVIEW (subject + body) so the human approves
# the exact copy that was staged — plus the prospect's name/company so the
# Tray card is reviewable without a lookup. No credential ever rides the blob.
#
# Created 2026-07-27 (feat/growth-g4): new module.

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# The key the blob lives under on ``Action.parameters``.
GROWTH_SEND_PARAM_KEY = "_growth_send"
# Discriminator + schema version. Bump the schema when the blob shape changes so
# a stale pending Action approved after a deploy fails LOUD rather than
# dispatching a misinterpreted send.
GROWTH_SEND_KIND = "growth_send"
GROWTH_SEND_SCHEMA = 1


def _emit_agent_proposed(
    *,
    correlation_id: UUID,
    action_id: str,
    channel: str,
    target_label: str,
    workspace_id: str,
    user_id: str,
) -> UUID | None:
    """Emit the chain-opening ``agent.proposed`` event for a growth send.

    Mirrors ``ship.propose._emit_agent_proposed``: the proposing caller is the
    actor, and a growth send is workspace-scoped rather than pocket-bound, so
    the chain's ``pocket_id`` carries the workspace id (matching how the
    Action's ``pocket_id`` field does). Returns the emitted ``EventEntry.id``
    for the blob's ``proposed_event_id`` (the ``human.corrected`` causation
    link), or ``None`` when the emit raised — best-effort per RFC 09.
    """
    try:
        from soul_protocol.spec.journal import Actor

        from pocketpaw_ee.cloud.decisions.journal_writer import record_agent_proposed

        actor = Actor(
            kind="agent",
            id=f"user:{user_id or 'unknown'}",
            scope_context=[f"workspace:{workspace_id}"],
        )
        payload: dict[str, Any] = {
            "intent": f"send {channel} outreach to {target_label}",
            "action": "growth_send",
            "pocket_id": workspace_id,
            "inputs": [],
            "proposal_kind": "growth_send",
            "proposal": {"channel": channel, "target": target_label},
            "action_id": action_id,
        }
        entry = record_agent_proposed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
        )
        return entry.id
    except Exception:  # noqa: BLE001 — chain emission is best-effort (RFC 09)
        logger.warning(
            "growth: agent.proposed emit failed for action %s — the chain opens "
            "without causation; the reconciler catches orphans",
            action_id,
            exc_info=True,
        )
        return None


async def propose_growth_send(
    *,
    workspace_id: str,
    draft_id: str,
    prospect_id: str,
    channel: str,
    prospect_name: str,
    prospect_company: str,
    preview_subject: str | None,
    preview_body: str,
    requested_by: str,
    idempotency_key: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
) -> str:
    """Build + store an Instinct ``Action`` for a gated outbound send.

    Returns the proposed Action id — the ``proposal_id`` the propose route
    hands back to the client. NOTHING is dispatched here.

    Args:
        workspace_id: the originating tenant. The router's tenancy gate and the
            executor read it off the blob and refuse an empty one.
        draft_id / prospect_id: the draft being sent and its prospect.
        channel: the draft's channel (email / linkedin / whatsapp).
        prospect_name / prospect_company: for the Tray card — the human sees
            who this goes to without a lookup.
        preview_subject / preview_body: the RENDERED copy the human approves.
        requested_by: the user id that proposed (chain actor + the
            authorization re-check at execute).
        idempotency_key: so the executor never double-dispatches. Defaults to
            a deterministic value keyed on workspace + draft.
        correlation_id: an optional pre-minted chain id (fresh one when omitted).
        assignee: who should approve. Defaults to the proposer.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_growth_send requires a non-empty workspace_id")
    draft_id = str(draft_id or "")
    if not draft_id:
        raise ValueError("propose_growth_send requires a draft_id")

    target_label = f"{prospect_name} ({prospect_company})"
    idem = idempotency_key or f"{workspace_id}:growth_send:{draft_id}"
    corr = correlation_id or str(uuid4())
    human_summary = f"Send {channel} outreach to {target_label}."

    blob: dict[str, Any] = {
        "kind": GROWTH_SEND_KIND,
        "schema": GROWTH_SEND_SCHEMA,
        "workspace_id": workspace_id,
        "draft_id": draft_id,
        "prospect_id": prospect_id,
        "channel": channel,
        "prospect_name": prospect_name,
        "prospect_company": prospect_company,
        "preview": {"subject": preview_subject, "body": preview_body},
        "idempotency_key": idem,
        "requested_by": requested_by,
        "summary": human_summary,
        "correlation_id": corr,
        "proposed_event_id": None,
        # Back-written by the executor: {status, detail, executed_at}.
        "outcome": None,
    }

    title = f"Outreach — {channel} to {target_label}"
    subject_line = f"Subject: {preview_subject}\n\n" if preview_subject else ""
    recommendation = (
        f"Approve to send this {channel} message to {target_label}. "
        f"Nothing goes out without this approval.\n\n{subject_line}{preview_body}"
    )
    trigger = ActionTrigger(
        type="agent",
        source=requested_by or "growth",
        reason=f"/growth outbound '{channel}' send to {target_label} requires approval",
    )

    # Scope the store to the caller's workspace — mirrors ship.propose (no
    # ``current_workspace`` ContextVar is set on this path).
    store = get_instinct_store(workspace_id=workspace_id or None)
    action_obj = await store.propose(
        # ``pocket_id`` carries the workspace: a growth send is tenant-scoped,
        # not pocket-bound (same as ship / external actions / belt).
        pocket_id=workspace_id,
        title=title,
        description=human_summary,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.EXTERNAL,
        priority=ActionPriority.HIGH,
        parameters={GROWTH_SEND_PARAM_KEY: blob},
        assignee=assignee or requested_by or None,
        workspace_id=workspace_id,
    )

    action_id = str(getattr(action_obj, "id", "") or "")
    proposed_event_id = _emit_agent_proposed(
        correlation_id=UUID(corr),
        action_id=action_id,
        channel=channel,
        target_label=target_label,
        workspace_id=workspace_id,
        user_id=requested_by,
    )
    if proposed_event_id is not None:
        await _persist_proposed_event_id(
            store=store, action_id=action_id, blob=blob, event_id=str(proposed_event_id)
        )

    logger.info(
        "growth: proposed '%s' send for draft %s → Instinct action %s (workspace=%s)",
        channel,
        draft_id,
        action_id,
        workspace_id,
    )
    return action_id


async def _persist_proposed_event_id(
    *, store: Any, action_id: str, blob: dict[str, Any], event_id: str
) -> None:
    """Write the ``agent.proposed`` event id back onto the stored blob.

    Best-effort, mirroring ``ship.propose``: without it the eventual
    ``human.corrected`` emits with no causation id, which the Decision-Graph
    reconciler repairs. A failure here must never fail the propose.
    """
    try:
        import json as _json

        import aiosqlite

        blob["proposed_event_id"] = event_id
        params = {GROWTH_SEND_PARAM_KEY: blob}
        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "growth: failed to persist chain ids onto action %s — the chain's "
            "human.corrected will emit without causation_id",
            action_id,
            exc_info=True,
        )
