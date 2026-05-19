# Tests for ee/cloud/meetings/bot_coordinator.py — Vexa REST client.
# Verifies the HTTP shapes we send to / expect from Vexa.
# See https://github.com/Vexa-ai/vexa for the upstream API.

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def vexa_env(monkeypatch):
    """Set the Vexa env vars our coordinator expects."""
    monkeypatch.setenv("VEXA_BASE_URL", "http://vexa.test:18056")
    monkeypatch.setenv("VEXA_API_KEY", "test-api-key")
    # Clear the legacy name in case it's set in CI env.
    monkeypatch.delenv("VEXA_ADMIN_TOKEN", raising=False)
    yield


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in that records calls."""

    last_calls: list[dict] = []
    next_responses: list[MagicMock] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @classmethod
    def reset(cls, responses):
        cls.last_calls = []
        cls.next_responses = list(responses)

    async def post(self, url, json=None, headers=None):
        type(self).last_calls.append(
            {"method": "POST", "url": url, "json": json, "headers": headers}
        )
        return type(self).next_responses.pop(0)

    async def get(self, url, params=None, headers=None):
        type(self).last_calls.append(
            {"method": "GET", "url": url, "params": params, "headers": headers}
        )
        return type(self).next_responses.pop(0)

    async def delete(self, url, headers=None):
        type(self).last_calls.append({"method": "DELETE", "url": url, "headers": headers})
        return type(self).next_responses.pop(0)


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body)
    r.text = ""
    return r


# ---------------------------------------------------------------------------
# request_bot_for_meeting — POST /bots
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_request_bot_posts_correct_shape_to_vexa(vexa_env, monkeypatch):
    """We send platform + native_meeting_id + bot_name in the body, with Bearer auth."""
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="123456789",
        title="Sprint planning",
        join_url="https://zoom.us/j/123456789",
    )
    await meeting.insert()

    _FakeClient.reset(
        [_resp(200, {"id": "vexa-bot-abc", "status": "queued", "container_name": "bot-123"})]
    )
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    result = await bot_coordinator.request_bot_for_meeting("ws-alpha", str(meeting.id))

    call = _FakeClient.last_calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://vexa.test:18056/bots"
    assert call["json"]["platform"] == "zoom"
    assert call["json"]["native_meeting_id"] == "123456789"
    assert call["json"]["bot_name"] == "PocketPaw Bot"
    assert call["headers"]["X-API-Key"] == "test-api-key"
    assert "Authorization" not in call["headers"]

    # Returned response is normalized.
    assert result["bot_id"] == "vexa-bot-abc"
    assert result["status"] == "queued"
    assert result["vexa_native_meeting_id"] == "123456789"

    # Correlation persisted on the meeting row for the polling path.
    refreshed = await _MD.get(meeting.id)
    assert refreshed.raw_provider_payload["vexa"]["bot_id"] == "vexa-bot-abc"
    assert refreshed.raw_provider_payload["vexa"]["platform"] == "zoom"


@pytest.mark.usefixtures("mongo_db")
async def test_request_bot_extracts_meet_code_from_join_url(vexa_env, monkeypatch):
    """For Google Meet, native_meeting_id is the meeting code, not spaces/<id>."""
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="google_meet",
        provider_meeting_id="spaces/abc123",
        title="Quick chat",
        join_url="https://meet.google.com/xyz-pdqr-stu",
    )
    await meeting.insert()

    _FakeClient.reset([_resp(200, {"id": "bot-1", "status": "queued"})])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    await bot_coordinator.request_bot_for_meeting("ws-1", str(meeting.id))

    call = _FakeClient.last_calls[0]
    assert call["json"]["platform"] == "google_meet"
    # Code from join URL, NOT the spaces/abc123 resource name.
    assert call["json"]["native_meeting_id"] == "xyz-pdqr-stu"


@pytest.mark.usefixtures("mongo_db")
async def test_request_bot_rejects_when_api_key_missing(monkeypatch):
    """No VEXA_API_KEY (and no legacy ADMIN_TOKEN) → structured error before HTTP."""
    from ee.cloud._core.errors import ValidationError
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    monkeypatch.delenv("VEXA_API_KEY", raising=False)
    monkeypatch.delenv("VEXA_ADMIN_TOKEN", raising=False)
    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    with pytest.raises(ValidationError) as exc_info:
        await bot_coordinator.request_bot_for_meeting("ws-1", str(meeting.id))
    assert exc_info.value.code == "meeting.bot_secret_missing"


@pytest.mark.usefixtures("mongo_db")
async def test_request_bot_propagates_vexa_4xx(vexa_env, monkeypatch):
    """Vexa returns 400 → we raise ``meeting.bot_service_error``."""
    from ee.cloud._core.errors import ValidationError
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    bad = _resp(400, {})
    bad.text = '{"error":"invalid native_meeting_id"}'
    _FakeClient.reset([bad])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    with pytest.raises(ValidationError) as exc_info:
        await bot_coordinator.request_bot_for_meeting("ws-1", str(meeting.id))
    assert exc_info.value.code == "meeting.bot_service_error"


# ---------------------------------------------------------------------------
# stop_bot — DELETE /bots/{platform}/{native_id}
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_stop_bot_url_shape(vexa_env, monkeypatch):
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="987654321",
        title="x",
        join_url="https://zoom.us/j/987654321",
    )
    await meeting.insert()

    _FakeClient.reset([_resp(200, {"ok": True})])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    result = await bot_coordinator.stop_bot("ws-1", str(meeting.id))
    assert result == {"ok": True, "stopped": True}
    call = _FakeClient.last_calls[0]
    assert call["method"] == "DELETE"
    assert call["url"] == "http://vexa.test:18056/bots/zoom/987654321"


@pytest.mark.usefixtures("mongo_db")
async def test_stop_bot_treats_404_as_idempotent_success(vexa_env, monkeypatch):
    """If bot isn't running, stop is a no-op success, not an error."""
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    _FakeClient.reset([_resp(404, {})])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)
    result = await bot_coordinator.stop_bot("ws-1", str(meeting.id))
    assert result == {"ok": True, "stopped": False, "reason": "not_running"}


