# ee/pocketpaw_ee/cloud/growth/mock_delivery.py — in-process fake delivery for
# a workspace with ``growth_mock_delivery`` on. The executor starts it INSTEAD
# of enqueueing ``growth.dispatch`` for an approved email/WhatsApp draft, so the
# outbound loop can be demoed with no Mailtrap, no MSG91 and no growth worker.
#
# It mirrors real delivery where it matters: the same eligibility guards (a
# refusal writes a ``blocked`` row and leaves the draft ``approved``), a
# ``sending`` row finalised to ``sent`` with ``sent_at`` (so follow-ups schedule
# exactly as for a real send), and the approved→sent flip through the service's
# gate seam. Every row carries provider ``"mock"``, which keeps it out of the
# real WhatsApp hourly cap and lets the queue label it. Never raises: an
# unexpected error becomes a ``failed`` row. Tasks live only in this process,
# so a restart mid-delay loses the delivery and strands a ``sending`` row;
# deliver-approved restarts it once the row is older than stale_after_seconds.
# ``GROWTH_MOCK_DELIVERY_SECONDS`` (default 3) is the staged provider latency.

from __future__ import annotations

import asyncio
import logging
import os
from uuid import uuid4

from pocketpaw_ee.cloud.growth import service as growth_service
from pocketpaw_ee.cloud.growth.domain import MOCK_DELIVERY_PROVIDER, Draft, Prospect

logger = logging.getLogger(__name__)

MOCK_PROVIDER = MOCK_DELIVERY_PROVIDER
_DELAY_ENV = "GROWTH_MOCK_DELIVERY_SECONDS"
_DEFAULT_DELAY = 3.0

# draft_id -> its running delivery. Holds the strong ref asyncio needs to keep
# the task alive, and makes a second start for the same draft a no-op.
_IN_FLIGHT: dict[str, asyncio.Task[None]] = {}


def delay_seconds() -> float:
    """The staged latency, read per call so tests can set it to 0."""
    raw = os.environ.get(_DELAY_ENV, "").strip()
    if not raw:
        return _DEFAULT_DELAY
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value < 0:
        logger.warning(
            "%s=%r is not a non-negative number — using %s", _DELAY_ENV, raw, _DEFAULT_DELAY
        )
        return _DEFAULT_DELAY
    return value


def stale_after_seconds() -> float:
    """How old a ``sending`` row must be before deliver-approved treats it as
    abandoned (its task died with a restart) rather than in flight."""
    return max(60.0, delay_seconds() * 4)


async def is_mock_delivery_on(workspace_id: str) -> bool:
    """Whether the workspace has mock delivery switched on. Any failure to
    read the setting means off, so real sending stays the default."""
    try:
        return await growth_service.mock_delivery_enabled(workspace_id)
    except Exception:  # noqa: BLE001 — fail closed to real delivery
        logger.debug("growth mock delivery: setting unreadable for %s", workspace_id, exc_info=True)
        return False


def _eligibility(draft: Draft, prospect: Prospect | None, channel: str) -> tuple[str, str, str]:
    """``(to_address, blocked_reason, error)`` — the same guards real delivery
    applies. An empty ``blocked_reason`` means the draft may be delivered."""
    if prospect is None:
        return "", "prospect_missing", "The prospect no longer exists"
    if channel == "email":
        to = next((e for e in prospect.emails if e and "@" in e), "")
        if not to:
            return "", "no_address", "The prospect has no email address"
        if not (draft.subject or "").strip():
            return to, "no_subject", "The draft has no subject"
        if not draft.body.strip():
            return to, "no_body", "The draft has no body"
        return to, "", ""
    to = (prospect.whatsapp_number or "").strip()
    if not prospect.opted_in:
        return to, "not_opted_in", "The prospect has not opted in to WhatsApp"
    if not to:
        return "", "no_number", "The prospect has no WhatsApp number"
    return to, "", ""


