# Tests for the Recall.ai meeting-bot integration:
#   * ee/cloud/meetings/recall_client.py — REST client
#   * ee/cloud/meetings/webhooks.py      — inbound Svix webhook
#   * ee/cloud/meetings/service.ingest_transcript_for_recall_bot
#
# See https://recall.ai for the upstream API.

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def recall_env(monkeypatch):
    """Point the client at a fake Recall host with a known API key."""
    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.setenv("RECALL_API_KEY", "test-key")
    monkeypatch.delenv("RECALL_REGION", raising=False)
    monkeypatch.delenv("RECALL_TRANSCRIPT_PROVIDER", raising=False)
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


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json = MagicMock(return_value=body)
    r.text = ""
    return r


# ---------------------------------------------------------------------------
# request_bot_for_meeting — POST /api/v1/bot/
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_request_bot_posts_correct_shape(recall_env, monkeypatch):
    """We send meeting_url + recording_config + metadata with Token auth."""
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-alpha",
        provider="zoom",
        provider_meeting_id="123456789",
        title="Sprint planning",
        join_url="https://zoom.us/j/123456789",
    )
    await meeting.insert()

    _FakeClient.reset([_resp(201, {"id": "bot-abc", "status_changes": [{"code": "joining_call"}]})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeClient)

    result = await recall_client.request_bot_for_meeting("ws-alpha", str(meeting.id))

    call = _FakeClient.last_calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://recall.test/api/v1/bot/"
    assert call["json"]["meeting_url"] == "https://zoom.us/j/123456789"
    assert call["json"]["bot_name"] == "PocketPaw Bot"
    assert call["json"]["recording_config"]["transcript"]["provider"] == {"meeting_captions": {}}
    assert call["json"]["metadata"]["workspace_id"] == "ws-alpha"
    assert call["headers"]["Authorization"] == "Token test-key"

    assert result["bot_id"] == "bot-abc"
    assert result["status"] == "joining_call"

    # Correlation persisted on the meeting row for webhook + polling paths.
    refreshed = await _MD.get(meeting.id)
    assert refreshed.raw_provider_payload["recall"]["bot_id"] == "bot-abc"


@pytest.mark.usefixtures("mongo_db")
async def test_request_bot_rejects_when_api_key_missing(monkeypatch):
    """No RECALL_API_KEY → structured error before any HTTP call."""
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    monkeypatch.setenv("RECALL_BASE_URL", "http://recall.test")
    monkeypatch.delenv("RECALL_API_KEY", raising=False)
    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    with pytest.raises(ValidationError) as exc_info:
        await recall_client.request_bot_for_meeting("ws-1", str(meeting.id))
    assert exc_info.value.code == "meeting.bot_secret_missing"


@pytest.mark.usefixtures("mongo_db")
async def test_request_bot_rejects_meeting_without_join_url(recall_env, monkeypatch):
    """A meeting with no join URL can't host a bot — structured error."""
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="",
    )
    await meeting.insert()

    with pytest.raises(ValidationError) as exc_info:
        await recall_client.request_bot_for_meeting("ws-1", str(meeting.id))
    assert exc_info.value.code == "meeting.no_join_url"


@pytest.mark.usefixtures("mongo_db")
async def test_request_bot_propagates_recall_4xx(recall_env, monkeypatch):
    """Recall returns 400 → we raise ``meeting.bot_service_error``."""
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    bad = _resp(400, {})
    bad.text = '{"detail":"invalid meeting_url"}'
    _FakeClient.reset([bad])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeClient)

    with pytest.raises(ValidationError) as exc_info:
        await recall_client.request_bot_for_meeting("ws-1", str(meeting.id))
    assert exc_info.value.code == "meeting.bot_service_error"


