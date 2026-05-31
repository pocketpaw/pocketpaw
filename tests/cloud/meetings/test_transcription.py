# Tests for the transcription-mode feature:
#   - meetings/settings.py — provider resolution (doc vs env), validation
#   - recall_client — create_async_transcript / fetch_async_transcript_vtt
#   - service.start_async_transcript — recording.done -> create_transcript

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import UpdateMeetingsSettingsRequest
from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
from pocketpaw_ee.cloud.meetings.providers.recall import settings as meetings_settings
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc
from pocketpaw_ee.cloud.models.meeting import MeetingsSettings as _SettingsDoc

# ---------------------------------------------------------------------------
# Fake httpx
# ---------------------------------------------------------------------------


class _FakeHttp:
    """httpx.AsyncClient stand-in — pops queued responses, records calls."""

    calls: list = []
    queue: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @classmethod
    def reset(cls, responses):
        cls.calls = []
        cls.queue = list(responses)

    async def post(self, url, json=None, headers=None):
        _FakeHttp.calls.append({"method": "POST", "url": url, "json": json})
        return _FakeHttp.queue.pop(0)

    async def get(self, url, headers=None, params=None):
        _FakeHttp.calls.append({"method": "GET", "url": url})
        return _FakeHttp.queue.pop(0)


def _resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value={} if body is None else body)
    r.text = ""
    return r


# ---------------------------------------------------------------------------
# settings — provider classification + resolution
# ---------------------------------------------------------------------------


def test_is_async_provider():
    assert meetings_settings.is_async_provider("deepgram_async") is True
    assert meetings_settings.is_async_provider("deepgram_streaming") is False
    assert meetings_settings.is_async_provider("meeting_captions") is False


async def test_resolve_env_fallback(mongo_db, monkeypatch):
    monkeypatch.delenv("RECALL_TRANSCRIPT_PROVIDER", raising=False)
    monkeypatch.delenv("RECALL_TRANSCRIPT_MODEL", raising=False)
    assert await meetings_settings.resolve() == {"provider": "meeting_captions", "model": ""}


async def test_resolve_env_override(mongo_db, monkeypatch):
    monkeypatch.setenv("RECALL_TRANSCRIPT_PROVIDER", "deepgram_async")
    monkeypatch.setenv("RECALL_TRANSCRIPT_MODEL", "nova-3")
    assert await meetings_settings.resolve() == {"provider": "deepgram_async", "model": "nova-3"}


async def test_stored_settings_win_over_env(mongo_db, monkeypatch):
    monkeypatch.setenv("RECALL_TRANSCRIPT_PROVIDER", "meeting_captions")
    snap = await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(
            transcript_provider="deepgram_async", transcript_model="nova-3"
        )
    )
    assert snap.mode == "async"
    assert await meetings_settings.resolve() == {"provider": "deepgram_async", "model": "nova-3"}


async def test_update_settings_is_a_singleton(mongo_db):
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(transcript_provider="deepgram_streaming", transcript_model="")
    )
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(transcript_provider="meeting_captions", transcript_model="")
    )
    rows = await _SettingsDoc.find_all().to_list()
    assert len(rows) == 1  # upsert, not a second insert
    assert rows[0].transcript_provider == "meeting_captions"


async def test_update_settings_rejects_unknown_provider(mongo_db):
    with pytest.raises(ValidationError):
        await meetings_settings.update_settings(
            UpdateMeetingsSettingsRequest(transcript_provider="banana", transcript_model="")
        )


async def test_get_settings_derives_mode(mongo_db, monkeypatch):
    monkeypatch.setenv("RECALL_TRANSCRIPT_PROVIDER", "deepgram_streaming")
    monkeypatch.delenv("RECALL_TRANSCRIPT_MODEL", raising=False)
    snap = await meetings_settings.get_settings()
    assert snap.mode == "realtime"
    assert snap.transcript_provider == "deepgram_streaming"


# ---------------------------------------------------------------------------
# recall_client — async transcription
# ---------------------------------------------------------------------------


