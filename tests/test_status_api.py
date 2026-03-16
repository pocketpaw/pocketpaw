"""Tests for agent status API endpoint auth and CLI formatting."""

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.status import StatusTracker


@pytest.fixture(autouse=True)
def _clear_status_key_cache():
    """Reset the cached status API key between tests."""
    from pocketpaw.api.v1 import agent_status

    if hasattr(agent_status._get_status_api_key, "_value"):
        del agent_status._get_status_api_key._value
    yield
    if hasattr(agent_status._get_status_api_key, "_value"):
        del agent_status._get_status_api_key._value


class TestAgentStatusAuth:
    """Test the status endpoint auth logic."""

    def test_rejects_bad_key(self):
        """Verify auth check rejects wrong key."""
        from fastapi import HTTPException

        from pocketpaw.api.v1.agent_status import _check_status_key

        mock_request = MagicMock()
        mock_request.headers = {"x-status-key": "wrong"}

        with patch("pocketpaw.api.v1.agent_status._get_status_api_key", return_value="correct-key"):
            with pytest.raises(HTTPException) as exc_info:
                _check_status_key(mock_request, None)
            assert exc_info.value.status_code == 403

    def test_allows_correct_key_via_header(self):
        from pocketpaw.api.v1.agent_status import _check_status_key

        mock_request = MagicMock()
        mock_request.headers = {"x-status-key": "my-key"}

        with patch("pocketpaw.api.v1.agent_status._get_status_api_key", return_value="my-key"):
            _check_status_key(mock_request, None)  # Should not raise

    def test_allows_correct_key_via_query_param(self):
        from pocketpaw.api.v1.agent_status import _check_status_key

        mock_request = MagicMock()
        mock_request.headers = {}

        with patch("pocketpaw.api.v1.agent_status._get_status_api_key", return_value="my-key"):
            _check_status_key(mock_request, "my-key")  # Should not raise

    def test_allows_when_no_key_configured(self):
        from pocketpaw.api.v1.agent_status import _check_status_key

        mock_request = MagicMock()
        mock_request.headers = {}

        with patch("pocketpaw.api.v1.agent_status._get_status_api_key", return_value=""):
            _check_status_key(mock_request, None)  # Should not raise


class TestSnapshotShape:
    """Verify snapshot structure matches API contract."""

    def test_idle_snapshot_shape(self):
        tracker = StatusTracker(max_concurrent=5)
        snap = tracker.snapshot()

        assert "global" in snap
        assert "sessions" in snap
        assert snap["global"]["state"] == "idle"
        assert snap["global"]["max_concurrent"] == 5
        assert snap["global"]["active_sessions"] == 0
        assert isinstance(snap["global"]["uptime_seconds"], int)
        assert isinstance(snap["sessions"], list)

    async def test_active_snapshot_shape(self):
        from pocketpaw.bus.events import SystemEvent

        tracker = StatusTracker(max_concurrent=3)
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "websocket:abc123"})
        )
        await tracker._on_event(
            SystemEvent(
                event_type="tool_start",
                data={"session_key": "websocket:abc123", "name": "bash"},
            )
        )
        snap = tracker.snapshot()

        assert snap["global"]["state"] == "active"
        session = snap["sessions"][0]
        assert session["session_key"] == "websocket:abc123"
        assert session["session_id"] == "abc123"
        assert session["channel"] == "websocket"
        assert session["state"] == "tool_running"
        assert session["tool_name"] == "bash"
        assert isinstance(session["duration_seconds"], float)


class TestVersionTracking:
    """Test the version-based change detection."""

    async def test_version_increments_on_state_change(self):
        from pocketpaw.bus.events import SystemEvent

        tracker = StatusTracker()
        v0 = tracker.version
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        assert tracker.version > v0

    async def test_wait_for_change_returns_immediately_when_version_advanced(self):
        from pocketpaw.bus.events import SystemEvent

        tracker = StatusTracker()
        v0 = tracker.version
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        # Version already advanced, should return True immediately
        result = await tracker.wait_for_change(since_version=v0, timeout=0.01)
        assert result is True


class TestCLIFormat:
    """Test CLI formatting helpers."""

    def test_format_duration_seconds(self):
        from pocketpaw.cli.status import _format_duration

        assert _format_duration(0) == "0s"
        assert _format_duration(45) == "45s"

    def test_format_duration_minutes(self):
        from pocketpaw.cli.status import _format_duration

        assert _format_duration(90) == "1m 30s"
        assert _format_duration(120) == "2m 0s"

    def test_format_duration_hours(self):
        from pocketpaw.cli.status import _format_duration

        assert _format_duration(3661) == "1h 1m 1s"
