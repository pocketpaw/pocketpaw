# Meetings — Vexa bot service client.
# Created: 2026-05-19. Replaces the earlier coordination layer for our
# in-tree meeting-bot/ workspace (now archived) with a thin client over
# Vexa's REST API (https://github.com/Vexa-ai/vexa, Apache 2.0).
#
# See memory: project-meeting-bot-decision for the architectural rationale.
#
# Surface:
#   request_bot_for_meeting(workspace_id, meeting_id) — POST /bots
#   stop_bot(workspace_id, meeting_id)               — DELETE /bots/{platform}/{id}
#   fetch_transcript(workspace_id, meeting_id)       — GET /recordings → find match
#
# Vexa does NOT push transcripts via webhook. We poll their /recordings
# endpoint on-demand (when the agent calls find_meeting_transcript) and
# from the nightly batch (ee/cloud/meetings/jobs.py). Trade-off: we pay
# a small latency on first-ask vs. a real-time push; in exchange we
# avoid running another listener.

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud.models.meeting import Meeting as _MeetingDoc

logger = logging.getLogger(__name__)


# Vexa platform identifiers — the path segment for DELETE /bots/{platform}/...
# Their docs use these exact strings; map our internal provider names.
_PROVIDER_TO_VEXA = {
    "zoom": "zoom",
    "google_meet": "google_meet",
}


def _vexa_base_url() -> str:
    """Vexa meeting-api base URL.

    Defaults to the hosted cloud at ``api.cloud.vexa.ai``. Override for
    self-hosted via env (``VEXA_BASE_URL=http://vexa-meeting-api:18056``
    or your Coolify service URL).
    """
    return os.environ.get("VEXA_BASE_URL", "https://api.cloud.vexa.ai").rstrip("/")


def _vexa_api_key() -> str:
    """User API key from vexa.ai/account (cloud) or your self-hosted instance.

    Backward-compat: also accepts the older ``VEXA_ADMIN_TOKEN`` name
    used by some self-hosted deployments, with a deprecation warning.
    """
    key = os.environ.get("VEXA_API_KEY", "")
    if not key:
        # Self-hosted users may still have the old env var set.
        legacy = os.environ.get("VEXA_ADMIN_TOKEN", "")
        if legacy:
            logger.warning("VEXA_ADMIN_TOKEN is deprecated — set VEXA_API_KEY instead.")
            return legacy
        raise ValidationError(
            "meeting.bot_secret_missing",
            "VEXA_API_KEY is not configured — Vexa integration is disabled. "
            "Get a key at https://vexa.ai/account (cloud) or mint one against "
            "your self-hosted instance.",
        )
    return key


def _vexa_headers() -> dict[str, str]:
    """Auth + content headers for every Vexa call.

    Vexa's User API uses ``X-API-Key`` (not ``Authorization: Bearer``).
    """
    return {
        "X-API-Key": _vexa_api_key(),
        "Content-Type": "application/json",
    }


def _native_meeting_id(meeting: _MeetingDoc) -> str:
    """Convert our stored provider_meeting_id to Vexa's native_meeting_id form.

    Zoom: numeric meeting ID, used as-is.
    Google Meet: Vexa expects the meeting code (the short code in the URL like
        ``abc-defg-hij``), not our internal ``spaces/<id>`` resource name.
        We extract the code from join_url when available, else fall back
        to the raw provider_meeting_id (good enough for codes already
        stored as bare strings).
    """
    if meeting.provider == "google_meet":
        # join_url is ``https://meet.google.com/<code>``
        if meeting.join_url and "meet.google.com/" in meeting.join_url:
            code = meeting.join_url.rsplit("meet.google.com/", 1)[1].strip("/")
            if code:
                return code
    return meeting.provider_meeting_id


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------


