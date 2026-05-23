# Tests for pocketpaw_ee/cloud/meetings/clients/google_meet.py
# Verifies the REST contract: refresh-token grant + Google error mapping.

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from pocketpaw_ee.cloud.meetings.providers.recall.clients.google_meet import (
    GoogleMeetAPIError,
    GoogleMeetClient,
)


@pytest.fixture
def seeded_client():
    """A GoogleMeetClient with a pre-cached access token (skips the refresh grant)."""
    client = GoogleMeetClient("cid", "csec", "rrr")
    client._token = "meet_tok"
    client._token_expiry = time.monotonic() + 3600
    return client


def _mock_resp(status: int, json_body: dict | None = None, text_body: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_body or {})
    resp.content = b"{}" if json_body is not None else b""
    resp.text = text_body
    return resp


class _FakeAsyncClient:
    last_call: dict = {}
    _next_response = None

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
        type(self).last_call = {"method": "POST", "url": url, "data": data}
        return type(self)._next_response


_PATCH = "pocketpaw_ee.cloud.meetings.providers.recall.clients.google_meet.httpx.AsyncClient"


# ---------------------------------------------------------------------------
# Refresh-token grant
# ---------------------------------------------------------------------------


async def test_get_token_does_refresh_grant():
    """_get_token exchanges the refresh token for an access token."""
    client = GoogleMeetClient("cid", "csec", "rrr")
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"access_token": "fresh", "expires_in": 3600}))
    with patch(_PATCH, _FakeAsyncClient):
        token = await client._get_token()
    assert token == "fresh"
    call = _FakeAsyncClient.last_call
    assert call["url"] == "https://oauth2.googleapis.com/token"
    assert call["data"]["grant_type"] == "refresh_token"
    assert call["data"]["refresh_token"] == "rrr"


async def test_get_token_propagates_failure():
    """A non-200 token response raises GoogleMeetAPIError."""
    client = GoogleMeetClient("cid", "csec", "bad")
    _FakeAsyncClient.reset(_mock_resp(400, json_body=None, text_body="invalid_grant"))
    with patch(_PATCH, _FakeAsyncClient):
        with pytest.raises(GoogleMeetAPIError):
            await client._get_token()


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


async def test_create_space_payload(seeded_client):
    """create_space sends config block with access_type, returns the space record."""
    _FakeAsyncClient.reset(
        _mock_resp(
            200,
            json_body={
                "name": "spaces/abc",
                "meetingUri": "https://meet.google.com/xyz-pdq",
                "meetingCode": "xyz-pdq",
            },
        )
    )
    with patch(_PATCH, _FakeAsyncClient):
        data = await seeded_client.create_space(access_type="OPEN")
    assert data["meetingUri"].startswith("https://meet.google.com")
    call = _FakeAsyncClient.last_call
    assert call["url"].endswith("/spaces")
    assert call["json"]["config"]["accessType"] == "OPEN"


async def test_get_space_accepts_bare_id_or_resource_name(seeded_client):
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"name": "spaces/abc"}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.get_space("abc")
    assert _FakeAsyncClient.last_call["url"].endswith("/spaces/abc")

    _FakeAsyncClient.reset(_mock_resp(200, json_body={"name": "spaces/abc"}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.get_space("spaces/abc")
    assert _FakeAsyncClient.last_call["url"].endswith("/spaces/abc")


async def test_list_conference_records_passes_filter(seeded_client):
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"conferenceRecords": []}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.list_conference_records(filter_='space.name="spaces/abc"', page_size=10)
    params = _FakeAsyncClient.last_call["params"]
    assert params["filter"] == 'space.name="spaces/abc"'
    assert params["pageSize"] == 10


async def test_list_transcripts_path(seeded_client):
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"transcripts": []}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.list_transcripts("conferenceRecords/abc")
    assert _FakeAsyncClient.last_call["url"].endswith("/conferenceRecords/abc/transcripts")


async def test_maps_google_error_envelope(seeded_client):
    _FakeAsyncClient.reset(
        _mock_resp(
            403,
            json_body={
                "error": {
                    "code": 403,
                    "message": "Permission denied",
                    "status": "PERMISSION_DENIED",
                }
            },
        )
    )
    with patch(_PATCH, _FakeAsyncClient):
        with pytest.raises(GoogleMeetAPIError) as exc_info:
            await seeded_client.get_space("abc")
    assert exc_info.value.status_code == 403
    assert "Permission denied" in str(exc_info.value)


async def test_end_active_conference_uses_colon_endpoint(seeded_client):
    """``:endActiveConference`` is a Google-specific operation suffix, not a path segment."""
    _FakeAsyncClient.reset(_mock_resp(200, json_body={}))
    with patch(_PATCH, _FakeAsyncClient):
        await seeded_client.end_active_conference("spaces/abc")
    assert _FakeAsyncClient.last_call["url"].endswith(":endActiveConference")
