# Tests for API v1 remote tunnel router.
# Created: 2026-02-20

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.remote import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestTunnelStatus:
    """Tests for GET /api/v1/remote/status."""

    @patch("pocketpaw.tunnel.get_tunnel_manager")
    def test_status_inactive(self, mock_get, client):
        mgr = MagicMock()
        mgr.get_status.return_value = {"active": False, "url": None}
        mock_get.return_value = mgr

        resp = client.get("/api/v1/remote/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert data["url"] is None

    @patch("pocketpaw.tunnel.get_tunnel_manager")
    def test_status_active(self, mock_get, client):
        mgr = MagicMock()
        mgr.get_status.return_value = {"active": True, "url": "https://abc.trycloudflare.com"}
        mock_get.return_value = mgr

        resp = client.get("/api/v1/remote/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert "trycloudflare" in data["url"]


class TestTunnelStart:
    """Tests for POST /api/v1/remote/start."""

    @patch("pocketpaw.tunnel.get_tunnel_manager")
    def test_start_success(self, mock_get, client):
        mgr = MagicMock()
        mgr.start = AsyncMock(return_value="https://xyz.trycloudflare.com")
        mock_get.return_value = mgr

        resp = client.post("/api/v1/remote/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["url"] == "https://xyz.trycloudflare.com"

    @patch("pocketpaw.tunnel.get_tunnel_manager")
    def test_start_failure(self, mock_get, client):
        mgr = MagicMock()
        mgr.start = AsyncMock(side_effect=RuntimeError("cloudflared not found"))
        mock_get.return_value = mgr

        resp = client.post("/api/v1/remote/start")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False
        assert "cloudflared" in data["error"]


class TestTunnelStop:
    """Tests for POST /api/v1/remote/stop."""

    @patch("pocketpaw.tunnel.get_tunnel_manager")
    def test_stop(self, mock_get, client):
        mgr = MagicMock()
        mgr.stop = AsyncMock()
        mock_get.return_value = mgr

        resp = client.post("/api/v1/remote/stop")
        assert resp.status_code == 200
        assert resp.json()["active"] is False
        mgr.stop.assert_called_once()
