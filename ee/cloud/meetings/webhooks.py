# Meetings — inbound Recall.ai webhook.
#
# Recall.ai pushes bot lifecycle + transcript events to a single endpoint
# configured in the Recall dashboard, delivered through Svix. We act on
# `transcript.done` (and `bot.done` as a backstop): resolve the meeting
# from the bot id, then fetch + store the transcript.
#
# This router carries NO auth dependency — Recall is the caller. Trust is
# established by the Svix signature instead. It is mounted separately from
# the licensed meetings router in ee/cloud/__init__.py.
#
# The on-demand fetch path (recall_client.fetch_transcript_vtt) and the
# nightly jobs.py batch remain as fallbacks for missed/over-budget events.

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

from fastapi import APIRouter, Request
from starlette.datastructures import Headers

from ee.cloud._core.errors import Forbidden
from ee.cloud.meetings import service as meetings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings/webhooks", tags=["Meetings"])

# Events worth acting on. `transcript.done` is the real signal; `bot.done`
# is a backstop in case transcription finishes before the bot shuts down.
_ACTIONABLE_EVENTS = {"transcript.done", "bot.done"}

# Svix tolerates a 5-minute clock skew on the signed timestamp.
_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60


@router.post("/recall")
async def recall_webhook(request: Request) -> dict:
    """Ingest a Recall.ai webhook.

    Returns 200 on success or for ignored event types; raises ``Forbidden``
    on a bad signature. A processing failure is re-raised (→ 5xx) so Recall
    retries — ``ingest_transcript_for_recall_bot`` is idempotent.
    """
    raw = await request.body()
    _verify_signature(request.headers, raw)

    try:
        event = json.loads(raw or b"{}")
    except ValueError:
        logger.warning("Recall webhook body was not valid JSON")
        return {"ok": True, "ignored": "malformed_json"}

    event_type = str(event.get("event") or "")
    if event_type not in _ACTIONABLE_EVENTS:
        return {"ok": True, "ignored": event_type or "unknown"}

    bot_id = _extract_bot_id(event)
    if not bot_id:
        logger.warning("Recall webhook %s carried no bot id", event_type)
        return {"ok": True, "ignored": "no_bot_id"}

    stored = await meetings_service.ingest_transcript_for_recall_bot(bot_id)
    logger.info("Recall webhook %s bot=%s transcript_stored=%s", event_type, bot_id, stored)
    return {"ok": True, "bot_id": bot_id, "transcript_stored": stored}


# ---------------------------------------------------------------------------
# Svix signature verification
# ---------------------------------------------------------------------------


def _verify_signature(headers: Headers, body: bytes) -> None:
    """Verify the Svix signature Recall.ai attaches to every webhook.

    No-op (with a loud warning) when ``RECALL_WEBHOOK_SECRET`` is unset —
    so a fresh deployment can be wired up before the secret is pasted in.
    Raises ``Forbidden`` when a secret IS configured and the signature
    fails. The signing scheme is Svix's: HMAC-SHA256 over
    ``{id}.{timestamp}.{body}`` keyed by the base64 secret.
    """
    secret = os.environ.get("RECALL_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.warning(
            "RECALL_WEBHOOK_SECRET is not set — accepting the Recall webhook "
            "WITHOUT signature verification. Set it in production."
        )
        return

    svix_id = headers.get("svix-id") or headers.get("webhook-id")
    svix_ts = headers.get("svix-timestamp") or headers.get("webhook-timestamp")
    svix_sig = headers.get("svix-signature") or headers.get("webhook-signature")
    if not (svix_id and svix_ts and svix_sig):
        raise Forbidden(
            "meeting.webhook_unsigned", "Recall webhook is missing Svix signature headers."
        )

    try:
        ts = int(svix_ts)
    except ValueError as exc:
        raise Forbidden(
            "meeting.webhook_signature_invalid", "Svix timestamp header is malformed."
        ) from exc
    if abs(time.time() - ts) > _TIMESTAMP_TOLERANCE_SECONDS:
        raise Forbidden(
            "meeting.webhook_signature_invalid", "Svix timestamp is outside the tolerance window."
        )

    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, TypeError) as exc:
        raise Forbidden(
            "meeting.webhook_signature_invalid", "RECALL_WEBHOOK_SECRET is not valid base64."
        ) from exc

    signed = f"{svix_id}.{svix_ts}.{body.decode('utf-8', 'replace')}".encode()
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    # The header is space-delimited `v1,<sig>` entries — accept any match.
    for entry in svix_sig.split():
        _, _, sig = entry.partition(",")
        if sig and hmac.compare_digest(sig, expected):
            return
    raise Forbidden("meeting.webhook_signature_invalid", "Recall webhook signature did not verify.")


def _extract_bot_id(event: dict) -> str:
    """Pull the Recall ``bot.id`` out of a webhook payload.

    Recall nests it at ``data.bot.id`` across the events we handle.
    """
    data = event.get("data")
    if not isinstance(data, dict):
        return ""
    bot = data.get("bot")
    if isinstance(bot, dict):
        return str(bot.get("id") or "")
    return ""
