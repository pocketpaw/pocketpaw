# Meetings — Recall.ai bot service client.
#
# Recall.ai (https://recall.ai) is a hosted meeting-bot API: it sends a
# bot into a Zoom / Google Meet / Teams call, records it, and produces a
# transcript. This module is a thin typed httpx wrapper over the v1 REST
# API — Recall ships no maintained Python SDK, so a small in-tree client
# (mirroring src/pocketpaw/clients/zoom.py) is the right call.
#
# Replaces the earlier Vexa integration. Differences that shaped this
# rewrite:
#   * Recall takes the meeting *URL* directly — no per-platform native-id
#     mapping, and Teams works for free.
#   * Recall is single-account: one operator API key (RECALL_API_KEY),
#     not per-tenant BYO creds.
#   * Transcripts arrive two ways, both feeding the same VTT assembler:
#       - push: Recall fires `transcript.done` to our webhook (webhooks.py)
#       - pull: on-demand fetch here + the nightly jobs.py batch
#
# Surface:
#   request_bot_for_meeting(workspace_id, meeting_id) — POST /api/v1/bot/
#   stop_bot(workspace_id, meeting_id)                — POST .../leave_call/
#   fetch_transcript_vtt(workspace_id, meeting_id)    — GET bot → transcript
#   create_async_transcript(recording_id)            — POST .../create_transcript/
#   fetch_async_transcript_vtt(transcript_id)        — GET transcript → VTT

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.meetings.providers.recall import settings as meetings_settings
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

logger = logging.getLogger(__name__)

# Recall.ai runs four data-isolated regions, each its own base URL and
# its own API key. Pick one with RECALL_REGION; RECALL_BASE_URL overrides
# it outright (useful for tests / proxies).
_RECALL_REGIONS = {
    "us-east-1": "https://us-east-1.recall.ai",
    "us-west-2": "https://us-west-2.recall.ai",
    "eu-central-1": "https://eu-central-1.recall.ai",
    "ap-northeast-1": "https://ap-northeast-1.recall.ai",
}
_DEFAULT_REGION = "us-east-1"


def _recall_base_url() -> str:
    """Resolve the Recall.ai API base URL for this deployment."""
    explicit = os.environ.get("RECALL_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    region = os.environ.get("RECALL_REGION", _DEFAULT_REGION).strip() or _DEFAULT_REGION
    base = _RECALL_REGIONS.get(region)
    if base is None:
        raise ValidationError(
            "meeting.recall_region_invalid",
            f"RECALL_REGION '{region}' is not a known Recall.ai region. "
            f"Use one of: {', '.join(sorted(_RECALL_REGIONS))}.",
        )
    return base


def _recall_api_key() -> str:
    """Operator API key from the Recall.ai dashboard (region-specific)."""
    key = os.environ.get("RECALL_API_KEY", "").strip()
    if not key:
        raise ValidationError(
            "meeting.bot_secret_missing",
            "RECALL_API_KEY is not configured — the meeting-bot integration "
            "is disabled. Create a key at https://recall.ai for your region.",
        )
    return key


def _recall_headers() -> dict[str, str]:
    """Auth + content headers for every Recall.ai call.

    Recall authenticates with ``Authorization: Token <key>`` — not a
    Bearer token.
    """
    return {
        "Authorization": f"Token {_recall_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------


async def request_bot_for_meeting(workspace_id: str, meeting_id: str) -> dict[str, Any]:
    """Send a Recall.ai bot to this meeting to record + transcribe it.

    Persists the returned ``bot_id`` on the meeting row so the webhook
    (webhooks.py) and the on-demand fetch path can correlate later.

    Raises ``NotFound`` if the meeting is unknown to the workspace, and
    ``ValidationError`` for misconfigured envs, a missing join URL, or a
    Recall-side rejection.
    """
    meeting = await _resolve_meeting(workspace_id, meeting_id)
    if not meeting.join_url:
        raise ValidationError(
            "meeting.no_join_url",
            "Meeting has no join URL — cannot dispatch a recording bot.",
        )

    body: dict[str, Any] = {
        "meeting_url": meeting.join_url,
        "bot_name": os.environ.get("POCKETPAW_BOT_DISPLAY_NAME", "PocketPaw Bot"),
        # Echoed back on every webhook for this bot — lets the webhook
        # cross-check the workspace without a DB round-trip.
        "metadata": {"workspace_id": workspace_id, "meeting_id": str(meeting.id)},
    }
    # Realtime providers are configured on the bot here and transcribe
    # live. Async providers (`*_async`) are NOT valid in recording_config
    # — the bot just records, and transcription is kicked off post-call
    # against the finished recording (create_async_transcript, driven by
    # the `recording.done` webhook).
    transcript_cfg = await meetings_settings.resolve()
    if not meetings_settings.is_async_provider(transcript_cfg["provider"]):
        options = _provider_options(transcript_cfg["provider"], transcript_cfg["model"])
        body["recording_config"] = {
            "transcript": {"provider": {transcript_cfg["provider"]: options}}
        }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_recall_base_url()}/api/v1/bot/", json=body, headers=_recall_headers()
        )
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.bot_service_error",
            f"Recall.ai rejected the bot request: {resp.status_code} {resp.text[:300]}",
        )
    payload = resp.json() or {}
    bot_id = str(payload.get("id") or "")
    if not bot_id:
        raise ValidationError("meeting.bot_service_error", "Recall.ai did not return a bot id.")

    status = _latest_status(payload)
    meeting.raw_provider_payload = {
        **(meeting.raw_provider_payload or {}),
        "recall": {"bot_id": bot_id, "status": status},
    }
    await meeting.save()

    logger.info(
        "requested Recall.ai bot ws=%s meeting=%s bot=%s status=%s",
        workspace_id,
        meeting_id,
        bot_id,
        status,
    )
    return {"bot_id": bot_id, "meeting_id": meeting_id, "status": status}


