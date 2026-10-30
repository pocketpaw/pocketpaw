# Tests for pocketpaw_ee/cloud/meetings/clients/zoom.py
# Verifies the REST contract: S2S token grant, auth headers, body shapes,
# error envelope mapping.

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from pocketpaw_ee.cloud.meetings.providers.recall.clients.zoom import ZoomAPIError, ZoomClient


@pytest.fixture
def seeded_client():
    """A ZoomClient with a pre-cached access token (skips the S2S grant)."""
    client = ZoomClient("acct-abc", "cid", "csec")
    client._token = "tok_xyz"
    client._token_expiry = time.monotonic() + 3600
    return client


def _mock_resp(status: int, json_body: dict | None = None, text_body: str = ""):
    """Build a MagicMock httpx response with the given status + body."""
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_body or {})
    resp.text = text_body or (str(json_body) if json_body else "")
    resp.content = b"" if (status == 204 or json_body is None) else b"{}"
    return resp


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in — records calls, returns a stubbed response."""

    last_call: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @classmethod
    def reset(cls, response):
        cls.last_call = {}
        cls._next_response = response

    async def request(self, method, url, headers=None, json=None, params=None):
        type(self).last_call = {
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
            "params": params,
        }
        return type(self)._next_response

    async def post(self, url, data=None, headers=None, json=None):
        type(self).last_call = {
            "method": "POST",
            "url": url,
            "data": data,
            "headers": headers,
        }
        return type(self)._next_response

    async def get(self, url, headers=None):
        type(self).last_call = {"method": "GET", "url": url, "headers": headers}
        return type(self)._next_response


_PATCH = "pocketpaw_ee.cloud.meetings.providers.recall.clients.zoom.httpx.AsyncClient"


# ---------------------------------------------------------------------------
# S2S token grant
# ---------------------------------------------------------------------------


async def test_get_token_does_s2s_grant():
    """_get_token mints an access token via the account_credentials grant."""
    client = ZoomClient("acct-abc", "cid", "csec")
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"access_token": "fresh", "expires_in": 3600}))
    with patch(_PATCH, _FakeAsyncClient):
        token = await client._get_token()
    assert token == "fresh"
    call = _FakeAsyncClient.last_call
    assert call["url"] == "https://zoom.us/oauth/token"
    assert call["data"] == {"grant_type": "account_credentials", "account_id": "acct-abc"}
    assert call["headers"]["Authorization"].startswith("Basic ")


async def test_get_token_propagates_failure():
    """A non-200 token response raises ZoomAPIError."""
    client = ZoomClient("acct-abc", "cid", "csec")
    bad = _mock_resp(401, json_body=None, text_body="invalid_client")
    _FakeAsyncClient.reset(bad)
    with patch(_PATCH, _FakeAsyncClient):
        with pytest.raises(ZoomAPIError):
            await client._get_token()


# ---------------------------------------------------------------------------
# Auth + error mapping
# ---------------------------------------------------------------------------


async def test_request_bearer_auth_header(seeded_client):
    """Every request carries the cached access token in the Authorization header."""
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"id": "m1"}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.get_meeting("m1")
    assert _FakeAsyncClient.last_call["headers"] == {"Authorization": "Bearer tok_xyz"}


async def test_request_maps_zoom_error_envelope(seeded_client):
    """Non-2xx with Zoom's ``{"code","message"}`` envelope becomes ZoomAPIError."""
    _FakeAsyncClient.reset(
        _mock_resp(404, json_body={"code": 3001, "message": "Meeting does not exist"})
    )
    with patch(_PATCH, _FakeAsyncClient):
        with pytest.raises(ZoomAPIError) as exc_info:
            await seeded_client.get_meeting("not-a-meeting")
    assert exc_info.value.status_code == 404
    assert "does not exist" in str(exc_info.value)
    assert exc_info.value.body["code"] == 3001


# ---------------------------------------------------------------------------
# create_meeting
# ---------------------------------------------------------------------------


async def test_create_instant_meeting(seeded_client):
    """No start_time → type=1 (instant); no start_time field on the body."""
    _FakeAsyncClient.reset(
        _mock_resp(201, json_body={"id": 1234567890, "join_url": "https://zoom.us/j/1234567890"})
    )
    with patch(_PATCH, _FakeAsyncClient):
        data = await seeded_client.create_meeting(topic="Standup", duration_minutes=15)

    assert data["id"] == 1234567890
    call = _FakeAsyncClient.last_call
    assert call["method"] == "POST"
    assert call["url"].endswith("/users/me/meetings")
    assert call["json"] == {"topic": "Standup", "type": 1, "duration": 15, "agenda": ""}


async def test_create_scheduled_meeting_with_rfc3339_start(seeded_client):
    """start_time present → type=2 + RFC 3339 start_time with trailing Z."""
    from datetime import UTC, datetime

    _FakeAsyncClient.reset(_mock_resp(201, json_body={"id": 1}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.create_meeting(
            topic="Planning",
            start_time=datetime(2026, 6, 1, 14, 30, 0, tzinfo=UTC),
            duration_minutes=60,
        )

    body = _FakeAsyncClient.last_call["json"]
    assert body["type"] == 2
    assert body["start_time"] == "2026-06-01T14:30:00Z"
    assert body["duration"] == 60


# ---------------------------------------------------------------------------
# list_meetings + get_meeting + cancel
# ---------------------------------------------------------------------------


async def test_list_meetings_query_params(seeded_client):
    """type/page_size/page_number flow through to query params."""
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"meetings": []}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.list_meetings(meeting_type="upcoming", page_size=50)
    params = _FakeAsyncClient.last_call["params"]
    assert params["type"] == "upcoming"
    assert params["page_size"] == 50
    assert params["page_number"] == 1


async def test_list_meetings_clamps_page_size(seeded_client):
    """page_size > 300 clamps to 300 (Zoom's documented max)."""
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"meetings": []}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.list_meetings(page_size=9999)
    assert _FakeAsyncClient.last_call["params"]["page_size"] == 300


async def test_cancel_meeting_returns_204_no_content(seeded_client):
    """204 returns gracefully with no body parsing."""
    _FakeAsyncClient.reset(_mock_resp(204))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.cancel_meeting("m1")  # should not raise
    assert _FakeAsyncClient.last_call["method"] == "DELETE"


# ---------------------------------------------------------------------------
# Recordings + transcript download
# ---------------------------------------------------------------------------


async def test_list_recordings(seeded_client):
    _FakeAsyncClient.reset(
        _mock_resp(
            200,
            json_body={
                "recording_files": [
                    {"id": "r1", "file_type": "MP4", "download_url": "https://x/mp4"},
                    {"id": "r2", "file_type": "TRANSCRIPT", "download_url": "https://x/vtt"},
                ]
            },
        )
    )
    with patch(_PATCH, _FakeAsyncClient):
        data = await seeded_client.list_recordings("m1")
    assert len(data["recording_files"]) == 2


async def test_download_transcript_carries_bearer_token(seeded_client):
    """Transcript downloads MUST send the bearer header (Zoom requirement)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "WEBVTT\n\n00:00.000 --> 00:01.000\nHello"

    _FakeAsyncClient.reset(resp)
    with patch(_PATCH, _FakeAsyncClient):
        text = await seeded_client.download_transcript("https://files.zoom.us/transcript.vtt")
    assert text.startswith("WEBVTT")
    assert _FakeAsyncClient.last_call["headers"] == {"Authorization": "Bearer tok_xyz"}
