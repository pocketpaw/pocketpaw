# Tests for API v1 plan mode router.
# Created: 2026-02-20

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.plan_mode import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestApprovePlan:
    """Tests for POST /api/v1/plan/approve."""

    @patch("pocketpaw.agents.plan_mode.get_plan_manager")
    def test_approve_success(self, mock_get_pm, client):
        pm = MagicMock()
        plan = MagicMock()
        pm.approve_plan.return_value = plan
        mock_get_pm.return_value = pm

        resp = client.post("/api/v1/plan/approve", json={"session_key": "sess-123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_key"] == "sess-123"
        assert data["action"] == "approved"
        pm.approve_plan.assert_called_once_with("sess-123")

    @patch("pocketpaw.agents.plan_mode.get_plan_manager")
    def test_approve_no_active_plan(self, mock_get_pm, client):
        pm = MagicMock()
        pm.approve_plan.return_value = None
        mock_get_pm.return_value = pm

        resp = client.post("/api/v1/plan/approve", json={"session_key": "nonexistent"})
        assert resp.status_code == 404
        assert "no active plan" in resp.json()["detail"].lower()

    def test_approve_missing_session_key(self, client):
        resp = client.post("/api/v1/plan/approve", json={})
        assert resp.status_code == 422


class TestRejectPlan:
    """Tests for POST /api/v1/plan/reject."""

    @patch("pocketpaw.agents.plan_mode.get_plan_manager")
    def test_reject_success(self, mock_get_pm, client):
        pm = MagicMock()
        plan = MagicMock()
        pm.reject_plan.return_value = plan
        mock_get_pm.return_value = pm

        resp = client.post("/api/v1/plan/reject", json={"session_key": "sess-123"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_key"] == "sess-123"
        assert data["action"] == "rejected"
        pm.reject_plan.assert_called_once_with("sess-123")

    @patch("pocketpaw.agents.plan_mode.get_plan_manager")
    def test_reject_no_active_plan(self, mock_get_pm, client):
        pm = MagicMock()
        pm.reject_plan.return_value = None
        mock_get_pm.return_value = pm

        resp = client.post("/api/v1/plan/reject", json={"session_key": "nonexistent"})
        assert resp.status_code == 404

    def test_reject_empty_session_key(self, client):
        resp = client.post("/api/v1/plan/reject", json={"session_key": ""})
        assert resp.status_code == 422
