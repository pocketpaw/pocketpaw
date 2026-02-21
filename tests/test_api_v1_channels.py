# Tests for API v1 channels router.
# Created: 2026-02-21

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pocketpaw.dashboard  # noqa: F401 — force module-level side effects
from pocketpaw.api.v1.channels import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestChannelsStatus:
    """Tests for GET /channels/status."""

    def test_returns_all_channels(self, client):
        mock_settings = MagicMock(whatsapp_mode="business")
        with (
            patch("pocketpaw.dashboard_state._channel_autostart_enabled", return_value=False),
            patch("pocketpaw.dashboard_state._channel_is_running", return_value=False),
            patch("pocketpaw.dashboard_state._channel_is_configured", return_value=False),
            patch("pocketpaw.config.Settings.load", return_value=mock_settings),
        ):
            resp = client.get("/api/v1/channels/status")
        assert resp.status_code == 200
        data = resp.json()
        expected_channels = {
            "discord", "slack", "whatsapp", "telegram",
            "signal", "matrix", "teams", "google_chat",
        }
        assert set(data.keys()) == expected_channels

    def test_channel_has_status_fields(self, client):
        mock_settings = MagicMock(whatsapp_mode="business")
        with (
            patch("pocketpaw.dashboard_state._channel_autostart_enabled", return_value=False),
            patch("pocketpaw.dashboard_state._channel_is_running", return_value=False),
            patch("pocketpaw.dashboard_state._channel_is_configured", return_value=False),
            patch("pocketpaw.config.Settings.load", return_value=mock_settings),
        ):
            resp = client.get("/api/v1/channels/status")
        data = resp.json()
        for ch, status in data.items():
            assert "configured" in status
            assert "running" in status
            assert "autostart" in status

    def test_whatsapp_has_mode(self, client):
        mock_settings = MagicMock(whatsapp_mode="personal")
        with (
            patch("pocketpaw.dashboard_state._channel_autostart_enabled", return_value=False),
            patch("pocketpaw.dashboard_state._channel_is_running", return_value=False),
            patch("pocketpaw.dashboard_state._channel_is_configured", return_value=False),
            patch("pocketpaw.config.Settings.load", return_value=mock_settings),
        ):
            resp = client.get("/api/v1/channels/status")
        assert resp.json()["whatsapp"]["mode"] == "personal"


class TestSaveChannelConfig:
    """Tests for POST /channels/save."""

    def test_save_valid_channel(self, client):
        mock_s = MagicMock()
        mock_s.channel_autostart = {}
        with (
            patch(
                "pocketpaw.dashboard_state._CHANNEL_CONFIG_KEYS",
                {"discord": {"bot_token": "discord_bot_token"}},
            ),
            patch("pocketpaw.config.Settings.load", return_value=mock_s),
        ):
            resp = client.post(
                "/api/v1/channels/save",
                json={"channel": "discord", "config": {"bot_token": "test-token"}},
            )
        assert resp.status_code == 200
        mock_s.save.assert_called_once()

    def test_save_unknown_channel(self, client):
        resp = client.post(
            "/api/v1/channels/save",
            json={"channel": "unknown_channel_xyz", "config": {}},
        )
        assert resp.status_code == 400


class TestToggleChannel:
    """Tests for POST /channels/toggle."""

    def test_toggle_unknown_channel(self, client):
        resp = client.post(
            "/api/v1/channels/toggle",
            json={"channel": "nonexistent_xyz", "action": "start"},
        )
        assert resp.status_code == 400

    def test_start_already_running(self, client):
        # The channels router does `from pocketpaw.dashboard import _channel_is_running`
        # inside the function body, so we must patch on the dashboard module.
        import pocketpaw.dashboard as _dash

        orig_running = _dash._channel_is_running
        orig_keys = _dash._CHANNEL_CONFIG_KEYS
        _dash._channel_is_running = lambda ch: True
        _dash._CHANNEL_CONFIG_KEYS = {"discord": {}}
        try:
            with patch("pocketpaw.config.Settings.load", return_value=MagicMock()):
                resp = client.post(
                    "/api/v1/channels/toggle",
                    json={"channel": "discord", "action": "start"},
                )
        finally:
            _dash._channel_is_running = orig_running
            _dash._CHANNEL_CONFIG_KEYS = orig_keys
        assert resp.status_code == 200
        assert "already running" in resp.json().get("error", "")

    def test_invalid_action(self, client):
        with (
            patch("pocketpaw.dashboard_state._CHANNEL_CONFIG_KEYS", {"discord": {}}),
            patch("pocketpaw.config.Settings.load", return_value=MagicMock()),
        ):
            resp = client.post(
                "/api/v1/channels/toggle",
                json={"channel": "discord", "action": "invalid"},
            )
        assert resp.status_code == 400


class TestExtrasCheck:
    """Tests for GET /extras/check."""

    def test_check_installed(self, client):
        import pocketpaw.dashboard as _dash

        orig_deps = _dash._CHANNEL_DEPS
        orig_importable = _dash._is_module_importable
        _dash._CHANNEL_DEPS = {"discord": ("discord", "discord.py", "discord.py>=2.0")}
        _dash._is_module_importable = lambda mod: True
        try:
            resp = client.get("/api/v1/extras/check?channel=discord")
        finally:
            _dash._CHANNEL_DEPS = orig_deps
            _dash._is_module_importable = orig_importable
        assert resp.status_code == 200
        assert resp.json()["installed"] is True

    def test_check_no_deps_needed(self, client):
        import pocketpaw.dashboard as _dash

        orig = _dash._CHANNEL_DEPS
        _dash._CHANNEL_DEPS = {}
        try:
            resp = client.get("/api/v1/extras/check?channel=telegram")
        finally:
            _dash._CHANNEL_DEPS = orig
        assert resp.status_code == 200
        assert resp.json()["installed"] is True
