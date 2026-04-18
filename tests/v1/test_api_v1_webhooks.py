# Tests for API v1 webhooks router.
# Created: 2026-02-21

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.webhooks import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestListWebhooks:
    """Tests for GET /webhooks."""

    @patch("pocketpaw.config.Settings.load")
    def test_list_webhooks(self, mock_load, client):
        mock_s = MagicMock()
        mock_s.web_port = 8888
        mock_s.webhook_configs = [
            {"name": "test-hook", "secret": "abcdef12345678", "description": "Test webhook"}
        ]
        mock_s.webhook_sync_timeout = 30
        mock_load.return_value = mock_s

        resp = client.get("/api/v1/webhooks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["webhooks"]) == 1
        hook = data["webhooks"][0]
        assert hook["name"] == "test-hook"
        # Secret must be redacted
        assert "abcdef" not in hook["secret"]
        assert hook["secret"].startswith("***")

    @patch("pocketpaw.config.Settings.load")
    def test_list_empty(self, mock_load, client):
        mock_s = MagicMock()
        mock_s.web_port = 8888
        mock_s.webhook_configs = []
        mock_load.return_value = mock_s

        resp = client.get("/api/v1/webhooks")
        assert resp.status_code == 200
        assert resp.json()["webhooks"] == []


class TestAddWebhook:
    """Tests for POST /webhooks/add."""

    @patch("pocketpaw.config.Settings.load")
    def test_add_webhook(self, mock_load, client):
        mock_s = MagicMock()
        mock_s.webhook_configs = []
        mock_s.webhook_sync_timeout = 30
        mock_load.return_value = mock_s

        resp = client.post(
            "/api/v1/webhooks/add",
            json={"name": "my-hook", "description": "Test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["webhook"]["name"] == "my-hook"
        assert len(data["webhook"]["secret"]) > 10
        mock_s.save.assert_called_once()

    def test_add_missing_name(self, client):
        resp = client.post("/api/v1/webhooks/add", json={"name": ""})
        assert resp.status_code == 400

    def test_add_invalid_name(self, client):
        resp = client.post("/api/v1/webhooks/add", json={"name": "bad name!"})
        assert resp.status_code == 400

    @patch("pocketpaw.config.Settings.load")
    def test_add_duplicate(self, mock_load, client):
        mock_s = MagicMock()
        mock_s.webhook_configs = [{"name": "existing"}]
        mock_load.return_value = mock_s

        resp = client.post(
            "/api/v1/webhooks/add",
            json={"name": "existing"},
        )
        assert resp.status_code == 409


class TestRemoveWebhook:
    """Tests for POST /webhooks/remove."""

    @patch("pocketpaw.config.Settings.load")
    def test_remove_existing(self, mock_load, client):
        mock_s = MagicMock()
        mock_s.webhook_configs = [{"name": "delete-me"}]
        mock_load.return_value = mock_s

        resp = client.post("/api/v1/webhooks/remove", json={"name": "delete-me"})
        assert resp.status_code == 200
        mock_s.save.assert_called_once()

    @patch("pocketpaw.config.Settings.load")
    def test_remove_nonexistent(self, mock_load, client):
        mock_s = MagicMock()
        mock_s.webhook_configs = []
        mock_load.return_value = mock_s

        resp = client.post("/api/v1/webhooks/remove", json={"name": "nope"})
        assert resp.status_code == 404


class TestRegenerateSecret:
    """Tests for POST /webhooks/regenerate-secret."""

    @patch("pocketpaw.config.Settings.load")
    def test_regenerate(self, mock_load, client):
        mock_s = MagicMock()
        old_secret = "old-secret-value"
        mock_s.webhook_configs = [{"name": "my-hook", "secret": old_secret}]
        mock_load.return_value = mock_s

        resp = client.post(
            "/api/v1/webhooks/regenerate-secret",
            json={"name": "my-hook"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["secret"] != old_secret

    @patch("pocketpaw.config.Settings.load")
    def test_regenerate_not_found(self, mock_load, client):
        mock_s = MagicMock()
        mock_s.webhook_configs = []
        mock_load.return_value = mock_s

        resp = client.post(
            "/api/v1/webhooks/regenerate-secret",
            json={"name": "nope"},
        )
        assert resp.status_code == 404
