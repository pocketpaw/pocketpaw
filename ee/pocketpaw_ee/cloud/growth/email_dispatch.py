# ee/pocketpaw_ee/cloud/growth/email_dispatch.py — the email branch of the
# ``growth.dispatch`` arq job (G-5): an APPROVED draft actually leaves the
# building.
#
# Created 2026-07-27 (feat/growth-g5): new module. Lives beside ``worker.py``
# rather than inside it so each channel's delivery owns its own file — the
# worker keeps a one-line branch per channel.
#
# ORDER OF OPERATIONS, and why:
#
#   1. Load the draft (the queue hands the worker only an id) and REFUSE
#      anything that is not ``approved``. draft / proposed / sent / replied /
#      rejected are all no-ops with a warning and NO provider call. This is the
#      last line of the send gate: the enqueue side already proves a human
#      approved the ``_growth_send`` proposal, but a job that sat in the queue
#      while the draft was rejected — or a redelivered job for a draft already
#      ``sent`` — must never put a second message on the wire.
#   2. Send through ``growth.connector`` (Mailtrap HTTPS API, per-workspace
#      credential out of connector state).
#   3. Write the ``MessageLog`` audit row BEFORE the status flip. If the flip
#      then fails, the message physically left and the audit row proves it; the
#      reverse order could lose the only record of a real send.
#   4. Flip approved→sent through ``service.gate_transition`` — the EXISTING
#      gate seam that owns that edge. No second path is added here.
#
# FAILURE PATH: a connector error writes ``MessageLog(outcome="failed",
# error=...)`` and leaves the draft ``approved``, which is exactly the
# retryable state (the human's approval still stands; only delivery failed).
# Nothing raises out of this module — the growth worker runs ``max_tries=1``
# specifically so outbound work can never be auto-retried into a double-send,
# so an escaping exception would buy nothing and lose the audit row. The
# durable failure record IS the MessageLog row, mirroring how the executor's
# ``_fail`` chokepoint records an outcome instead of propagating.

"""Email delivery for the ``growth.dispatch`` job."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pocketpaw_ee.cloud.growth import connector as growth_connector
from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.domain import Draft

logger = logging.getLogger(__name__)

# The only draft status a send may happen from.
_SENDABLE_STATUS = "approved"

EMAIL_CHANNEL = "email"


async def dispatch_email(draft_id: str) -> None:
    """Deliver one approved email draft. Never raises."""
    draft = await growth_service.get_draft_for_dispatch(draft_id)
    if draft is None:
        logger.warning("growth email dispatch: draft %s no longer exists — nothing sent", draft_id)
        return
    if draft.channel != EMAIL_CHANNEL:
        logger.warning(
            "growth email dispatch: draft %s is channel=%r, not email — nothing sent",
            draft_id,
            draft.channel,
        )
        return
    if draft.status != _SENDABLE_STATUS:
        # The gate's last line. Not an error — a queued job whose draft moved
        # on is expected; it just must not send.
        logger.warning(
            "growth email dispatch: draft %s is %r, not %r — refusing to send",
            draft_id,
            draft.status,
            _SENDABLE_STATUS,
        )
        return

    workspace_id = draft.workspace_id
    prospect = await growth_service.get_prospect_for_dispatch(workspace_id, draft.prospect_id)
    to_address = next((e for e in (prospect.emails if prospect else ()) if e and "@" in e), "")

    try:
        sent = await growth_connector.send_email(
            workspace_id=workspace_id,
            to_address=to_address,
            subject=draft.subject or "",
            body=draft.body,
        )
    except Exception as exc:  # noqa: BLE001 — a failed send is recorded, never raised
        reason = (
            str(exc)
            if isinstance(exc, growth_connector.EmailSendError)
            # An unexpected exception's str() is not known-sanitised; record the
            # type only so no credential can reach the audit row.
            else f"unexpected dispatch error ({type(exc).__name__})"
        )
        logger.error("growth email dispatch: draft %s failed — %s", draft_id, reason)
        await _record(
            draft=draft,
            to_address=to_address,
            outcome="failed",
            error=reason,
        )
        return

    await _record(
        draft=draft,
        to_address=sent.to_address,
        outcome="sent",
        provider_message_id=sent.provider_message_id,
        sent_at=datetime.now(UTC),
    )

    try:
        await growth_service.gate_transition(workspace_id, draft_id, "sent")
    except Exception:  # noqa: BLE001 — the message is already out; don't re-send
        logger.exception(
            "growth email dispatch: draft %s was DELIVERED but the sent flip failed — "
            "the MessageLog row is the record of truth",
            draft_id,
        )


async def _record(
    *,
    draft: Draft,
    to_address: str,
    outcome: str,
    provider_message_id: str | None = None,
    sent_at: datetime | None = None,
    error: str | None = None,
) -> None:
    """Write the audit row. Best-effort — a log failure must not re-raise."""
    try:
        await growth_service.record_message_log(
            workspace_id=draft.workspace_id,
            draft_id=draft.id,
            prospect_id=draft.prospect_id,
            channel=EMAIL_CHANNEL,
            provider=growth_connector.MAILTRAP_CONNECTOR_NAME,
            to_address=to_address,
            outcome=outcome,
            provider_message_id=provider_message_id,
            sent_at=sent_at,
            error=error,
        )
    except Exception:  # noqa: BLE001 — audit write failure must not break the job
        logger.exception(
            "growth email dispatch: could not write the %s message log for draft %s",
            outcome,
            draft.id,
        )


__all__ = ["EMAIL_CHANNEL", "dispatch_email"]
