# Tests for ee/cloud/meetings/oauth_flow.py — Google Meet 3-leg flow.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def meetings_app(monkeypatch, mongo_db, tmp_path):  # noqa: ARG001
    """Mount /api/v1/meetings with auth + RBAC stubbed."""
    from ee.cloud._core.http import add_error_handler
    from ee.cloud.auth import current_active_user
    from ee.cloud.license import require_license
    from ee.cloud.meetings.router import router as meetings_router

    monkeypatch.setattr("pocketpaw.clients.token_store._get_oauth_dir", lambda: tmp_path)
    from pocketpaw.ee.guards import deps as guards_deps

    monkeypatch.setattr(guards_deps, "check_workspace_action", lambda *a, **k: None)

    fake_user = SimpleNamespace(
        id="user-1",
        active_workspace="ws-alpha",
        workspaces=[SimpleNamespace(workspace="ws-alpha", role="owner")],
    )

    async def fake_current_active_user():
        return fake_user

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = fake_current_active_user
    app.include_router(meetings_router, prefix="/api/v1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test.local") as client:
        yield client


async def test_auth_url_requires_client_id_pasted_first(meetings_app: AsyncClient) -> None:
    """GET auth-url before pasting client creds returns 404."""
    resp = await meetings_app.get("/api/v1/meetings/credentials/google_meet/auth-url")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "meeting_credentials.not_found"


async def test_full_oauth_flow(meetings_app: AsyncClient, monkeypatch) -> None:
    """Paste creds → fetch auth-url → callback → row enabled."""

    # 1. Paste client_id/client_secret.
    init = await meetings_app.post(
        "/api/v1/meetings/credentials/google_meet",
        json={"client_id": "google-cid", "client_secret": "google-csec"},
    )
    assert init.status_code == 200
    assert init.json()["enabled"] is False  # awaiting consent

    # 2. Fetch the auth URL — should contain client_id + scopes.
    auth_resp = await meetings_app.get("/api/v1/meetings/credentials/google_meet/auth-url")
    assert auth_resp.status_code == 200, auth_resp.text
    body = auth_resp.json()
    assert "client_id=google-cid" in body["auth_url"]
    assert "state=" in body["auth_url"]
    assert body["redirect_uri"].endswith("/api/v1/meetings/credentials/google_meet/callback")

    # Extract the state we just minted (the persisted nonce path).
    import urllib.parse

    parsed = urllib.parse.urlparse(body["auth_url"])
    state = urllib.parse.parse_qs(parsed.query)["state"][0]

    # 3. Mock the Google token exchange call.
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, data=None):  # noqa: ARG002
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "access_token": "ya29.abc",
                    "refresh_token": "1//def",
                    "expires_in": 3599,
                    "token_type": "Bearer",
                }
            )
            return resp

    monkeypatch.setattr("pocketpaw.clients.oauth.httpx.AsyncClient", _FakeClient)

    callback = await meetings_app.post(
        "/api/v1/meetings/credentials/google_meet/callback",
        json={"code": "auth-code-xyz", "state": state},
    )
    assert callback.status_code == 200, callback.text
    body = callback.json()
    assert body["provider"] == "google_meet"
    assert body["enabled"] is True
    assert body["last_validated_at"] is not None


async def test_callback_rejects_replayed_nonce(meetings_app: AsyncClient, monkeypatch) -> None:
    """A callback with a tampered/old state nonce is rejected."""
    await meetings_app.post(
        "/api/v1/meetings/credentials/google_meet",
        json={"client_id": "cid", "client_secret": "csec"},
    )
    # Build a state by hand with a wrong nonce.
    import base64
    import json

    bad_state = (
        base64.urlsafe_b64encode(
            json.dumps({"workspace_id": "ws-alpha", "nonce": "stale"}).encode()
        )
        .decode()
        .rstrip("=")
    )

    resp = await meetings_app.post(
        "/api/v1/meetings/credentials/google_meet/callback",
        json={"code": "x", "state": bad_state},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "meeting.oauth_nonce_mismatch"
