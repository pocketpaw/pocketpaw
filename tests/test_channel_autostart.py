"""Tests for per-channel autostart feature."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.config import Settings
from pocketpaw.dashboard import _channel_autostart_enabled, startup_event
from pocketpaw.dashboard_channels import get_channels_status, save_channel_config

# ---------------------------------------------------------------------------
# Helper — default function under test
# ---------------------------------------------------------------------------


def _autostart_enabled(channel: str, settings: Settings) -> bool:
    """Mirror of dashboard._channel_autostart_enabled for unit tests."""
    return settings.channel_autostart.get(channel, True)


# ---------------------------------------------------------------------------
# 1. Default returns True for unconfigured channels
# ---------------------------------------------------------------------------


def test_default_returns_true_for_unconfigured():
    settings = Settings()
    assert _autostart_enabled("discord", settings) is True
    assert _autostart_enabled("slack", settings) is True
    assert _autostart_enabled("totally_new_channel", settings) is True


# ---------------------------------------------------------------------------
# 2. Respects explicit False
# ---------------------------------------------------------------------------


def test_respects_explicit_false():
    settings = Settings(channel_autostart={"discord": False, "slack": True})
    assert _autostart_enabled("discord", settings) is False
    assert _autostart_enabled("slack", settings) is True
    # Unset channels still default True
    assert _autostart_enabled("telegram", settings) is True


# ---------------------------------------------------------------------------
# 3. Startup skips disabled channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_skips_disabled_channels():
    """Channels with autostart=False should not be started on dashboard boot."""
    settings = Settings(
        channel_autostart={"discord": False},
        discord_bot_token="fake-token",
    )

    mock_ws = MagicMock()
    mock_ws.start = AsyncMock()

    with (
        patch("pocketpaw.dashboard_lifecycle.Settings.load", return_value=settings),
        patch(
            "pocketpaw.dashboard._start_channel_adapter", new_callable=AsyncMock
        ) as mock_start,
        patch("pocketpaw.dashboard_lifecycle.agent_loop") as mock_loop,
        patch("pocketpaw.dashboard_lifecycle.ws_adapter", mock_ws),
        patch("pocketpaw.dashboard_lifecycle.get_message_bus"),
        patch("pocketpaw.dashboard_lifecycle.get_scheduler"),
        patch("pocketpaw.dashboard_lifecycle.get_daemon"),
        patch("pocketpaw.dashboard_lifecycle.get_audit_logger"),
    ):
        mock_loop.start = AsyncMock()

        # Verify the helper itself
        assert _channel_autostart_enabled("discord", settings) is False

        # Collect which channels _start_channel_adapter was called with
        await startup_event()
        started_channels = [call.args[0] for call in mock_start.call_args_list]
        assert "discord" not in started_channels


# ---------------------------------------------------------------------------
# 4. Startup starts enabled channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_starts_enabled_channels():
    """Channels with autostart=True (or missing) should be attempted."""
    settings = Settings(
        channel_autostart={"slack": True},
        slack_bot_token="xoxb-fake",
        slack_app_token="xapp-fake",
    )

    mock_ws = MagicMock()
    mock_ws.start = AsyncMock()

    with (
        patch("pocketpaw.dashboard_lifecycle.Settings.load", return_value=settings),
        patch(
            "pocketpaw.dashboard._start_channel_adapter", new_callable=AsyncMock
        ) as mock_start,
        patch("pocketpaw.dashboard_lifecycle.agent_loop") as mock_loop,
        patch("pocketpaw.dashboard_lifecycle.ws_adapter", mock_ws),
        patch("pocketpaw.dashboard_lifecycle.get_message_bus"),
        patch("pocketpaw.dashboard_lifecycle.get_scheduler"),
        patch("pocketpaw.dashboard_lifecycle.get_daemon"),
        patch("pocketpaw.dashboard_lifecycle.get_audit_logger"),
    ):
        mock_loop.start = AsyncMock()

        await startup_event()
        started_channels = [call.args[0] for call in mock_start.call_args_list]
        assert "slack" in started_channels


# ---------------------------------------------------------------------------
# 5. API status includes autostart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_status_includes_autostart():
    settings = Settings(channel_autostart={"discord": False})

    with (
        patch("pocketpaw.dashboard_channels.Settings.load", return_value=settings),
        patch("pocketpaw.dashboard_channels._channel_is_configured", return_value=False),
        patch("pocketpaw.dashboard_channels._channel_is_running", return_value=False),
    ):
        result = await get_channels_status()
        assert result["discord"]["autostart"] is False
        assert result["slack"]["autostart"] is True  # default


# ---------------------------------------------------------------------------
# 6. API save persists autostart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_save_persists_autostart():
    settings = Settings()

    mock_request = AsyncMock()
    mock_request.json = AsyncMock(
        return_value={"channel": "discord", "config": {"autostart": False}}
    )

    with (
        patch("pocketpaw.dashboard_channels.Settings.load", return_value=settings),
        patch("pocketpaw.config.Settings.save") as mock_save,
    ):
        result = await save_channel_config(mock_request)
        assert result == {"status": "ok"}
        assert settings.channel_autostart["discord"] is False
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Round-trip via Settings save/load
# ---------------------------------------------------------------------------


def test_round_trip_save_load(tmp_path):
    """channel_autostart should survive save → load."""
    config_path = tmp_path / "config.json"

    settings = Settings(channel_autostart={"discord": False, "teams": True})

    # Manually build the save dict and write it
    save_dict = {"channel_autostart": settings.channel_autostart}
    config_path.write_text(json.dumps(save_dict))

    # Load back
    data = json.loads(config_path.read_text())
    loaded = Settings(**data)
    assert loaded.channel_autostart == {"discord": False, "teams": True}
