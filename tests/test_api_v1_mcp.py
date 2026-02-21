# Tests for API v1 MCP router.
# Created: 2026-02-21

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.mcp import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestMCPStatus:
    """Tests for GET /mcp/status."""

    @patch("pocketpaw.mcp.manager.get_mcp_manager")
    def test_get_status(self, mock_get_mgr, client):
        mgr = MagicMock()
        mgr.get_server_status.return_value = {
            "test-server": {"connected": True, "tools": 3}
        }
        mock_get_mgr.return_value = mgr
        resp = client.get("/api/v1/mcp/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "test-server" in data


class TestMCPAdd:
    """Tests for POST /mcp/add."""

    @patch("pocketpaw.mcp.manager.get_mcp_manager")
    def test_add_server(self, mock_get_mgr, client):
        mgr = MagicMock()
        mgr.start_server = AsyncMock(return_value=True)
        mock_get_mgr.return_value = mgr
        resp = client.post(
            "/api/v1/mcp/add",
            json={"name": "test", "transport": "stdio", "command": "npx", "args": ["-y", "test"]},
        )
        assert resp.status_code == 200
        mgr.add_server_config.assert_called_once()

    def test_add_server_missing_name(self, client):
        resp = client.post(
            "/api/v1/mcp/add",
            json={"name": "", "transport": "stdio"},
        )
        assert resp.status_code == 400


class TestMCPRemove:
    """Tests for POST /mcp/remove."""

    @patch("pocketpaw.mcp.manager.get_mcp_manager")
    def test_remove_existing(self, mock_get_mgr, client):
        mgr = MagicMock()
        mgr.stop_server = AsyncMock()
        mgr.remove_server_config.return_value = True
        mock_get_mgr.return_value = mgr
        resp = client.post("/api/v1/mcp/remove", json={"name": "test"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @patch("pocketpaw.mcp.manager.get_mcp_manager")
    def test_remove_nonexistent(self, mock_get_mgr, client):
        mgr = MagicMock()
        mgr.stop_server = AsyncMock()
        mgr.remove_server_config.return_value = False
        mock_get_mgr.return_value = mgr
        resp = client.post("/api/v1/mcp/remove", json={"name": "nope"})
        assert resp.status_code == 200
        assert "error" in resp.json()


class TestMCPToggle:
    """Tests for POST /mcp/toggle."""

    @patch("pocketpaw.mcp.manager.get_mcp_manager")
    def test_toggle_not_found(self, mock_get_mgr, client):
        mgr = MagicMock()
        mgr.get_server_status.return_value = {}
        mock_get_mgr.return_value = mgr
        resp = client.post("/api/v1/mcp/toggle", json={"name": "nope"})
        assert resp.status_code == 200
        assert "error" in resp.json()


class TestMCPOAuthCallback:
    """Tests for GET /mcp/oauth/callback."""

    def test_callback_missing_params(self, client):
        resp = client.get("/api/v1/mcp/oauth/callback")
        assert resp.status_code == 400

    @patch("pocketpaw.mcp.manager.set_oauth_callback_result", return_value=True)
    def test_callback_success(self, _mock, client):
        resp = client.get("/api/v1/mcp/oauth/callback?code=abc&state=xyz")
        assert resp.status_code == 200
        assert "Authenticated" in resp.text

    @patch("pocketpaw.mcp.manager.set_oauth_callback_result", return_value=False)
    def test_callback_expired(self, _mock, client):
        resp = client.get("/api/v1/mcp/oauth/callback?code=abc&state=xyz")
        assert resp.status_code == 400
