# Tests for API v1 settings router.
# Created: 2026-02-20

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.settings import _IMMUTABLE_FIELDS, router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestGetSettings:
    """Tests for GET /api/v1/settings."""

    @patch("pocketpaw.config.Settings.load")
    def test_get_settings_returns_dict(self, mock_load, client):
        settings = MagicMock()
        settings.model_fields = {"agent_backend": None, "web_port": None}
        settings.agent_backend = "claude_agent_sdk"
        settings.web_port = 8888
        mock_load.return_value = settings
        resp = client.get("/api/v1/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_backend"] == "claude_agent_sdk"
        assert data["web_port"] == 8888


class TestUpdateSettings:
    """Tests for PUT /api/v1/settings."""

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.config.Settings.load")
    def test_update_settings(self, mock_load, mock_get_settings, client):
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_load.return_value = settings
        resp = client.put(
            "/api/v1/settings",
            json={"settings": {"agent_backend": "openai_agents"}},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        # Verify setattr was called
        assert settings.agent_backend == "openai_agents"
        settings.save.assert_called_once()

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.config.Settings.load")
    def test_update_ignores_private_fields(self, mock_load, mock_get_settings, client):
        settings = MagicMock()
        # hasattr returns True for MagicMock, but the router checks startswith("_")
        mock_load.return_value = settings
        resp = client.put(
            "/api/v1/settings",
            json={"settings": {"_internal": "secret", "agent_backend": "openai_agents"}},
        )
        assert resp.status_code == 200
        # agent_backend should be set, _internal should not
        assert settings.agent_backend == "openai_agents"

    @pytest.mark.parametrize("field", sorted(_IMMUTABLE_FIELDS))
    def test_rejects_immutable_field(self, field, client):
        """PUT /settings must return 403 for every security-critical field."""
        resp = client.put(
            "/api/v1/settings",
            json={"settings": {field: "evil"}},
        )
        assert resp.status_code == 403
        assert field in resp.json()["detail"]

    def test_rejects_multiple_immutable_fields(self, client):
        """All blocked field names appear in the error when several are sent at once."""
        payload = {"file_jail_path": "/", "bypass_permissions": True}
        resp = client.put("/api/v1/settings", json={"settings": payload})
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert "bypass_permissions" in detail
        assert "file_jail_path" in detail

    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.config.Settings.load")
    def test_safe_field_still_accepted(self, mock_load, mock_get_settings, client):
        """Non-blocked fields are still written normally."""
        settings = MagicMock()
        settings.agent_backend = "claude_agent_sdk"
        mock_load.return_value = settings
        resp = client.put(
            "/api/v1/settings",
            json={"settings": {"agent_backend": "openai_agents"}},
        )
        assert resp.status_code == 200
        assert settings.agent_backend == "openai_agents"