async def stop_bot(workspace_id: str, meeting_id: str) -> dict[str, Any]:
    """Tell an active Recall.ai bot to leave this meeting.

    Idempotent: a 404 (bot already gone / never dispatched) maps to a
    no-op success rather than an error.
    """
    meeting = await _resolve_meeting(workspace_id, meeting_id)
    bot_id = _bot_id_of(meeting)
    if not bot_id:
        return {"ok": True, "stopped": False, "reason": "no_bot"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_recall_base_url()}/api/v1/bot/{bot_id}/leave_call/",
            headers=_recall_headers(),
        )
    if resp.status_code == 404:
        return {"ok": True, "stopped": False, "reason": "not_running"}
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.bot_service_error",
            f"Recall.ai rejected the stop request: {resp.status_code} {resp.text[:200]}",
        )
    return {"ok": True, "stopped": True}


async def get_bot_status(bot_id: str) -> dict[str, Any] | None:
    """Fetch a Recall bot's current lifecycle status by ``bot_id``.

    Returns ``{status, sub_code, updated_at}`` from the latest
    ``status_changes`` entry — e.g. ``in_waiting_room`` (bot is knocking,
    needs admission), ``in_call_recording``, ``done``, ``fatal``. Returns
    ``None`` if Recall has no such bot.

    A single on-demand call — not a poll loop. Continuous tracking comes
    from the ``bot.status_change`` webhook (see webhooks.py).
    """
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{_recall_base_url()}/api/v1/bot/{bot_id}/", headers=_recall_headers()
        )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.bot_service_error",
            f"Recall.ai GET bot failed: {resp.status_code} {resp.text[:200]}",
        )
    changes = (resp.json() or {}).get("status_changes") or []
    latest = changes[-1] if isinstance(changes, list) and changes else {}
    return {
        "status": str(latest.get("code") or "unknown"),
        "sub_code": latest.get("sub_code"),
        "updated_at": latest.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Transcript fetch
# ---------------------------------------------------------------------------


async def fetch_transcript_vtt(workspace_id: str, meeting_id: str) -> str | None:
    """Fetch this meeting's bot transcript from Recall.ai as WebVTT.

    Returns the VTT text, or ``None`` when there is nothing to fetch yet
    — no bot was dispatched, the bot is still in the call, or Recall is
    still transcribing. Callers retry; the webhook delivers the same
    transcript the moment Recall finishes.

    Raises ``NotFound`` if the meeting is unknown to OUR side.
    """
    meeting = await _resolve_meeting(workspace_id, meeting_id)
    bot_id = _bot_id_of(meeting)
    if not bot_id:
        return None
    return await fetch_transcript_vtt_for_bot(bot_id)


async def fetch_transcript_vtt_for_bot(bot_id: str) -> str | None:
    """Pull a bot's transcript by Recall ``bot_id`` and assemble it to VTT.

    Shared by the on-demand path and the webhook listener. ``None`` means
    Recall has no ready transcript for this bot yet.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_recall_base_url()}/api/v1/bot/{bot_id}/", headers=_recall_headers()
        )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.bot_service_error",
            f"Recall.ai GET bot failed: {resp.status_code} {resp.text[:300]}",
        )
    download_url = _transcript_download_url(resp.json() or {})
    if not download_url:
        return None

    # The download URL is a pre-signed storage link — no auth header, and
    # it must NOT carry our Recall token.
    async with httpx.AsyncClient(timeout=60) as client:
        dl = await client.get(download_url)
    if dl.status_code >= 400:
        logger.warning("Recall transcript download failed: %s", dl.status_code)
        return None
    try:
        segments = dl.json()
    except ValueError:
        logger.warning("Recall transcript download was not JSON for bot=%s", bot_id)
        return None
    return _segments_to_vtt(segments) or None


async def create_async_transcript(recording_id: str) -> str:
    """Kick off async transcription for a finished recording.

    POSTs to ``/api/v1/recording/{id}/create_transcript/`` with the
    deployment's configured async provider + model. Returns the Recall
    transcript id. Driven by the ``recording.done`` webhook — see
    service.start_async_transcript.
    """
    resolved = await meetings_settings.resolve()
    options = _provider_options(resolved["provider"], resolved["model"])

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_recall_base_url()}/api/v1/recording/{recording_id}/create_transcript/",
            json={"provider": {resolved["provider"]: options}},
            headers=_recall_headers(),
        )
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.transcript_service_error",
            f"Recall.ai rejected create_transcript: {resp.status_code} {resp.text[:300]}",
        )
    transcript_id = str((resp.json() or {}).get("id") or "")
    if not transcript_id:
        raise ValidationError(
            "meeting.transcript_service_error", "Recall.ai create_transcript returned no id."
        )
    return transcript_id


async def fetch_async_transcript_vtt(transcript_id: str) -> str | None:
    """Fetch a completed async transcript by Recall transcript id, as VTT.

    ``GET /api/v1/transcript/{id}/`` → ``data.download_url`` → the
    speaker-turn JSON, run through the shared VTT assembler. Returns
    ``None`` when Recall has no ready transcript yet (no download URL).
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{_recall_base_url()}/api/v1/transcript/{transcript_id}/",
            headers=_recall_headers(),
        )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise ValidationError(
            "meeting.transcript_service_error",
            f"Recall.ai GET transcript failed: {resp.status_code} {resp.text[:200]}",
        )
    download_url = ((resp.json() or {}).get("data") or {}).get("download_url")
    if not isinstance(download_url, str) or not download_url:
        return None

    # Pre-signed storage link — no auth header (must NOT carry our token).
    async with httpx.AsyncClient(timeout=60) as client:
        dl = await client.get(download_url)
    if dl.status_code >= 400:
        logger.warning("Recall async transcript download failed: %s", dl.status_code)
        return None
    try:
        segments = dl.json()
    except ValueError:
        logger.warning("Recall async transcript was not JSON for transcript=%s", transcript_id)
        return None
    return _segments_to_vtt(segments) or None


