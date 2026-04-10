from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.dashboard_auth import auth_router

MASTER_TOKEN = "test-master-token-123"


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(auth_router)
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestDashboardCookieLogin:
    @patch("pocketpaw.dashboard_auth.get_access_token", return_value=MASTER_TOKEN)
    @patch("pocketpaw.dashboard_auth.Settings.load")
    @patch("pocketpaw.dashboard_auth.create_session_token", return_value="sess:xyz")
    def test_login_sets_cookie_without_secure_by_default(
        self, mock_create, mock_load, mock_get, client
    ):
        mock_load.return_value = MagicMock(session_token_ttl_hours=24)
        resp = client.post("/api/auth/login", json={"token": MASTER_TOKEN})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "pocketpaw_session" in resp.cookies
        assert "Secure" not in resp.headers["set-cookie"]

    @patch("pocketpaw.dashboard_auth.get_access_token", return_value=MASTER_TOKEN)
    @patch("pocketpaw.dashboard_auth.Settings.load")
    @patch("pocketpaw.dashboard_auth.create_session_token", return_value="sess:xyz")
    def test_login_sets_secure_cookie_for_forwarded_https(
        self, mock_create, mock_load, mock_get, client
    ):
        mock_load.return_value = MagicMock(session_token_ttl_hours=24)
        resp = client.post(
            "/api/auth/login",
            json={"token": MASTER_TOKEN},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert resp.status_code == 200
        assert "Secure" in resp.headers["set-cookie"]
