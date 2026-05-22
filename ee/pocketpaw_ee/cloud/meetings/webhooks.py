# Meetings — inbound Recall.ai webhook.
#
# Recall.ai pushes bot lifecycle + transcript events to a single endpoint
# configured in the Recall dashboard, delivered through Svix. We handle:
#   * `bot.*` lifecycle events       → persist the bot's status on the meeting
#   * `recording.done`               → kick off async transcription (async mode)
#   * `transcript.done` / `bot.done` → fetch + store the transcript
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

from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.meetings import service as meetings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings/webhooks", tags=["Meetings"])

# Events that trigger transcript ingestion. `transcript.done` is the real
# signal; `bot.done` is a backstop in case transcription finishes before
# the bot shuts down.
_TRANSCRIPT_EVENTS = {"transcript.done", "bot.done"}

# Svix tolerates a 5-minute clock skew on the signed timestamp.
_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60


@router.post("/recall")
async def recall_webhook(request: Request) -> dict:
    """Ingest a Recall.ai webhook.

    Handles two event families:
      * ``bot.*`` lifecycle events (``bot.joining_call``,
        ``bot.in_waiting_room``, ``bot.in_call_recording``, ``bot.done``,
        ``bot.fatal``, …) — persist the bot's status. Recall puts the
        specific status in the ``event`` field; there is no generic
        ``bot.status_change`` event.
      * ``transcript.done`` / ``bot.done`` — fetch + store the transcript.

    Returns 200 on success or for ignored event types; raises ``Forbidden``
    on a bad signature. A processing failure is re-raised (→ 5xx) so Recall
    retries — both service entry points are idempotent.
    """
    raw = await request.body()
    _verify_signature(request.headers, raw)

    try:
        event = json.loads(raw or b"{}")
    except ValueError:
        logger.warning("Recall webhook body was not valid JSON")
        return {"ok": True, "ignored": "malformed_json"}

    event_type = str(event.get("event") or "")
    bot_id = _extract_bot_id(event)
    if not bot_id:
        logger.warning("Recall webhook %s carried no bot id", event_type)
        return {"ok": True, "ignored": "no_bot_id"}

    result: dict = {"ok": True, "bot_id": bot_id}

    # Every `bot.*` event is a lifecycle status change. The status code is
    # in `data.data.code`; fall back to the event suffix if it's absent.
    if event_type.startswith("bot."):
        code, sub_code = _extract_status(event)
        code = code or event_type.removeprefix("bot.")
        matched = await meetings_service.update_bot_status_for_recall_bot(bot_id, code, sub_code)
        result["bot_status"] = code
        logger.info(
            "Recall webhook %s bot=%s status=%s matched=%s",
            event_type,
            bot_id,
            code,
            matched,
        )

    # recording.done → in async mode, kick off transcription against the
    # finished recording. A no-op in realtime mode (guarded service-side).
    if event_type == "recording.done":
        recording_id = _extract_recording_id(event)
        if recording_id:
            started = await meetings_service.start_async_transcript(bot_id, recording_id)
            result["transcript_started"] = started
            logger.info(
                "Recall webhook recording.done bot=%s recording=%s started=%s",
                bot_id,
                recording_id,
                started,
            )

    # transcript.done is the real transcript signal; bot.done is a backstop.
    if event_type in _TRANSCRIPT_EVENTS:
        stored = await meetings_service.ingest_transcript_for_recall_bot(bot_id)
        result["transcript_stored"] = stored
        logger.info("Recall webhook %s bot=%s transcript_stored=%s", event_type, bot_id, stored)

    if (
        "bot_status" not in result
        and "transcript_stored" not in result
        and "transcript_started" not in result
    ):
        return {"ok": True, "ignored": event_type or "unknown"}
    return result


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


def _extract_recording_id(event: dict) -> str:
    """Pull the Recall ``recording.id`` out of a ``recording.done`` payload.

    Recall nests it at ``data.recording.id``.
    """
    data = event.get("data")
    if not isinstance(data, dict):
        return ""
    rec = data.get("recording")
    if isinstance(rec, dict):
        return str(rec.get("id") or "")
    return ""


def _extract_status(event: dict) -> tuple[str, str | None]:
    """Pull the status ``code`` + ``sub_code`` from a ``bot.*`` event payload.

    Recall nests these at ``data.data.{code,sub_code}``.
    """
    data = event.get("data")
    if not isinstance(data, dict):
        return "", None
    inner = data.get("data")
    if isinstance(inner, dict):
        return str(inner.get("code") or ""), inner.get("sub_code")
    return "", None