# ---------------------------------------------------------------------------
# stop_bot — POST /api/v1/bot/{id}/leave_call/
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_stop_bot_url_shape(recall_env, monkeypatch):
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
        raw_provider_payload={"recall": {"bot_id": "bot-987"}},
    )
    await meeting.insert()

    _FakeClient.reset([_resp(200, {})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeClient)

    result = await recall_client.stop_bot("ws-1", str(meeting.id))
    assert result == {"ok": True, "stopped": True}
    call = _FakeClient.last_calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://recall.test/api/v1/bot/bot-987/leave_call/"


@pytest.mark.usefixtures("mongo_db")
async def test_stop_bot_without_dispatched_bot_is_noop(recall_env, monkeypatch):
    """No correlated bot id → no-op success, no HTTP call."""
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    result = await recall_client.stop_bot("ws-1", str(meeting.id))
    assert result == {"ok": True, "stopped": False, "reason": "no_bot"}


@pytest.mark.usefixtures("mongo_db")
async def test_stop_bot_treats_404_as_idempotent_success(recall_env, monkeypatch):
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
        raw_provider_payload={"recall": {"bot_id": "bot-gone"}},
    )
    await meeting.insert()

    _FakeClient.reset([_resp(404, {})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeClient)
    result = await recall_client.stop_bot("ws-1", str(meeting.id))
    assert result == {"ok": True, "stopped": False, "reason": "not_running"}


# ---------------------------------------------------------------------------
# fetch_transcript_vtt — GET bot → transcript download_url → VTT
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_fetch_transcript_assembles_vtt_from_recall_shape(recall_env, monkeypatch):
    """GET bot exposes a download URL; the linked JSON becomes WebVTT."""
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="google_meet",
        provider_meeting_id="spaces/abc",
        title="x",
        join_url="https://meet.google.com/abc-defg-hij",
        raw_provider_payload={"recall": {"bot_id": "bot-xyz"}},
    )
    await meeting.insert()

    bot_payload = {
        "id": "bot-xyz",
        "recordings": [
            {"media_shortcuts": {"transcript": {"data": {"download_url": "http://dl.test/t.json"}}}}
        ],
    }
    transcript_json = [
        {
            "participant": {"id": 1, "name": "Rohit Kushwaha"},
            "words": [
                {
                    "text": "Hello",
                    "start_timestamp": {"relative": 0.907},
                    "end_timestamp": {"relative": 1.2},
                },
                {
                    "text": "everyone",
                    "start_timestamp": {"relative": 1.3},
                    "end_timestamp": {"relative": 9.267},
                },
            ],
        }
    ]
    _FakeClient.reset([_resp(200, bot_payload), _resp(200, transcript_json)])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeClient)

    vtt = await recall_client.fetch_transcript_vtt("ws-1", str(meeting.id))

    assert vtt is not None
    assert vtt.startswith("WEBVTT")
    assert "<v Rohit Kushwaha>Hello everyone</v>" in vtt
    assert "00:00:00.907 --> 00:00:09.267" in vtt
    # First call is the bot GET with auth; second is the unauthenticated
    # pre-signed download.
    assert _FakeClient.last_calls[0]["url"] == "http://recall.test/api/v1/bot/bot-xyz/"
    assert _FakeClient.last_calls[0]["headers"]["Authorization"] == "Token test-key"
    assert _FakeClient.last_calls[1]["url"] == "http://dl.test/t.json"
    assert _FakeClient.last_calls[1]["headers"] is None


@pytest.mark.usefixtures("mongo_db")
async def test_fetch_transcript_returns_none_without_bot(recall_env, monkeypatch):
    """No correlated bot → nothing to fetch, return None (caller retries)."""
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
    )
    await meeting.insert()

    vtt = await recall_client.fetch_transcript_vtt("ws-1", str(meeting.id))
    assert vtt is None


@pytest.mark.usefixtures("mongo_db")
async def test_fetch_transcript_returns_none_when_not_ready(recall_env, monkeypatch):
    """Bot exists but Recall hasn't produced a transcript URL yet → None."""
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-1",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
        raw_provider_payload={"recall": {"bot_id": "bot-pending"}},
    )
    await meeting.insert()

    _FakeClient.reset([_resp(200, {"id": "bot-pending", "recordings": []})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeClient)

    vtt = await recall_client.fetch_transcript_vtt("ws-1", str(meeting.id))
    assert vtt is None


def test_segments_to_vtt_skips_empty_turns():
    """Turns with no words produce no cue; speaker falls back to id."""
    from pocketpaw_ee.cloud.meetings.providers.recall.client import _segments_to_vtt

    segments = [
        {"participant": {"id": 7}, "words": []},
        {
            "participant": {"id": 8},
            "words": [
                {
                    "text": "ok",
                    "start_timestamp": {"relative": 2.0},
                    "end_timestamp": {"relative": 2.5},
                }
            ],
        },
    ]
    vtt = _segments_to_vtt(segments)
    assert vtt.startswith("WEBVTT")
    assert vtt.count("-->") == 1
    assert "<v Speaker 8>ok</v>" in vtt


# ---------------------------------------------------------------------------
# Webhook — signature verification + dispatch
# ---------------------------------------------------------------------------

_WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"recall-webhook-test-secret-key!!").decode()


def _svix_headers(secret: str, body: bytes, *, ts: int | None = None) -> dict:
    svix_id = "msg_test123"
    svix_ts = str(ts if ts is not None else int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{svix_id}.{svix_ts}.{body.decode()}".encode()
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "svix-id": svix_id,
        "svix-timestamp": svix_ts,
        "svix-signature": f"v1,{sig}",
    }


def _make_request(body: bytes, headers: dict) -> Request:
    header_list = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/meetings/webhooks/recall",
        "headers": header_list,
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