async def mock_deliver(workspace_id: str, draft_id: str, channel: str) -> None:
    """Deliver one approved draft through the fake provider. Never raises."""
    draft: Draft | None = None
    to_address = ""
    log_id: str | None = None
    try:
        draft = await growth_service.get_draft_for_dispatch(draft_id)
        if (
            draft is None
            or draft.workspace_id != workspace_id
            or draft.channel != channel
            or draft.status != "approved"
        ):
            logger.warning(
                "growth mock delivery: draft %s is not an approved %s draft in %s — skipped",
                draft_id,
                channel,
                workspace_id,
            )
            return
        prospect = await growth_service.get_prospect_for_dispatch(workspace_id, draft.prospect_id)
        to_address, blocked_reason, error = _eligibility(draft, prospect, channel)
        opted_in = bool(prospect and prospect.opted_in)
        if blocked_reason:
            await growth_service.record_delivery_attempt(
                workspace_id,
                draft_id=draft_id,
                prospect_id=draft.prospect_id,
                channel=channel,
                provider=MOCK_PROVIDER,
                to_address=to_address,
                status="blocked",
                blocked_reason=blocked_reason,
                opted_in_at_attempt=opted_in,
                error=error,
            )
            return
        log_id = await growth_service.record_delivery_attempt(
            workspace_id,
            draft_id=draft_id,
            prospect_id=draft.prospect_id,
            channel=channel,
            provider=MOCK_PROVIDER,
            to_address=to_address,
            status="sending",
            opted_in_at_attempt=opted_in,
        )
        await asyncio.sleep(delay_seconds())
        await growth_service.finish_delivery_attempt(
            log_id,
            workspace_id=workspace_id,
            status="sent",
            provider_message_id=f"mock-{uuid4().hex[:12]}",
        )
        await growth_service.gate_transition(workspace_id, draft_id, "sent")
    except asyncio.CancelledError:
        await _record_failure(
            workspace_id, draft, to_address, log_id, "interrupted before delivery"
        )
        raise
    except Exception as exc:  # noqa: BLE001 — recorded on the row, never raised
        logger.exception("growth mock delivery: draft %s failed", draft_id)
        await _record_failure(
            workspace_id, draft, to_address, log_id, f"mock delivery failed ({type(exc).__name__})"
        )


async def _record_failure(
    workspace_id: str, draft: Draft | None, to_address: str, log_id: str | None, error: str
) -> None:
    """Finalise the attempt's row as ``failed`` (or write one if the failure
    came before it existed). The draft stays ``approved``, so it can be
    delivered again."""
    try:
        if log_id is not None:
            await growth_service.finish_delivery_attempt(
                log_id, workspace_id=workspace_id, status="failed", error=error
            )
        elif draft is not None:
            await growth_service.record_delivery_attempt(
                workspace_id,
                draft_id=draft.id,
                prospect_id=draft.prospect_id,
                channel=draft.channel,
                provider=MOCK_PROVIDER,
                to_address=to_address,
                status="failed",
                error=error,
            )
    except Exception:  # noqa: BLE001 — nothing left to record it on
        logger.exception("growth mock delivery: could not record the failure for %s", log_id)


def start_mock_delivery(workspace_id: str, draft_id: str, channel: str) -> bool:
    """Schedule ``mock_deliver`` on the running loop. Returns False (and starts
    nothing) when this draft already has a delivery in flight here."""
    if draft_id in _IN_FLIGHT:
        return False
    task = asyncio.create_task(mock_deliver(workspace_id, draft_id, channel))
    _IN_FLIGHT[draft_id] = task
    task.add_done_callback(lambda _t: _IN_FLIGHT.pop(draft_id, None))
    return True


async def drain_mock_deliveries() -> None:
    """Wait for every in-flight delivery to finish (tests, shutdown)."""
    while _IN_FLIGHT:
        await asyncio.gather(*list(_IN_FLIGHT.values()), return_exceptions=True)


__all__ = [
    "MOCK_PROVIDER",
    "delay_seconds",
    "drain_mock_deliveries",
    "is_mock_delivery_on",
    "mock_deliver",
    "start_mock_delivery",
]
