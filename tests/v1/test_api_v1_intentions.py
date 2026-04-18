# Tests for API v1 intentions router.
# Created: 2026-02-20

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.intentions import router

_SAMPLE_INTENTION = {
    "id": "int-001",
    "name": "Morning Standup",
    "prompt": "What are your top 3 priorities?",
    "trigger": {"type": "cron", "schedule": "0 8 * * 1-5"},
    "context_sources": ["system_status"],
    "enabled": True,
    "created_at": "2026-02-20T08:00:00",
    "last_run": None,
    "next_run": "2026-02-21T08:00:00",
}


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestListIntentions:
    """Tests for GET /api/v1/intentions."""

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_list_empty(self, mock_get, client):
        daemon = MagicMock()
        daemon.get_intentions.return_value = []
        mock_get.return_value = daemon

        resp = client.get("/api/v1/intentions")
        assert resp.status_code == 200
        assert resp.json()["intentions"] == []

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_list_with_intentions(self, mock_get, client):
        daemon = MagicMock()
        daemon.get_intentions.return_value = [_SAMPLE_INTENTION]
        mock_get.return_value = daemon

        resp = client.get("/api/v1/intentions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["intentions"]) == 1
        assert data["intentions"][0]["name"] == "Morning Standup"
        assert data["intentions"][0]["enabled"] is True


class TestCreateIntention:
    """Tests for POST /api/v1/intentions."""

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_create_success(self, mock_get, client):
        daemon = MagicMock()
        daemon.create_intention.return_value = _SAMPLE_INTENTION
        mock_get.return_value = daemon

        resp = client.post(
            "/api/v1/intentions",
            json={
                "name": "Morning Standup",
                "prompt": "What are your top 3 priorities?",
                "trigger": {"type": "cron", "schedule": "0 8 * * 1-5"},
                "context_sources": ["system_status"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["intention"]["name"] == "Morning Standup"

    def test_create_missing_name(self, client):
        resp = client.post("/api/v1/intentions", json={"prompt": "test"})
        assert resp.status_code == 422

    def test_create_missing_prompt(self, client):
        resp = client.post("/api/v1/intentions", json={"name": "test"})
        assert resp.status_code == 422

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_create_failure(self, mock_get, client):
        daemon = MagicMock()
        daemon.create_intention.side_effect = RuntimeError("boom")
        mock_get.return_value = daemon

        resp = client.post(
            "/api/v1/intentions",
            json={"name": "Fail", "prompt": "test"},
        )
        assert resp.status_code == 500
        assert "boom" in resp.json()["detail"]


class TestUpdateIntention:
    """Tests for PATCH /api/v1/intentions/{id}."""

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_update_success(self, mock_get, client):
        updated = {**_SAMPLE_INTENTION, "name": "Updated Name"}
        daemon = MagicMock()
        daemon.update_intention.return_value = updated
        mock_get.return_value = daemon

        resp = client.patch("/api/v1/intentions/int-001", json={"name": "Updated Name"})
        assert resp.status_code == 200
        assert resp.json()["intention"]["name"] == "Updated Name"

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_update_not_found(self, mock_get, client):
        daemon = MagicMock()
        daemon.update_intention.return_value = None
        mock_get.return_value = daemon

        resp = client.patch("/api/v1/intentions/nonexistent", json={"name": "X"})
        assert resp.status_code == 404

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_update_no_fields(self, mock_get, client):
        daemon = MagicMock()
        mock_get.return_value = daemon

        resp = client.patch("/api/v1/intentions/int-001", json={})
        assert resp.status_code == 400
        assert "no updates" in resp.json()["detail"].lower()


class TestDeleteIntention:
    """Tests for DELETE /api/v1/intentions/{id}."""

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_delete_success(self, mock_get, client):
        daemon = MagicMock()
        daemon.delete_intention.return_value = True
        mock_get.return_value = daemon

        resp = client.delete("/api/v1/intentions/int-001")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_delete_not_found(self, mock_get, client):
        daemon = MagicMock()
        daemon.delete_intention.return_value = False
        mock_get.return_value = daemon

        resp = client.delete("/api/v1/intentions/nonexistent")
        assert resp.status_code == 404


class TestToggleIntention:
    """Tests for POST /api/v1/intentions/{id}/toggle."""

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_toggle_success(self, mock_get, client):
        toggled = {**_SAMPLE_INTENTION, "enabled": False}
        daemon = MagicMock()
        daemon.toggle_intention.return_value = toggled
        mock_get.return_value = daemon

        resp = client.post("/api/v1/intentions/int-001/toggle")
        assert resp.status_code == 200
        assert resp.json()["intention"]["enabled"] is False

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_toggle_not_found(self, mock_get, client):
        daemon = MagicMock()
        daemon.toggle_intention.return_value = None
        mock_get.return_value = daemon

        resp = client.post("/api/v1/intentions/nonexistent/toggle")
        assert resp.status_code == 404


class TestRunIntention:
    """Tests for POST /api/v1/intentions/{id}/run."""

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_run_success(self, mock_get, client):
        daemon = MagicMock()
        daemon.get_intentions.return_value = [_SAMPLE_INTENTION]
        daemon.run_intention_now = AsyncMock()
        mock_get.return_value = daemon

        resp = client.post("/api/v1/intentions/int-001/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert "Morning Standup" in data["message"]

    @patch("pocketpaw.daemon.proactive.get_daemon")
    def test_run_not_found(self, mock_get, client):
        daemon = MagicMock()
        daemon.get_intentions.return_value = []
        mock_get.return_value = daemon

        resp = client.post("/api/v1/intentions/nonexistent/run")
        assert resp.status_code == 404
