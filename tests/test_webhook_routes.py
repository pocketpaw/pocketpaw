"""Tests for the generic inbound webhook dashboard routes.

Created: 2026-02-09
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from pocketclaw.dashboard import _channel_adapters, app

# ---------- fixtures ----------

_TEST_SLOT = {
    "name": "test-hook",
    "secret": "supersecret",
    "description": "Test webhook",
    "sync_timeout": 5,
}

_TEST_TOKEN = "test-dashboard-token-12345"


@pytest.fixture(autouse=True)
def _mock_settings():
    """Patch Settings.load() to return a Settings with one webhook config."""
    with patch("pocketclaw.dashboard.Settings") as MockSettings:
        mock_instance = MagicMock()
        mock_instance.webhook_configs = [_TEST_SLOT.copy()]
        mock_instance.webhook_sync_timeout = 30
        mock_instance.web_port = 8888
        mock_instance.save = MagicMock()
        MockSettings.load.return_value = mock_instance
        yield mock_instance


@pytest.fixture(autouse=True)
def _mock_token():
    """Mock get_access_token so dashboard auth middleware allows /api/* calls."""
    with patch("pocketclaw.dashboard.get_access_token", return_value=_TEST_TOKEN):
        yield


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _auth_headers(**extra):
    """Return headers with dashboard Bearer token + any extras."""
    h = {"Authorization": f"Bearer {_TEST_TOKEN}"}
    h.update(extra)
    return h


@pytest.fixture(autouse=True)
def _mock_adapter():
    """Ensure a mock webhook adapter is in _channel_adapters."""
    mock_adapter = MagicMock()
    mock_adapter.handle_webhook = AsyncMock(return_value=None)
    _channel_adapters["webhook"] = mock_adapter
    yield mock_adapter
    _channel_adapters.pop("webhook", None)


# ---------- auth tests ----------


class TestWebhookAuth:
    def test_valid_secret_header(self, client, _mock_adapter):
        resp = client.post(
            "/webhook/inbound/test-hook",
            json={"content": "hello"},
            headers={"X-Webhook-Secret": "supersecret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"

    def test_invalid_secret_header(self, client):
        resp = client.post(
            "/webhook/inbound/test-hook",
            json={"content": "hello"},
            headers={"X-Webhook-Secret": "wrongsecret"},
        )
        assert resp.status_code == 403

    def test_missing_auth(self, client):
        resp = client.post(
            "/webhook/inbound/test-hook",
            json={"content": "hello"},
        )
        assert resp.status_code == 403

    def test_hmac_signature_valid(self, client, _mock_adapter):
        body = json.dumps({"content": "hello"}).encode()
        sig = hmac.new(b"supersecret", body, hashlib.sha256).hexdigest()

        resp = client.post(
            "/webhook/inbound/test-hook",
            content=body,
            headers={
                "X-Webhook-Signature": f"sha256={sig}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    def test_hmac_signature_invalid(self, client):
        body = json.dumps({"content": "hello"}).encode()

        resp = client.post(
            "/webhook/inbound/test-hook",
            content=body,
            headers={
                "X-Webhook-Signature": "sha256=deadbeef",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 403


class TestWebhookInbound:
    def test_unknown_slot_404(self, client):
        resp = client.post(
            "/webhook/inbound/nonexistent",
            json={"content": "hello"},
            headers={"X-Webhook-Secret": "any"},
        )
        assert resp.status_code == 404

    def test_async_mode(self, client, _mock_adapter):
        resp = client.post(
            "/webhook/inbound/test-hook",
            json={"content": "hello"},
            headers={"X-Webhook-Secret": "supersecret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert "request_id" in data
        _mock_adapter.handle_webhook.assert_called_once()

    def test_sync_mode_timeout(self, client, _mock_adapter):
        """Sync mode returns timeout when adapter returns None."""
        _mock_adapter.handle_webhook = AsyncMock(return_value=None)

        resp = client.post(
            "/webhook/inbound/test-hook?wait=true",
            json={"content": "hello"},
            headers={"X-Webhook-Secret": "supersecret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "timeout"

    def test_sync_mode_response(self, client, _mock_adapter):
        """Sync mode returns agent response."""
        _mock_adapter.handle_webhook = AsyncMock(return_value="Agent says hi")

        resp = client.post(
            "/webhook/inbound/test-hook?wait=true",
            json={"content": "hello"},
            headers={"X-Webhook-Secret": "supersecret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["response"] == "Agent says hi"


class TestWebhookCRUD:
    def test_list_webhooks(self, client):
        resp = client.get("/api/webhooks", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["webhooks"]) == 1
        assert data["webhooks"][0]["name"] == "test-hook"
        assert "url" in data["webhooks"][0]

    def test_add_webhook(self, client, _mock_settings):
        resp = client.post(
            "/api/webhooks/add",
            json={"name": "new-hook", "description": "New one"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["webhook"]["name"] == "new-hook"
        assert len(data["webhook"]["secret"]) > 10
        _mock_settings.save.assert_called()

    def test_add_webhook_duplicate(self, client):
        resp = client.post(
            "/api/webhooks/add",
            json={"name": "test-hook"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 409

    def test_add_webhook_empty_name(self, client):
        resp = client.post(
            "/api/webhooks/add",
            json={"name": ""},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_add_webhook_invalid_name(self, client):
        resp = client.post(
            "/api/webhooks/add",
            json={"name": "has spaces"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_remove_webhook(self, client, _mock_settings):
        resp = client.post(
            "/api/webhooks/remove",
            json={"name": "test-hook"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        _mock_settings.save.assert_called()

    def test_remove_webhook_not_found(self, client):
        resp = client.post(
            "/api/webhooks/remove",
            json={"name": "nonexistent"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 404

    def test_regenerate_secret(self, client, _mock_settings):
        resp = client.post(
            "/api/webhooks/regenerate-secret",
            json={"name": "test-hook"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "secret" in data
        _mock_settings.save.assert_called()

    def test_regenerate_secret_not_found(self, client):
        resp = client.post(
            "/api/webhooks/regenerate-secret",
            json={"name": "nonexistent"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 404