async def request_bot_for_meeting(workspace_id: str, meeting_id: str) -> dict[str, Any]:
    """Ask Vexa to send a bot to this meeting.

    Returns Vexa's response payload (which includes a bot identifier we
    persist on the meeting row for correlation).

    Raises ``NotFound`` if the meeting doesn't exist;
    ``ValidationError`` for misconfigured envs or Vexa-side rejections.
    """
    meeting = await _resolve_meeting(workspace_id, meeting_id)
    vexa_platform = _PROVIDER_TO_VEXA.get(meeting.provider)
    if vexa_platform is None:
        raise ValidationError(
            "meeting.unsupported_provider",
            f"Vexa does not support provider '{meeting.provider}'",
        )

    body = {
        "platform": vexa_platform,
        "native_meeting_id": _native_meeting_id(meeting),
        "bot_name": os.environ.get("POCKETPAW_BOT_DISPLAY_NAME", "PocketPaw Bot"),
        # Vexa supports per-bot language hint; default English. Overridable
        # later via Meeting metadata if we want per-meeting languages.
        "language": "en",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{_vexa_base_url()}/bots", json=body, headers=_vexa_headers())
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.bot_service_error",
            f"Vexa rejected bot request: {resp.status_code} {resp.text[:200]}",
        )
    payload = resp.json()

    # Record correlation so the polling path can find this back.
    meeting.raw_provider_payload = {
        **(meeting.raw_provider_payload or {}),
        "vexa": {
            "platform": vexa_platform,
            "native_meeting_id": body["native_meeting_id"],
            "bot_id": payload.get("id") or payload.get("bot_id") or "",
            "container_name": payload.get("container_name", ""),
        },
    }
    await meeting.save()

    logger.info(
        "requested Vexa bot ws=%s meeting=%s platform=%s native_id=%s",
        workspace_id,
        meeting_id,
        vexa_platform,
        body["native_meeting_id"],
    )
    # Normalize response shape so callers (the MCP tool) don't have to
    # know Vexa's exact field names.
    return {
        "bot_id": payload.get("id") or payload.get("bot_id") or "",
        "meeting_id": meeting_id,
        "status": payload.get("status", "queued"),
        "vexa_native_meeting_id": body["native_meeting_id"],
    }


async def stop_bot(workspace_id: str, meeting_id: str) -> dict[str, Any]:
    """Stop an active Vexa bot for this meeting.

    Idempotent at the API level: Vexa returns 404 if the bot isn't
    running, which we map to a no-op success.
    """
    meeting = await _resolve_meeting(workspace_id, meeting_id)
    vexa_platform = _PROVIDER_TO_VEXA.get(meeting.provider)
    if vexa_platform is None:
        return {"ok": False, "reason": "unsupported_provider"}
    native_id = _native_meeting_id(meeting)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{_vexa_base_url()}/bots/{vexa_platform}/{native_id}",
            headers=_vexa_headers(),
        )
    if resp.status_code == 404:
        return {"ok": True, "stopped": False, "reason": "not_running"}
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.bot_service_error",
            f"Vexa rejected stop request: {resp.status_code} {resp.text[:200]}",
        )
    return {"ok": True, "stopped": True}


# ---------------------------------------------------------------------------
# Transcript fetch — polling Vexa's recordings on demand
# ---------------------------------------------------------------------------


