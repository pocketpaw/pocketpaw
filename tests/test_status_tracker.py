"""Tests for StatusTracker."""

import pytest

from pocketpaw.bus.events import SystemEvent
from pocketpaw.status import StatusTracker


@pytest.fixture
def tracker():
    return StatusTracker(max_concurrent=3)


class TestStatusTracker:
    async def test_idle_by_default(self, tracker):
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "idle"
        assert snap["global"]["active_sessions"] == 0
        assert snap["sessions"] == []

    async def test_agent_start_creates_session(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "websocket:abc"})
        )
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "active"
        assert snap["global"]["active_sessions"] == 1
        assert snap["sessions"][0]["session_key"] == "websocket:abc"
        assert snap["sessions"][0]["channel"] == "websocket"
        assert snap["sessions"][0]["session_id"] == "abc"

    async def test_thinking_state(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(SystemEvent(event_type="thinking", data={"session_key": "ws:1"}))
        snap = tracker.snapshot()
        assert snap["sessions"][0]["state"] == "thinking"

    async def test_tool_running_state(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(
            SystemEvent(
                event_type="tool_start",
                data={"session_key": "ws:1", "name": "bash"},
            )
        )
        snap = tracker.snapshot()
        assert snap["sessions"][0]["state"] == "tool_running"
        assert snap["sessions"][0]["tool_name"] == "bash"

    async def test_tool_result_transitions_to_streaming(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(
            SystemEvent(event_type="tool_start", data={"session_key": "ws:1", "name": "bash"})
        )
        await tracker._on_event(SystemEvent(event_type="tool_result", data={"session_key": "ws:1"}))
        snap = tracker.snapshot()
        assert snap["sessions"][0]["state"] == "streaming"
        assert snap["sessions"][0]["tool_name"] is None

    async def test_error_state_sets_degraded(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(
            SystemEvent(
                event_type="error",
                data={"session_key": "ws:1", "message": "Rate limit"},
            )
        )
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "degraded"
        assert snap["sessions"][0]["state"] == "error"
        assert snap["sessions"][0]["error_message"] == "Rate limit"

    async def test_agent_end_removes_session(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(SystemEvent(event_type="agent_end", data={"session_key": "ws:1"}))
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "idle"
        assert snap["sessions"] == []

    async def test_waiting_for_user_state(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(
            SystemEvent(event_type="ask_user_question", data={"session_key": "ws:1"})
        )
        snap = tracker.snapshot()
        assert snap["sessions"][0]["state"] == "waiting_for_user"

    async def test_token_usage_accumulates(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(
            SystemEvent(
                event_type="token_usage",
                data={"session_key": "ws:1", "input": 100, "output": 50},
            )
        )
        await tracker._on_event(
            SystemEvent(
                event_type="token_usage",
                data={"session_key": "ws:1", "input": 200, "output": 80},
            )
        )
        snap = tracker.snapshot()
        assert snap["sessions"][0]["token_usage"] == {"input": 300, "output": 130}

    async def test_max_concurrent_in_snapshot(self, tracker):
        snap = tracker.snapshot()
        assert snap["global"]["max_concurrent"] == 3

    async def test_ignores_events_without_session_key(self, tracker):
        await tracker._on_event(SystemEvent(event_type="thinking", data={}))
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "idle"

    async def test_multiple_sessions(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "discord:2"})
        )
        snap = tracker.snapshot()
        assert snap["global"]["active_sessions"] == 2
        assert len(snap["sessions"]) == 2

    async def test_degraded_with_mixed_states(self, tracker):
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:1"}))
        await tracker._on_event(SystemEvent(event_type="agent_start", data={"session_key": "ws:2"}))
        await tracker._on_event(
            SystemEvent(
                event_type="error",
                data={"session_key": "ws:1", "message": "fail"},
            )
        )
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "degraded"
        assert snap["global"]["active_sessions"] == 2