def _transcript_download_url(bot_payload: dict[str, Any]) -> str | None:
    """Find the transcript artifact's download URL in a bot payload.

    Recall exposes it at ``recordings[].media_shortcuts.transcript.data
    .download_url``. The URL is absent until transcription completes.
    """
    for rec in bot_payload.get("recordings") or []:
        if not isinstance(rec, dict):
            continue
        transcript = (rec.get("media_shortcuts") or {}).get("transcript") or {}
        url = (transcript.get("data") or {}).get("download_url")
        if isinstance(url, str) and url:
            return url
    return None


def _segments_to_vtt(segments: Any) -> str:
    """Convert Recall.ai's transcript JSON into a single WebVTT blob.

    Recall's async transcript download is a list of speaker turns:
    ``[{participant: {id, name}, language_code, words: [{text,
    start_timestamp: {relative}, end_timestamp: {relative}}]}]``. One
    VTT cue per turn.
    """
    if not isinstance(segments, list):
        return ""
    cues: list[str] = []
    languages: set[str] = set()
    turns_with_words = 0
    turns_without_words = 0
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        lang = seg.get("language_code")
        if isinstance(lang, str) and lang:
            languages.add(lang)
        words = seg.get("words") or []
        text = " ".join(
            (w.get("text") or "").strip()
            for w in words
            if isinstance(w, dict) and (w.get("text") or "").strip()
        ).strip()
        if not text:
            turns_without_words += 1
            continue
        turns_with_words += 1
        participant = seg.get("participant") or {}
        speaker = participant.get("name") or f"Speaker {participant.get('id', '?')}"
        start = _word_ts(words[0], "start_timestamp")
        end = _word_ts(words[-1], "end_timestamp")
        if end < start:
            end = start
        cues.append(
            f"{_seconds_to_vtt_ts(start)} --> {_seconds_to_vtt_ts(end)}\n<v {speaker}>{text}</v>"
        )
    # Surface what Recall/Deepgram actually detected — the symptom of
    # "wrong language" almost always shows up here as `languages={'en'}`
    # for a non-English meeting (multilingual not configured) or as an
    # all-empty turn list (recording captured no usable audio).
    logger.info(
        "Recall transcript turns: with_words=%d empty=%d detected_languages=%s",
        turns_with_words,
        turns_without_words,
        sorted(languages) or "n/a",
    )
    if not cues:
        return ""
    return "WEBVTT\n\n" + "\n\n".join(cues)