async def fetch_transcript_vtt(workspace_id: str, meeting_id: str) -> str | None:
    """Find this meeting's transcript on Vexa and return it as VTT.

    Vexa's user API exposes one endpoint per meeting:
    ``GET /transcripts/{platform}/{native_meeting_id}``. Response is a
    JSON envelope containing segments + (optionally) recordings with
    downloadable media files.

    Returns the VTT text, or ``None`` when Vexa has no transcript yet
    (bot still running, transcription pipeline still processing, or
    audio not yet captured because the bot wasn't admitted).

    Raises ``NotFound`` for missing meetings on OUR side;
    ``ValidationError`` for Vexa errors that aren't 404.
    """
    meeting = await _resolve_meeting(workspace_id, meeting_id)
    vexa_platform = _PROVIDER_TO_VEXA.get(meeting.provider)
    if vexa_platform is None:
        return None
    native_id = _native_meeting_id(meeting)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_vexa_base_url()}/transcripts/{vexa_platform}/{native_id}",
            headers=_vexa_headers(),
        )
    if resp.status_code == 404:
        # No transcript for this meeting yet — not an error.
        return None
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.bot_service_error",
            f"Vexa /transcripts failed: {resp.status_code} {resp.text[:300]}",
        )
    body = resp.json() or {}

    # Path 1 — inline VTT field directly on the payload.
    for key in ("transcript_vtt", "vtt", "transcript"):
        v = body.get(key)
        if isinstance(v, str) and v.strip().startswith("WEBVTT"):
            return v

    # Path 2 — segments array we assemble into VTT ourselves.
    segments = body.get("segments") or body.get("transcript_segments") or []
    if isinstance(segments, list) and segments:
        vtt = _segments_to_vtt(segments)
        if vtt:
            return vtt

    # Path 3 — recordings[].media_files[] with a transcript file we download.
    recordings = body.get("recordings") or []
    if isinstance(recordings, list) and recordings:
        recordings.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        rec = recordings[0]
        files = rec.get("media_files") or []
        vtt_file = next(
            (f for f in files if (f.get("kind") == "transcript" or f.get("mime") == "text/vtt")),
            None,
        )
        rec_id = rec.get("id")
        file_id = vtt_file.get("id") if vtt_file else None
        if rec_id and file_id:
            async with httpx.AsyncClient(timeout=60) as client:
                dl = await client.get(
                    f"{_vexa_base_url()}/recordings/{rec_id}/media/{file_id}/download",
                    headers=_vexa_headers(),
                )
            if dl.status_code < 400 and dl.text.strip():
                return dl.text

    # Vexa returned a payload but in a shape we don't recognize. Log a
    # diagnostic so future failures point at the schema instead of a
    # silent None.
    logger.warning(
        "Vexa transcript payload for %s/%s had no recognizable VTT field. Keys present: %s",
        vexa_platform,
        native_id,
        sorted(body.keys()) if isinstance(body, dict) else type(body).__name__,
    )
    return None


def _segments_to_vtt(segments: list[dict]) -> str:
    """Convert Vexa's segments array to a single WebVTT blob.

    Tolerant of multiple field-name variants — different Vexa versions
    have used ``start_time``/``start_seconds``/``start``, etc.
    """
    cues: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = (
            seg.get("speaker") or seg.get("speaker_label") or seg.get("participant") or "Speaker"
        )
        start = seg.get("start_time", seg.get("start_seconds", seg.get("start", 0.0)))
        end = seg.get("end_time", seg.get("end_seconds", seg.get("end", 0.0)))
        try:
            start_ts = _seconds_to_vtt_ts(float(start))
            end_ts = _seconds_to_vtt_ts(float(end))
        except (TypeError, ValueError):
            # If timing info is unparseable, fall back to a synthetic
            # cue range so the text isn't lost.
            start_ts, end_ts = "00:00:00.000", "00:00:01.000"
        cues.append(f"{start_ts} --> {end_ts}\n<v {speaker}>{text}</v>")
    if not cues:
        return ""
    return "WEBVTT\n\n" + "\n\n".join(cues)


def _seconds_to_vtt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_meeting(workspace_id: str, meeting_id: str) -> _MeetingDoc:
    meeting = await _MeetingDoc.find_one(
        _MeetingDoc.workspace == workspace_id,
        _MeetingDoc.id == meeting_id,
    )
    if meeting is None:
        try:
            from beanie import PydanticObjectId

            meeting = await _MeetingDoc.find_one(
                _MeetingDoc.workspace == workspace_id,
                _MeetingDoc.id == PydanticObjectId(meeting_id),
            )
        except Exception:
            meeting = None
    if meeting is None:
        raise NotFound("meeting", meeting_id)
    return meeting
