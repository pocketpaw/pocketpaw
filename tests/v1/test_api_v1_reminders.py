# Tests for API v1 reminders router.
# Created: 2026-02-20

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.reminders import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestListReminders:
    """Tests for GET /api/v1/reminders."""

    @patch("pocketpaw.scheduler.get_scheduler")
    def test_list_empty(self, mock_get_sched, client):
        sched = MagicMock()
        sched.get_reminders.return_value = []
        mock_get_sched.return_value = sched

        resp = client.get("/api/v1/reminders")
        assert resp.status_code == 200
        assert resp.json()["reminders"] == []

    @patch("pocketpaw.scheduler.get_scheduler")
    def test_list_with_reminders(self, mock_get_sched, client):
        sched = MagicMock()
        sched.get_reminders.return_value = [
            {
                "id": "abc123",
                "text": "call mom",
                "trigger_at": "2026-02-20T15:00:00",
                "created_at": "2026-02-20T14:55:00",
            },
            {
                "id": "def456",
                "text": "check email",
                "trigger_at": "2026-02-20T16:00:00",
                "created_at": "2026-02-20T14:50:00",
            },
        ]
        sched.format_time_remaining.side_effect = ["in 5m", "in 1h 5m"]
        mock_get_sched.return_value = sched

        resp = client.get("/api/v1/reminders")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["reminders"]) == 2
        assert data["reminders"][0]["id"] == "abc123"
        assert data["reminders"][0]["text"] == "call mom"
        assert data["reminders"][0]["time_remaining"] == "in 5m"
        assert data["reminders"][1]["time_remaining"] == "in 1h 5m"


class TestAddReminder:
    """Tests for POST /api/v1/reminders."""

    @patch("pocketpaw.scheduler.get_scheduler")
    def test_add_success(self, mock_get_sched, client):
        sched = MagicMock()
        sched.add_reminder.return_value = {
            "id": "new123",
            "text": "call mom",
            "trigger_at": "2026-02-20T15:00:00",
            "created_at": "2026-02-20T14:55:00",
        }
        sched.format_time_remaining.return_value = "in 5m"
        mock_get_sched.return_value = sched

        resp = client.post("/api/v1/reminders", json={"message": "in 5 minutes call mom"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["reminder"]["id"] == "new123"
        assert data["reminder"]["text"] == "call mom"
        sched.add_reminder.assert_called_once_with("in 5 minutes call mom")

    @patch("pocketpaw.scheduler.get_scheduler")
    def test_add_parse_failure(self, mock_get_sched, client):
        sched = MagicMock()
        sched.add_reminder.return_value = None
        mock_get_sched.return_value = sched

        resp = client.post("/api/v1/reminders", json={"message": "something unparseable"})
        assert resp.status_code == 400
        assert "parse time" in resp.json()["detail"].lower()

    def test_add_empty_message(self, client):
        resp = client.post("/api/v1/reminders", json={"message": ""})
        assert resp.status_code == 422  # Pydantic validation


class TestDeleteReminder:
    """Tests for DELETE /api/v1/reminders/{id}."""

    @patch("pocketpaw.scheduler.get_scheduler")
    def test_delete_success(self, mock_get_sched, client):
        sched = MagicMock()
        sched.delete_reminder.return_value = True
        mock_get_sched.return_value = sched

        resp = client.delete("/api/v1/reminders/abc123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "abc123"
        assert data["deleted"] is True
        sched.delete_reminder.assert_called_once_with("abc123")

    @patch("pocketpaw.scheduler.get_scheduler")
    def test_delete_not_found(self, mock_get_sched, client):
        sched = MagicMock()
        sched.delete_reminder.return_value = False
        mock_get_sched.return_value = sched

        resp = client.delete("/api/v1/reminders/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()
