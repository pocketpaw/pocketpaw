# Tests for API v1 telegram router.
# Created: 2026-02-20

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.telegram import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestTelegramStatus:
    """Tests for GET /api/v1/telegram/status."""

    @patch("pocketpaw.config.Settings.load")
    def test_status_configured(self, mock_load, client):
        settings = MagicMock()
        settings.telegram_bot_token = "123:ABC"
        settings.allowed_user_id = 12345
        mock_load.return_value = settings

        resp = client.get("/api/v1/telegram/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["user_id"] == 12345

    @patch("pocketpaw.config.Settings.load")
    def test_status_not_configured(self, mock_load, client):
        settings = MagicMock()
        settings.telegram_bot_token = ""
        settings.allowed_user_id = None
        mock_load.return_value = settings

        resp = client.get("/api/v1/telegram/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["user_id"] is None


class TestTelegramPairingStatus:
    """Tests for GET /api/v1/telegram/pairing-status."""

    @patch("pocketpaw.dashboard._telegram_pairing_state", {"paired": False, "user_id": None})
    def test_not_paired(self, client):
        resp = client.get("/api/v1/telegram/pairing-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["paired"] is False

    @patch(
        "pocketpaw.dashboard._telegram_pairing_state",
        {"paired": True, "user_id": 99999, "temp_bot_app": None},
    )
    def test_paired(self, client):
        resp = client.get("/api/v1/telegram/pairing-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["paired"] is True
        assert data["user_id"] == 99999
