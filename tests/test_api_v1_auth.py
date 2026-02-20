# Tests for API v1 auth router.
# Created: 2026-02-20

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.auth import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


MASTER_TOKEN = "test-master-token-123"


class TestSessionExchange:
    """Tests for POST /api/v1/auth/session."""

    @patch("pocketpaw.config.get_access_token", return_value=MASTER_TOKEN)
    @patch("pocketpaw.config.Settings.load")
    @patch("pocketpaw.security.session_tokens.create_session_token", return_value="sess:abc123")
    def test_exchange_valid_token(self, mock_create, mock_load, mock_get, client):
        mock_load.return_value = MagicMock(session_token_ttl_hours=24)
        resp = client.post(
            "/api/v1/auth/session",
            headers={"Authorization": f"Bearer {MASTER_TOKEN}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_token"] == "sess:abc123"
        assert data["expires_in_hours"] == 24

    @patch("pocketpaw.config.get_access_token", return_value=MASTER_TOKEN)
    def test_exchange_invalid_token(self, mock_get, client):
        resp = client.post(
            "/api/v1/auth/session",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    @patch("pocketpaw.config.get_access_token", return_value=MASTER_TOKEN)
    def test_exchange_no_header(self, mock_get, client):
        resp = client.post("/api/v1/auth/session")
        assert resp.status_code == 401


class TestCookieLogin:
    """Tests for POST /api/v1/auth/login."""

    @patch("pocketpaw.config.get_access_token", return_value=MASTER_TOKEN)
    @patch("pocketpaw.config.Settings.load")
    @patch("pocketpaw.security.session_tokens.create_session_token", return_value="sess:xyz")
    def test_login_sets_cookie(self, mock_create, mock_load, mock_get, client):
        mock_load.return_value = MagicMock(session_token_ttl_hours=24)
        resp = client.post("/api/v1/auth/login", json={"token": MASTER_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "pocketpaw_session" in resp.cookies

    @patch("pocketpaw.config.get_access_token", return_value=MASTER_TOKEN)
    def test_login_wrong_token(self, mock_get, client):
        resp = client.post("/api/v1/auth/login", json={"token": "wrong"})
        assert resp.status_code == 401

    def test_login_invalid_json(self, client):
        resp = client.post(
            "/api/v1/auth/login",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


class TestLogout:
    """Tests for POST /api/v1/auth/logout."""

    def test_logout_clears_cookie(self, client):
        resp = client.post("/api/v1/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestTokenRegenerate:
    """Tests for POST /api/v1/token/regenerate."""

    @patch("pocketpaw.config.regenerate_token", return_value="new-token-456")
    def test_regenerate_returns_new_token(self, mock_regen, client):
        resp = client.post("/api/v1/token/regenerate")
        assert resp.status_code == 200
        assert resp.json()["token"] == "new-token-456"
        mock_regen.assert_called_once()


class TestQRCode:
    """Tests for GET /api/v1/qr."""

    @patch("pocketpaw.config.get_access_token", return_value=MASTER_TOKEN)
    @patch("pocketpaw.security.session_tokens.create_session_token", return_value="qr-token")
    @patch("pocketpaw.tunnel.get_tunnel_manager")
    def test_qr_returns_png(self, mock_tunnel, mock_create, mock_get, client):
        mock_tunnel.return_value.get_status.return_value = {"active": False}
        resp = client.get("/api/v1/qr")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert len(resp.content) > 100

    @patch("pocketpaw.config.get_access_token", return_value=MASTER_TOKEN)
    @patch("pocketpaw.security.session_tokens.create_session_token", return_value="qr-token")
    @patch("pocketpaw.tunnel.get_tunnel_manager")
    def test_qr_with_active_tunnel(self, mock_tunnel, mock_create, mock_get, client):
        mock_tunnel.return_value.get_status.return_value = {
            "active": True,
            "url": "https://test.trycloudflare.com",
        }
        resp = client.get("/api/v1/qr")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
