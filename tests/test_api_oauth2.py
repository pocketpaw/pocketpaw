# Tests for OAuth2/PKCE authorization server.
# Created: 2026-02-20

import base64
import hashlib
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.oauth2.models import OAuthClient
from pocketpaw.api.oauth2.server import AuthorizationServer, reset_oauth_server
from pocketpaw.api.oauth2.storage import DEFAULT_DESKTOP_CLIENT, OAuthStorage
from pocketpaw.api.v1.oauth2 import router


def _make_pkce_pair():
    """Generate a PKCE code_verifier and code_challenge pair."""
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


@pytest.fixture
def storage():
    return OAuthStorage()


@pytest.fixture
def server(storage):
    return AuthorizationServer(storage)


@pytest.fixture
def test_app(server, monkeypatch):
    import pocketpaw.api.oauth2.server as mod

    monkeypatch.setattr(mod, "_server", server)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


# ===================== AuthorizationServer unit tests =====================


class TestAuthorizationServer:
    """Tests for the OAuth2 authorization server logic."""

    def test_authorize_creates_code(self, server):
        verifier, challenge = _make_pkce_pair()
        code, error = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat sessions",
            code_challenge=challenge,
        )
        assert error is None
        assert code is not None

    def test_authorize_invalid_client(self, server):
        _, challenge = _make_pkce_pair()
        code, error = server.authorize(
            client_id="unknown",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        assert error == "invalid_client"
        assert code is None

    def test_authorize_invalid_redirect(self, server):
        _, challenge = _make_pkce_pair()
        code, error = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="https://evil.com/callback",
            scope="chat",
            code_challenge=challenge,
        )
        assert error == "invalid_redirect_uri"

    def test_authorize_invalid_scope(self, server):
        _, challenge = _make_pkce_pair()
        code, error = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="admin",
            code_challenge=challenge,
        )
        assert error == "invalid_scope"

    def test_exchange_with_valid_verifier(self, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat sessions",
            code_challenge=challenge,
        )
        result, error = server.exchange(
            code=code,
            client_id="pocketpaw-desktop",
            code_verifier=verifier,
        )
        assert error is None
        assert result is not None
        assert result["access_token"].startswith("ppat_")
        assert result["refresh_token"].startswith("pprt_")
        assert result["token_type"] == "Bearer"
        assert result["scope"] == "chat sessions"

    def test_exchange_invalid_verifier(self, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        result, error = server.exchange(
            code=code,
            client_id="pocketpaw-desktop",
            code_verifier="wrong-verifier",
        )
        assert error == "invalid_code_verifier"

    def test_exchange_invalid_code(self, server):
        result, error = server.exchange(
            code="nonexistent",
            client_id="pocketpaw-desktop",
            code_verifier="whatever",
        )
        assert error == "invalid_code"

    def test_exchange_code_reuse(self, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        server.exchange(code=code, client_id="pocketpaw-desktop", code_verifier=verifier)
        # Second use should fail
        result, error = server.exchange(
            code=code, client_id="pocketpaw-desktop", code_verifier=verifier
        )
        assert error == "code_already_used"

    def test_exchange_wrong_client(self, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        result, error = server.exchange(
            code=code, client_id="other-client", code_verifier=verifier
        )
        assert error == "client_mismatch"

    def test_refresh_token(self, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        tokens, _ = server.exchange(
            code=code, client_id="pocketpaw-desktop", code_verifier=verifier
        )
        new_tokens, error = server.refresh(tokens["refresh_token"])
        assert error is None
        assert new_tokens["access_token"] != tokens["access_token"]
        assert new_tokens["refresh_token"] != tokens["refresh_token"]

    def test_refresh_invalid_token(self, server):
        result, error = server.refresh("invalid-refresh")
        assert error == "invalid_refresh_token"

    def test_revoke_access_token(self, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        tokens, _ = server.exchange(
            code=code, client_id="pocketpaw-desktop", code_verifier=verifier
        )
        assert server.revoke(tokens["access_token"]) is True
        assert server.verify_access_token(tokens["access_token"]) is None

    def test_revoke_refresh_token(self, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        tokens, _ = server.exchange(
            code=code, client_id="pocketpaw-desktop", code_verifier=verifier
        )
        assert server.revoke(tokens["refresh_token"]) is True

    def test_verify_access_token(self, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        tokens, _ = server.exchange(
            code=code, client_id="pocketpaw-desktop", code_verifier=verifier
        )
        token = server.verify_access_token(tokens["access_token"])
        assert token is not None
        assert token.scope == "chat"


# ===================== API endpoint tests =====================


class TestOAuth2Endpoints:
    """Tests for OAuth2 REST endpoints."""

    def test_authorize_shows_consent(self, client):
        _, challenge = _make_pkce_pair()
        resp = client.get(
            "/api/v1/oauth/authorize",
            params={
                "client_id": "pocketpaw-desktop",
                "redirect_uri": "tauri://oauth-callback",
                "scope": "chat sessions",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert resp.status_code == 200
        assert "PocketPaw Desktop" in resp.text
        assert "chat" in resp.text

    def test_authorize_invalid_client(self, client):
        _, challenge = _make_pkce_pair()
        resp = client.get(
            "/api/v1/oauth/authorize",
            params={
                "client_id": "unknown",
                "redirect_uri": "tauri://oauth-callback",
                "scope": "chat",
                "code_challenge": challenge,
            },
        )
        assert resp.status_code == 400

    def test_token_exchange_full_flow(self, client, server):
        verifier, challenge = _make_pkce_pair()
        # Create code directly via server for simplicity
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        resp = client.post(
            "/api/v1/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": "pocketpaw-desktop",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"].startswith("ppat_")
        assert data["token_type"] == "Bearer"

    def test_token_refresh(self, client, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        tokens, _ = server.exchange(
            code=code, client_id="pocketpaw-desktop", code_verifier=verifier
        )
        resp = client.post(
            "/api/v1/oauth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["access_token"] != tokens["access_token"]

    def test_token_exchange_invalid_code(self, client):
        resp = client.post(
            "/api/v1/oauth/token",
            json={
                "grant_type": "authorization_code",
                "code": "invalid",
                "code_verifier": "whatever",
                "client_id": "pocketpaw-desktop",
            },
        )
        assert resp.status_code == 400

    def test_revoke_endpoint(self, client, server):
        verifier, challenge = _make_pkce_pair()
        code, _ = server.authorize(
            client_id="pocketpaw-desktop",
            redirect_uri="tauri://oauth-callback",
            scope="chat",
            code_challenge=challenge,
        )
        tokens, _ = server.exchange(
            code=code, client_id="pocketpaw-desktop", code_verifier=verifier
        )
        resp = client.post(
            "/api/v1/oauth/revoke",
            json={"token": tokens["access_token"]},
        )
        assert resp.status_code == 200
        assert resp.json()["revoked"] is True

    def test_consent_deny(self, client):
        _, challenge = _make_pkce_pair()
        resp = client.post(
            "/api/v1/oauth/authorize/consent",
            data={
                "action": "deny",
                "client_id": "pocketpaw-desktop",
                "redirect_uri": "tauri://oauth-callback",
                "scope": "chat",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "mystate",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "error=access_denied" in resp.headers["location"]
        assert "state=mystate" in resp.headers["location"]

    def test_consent_allow(self, client):
        _, challenge = _make_pkce_pair()
        resp = client.post(
            "/api/v1/oauth/authorize/consent",
            data={
                "action": "allow",
                "client_id": "pocketpaw-desktop",
                "redirect_uri": "tauri://oauth-callback",
                "scope": "chat",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "mystate",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "code=" in resp.headers["location"]
        assert "state=mystate" in resp.headers["location"]