# ---------------------------------------------------------------------------
# fetch_transcript_vtt — GET /recordings, pick the matching one
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_fetch_transcript_inline_vtt(vexa_env, monkeypatch):
    """When Vexa returns inline ``transcript_vtt``, we surface it as-is."""
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    body = {"transcript_vtt": "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello"}
    _FakeClient.reset([_resp(200, body)])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    vtt = await bot_coordinator.fetch_transcript_vtt("ws-1", str(meeting.id))
    assert vtt is not None
    assert vtt.startswith("WEBVTT")
    # Path is /transcripts/{platform}/{native_meeting_id}, no query string.
    call = _FakeClient.last_calls[0]
    assert call["url"] == "http://vexa.test:18056/transcripts/zoom/m1"
    assert call["params"] is None


@pytest.mark.usefixtures("mongo_db")
async def test_fetch_transcript_assembles_from_segments(vexa_env, monkeypatch):
    """When Vexa returns a segments array, we build a WebVTT blob from it.

    This is the common cloud response shape and the one that bit us
    in production — earlier code only handled inline VTT.
    """
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="google_meet",
        provider_meeting_id="spaces/abc",
        title="x",
        join_url="https://meet.google.com/abc-defg-hij",
    )
    await meeting.insert()

    body = {
        "segments": [
            {
                "speaker": "Speaker 0",
                "text": "hello there",
                "start_time": 0.0,
                "end_time": 1.5,
            },
            {
                "speaker": "Speaker 1",
                "text": "general kenobi",
                "start_time": 1.8,
                "end_time": 3.2,
            },
        ]
    }
    _FakeClient.reset([_resp(200, body)])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    vtt = await bot_coordinator.fetch_transcript_vtt("ws-1", str(meeting.id))
    assert vtt is not None
    assert vtt.startswith("WEBVTT")
    assert "<v Speaker 0>hello there" in vtt
    assert "<v Speaker 1>general kenobi" in vtt
    assert "00:00:00.000 --> 00:00:01.500" in vtt


