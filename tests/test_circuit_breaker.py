"""Tests for the distributed Circuit Breaker."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
import time

import pytest

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.circuit_breaker import CircuitBreaker, CircuitState, CircuitOpenException


def _make_events(*contents: str) -> list[AgentEvent]:
    events = [AgentEvent(type="text", content=c) for c in contents]
    events.append(AgentEvent(type="done", content=""))
    return events


@pytest.fixture
def breaker() -> CircuitBreaker:
    return CircuitBreaker(backend_name="test_backend", failure_threshold=3, recovery_timeout=1.0)


class TestCircuitBreaker:
    def test_initial_state(self, breaker: CircuitBreaker):
        assert breaker.state == CircuitState.CLOSED
        assert breaker.backend_name == "test_backend"

    def test_record_success_resets_failures(self, breaker: CircuitBreaker):
        breaker.record_failure()
        breaker.record_failure()
        assert breaker._failure_count == 2
        breaker.record_success()
        assert breaker._failure_count == 0

    def test_trips_open_after_threshold(self, breaker: CircuitBreaker):
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

    @patch("time.monotonic")
    def test_transitions_to_half_open_after_timeout(self, mock_time, breaker: CircuitBreaker):
        mock_time.return_value = 0.0
        
        # Trip the breaker
        for _ in range(3):
            breaker.record_failure()
            
        assert breaker.state == CircuitState.OPEN
        
        # Fast forward time past recovery timeout
        mock_time.return_value = 1.1
        
        # State should automatically evaluate to HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

    @patch("time.monotonic")
    def test_half_open_success_resets_to_closed(self, mock_time, breaker: CircuitBreaker):
        mock_time.return_value = 0.0
        for _ in range(3): breaker.record_failure()
        
        mock_time.return_value = 1.1
        assert breaker.state == CircuitState.HALF_OPEN
        
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED

    @patch("time.monotonic")
    def test_half_open_failure_returns_to_open(self, mock_time, breaker: CircuitBreaker):
        mock_time.return_value = 0.0
        for _ in range(3): breaker.record_failure()
        
        mock_time.return_value = 1.1
        assert breaker.state == CircuitState.HALF_OPEN
        
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN


class TestCircuitBreakerWrapGenerator:
    @pytest.mark.asyncio
    async def test_successful_yielding_records_success(self, breaker: CircuitBreaker):
        breaker.record_failure()  # Add 1 failure
        
        async def _run():
            for e in _make_events("ok"): yield e

        result = [e async for e in breaker.wrap_generator(_run())]
        assert len(result) == 2
        assert breaker._failure_count == 0  # Should be reset

    @pytest.mark.asyncio
    async def test_exception_records_failure(self, breaker: CircuitBreaker):
        async def _run():
            yield AgentEvent(type="text", content="part1")
            raise RuntimeError("API dead")

        with pytest.raises(RuntimeError):
            async for _ in breaker.wrap_generator(_run()):
                pass

        assert breaker._failure_count == 1

    @pytest.mark.asyncio
    async def test_cancelled_error_does_not_record_failure(self, breaker: CircuitBreaker):
        async def _run():
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        with pytest.raises(asyncio.CancelledError):
            async for _ in breaker.wrap_generator(_run()):
                pass

        assert breaker._failure_count == 0  # NOT heavily penalized
        
    @pytest.mark.asyncio
    async def test_fast_fails_when_open(self, breaker: CircuitBreaker):
        for _ in range(3): breaker.record_failure()
        
        async def _run():
            yield AgentEvent(type="text", content="never_reached")

        with pytest.raises(CircuitOpenException) as exc_info:
            async for _ in breaker.wrap_generator(_run()):
                pass
                
        assert "test_backend" in str(exc_info.value)