async def test_webhook_transcript_done_triggers_ingest(monkeypatch):
    """A signed transcript.done webhook ingests the transcript for that bot."""
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    captured: dict = {}

    async def _fake_ingest(bot_id):
        captured["bot_id"] = bot_id
        return True

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.service.ingest_transcript_for_recall_bot", _fake_ingest
    )

    body = json.dumps({"event": "transcript.done", "data": {"bot": {"id": "bot-hook-1"}}}).encode()
    request = _make_request(body, _svix_headers(_WEBHOOK_SECRET, body))

    result = await webhooks.recall_webhook(request)
    assert result["ok"] is True
    assert result["transcript_stored"] is True
    assert captured["bot_id"] == "bot-hook-1"


async def test_webhook_ignores_unrelated_events(monkeypatch):
    """An event we don't handle is acked 200 without touching the service."""
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps({"event": "recording.processing", "data": {"bot": {"id": "bot-x"}}}).encode()
    request = _make_request(body, _svix_headers(_WEBHOOK_SECRET, body))

    result = await webhooks.recall_webhook(request)
    assert result == {"ok": True, "ignored": "recording.processing"}


async def test_webhook_recording_done_starts_async_transcript(monkeypatch):
    """recording.done routes to start_async_transcript with the recording id."""
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    captured: dict = {}

    async def _fake_start(bot_id, recording_id):
        captured.update(bot_id=bot_id, recording_id=recording_id)
        return True

    monkeypatch.setattr("pocketpaw_ee.cloud.meetings.service.start_async_transcript", _fake_start)

    body = json.dumps(
        {
            "event": "recording.done",
            "data": {"recording": {"id": "rec-99"}, "bot": {"id": "bot-99"}},
        }
    ).encode()
    request = _make_request(body, _svix_headers(_WEBHOOK_SECRET, body))

    result = await webhooks.recall_webhook(request)
    assert result["ok"] is True
    assert result["transcript_started"] is True
    assert captured == {"bot_id": "bot-99", "recording_id": "rec-99"}


async def test_webhook_bot_status_event_persists_status(monkeypatch):
    """A bot.* lifecycle event routes to update_bot_status_for_recall_bot."""
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    captured: dict = {}

    async def _fake_update(bot_id, status, sub_code):
        captured.update(bot_id=bot_id, status=status, sub_code=sub_code)
        return True

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.service.update_bot_status_for_recall_bot", _fake_update
    )

    body = json.dumps(
        {
            "event": "bot.in_waiting_room",
            "data": {
                "data": {"code": "in_waiting_room", "sub_code": None},
                "bot": {"id": "bot-w"},
            },
        }
    ).encode()
    request = _make_request(body, _svix_headers(_WEBHOOK_SECRET, body))

    result = await webhooks.recall_webhook(request)
    assert result["ok"] is True
    assert result["bot_status"] == "in_waiting_room"
    assert captured == {"bot_id": "bot-w", "status": "in_waiting_room", "sub_code": None}


async def test_webhook_rejects_bad_signature(monkeypatch):
    """A wrong signature → Forbidden, ingestion never runs."""
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", _WEBHOOK_SECRET)

    body = json.dumps({"event": "transcript.done", "data": {"bot": {"id": "bot-1"}}}).encode()
    headers = _svix_headers(_WEBHOOK_SECRET, body)
    headers["svix-signature"] = "v1,deadbeefnotavalidsignature"
    request = _make_request(body, headers)

    with pytest.raises(Forbidden) as exc_info:
        await webhooks.recall_webhook(request)
    assert exc_info.value.code == "meeting.webhook_signature_invalid"


async def test_webhook_rejects_stale_timestamp(monkeypatch):
    """A signature older than the tolerance window is rejected."""
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", _WEBHOOK_SECRET)

    body = json.dumps({"event": "transcript.done", "data": {"bot": {"id": "bot-1"}}}).encode()
    stale = int(time.time()) - 3600
    request = _make_request(body, _svix_headers(_WEBHOOK_SECRET, body, ts=stale))

    with pytest.raises(Forbidden):
        await webhooks.recall_webhook(request)


async def test_webhook_skips_verification_without_secret(monkeypatch):
    """No RECALL_WEBHOOK_SECRET → accepted unsigned (dev convenience)."""
    from pocketpaw_ee.cloud.meetings.providers.recall import webhooks

    monkeypatch.delenv("RECALL_WEBHOOK_SECRET", raising=False)

    async def _fake_ingest(bot_id):
        return False

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.meetings.service.ingest_transcript_for_recall_bot", _fake_ingest
    )

    body = json.dumps({"event": "transcript.done", "data": {"bot": {"id": "bot-1"}}}).encode()
    request = _make_request(body, {})  # no svix headers at all

    result = await webhooks.recall_webhook(request)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# service.ingest_transcript_for_recall_bot
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mongo_db")
async def test_ingest_resolves_meeting_by_bot_id(monkeypatch):
    """The webhook entry point finds the meeting via the correlated bot id."""
    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-77",
        provider="zoom",
        provider_meeting_id="m1",
        title="x",
        join_url="https://zoom.us/j/m1",
        raw_provider_payload={"recall": {"bot_id": "bot-corr"}},
    )
    await meeting.insert()

    captured: dict = {}

    async def _fake_fetch(workspace_id, meeting_id):
        captured["workspace_id"] = workspace_id
        captured["meeting_id"] = meeting_id
        return object()  # truthy → transcript stored

    monkeypatch.setattr(ms, "fetch_and_store_transcript", _fake_fetch)

    stored = await ms.ingest_transcript_for_recall_bot("bot-corr")
    assert stored is True
    assert captured["workspace_id"] == "ws-77"
    assert captured["meeting_id"] == str(meeting.id)