@pytest.mark.usefixtures("mongo_db")
async def test_fetch_transcript_returns_none_on_404(vexa_env, monkeypatch):
    """No transcript yet → Vexa returns 404 → we return None (caller retries)."""
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    _FakeClient.reset([_resp(404, {})])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    vtt = await bot_coordinator.fetch_transcript_vtt("ws-1", str(meeting.id))
    assert vtt is None


@pytest.mark.usefixtures("mongo_db")
async def test_fetch_transcript_real_vexa_cloud_shape(vexa_env, monkeypatch):
    """Locks in parsing of the actual response from api.cloud.vexa.ai.

    This is a real (anonymized) payload captured 2026-05-19. Vexa uses
    ``start`` / ``end`` (not ``start_time`` / ``end_time``) for segment
    timings, and ``speaker`` carries the real participant display name.
    Recordings have AUDIO ``media_files`` only — no transcript file —
    so the segments path is the only one that yields text.
    """
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="google_meet",
        provider_meeting_id="spaces/anon",
        title="Vexa cloud test",
        join_url="https://meet.google.com/abc-defg-hij",
    )
    await meeting.insert()

    body = {
        "id": 12776,
        "platform": "google_meet",
        "native_meeting_id": "abc-defg-hij",
        "status": "completed",
        "start_time": "2026-05-19T09:18:41.934873",
        "end_time": "2026-05-19T09:19:41.268702",
        "recordings": [
            {
                "id": 613823435307,
                "source": "bot",
                "status": "completed",
                "media_files": [
                    # Audio only — no transcript media file. Common case.
                    {"id": 1, "type": "audio", "format": "webm"},
                    {"id": 2, "type": "audio", "format": "webm"},
                ],
            }
        ],
        "data": {"transcribe_enabled": True},
        "segments": [
            {
                "start": 0.907,
                "end": 9.267,
                "text": "Hello, am I audible? Hello, hello.",
                "language": "en",
                "speaker": "Rohit Kushwaha",
                "completed": True,
            },
            {
                "start": 9.267,
                "end": 32.494,
                "text": "This meeting is only for testing on Vexa.",
                "language": "en",
                "speaker": "Rohit Kushwaha",
                "completed": True,
            },
        ],
    }
    _FakeClient.reset([_resp(200, body)])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    vtt = await bot_coordinator.fetch_transcript_vtt("ws-1", str(meeting.id))

    assert vtt is not None, "real Vexa cloud payload must yield a transcript"
    assert vtt.startswith("WEBVTT")
    # Speaker name carries through verbatim.
    assert "<v Rohit Kushwaha>Hello, am I audible" in vtt
    assert "<v Rohit Kushwaha>This meeting is only for testing on Vexa" in vtt
    # Timestamps converted from float seconds to VTT format.
    assert "00:00:00.907 --> 00:00:09.267" in vtt
    assert "00:00:09.267 --> 00:00:32.494" in vtt


@pytest.mark.usefixtures("mongo_db")
async def test_fetch_transcript_returns_none_on_empty_segments(vexa_env, monkeypatch):
    """Vexa returns empty segments → None, not an error."""
    from ee.cloud.meetings import bot_coordinator
    from ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    _FakeClient.reset([_resp(200, {"segments": []})])
    monkeypatch.setattr(bot_coordinator.httpx, "AsyncClient", _FakeClient)

    vtt = await bot_coordinator.fetch_transcript_vtt("ws-1", str(meeting.id))
    assert vtt is None