async def test_create_async_transcript(mongo_db, monkeypatch):
    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.setenv("RECALL_API_KEY", "k")
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(
            transcript_provider="deepgram_async", transcript_model="nova-3"
        )
    )
    _FakeHttp.reset([_resp(200, {"id": "tr-1"})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeHttp)

    transcript_id = await recall_client.create_async_transcript("rec-1")
    assert transcript_id == "tr-1"
    call = _FakeHttp.calls[0]
    assert call["url"] == "http://recall.test/api/v1/recording/rec-1/create_transcript/"
    assert call["json"]["provider"] == {"deepgram_async": {"language": "multi", "model": "nova-3"}}


async def test_create_async_transcript_recallai_uses_language_code(mongo_db, monkeypatch):
    """Recall's own provider takes `language_code: auto`, not `language: multi`."""
    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.setenv("RECALL_API_KEY", "k")
    monkeypatch.delenv("RECALL_TRANSCRIPT_LANGUAGE", raising=False)
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(transcript_provider="recallai_async", transcript_model="")
    )
    _FakeHttp.reset([_resp(200, {"id": "tr-1"})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeHttp)

    await recall_client.create_async_transcript("rec-1")
    assert _FakeHttp.calls[0]["json"]["provider"] == {"recallai_async": {"language_code": "auto"}}


async def test_create_async_transcript_gladia_omits_language(mongo_db, monkeypatch):
    """Gladia v2 auto-detects when no language_code is sent."""
    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.setenv("RECALL_API_KEY", "k")
    monkeypatch.delenv("RECALL_TRANSCRIPT_LANGUAGE", raising=False)
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(transcript_provider="gladia_v2_async", transcript_model="")
    )
    _FakeHttp.reset([_resp(200, {"id": "tr-1"})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeHttp)

    await recall_client.create_async_transcript("rec-1")
    assert _FakeHttp.calls[0]["json"]["provider"] == {"gladia_v2_async": {}}


async def test_create_async_transcript_env_override(mongo_db, monkeypatch):
    """RECALL_TRANSCRIPT_LANGUAGE pins a single language via the provider's field."""
    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.setenv("RECALL_API_KEY", "k")
    monkeypatch.setenv("RECALL_TRANSCRIPT_LANGUAGE", "hi")
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(transcript_provider="recallai_async", transcript_model="")
    )
    _FakeHttp.reset([_resp(200, {"id": "tr-1"})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeHttp)

    await recall_client.create_async_transcript("rec-1")
    assert _FakeHttp.calls[0]["json"]["provider"] == {"recallai_async": {"language_code": "hi"}}


async def test_create_async_transcript_propagates_4xx(mongo_db, monkeypatch):
    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.setenv("RECALL_API_KEY", "k")
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(transcript_provider="deepgram_async", transcript_model="")
    )
    _FakeHttp.reset([_resp(400, {})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeHttp)
    with pytest.raises(ValidationError):
        await recall_client.create_async_transcript("rec-1")


async def test_fetch_async_transcript_vtt(monkeypatch):
    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.setenv("RECALL_API_KEY", "k")
    segments = [
        {
            "participant": {"id": 1, "name": "Ana"},
            "words": [
                {
                    "text": "hi",
                    "start_timestamp": {"relative": 0.0},
                    "end_timestamp": {"relative": 0.5},
                }
            ],
        }
    ]
    _FakeHttp.reset(
        [_resp(200, {"data": {"download_url": "http://dl.test/t.json"}}), _resp(200, segments)]
    )
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeHttp)

    vtt = await recall_client.fetch_async_transcript_vtt("tr-1")
    assert vtt is not None and vtt.startswith("WEBVTT")
    assert "<v Ana>hi</v>" in vtt
    assert _FakeHttp.calls[0]["url"] == "http://recall.test/api/v1/transcript/tr-1/"
    assert _FakeHttp.calls[1]["url"] == "http://dl.test/t.json"


async def test_fetch_async_transcript_not_ready(monkeypatch):
    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.setenv("RECALL_API_KEY", "k")
    _FakeHttp.reset([_resp(200, {"data": {}})])  # no download_url yet
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeHttp)
    assert await recall_client.fetch_async_transcript_vtt("tr-1") is None


# ---------------------------------------------------------------------------
# service.start_async_transcript — the recording.done entry point
# ---------------------------------------------------------------------------


async def test_start_async_transcript_noop_in_realtime_mode(mongo_db, monkeypatch):
    monkeypatch.setenv("RECALL_TRANSCRIPT_PROVIDER", "meeting_captions")
    # No settings doc → realtime → recording.done is not our trigger.
    assert await meetings_service.start_async_transcript("bot-1", "rec-1") is False


async def test_start_async_transcript_kicks_off_and_correlates(mongo_db, monkeypatch):
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(
            transcript_provider="deepgram_async", transcript_model="nova-3"
        )
    )
    meeting = _MeetingDoc(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
        raw_provider_payload={"recall": {"bot_id": "bot-a", "status": "done"}},
    )
    await meeting.insert()

    async def _fake_create(recording_id):
        assert recording_id == "rec-a"
        return "tr-a"

    monkeypatch.setattr(recall_client, "create_async_transcript", _fake_create)

    assert await meetings_service.start_async_transcript("bot-a", "rec-a") is True
    refreshed = await _MeetingDoc.get(meeting.id)
    recall = refreshed.raw_provider_payload["recall"]
    assert recall["transcript_id"] == "tr-a"
    assert recall["recording_id"] == "rec-a"
    assert recall["bot_id"] == "bot-a"  # existing correlation preserved


async def test_start_async_transcript_unknown_bot(mongo_db):
    await meetings_settings.update_settings(
        UpdateMeetingsSettingsRequest(transcript_provider="deepgram_async", transcript_model="")
    )
    assert await meetings_service.start_async_transcript("bot-missing", "rec-x") is False