@pytest.mark.usefixtures("mongo_db")
async def test_ingest_unknown_bot_returns_false():
    """An unknown bot id is a no-op, not an error."""
    from pocketpaw_ee.cloud.meetings import service as ms

    assert await ms.ingest_transcript_for_recall_bot("bot-does-not-exist") is False


# ---------------------------------------------------------------------------
# Bot status tracking
# ---------------------------------------------------------------------------


async def test_recall_client_get_bot_status_parses_latest(recall_env, monkeypatch):
    """get_bot_status returns the latest status_changes entry."""
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

    bot_payload = {
        "id": "bot-s",
        "status_changes": [
            {"code": "joining_call", "sub_code": None, "created_at": "2026-05-21T06:00:00Z"},
            {
                "code": "in_waiting_room",
                "sub_code": None,
                "created_at": "2026-05-21T06:00:05Z",
            },
        ],
    }
    _FakeClient.reset([_resp(200, bot_payload)])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeClient)

    status = await recall_client.get_bot_status("bot-s")
    assert status == {
        "status": "in_waiting_room",
        "sub_code": None,
        "updated_at": "2026-05-21T06:00:05Z",
    }


async def test_recall_client_get_bot_status_404_returns_none(recall_env, monkeypatch):
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client

    _FakeClient.reset([_resp(404, {})])
    monkeypatch.setattr(recall_client.httpx, "AsyncClient", _FakeClient)
    assert await recall_client.get_bot_status("bot-gone") is None


@pytest.mark.usefixtures("mongo_db")
async def test_update_bot_status_persists_to_meeting(monkeypatch):
    """The webhook entry point writes bot_status onto the matched meeting."""
    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-9",
        provider="google_meet",
        provider_meeting_id="m9",
        title="x",
        join_url="https://meet.google.com/m9",
        raw_provider_payload={"recall": {"bot_id": "bot-st"}},
    )
    await meeting.insert()

    matched = await ms.update_bot_status_for_recall_bot("bot-st", "in_waiting_room", None)
    assert matched is True
    refreshed = await _MD.get(meeting.id)
    assert refreshed.bot_status == "in_waiting_room"
    assert refreshed.bot_status_at is not None

    # Unknown bot is a no-op.
    assert await ms.update_bot_status_for_recall_bot("bot-nope", "done", None) is False


@pytest.mark.usefixtures("mongo_db")
async def test_get_bot_status_live_fetch_and_persist(monkeypatch):
    """get_bot_status live-checks Recall, returns + persists the status."""
    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.meetings.providers.recall import client as recall_client
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-9",
        provider="google_meet",
        provider_meeting_id="m9",
        title="x",
        join_url="https://meet.google.com/m9",
        raw_provider_payload={"recall": {"bot_id": "bot-live"}},
    )
    await meeting.insert()

    async def _fake_status(bot_id):
        assert bot_id == "bot-live"
        return {"status": "in_call_recording", "sub_code": None, "updated_at": None}

    monkeypatch.setattr(recall_client, "get_bot_status", _fake_status)

    result = await ms.get_bot_status("ws-9", str(meeting.id))
    assert result["has_bot"] is True
    assert result["status"] == "in_call_recording"
    assert "recording" in result["summary"]
    refreshed = await _MD.get(meeting.id)
    assert refreshed.bot_status == "in_call_recording"


@pytest.mark.usefixtures("mongo_db")
async def test_get_bot_status_no_bot_dispatched(monkeypatch):
    """A meeting with no dispatched bot reports has_bot False."""
    from pocketpaw_ee.cloud.meetings import service as ms
    from pocketpaw_ee.cloud.models.meeting import Meeting as _MD

    meeting = _MD(
        workspace="ws-9",
        provider="zoom",
        provider_meeting_id="m0",
        title="x",
        join_url="https://zoom.us/j/m0",
    )
    await meeting.insert()

    result = await ms.get_bot_status("ws-9", str(meeting.id))
    assert result["has_bot"] is False
    assert result["status"] is None
