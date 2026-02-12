# Tests for the /api/health endpoint and health.py module
# Verifies structured diagnostics, HTTP status codes, and auth exemption.
#
# Created: 2026-02-12
# Tests target collect_health() directly + endpoint integration.

from unittest.mock import MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Helpers — build mock subsystems using public APIs only
# ---------------------------------------------------------------------------


def _make_agent_loop(running: bool):
    loop = MagicMock()
    loop.is_running.return_value = running
    return loop


def _make_bus(subscriber_count: int = 0):
    bus = MagicMock()
    bus.get_subscriber_count.return_value = subscriber_count
    return bus


def _make_memory_manager(session_count: int = 0):
    mem = MagicMock()
    mem.get_session_count.return_value = session_count
    return mem


# ---------------------------------------------------------------------------
# Unit tests for collect_health()
# ---------------------------------------------------------------------------


def test_healthy_when_all_ok():
    """All subsystems ok + all channels connected → 'healthy', HTTP 200."""
    with (
        patch("pocketclaw.health.get_message_bus", return_value=_make_bus(3)),
        patch("pocketclaw.health.get_memory_manager", return_value=_make_memory_manager(5)),
    ):
        from pocketclaw.health import collect_health

        body, code = collect_health(
            agent_loop=_make_agent_loop(True),
            channel_status_fn=lambda ch: True,  # all channels connected
            start_time=0.0,
        )
    assert code == 200
    assert body["status"] == "healthy"


def test_degraded_when_channel_disconnected():
    """Core ok but a channel disconnected → 'degraded', HTTP 200."""
    with (
        patch("pocketclaw.health.get_message_bus", return_value=_make_bus(2)),
        patch("pocketclaw.health.get_memory_manager", return_value=_make_memory_manager()),
    ):
        from pocketclaw.health import collect_health

        body, code = collect_health(
            agent_loop=_make_agent_loop(True),
            channel_status_fn=lambda ch: False,  # no channels running
            start_time=0.0,
        )
    assert code == 200
    assert body["status"] == "degraded"


def test_unhealthy_when_agent_down():
    """Agent loop down → 'unhealthy', HTTP 503."""
    with (
        patch("pocketclaw.health.get_message_bus", return_value=_make_bus()),
        patch("pocketclaw.health.get_memory_manager", return_value=_make_memory_manager()),
    ):
        from pocketclaw.health import collect_health

        body, code = collect_health(
            agent_loop=_make_agent_loop(False),
            channel_status_fn=lambda ch: False,
            start_time=0.0,
        )
    assert code == 503
    assert body["status"] == "unhealthy"
    assert body["subsystems"]["agent_backend"]["status"] == "down"


def test_unhealthy_when_memory_error():
    """Memory subsystem error → 'unhealthy', HTTP 503 (memory is critical)."""
    with (
        patch("pocketclaw.health.get_message_bus", return_value=_make_bus()),
        patch("pocketclaw.health.get_memory_manager", side_effect=RuntimeError("store broken")),
    ):
        from pocketclaw.health import collect_health

        body, code = collect_health(
            agent_loop=_make_agent_loop(True),
            channel_status_fn=lambda ch: True,
            start_time=0.0,
        )
    assert code == 503
    assert body["status"] == "unhealthy"
    assert body["subsystems"]["memory"]["status"] == "error"


def test_required_top_level_fields():
    """Response must contain all documented top-level keys."""
    with (
        patch("pocketclaw.health.get_message_bus", return_value=_make_bus()),
        patch("pocketclaw.health.get_memory_manager", return_value=_make_memory_manager()),
    ):
        from pocketclaw.health import collect_health

        body, _ = collect_health(
            agent_loop=_make_agent_loop(True),
            channel_status_fn=lambda ch: True,
            start_time=0.0,
        )
    required = {"status", "version", "uptime_seconds", "subsystems", "python_version", "platform"}
    assert required.issubset(body.keys())


def test_subsystem_structure():
    """Subsystems dict must contain agent_backend, memory, message_bus, channels."""
    with (
        patch("pocketclaw.health.get_message_bus", return_value=_make_bus(2)),
        patch("pocketclaw.health.get_memory_manager", return_value=_make_memory_manager(1)),
    ):
        from pocketclaw.health import collect_health

        body, _ = collect_health(
            agent_loop=_make_agent_loop(True),
            channel_status_fn=lambda ch: ch == "telegram",
            start_time=0.0,
        )
    subs = body["subsystems"]
    assert "agent_backend" in subs
    assert "memory" in subs
    assert "message_bus" in subs
    assert "channels" in subs
    # Channels must list all 4
    for ch in ("telegram", "discord", "slack", "whatsapp"):
        assert ch in subs["channels"]
    # Telegram connected, others disconnected
    assert subs["channels"]["telegram"] == "connected"
    assert subs["channels"]["discord"] == "disconnected"
    # Subscriber count uses public API
    assert subs["message_bus"]["subscribers"] == 2


# ---------------------------------------------------------------------------
# Integration: endpoint accessible without auth
# ---------------------------------------------------------------------------


def test_health_endpoint_exempt_from_auth():
    """The /api/health route must be accessible without any auth token (no 401)."""
    with (
        patch("pocketclaw.dashboard.agent_loop") as mock_loop,
        patch("pocketclaw.health.get_message_bus", return_value=_make_bus()),
        patch("pocketclaw.health.get_memory_manager", return_value=_make_memory_manager()),
        patch("pocketclaw.dashboard._channel_is_running", return_value=True),
        patch("pocketclaw.dashboard._app_start_time", 0.0),
    ):
        mock_loop.is_running.return_value = True

        from fastapi.testclient import TestClient
        from pocketclaw.dashboard import app

        tc = TestClient(app, raise_server_exceptions=False)
        resp = tc.get("/api/health")
        assert resp.status_code != 401
        assert resp.status_code == 200
