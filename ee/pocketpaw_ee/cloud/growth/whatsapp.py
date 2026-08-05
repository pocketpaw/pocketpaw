# ee/pocketpaw_ee/cloud/growth/whatsapp.py — the ``channel="whatsapp"`` branch of
# the ``growth.dispatch`` job (G-6).
#
# THE OPT-IN GUARD IS THE POINT OF THIS MODULE. Meta's policy on
# business-initiated template messages is not a style preference: sending a
# template to a number that never opted in tanks the WhatsApp Business Account's
# quality rating, and a repeat offender's number gets restricted and then banned
# — for the whole tenant, not the one draft. So ``prospect.opted_in`` is enforced
# HERE, at the service layer, one call above the provider, rather than as a UI
# convention or a router check. A not-opted-in prospect makes NO provider call at
# all: the guard writes a ``blocked`` compliance row, raises ``OptInRequired``,
# and leaves the draft sitting in ``approved`` so a human can see it never went.
#
# Guard sequence (order matters — the cheap, irreversible-consequence checks
# come first, and every refusal writes its own ``blocked`` row):
#   1. Draft exists and is still ``approved``. The gate (G-4) owns that status;
#      a draft that moved between enqueue and dispatch must not send.
#   2. The prospect still exists in the DRAFT's workspace (tenancy is threaded
#      from the doc, never from the job payload).
#   3. ``prospect.opted_in`` — the hard guard. No opt-in, no provider call.
#   4. A reachable number.
#   5. The per-hour rate cap (``GROWTH_WHATSAPP_MAX_PER_HOUR``).
#   6. Resolvable MSG91 credentials for that workspace.
# Only then: write the ``sending`` row, call the provider, finalise the row, and
# flip approved→sent through the EXISTING ``service.gate_transition`` seam — the
# gate owns that edge, this module does not reimplement it.
#
# WHY A RATE CAP AT ALL: WhatsApp quality rating is computed over a rolling
# window of recent business-initiated messages. A burst — a bulk approval, a
# retry storm, a mis-scoped follow-up cron — is exactly the shape that trips it,
# and the damage (messaging-limit downgrade, then restriction) lands on the
# WABA, not on the individual send. Capping outbound per hour keeps a bug
# expensive-but-survivable instead of account-fatal. Refused sends do NOT
# consume the window (they never reached Meta) and are never silently dropped:
# each writes a ``rate_capped`` row and fails the job loudly.
#
# Created 2026-07-27 (feat/growth-g6): new module.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud.growth.msg91 import (
    Msg91Credentials,
    Msg91Error,
    Msg91NotConfigured,
    Msg91WhatsAppClient,
)

logger = logging.getLogger(__name__)

# Per-workspace outbound ceiling per rolling hour. 20 is deliberately low: the
# /growth engine is a considered-outbound tool, not a blaster, and a human
# approves every single send through the Instinct gate anyway — so the cap is a
# blast-radius bound on bugs, not a throughput knob.
_MAX_PER_HOUR_ENV = "GROWTH_WHATSAPP_MAX_PER_HOUR"
_DEFAULT_MAX_PER_HOUR = 20
_RATE_WINDOW = timedelta(hours=1)


class WhatsAppDispatchError(Exception):
    """A WhatsApp send was refused or failed. ``code`` is machine-readable."""

    code = "growth.whatsapp_dispatch_failed"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DraftNotApproved(WhatsAppDispatchError):
    """The draft is not in ``approved`` — the gate did not clear this send."""

    code = "growth.draft_not_approved"


class ProspectUnavailable(WhatsAppDispatchError):
    """The draft's prospect is gone, or has no reachable WhatsApp number."""

    code = "growth.prospect_unavailable"


class OptInRequired(WhatsAppDispatchError):
    """The prospect has not opted in — the hard compliance stop.

    Raised BEFORE any provider client is constructed or called. If you are
    reading this because a send failed: the fix is an opt-in, never a bypass.
    """

    code = "growth.whatsapp_opt_in_required"


class RateCapExceeded(WhatsAppDispatchError):
    """The workspace already sent its hourly allowance."""

    code = "growth.whatsapp_rate_capped"


class WhatsAppNotConfigured(WhatsAppDispatchError):
    """No usable MSG91 credentials for the draft's workspace."""

    code = "growth.whatsapp_not_configured"


def max_per_hour() -> int:
    """The configured hourly cap. Read per call so ops can retune without a
    redeploy and so tests can drive the boundary with ``monkeypatch.setenv``.
    A non-numeric or negative value falls back to the default rather than
    accidentally disabling the protection."""
    raw = os.environ.get(_MAX_PER_HOUR_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_PER_HOUR
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer — falling back to %d",
            _MAX_PER_HOUR_ENV,
            raw,
            _DEFAULT_MAX_PER_HOUR,
        )
        return _DEFAULT_MAX_PER_HOUR
    return value if value >= 0 else _DEFAULT_MAX_PER_HOUR


def _build_client(credentials: Msg91Credentials) -> Any:
    """Construct the provider client.

    Module-level indirection so tests inject a fake by monkeypatching THIS
    function — the same seam shape as ``executor._get_pool``. Because the fake
    is the only client the suite ever builds, "the provider was never called"
    is assertable directly on the fake instead of by mocking ``httpx``.
    """
    return Msg91WhatsAppClient(credentials)


