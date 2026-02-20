# Tests for backward compatibility — existing /api/ paths still work.
# Created: 2026-02-20
#
# Verifies that the original /api/ endpoints in dashboard.py still respond
# correctly alongside the new /api/v1/ versioned endpoints.

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create a TestClient from the actual dashboard app."""
    from pocketpaw.dashboard import app

    return TestClient(app)


@patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
class TestBackwardCompatEndpoints:
    """Verify /api/ backward-compat endpoints still respond."""

    def test_health_endpoint(self, _mock, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_health_errors(self, _mock, client):
        resp = client.get("/api/health/errors")
        assert resp.status_code == 200

    @patch("pocketpaw.config.Settings.load")
    def test_telegram_status(self, mock_load, _mock, client):
        settings = MagicMock()
        settings.telegram_bot_token = ""
        settings.allowed_user_id = None
        mock_load.return_value = settings

        resp = client.get("/api/telegram/status")
        assert resp.status_code == 200

    def test_remote_status(self, _mock, client):
        resp = client.get("/api/remote/status")
        assert resp.status_code == 200

    def test_backends_list(self, _mock, client):
        resp = client.get("/api/backends")
        assert resp.status_code == 200

    def test_sessions_list(self, _mock, client):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200

    def test_channels_status(self, _mock, client):
        resp = client.get("/api/channels/status")
        assert resp.status_code == 200

    def test_skills_list(self, _mock, client):
        resp = client.get("/api/skills")
        assert resp.status_code == 200

    def test_webhooks_list(self, _mock, client):
        resp = client.get("/api/webhooks")
        assert resp.status_code == 200

    def test_memory_long_term(self, _mock, client):
        resp = client.get("/api/memory/long_term")
        assert resp.status_code == 200

    def test_audit_log(self, _mock, client):
        resp = client.get("/api/audit")
        assert resp.status_code == 200

    def test_identity_get(self, _mock, client):
        resp = client.get("/api/identity")
        assert resp.status_code == 200


@patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
class TestV1Endpoints:
    """Verify /api/v1/ versioned endpoints respond."""

    def test_v1_health(self, _mock, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_v1_backends(self, _mock, client):
        resp = client.get("/api/v1/backends")
        assert resp.status_code == 200

    def test_v1_sessions(self, _mock, client):
        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200

    def test_v1_channels(self, _mock, client):
        resp = client.get("/api/v1/channels/status")
        assert resp.status_code == 200

    def test_v1_skills(self, _mock, client):
        resp = client.get("/api/v1/skills")
        assert resp.status_code == 200

    def test_v1_webhooks(self, _mock, client):
        resp = client.get("/api/v1/webhooks")
        assert resp.status_code == 200

    def test_v1_memory_long_term(self, _mock, client):
        resp = client.get("/api/v1/memory/long_term")
        assert resp.status_code == 200

    def test_v1_identity(self, _mock, client):
        resp = client.get("/api/v1/identity")
        assert resp.status_code == 200


@patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)
class TestOpenAPIDocs:
    """Verify OpenAPI documentation endpoints."""

    def test_openapi_json(self, _mock, client):
        resp = client.get("/api/v1/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "openapi" in data
        assert "paths" in data
        assert data["info"]["title"] == "PocketPaw API"

    def test_docs_page(self, _mock, client):
        resp = client.get("/api/v1/docs")
        assert resp.status_code == 200
        assert "swagger" in resp.text.lower()

    def test_redoc_page(self, _mock, client):
        resp = client.get("/api/v1/redoc")
        assert resp.status_code == 200
        assert "redoc" in resp.text.lower()
