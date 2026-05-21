# Tests for src/pocketpaw/clients/google_meet.py
# Verifies REST contract + Google error envelope mapping.

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from pocketpaw_ee.cloud.meetings.clients.google_meet import GoogleMeetAPIError, GoogleMeetClient

from pocketpaw.clients.token_store import OAuthTokens, TokenStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("pocketpaw.clients.token_store._get_oauth_dir", lambda: tmp_path)
    return TokenStore()


@pytest.fixture
def seeded_client(store):
    store.save(
        OAuthTokens(
            service="workspace-w1-google_meet",
            access_token="meet_tok",
            refresh_token="rrr",
            expires_at=time.time() + 3600,
            extra={"client_id": "cid", "client_secret": "csec"},
        )
    )
    return GoogleMeetClient(
        service_name="workspace-w1-google_meet",
        client_id="cid",
        client_secret="csec",
        token_store=store,
    )


def _mock_resp(status: int, json_body: dict | None = None):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_body or {})
    resp.content = b"{}" if json_body is not None else b""
    resp.text = ""
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


async def test_get_token_raises_when_unprovisioned(store):
    client = GoogleMeetClient("ws-missing", "cid", "csec", token_store=store)
    with pytest.raises(RuntimeError, match="Complete the Settings"):
        await client._get_token()


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
    with patch(
        "pocketpaw_ee.cloud.meetings.clients.google_meet.httpx.AsyncClient", _FakeAsyncClient
    ):
        data = await seeded_client.create_space(access_type="OPEN")
    assert data["meetingUri"].startswith("https://meet.google.com")
    call = _FakeAsyncClient.last_call
    assert call["url"].endswith("/spaces")
    assert call["json"]["config"]["accessType"] == "OPEN"


async def test_get_space_accepts_bare_id_or_resource_name(seeded_client):
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"name": "spaces/abc"}))
    with patch(
        "pocketpaw_ee.cloud.meetings.clients.google_meet.httpx.AsyncClient", _FakeAsyncClient
    ):
        await seeded_client.get_space("abc")
    assert _FakeAsyncClient.last_call["url"].endswith("/spaces/abc")

    _FakeAsyncClient.reset(_mock_resp(200, json_body={"name": "spaces/abc"}))
    with patch(
        "pocketpaw_ee.cloud.meetings.clients.google_meet.httpx.AsyncClient", _FakeAsyncClient
    ):
        await seeded_client.get_space("spaces/abc")
    assert _FakeAsyncClient.last_call["url"].endswith("/spaces/abc")


async def test_list_conference_records_passes_filter(seeded_client):
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"conferenceRecords": []}))
    with patch(
        "pocketpaw_ee.cloud.meetings.clients.google_meet.httpx.AsyncClient", _FakeAsyncClient
    ):
        await seeded_client.list_conference_records(filter_='space.name="spaces/abc"', page_size=10)
    params = _FakeAsyncClient.last_call["params"]
    assert params["filter"] == 'space.name="spaces/abc"'
    assert params["pageSize"] == 10


async def test_list_transcripts_path(seeded_client):
    _FakeAsyncClient.reset(_mock_resp(200, json_body={"transcripts": []}))
    with patch(
        "pocketpaw_ee.cloud.meetings.clients.google_meet.httpx.AsyncClient", _FakeAsyncClient
    ):
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
    with patch(
        "pocketpaw_ee.cloud.meetings.clients.google_meet.httpx.AsyncClient", _FakeAsyncClient
    ):
        with pytest.raises(GoogleMeetAPIError) as exc_info:
            await seeded_client.get_space("abc")
    assert exc_info.value.status_code == 403
    assert "Permission denied" in str(exc_info.value)


async def test_end_active_conference_uses_colon_endpoint(seeded_client):
    """``:endActiveConference`` is a Google-specific operation suffix, not a path segment."""
    _FakeAsyncClient.reset(_mock_resp(200, json_body={}))
    with patch(
        "pocketpaw_ee.cloud.meetings.clients.google_meet.httpx.AsyncClient", _FakeAsyncClient
    ):
        await seeded_client.end_active_conference("spaces/abc")
    assert _FakeAsyncClient.last_call["url"].endswith(":endActiveConference")