def _word_ts(word: Any, key: str) -> float:
    """Extract a word's relative timestamp (seconds) — tolerant of gaps."""
    if not isinstance(word, dict):
        return 0.0
    raw = (word.get(key) or {}).get("relative", 0.0)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


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


def _provider_options(provider: str, model: str) -> dict[str, Any]:
    """Build the provider-options object Recall expects, language + model.

    Used by BOTH paths so realtime and async stay in sync:
      * realtime bot creation — embedded in ``recording_config.transcript
        .provider.{provider}``
      * async post-call transcription — embedded in
        ``provider.{provider}`` on ``create_transcript``

    Each Recall provider names the language field differently and accepts
    different sentinel values for auto-detect / multilingual. Sending the
    wrong field name is silently ignored by Recall and the provider then
    defaults to English — which is how a Hinglish call came back English-
    only despite a "multi" setting. ``RECALL_TRANSCRIPT_LANGUAGE`` is an
    override; otherwise we pick a sensible auto/multilingual default per
    provider. ``model`` (e.g. ``nova-3``) is merged in when set so the
    realtime path no longer drops it on the floor.
    """
    override = os.environ.get("RECALL_TRANSCRIPT_LANGUAGE", "").strip()
    options: dict[str, Any] = {}

    # --- Deepgram --------------------------------------------------------
    if provider in ("deepgram_async", "deepgram_streaming"):
        # nova-3 multilingual; field name is `language`, value `multi`.
        options["language"] = override or "multi"

    # --- Recall.ai's own STT --------------------------------------------
    elif provider in ("recallai_async", "recallai_streaming"):
        # Recall's own; field `language_code`, value `auto` for detect.
        # Streaming additionally needs prioritize_accuracy for non-English
        # / auto-detect — prioritize_low_latency is English-only.
        options["language_code"] = override or "auto"
        if provider == "recallai_streaming":
            options["mode"] = "prioritize_accuracy"

    # --- Gladia v2 -------------------------------------------------------
    elif provider in ("gladia_v2_async", "gladia_v2_streaming"):
        # Whisper-based; auto-detects when language_code is omitted. Only
        # emit the field if an override is set.
        if override:
            options["language_code"] = override

    # --- Speechmatics ----------------------------------------------------
    elif provider in ("speechmatics_async", "speechmatics_streaming"):
        options["language"] = override or "auto"

    # --- AssemblyAI ------------------------------------------------------
    elif provider in ("assembly_ai_async", "assembly_ai_v3_streaming"):
        # Universal model handles multi-lang without a hint.
        if override:
            options["language_code"] = override

    # --- ElevenLabs / AWS / Rev (streaming only today) -------------------
    elif provider in (
        "elevenlabs_streaming",
        "aws_transcribe_streaming",
        "rev_streaming",
    ):
        # No published unified field name. Pass `language` when overridden;
        # otherwise let the provider default. Don't guess multilingual.
        if override:
            options["language"] = override

    # --- Unknown / new --------------------------------------------------
    else:
        if override:
            options["language"] = override

    if model:
        options["model"] = model
    return options


def _latest_status(bot_payload: dict[str, Any]) -> str:
    """Most recent bot status from a Recall bot payload's status_changes."""
    changes = bot_payload.get("status_changes") or []
    if isinstance(changes, list) and changes and isinstance(changes[-1], dict):
        return str(changes[-1].get("code") or "joining_call")
    return "joining_call"


def _bot_id_of(meeting: _MeetingDoc) -> str:
    """Read the correlated Recall bot id off a meeting row, or '' if none."""
    return str((meeting.raw_provider_payload or {}).get("recall", {}).get("bot_id") or "")


async def _resolve_meeting(workspace_id: str, meeting_id: str) -> _MeetingDoc:
    """Load a workspace-scoped meeting, tolerating string or ObjectId ids."""
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