async def dispatch_whatsapp(draft_id: str) -> None:
    """Send one approved WhatsApp draft via MSG91. See the module header.

    Raises a ``WhatsAppDispatchError`` subclass on every refusal. The growth
    worker runs with ``max_tries = 1``, so a raise records a failed job for
    operator review rather than retrying an outbound message.
    """
    from pocketpaw_ee.cloud.growth import service as growth_service

    draft = await growth_service.get_draft_for_dispatch(draft_id)
    if draft is None:
        # Nothing to block, nothing to send, nothing to attribute to a
        # workspace — a deleted draft is a non-event, not a failure.
        logger.warning("growth/whatsapp: draft %s no longer exists — nothing dispatched", draft_id)
        return

    workspace_id = draft.workspace_id

    async def _blocked(reason: str, *, to_number: str = "", opted_in: bool = False) -> None:
        """Record a refusal. No provider call has happened or will happen."""
        await growth_service.record_whatsapp_attempt(
            workspace_id,
            draft_id=draft_id,
            prospect_id=draft.prospect_id,
            to_number=to_number,
            status="blocked",
            blocked_reason=reason,
            opted_in_at_attempt=opted_in,
        )

    # (1) The gate (G-4) owns ``approved``. A draft that moved between enqueue
    # and dispatch — rejected by hand, already sent — must not go out.
    if draft.status != "approved":
        await _blocked("draft_not_approved")
        raise DraftNotApproved(
            f"draft {draft_id} is '{draft.status}', not 'approved' — refusing to send"
        )

    # (2) Tenancy comes off the draft doc, never off the job payload.
    prospect = await growth_service.get_prospect_for_dispatch(workspace_id, draft.prospect_id)
    if prospect is None:
        await _blocked("prospect_missing")
        raise ProspectUnavailable(
            f"draft {draft_id} points at a prospect that no longer exists in its workspace"
        )

    # (3) THE HARD GUARD. Not a warning, not a UI convention: no opt-in, no
    # provider call, draft left approved, refusal recorded.
    if not prospect.opted_in:
        await _blocked("not_opted_in", to_number=prospect.whatsapp_number or "")
        logger.warning(
            "growth/whatsapp: REFUSED draft %s — prospect %s has not opted in "
            "(no provider call made)",
            draft_id,
            draft.prospect_id,
        )
        raise OptInRequired(
            "WhatsApp template sends require a recorded opt-in from the prospect; "
            f"prospect {draft.prospect_id} has none"
        )

    to_number = (prospect.whatsapp_number or "").strip()
    # (4) An opted-in prospect with no number is a data problem, not a policy one.
    if not to_number:
        await _blocked("no_number", opted_in=True)
        raise ProspectUnavailable(
            f"prospect {draft.prospect_id} has opted in but carries no WhatsApp number"
        )

    # (5) Quality-rating protection. Counted over the trailing hour of attempts
    # that actually reached the provider; refusals don't consume the budget.
    # There is deliberately no "disabled" value: a cap of 0 refuses everything
    # rather than meaning "unlimited", so a fat-fingered env var fails closed.
    cap = max_per_hour()
    since = datetime.now(UTC) - _RATE_WINDOW
    recent = await growth_service.count_whatsapp_attempts_since(workspace_id, since)
    if recent >= cap:
        await _blocked("rate_capped", to_number=to_number, opted_in=True)
        logger.warning(
            "growth/whatsapp: REFUSED draft %s — workspace %s already made %d/%d "
            "attempts this hour (no provider call made)",
            draft_id,
            workspace_id,
            recent,
            cap,
        )
        raise RateCapExceeded(
            f"workspace {workspace_id} has already sent {recent} WhatsApp messages in "
            f"the last hour (cap {cap}, {_MAX_PER_HOUR_ENV})"
        )

    # (6) Credentials last — an unconfigured workspace is a blocked attempt, not
    # a failed one, because nothing was tried.
    try:
        credentials = await resolve_credentials(workspace_id)
    except Msg91NotConfigured as exc:
        await _blocked("not_configured", to_number=to_number, opted_in=True)
        raise WhatsAppNotConfigured(str(exc)) from exc

    log_id = await growth_service.record_whatsapp_attempt(
        workspace_id,
        draft_id=draft_id,
        prospect_id=draft.prospect_id,
        to_number=to_number,
        status="sending",
        opted_in_at_attempt=True,
    )

    client = _build_client(credentials)
    try:
        provider_message_id = await client.send_template(to_number=to_number, body_text=draft.body)
    except Msg91Error as exc:
        await growth_service.finish_whatsapp_attempt(
            log_id, status="failed", error_code=exc.code, error=exc.message
        )
        # The draft stays ``approved`` — the approval stands, the delivery
        # didn't. An operator can re-dispatch without a second human approval.
        raise WhatsAppDispatchError(f"MSG91 send failed: {exc.message}") from exc

    await growth_service.finish_whatsapp_attempt(
        log_id, status="sent", provider_message_id=provider_message_id
    )
    # The gate owns approved→sent; this module walks it through the existing
    # seam rather than writing the status itself.
    await growth_service.gate_transition(workspace_id, draft_id, "sent")
    logger.info(
        "growth/whatsapp: sent draft %s (workspace=%s, provider_message_id=%r)",
        draft_id,
        workspace_id,
        provider_message_id,
    )


async def resolve_credentials(workspace_id: str) -> Msg91Credentials:
    """Thin re-export of the msg91 resolver.

    Kept as a module-level function here so a test can stub credential
    resolution at the dispatch seam without reaching into the connector state
    store — and so the credential object never has to be threaded through the
    job payload.
    """
    from pocketpaw_ee.cloud.growth.msg91 import resolve_credentials as _resolve

    return await _resolve(workspace_id)


__all__ = [
    "DraftNotApproved",
    "OptInRequired",
    "ProspectUnavailable",
    "RateCapExceeded",
    "WhatsAppDispatchError",
    "WhatsAppNotConfigured",
    "dispatch_whatsapp",
    "max_per_hour",
]
