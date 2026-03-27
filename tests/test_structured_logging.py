"""Tests for structured logging in AgentRouter."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.router import AgentRouter


class MockSettings:
    """Minimal settings mock."""
    def __init__(self):
        self.agent_backend = "mock_primary"
        self.fallback_backends = ["mock_fallback"]


class MockBackend:
    """A backend that yields events with a small delay."""
    def __init__(self, name: str):
        self.name = name

    async def run(self, *args, **kwargs) -> AsyncIterator[AgentEvent]:
        # Simulate some processing time
        await asyncio.sleep(0.1)
        yield AgentEvent(type="message", content=f"Hello from {self.name}")
        yield AgentEvent(type="done", content="")

    async def stop(self):
        pass

    @classmethod
    def info(cls):
        from pocketpaw.agents.backend import BackendInfo
        return BackendInfo(name="mock", display_name="Mock Backend")


@pytest.mark.asyncio
async def test_router_structured_logging_success(caplog):
    """Verify that successful backend runs produce structured logs with latency."""
    caplog.set_level(logging.INFO)
    settings = MockSettings()

    primary = MockBackend("primary")
    fallback = MockBackend("fallback")

    def mock_get_backend(name):
        if name == "mock_primary": return lambda s: primary
        if name == "mock_fallback": return lambda s: fallback
        return None

    with patch("pocketpaw.agents.router.get_backend_class", side_effect=mock_get_backend):
        router = AgentRouter(settings)
        # Manually set primary backend because initialization in __init__ is complex to mock
        router._backend = primary
        router._active_backend_name = "mock_primary"

        async for _ in router.run("test"):
            pass

        # Check log records
        log_records = [r for r in caplog.records if r.name == "pocketpaw.agents.router"]
        
        # 1. Start log
        start_logs = [r for r in log_records if getattr(r, "event", None) == "backend_start"]
        assert len(start_logs) >= 1
        assert start_logs[0].backend == "mock_primary"
        assert start_logs[0].is_primary is True

        # 2. Success log
        success_logs = [r for r in log_records if getattr(r, "event", None) == "backend_success"]
        assert len(success_logs) == 1
        assert success_logs[0].backend == "mock_primary"
        # Latency should be at least 100ms (0.1s sleep)
        assert success_logs[0].latency_ms >= 100
        assert success_logs[0].is_primary is True


@pytest.mark.asyncio
async def test_router_structured_logging_failure(caplog):
    """Verify that failed backend runs produce structured logs with error and latency."""
    caplog.set_level(logging.WARNING)
    settings = MockSettings()

    class FailingBackend(MockBackend):
        async def run(self, *args, **kwargs) -> AsyncIterator[AgentEvent]:
            await asyncio.sleep(0.05)
            if False: yield AgentEvent(type="message", content="never")
            raise RuntimeError("Backend exploded")

    primary = FailingBackend("primary")
    fallback = MockBackend("fallback")

    def mock_get_backend(name):
        if name == "mock_primary": return lambda s: primary
        if name == "mock_fallback": return lambda s: fallback
        return None

    with patch("pocketpaw.agents.router.get_backend_class", side_effect=mock_get_backend):
        router = AgentRouter(settings)
        router._backend = primary
        router._active_backend_name = "mock_primary"

        # It should fall back to the fallback backend
        async for _ in router.run("test"):
            pass

        # Check failure log for primary
        failure_logs = [r for r in caplog.records if getattr(r, "event", None) == "backend_failure"]
        assert len(failure_logs) == 1
        assert failure_logs[0].backend == "mock_primary"
        assert "exploded" in failure_logs[0].error
        assert failure_logs[0].latency_ms >= 50
        assert failure_logs[0].is_primary is True
