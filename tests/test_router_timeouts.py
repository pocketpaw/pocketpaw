"""Tests for the Agent Router execution timeouts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.router import AgentRouter


class MockSettings:
    """Minimal settings mock."""
    def __init__(self, timeout: int = 1):
        self.agent_backend = "mock_backend"
        self.fallback_backends = []
        self.agent_execution_timeout_seconds = timeout


class MockHangingBackend:
    """A backend that sleeps longer than the timeout."""
    def __init__(self, settings):
        self.settings = settings

    async def run(self, *args, **kwargs) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(type="thinking", content="I am thinking...")
        await asyncio.sleep(5)  # Sleep longer than the 1s timeout
        yield AgentEvent(type="message", content="I finished!")
        yield AgentEvent(type="done", content="")

    async def stop(self):
        pass

    @classmethod
    def info(cls):
        from pocketpaw.agents.backend import BackendInfo
        return BackendInfo(name="mock", display_name="Mock Hanging Backend")


@pytest.mark.asyncio
async def test_router_execution_timeout():
    """Verify that the router terminates and yields an error on timeout."""
    settings = MockSettings(timeout=1)
    
    # Mock the registry to return our hanging backend
    with patch("pocketpaw.agents.router.get_backend_class", return_value=MockHangingBackend):
        router = AgentRouter(settings)
        
        events = []
        async for event in router.run("Hello"):
            events.append(event)
            
        # Expectation:
        # 1. "thinking" event from backend
        # 2. "error" event from router's timeout catch
        # 3. "done" event (not yielded by timeout catch itself, but maybe by the final router yield)
        
        # Check that we got the timeout error
        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "timed out" in error_events[0].content
        
        # Check that the "message" event was never reached
        message_contents = [e.content for e in events if e.type == "message"]
        assert "I finished!" not in message_contents


@pytest.mark.asyncio
async def test_router_no_timeout_on_fast_backend():
    """Verify that the router finishes normally if the backend is fast."""
    settings = MockSettings(timeout=2)
    
    class MockFastBackend(MockHangingBackend):
        async def run(self, *args, **kwargs) -> AsyncIterator[AgentEvent]:
            yield AgentEvent(type="message", content="Fast response")
            yield AgentEvent(type="done", content="")

    with patch("pocketpaw.agents.router.get_backend_class", return_value=MockFastBackend):
        router = AgentRouter(settings)
        events = []
        async for event in router.run("Hello"):
            events.append(event)
            
        assert any(e.type == "done" for e in events)
        assert not any(e.type == "error" for e in events)
        assert events[0].content == "Fast response"
