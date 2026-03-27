"""Tests for the agent backend retry utility.

Validates distributed reliability features: Full Jitter, Global Time Budgets, and transient classifications.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch
import time

import pytest

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.retry import is_transient_error, with_retry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_events(*contents: str) -> list[AgentEvent]:
    events = [AgentEvent(type="text", content=c) for c in contents]
    events.append(AgentEvent(type="done", content=""))
    return events


async def _collect(gen: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in gen]


class TestIsTransientError:
    def test_cancelled_error_is_not_transient(self):
        assert is_transient_error(asyncio.CancelledError()) is False

    def test_timeout_error_is_transient(self):
        assert is_transient_error(TimeoutError("timed out")) is True

    def test_auth_401_substring(self):
        assert is_transient_error(Exception("403 forbidden")) is False

    def test_substring_529_in_message(self):
        assert is_transient_error(Exception("Error 529 overloaded")) is True
        
    def test_httpx_status_429_is_transient(self):
        try:
            import httpx
            request = httpx.Request("GET", "https://api.anthropic.com/")
            response = httpx.Response(429, request=request)
            exc = httpx.HTTPStatusError("429", request=request, response=response)
            assert is_transient_error(exc) is True
        except ImportError:
            pytest.skip("httpx not installed")

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_no_retry_on_success(self):
        """Backend succeeds on the first try — no retries."""
        async def _run(*_, **__):
            for e in _make_events("Hello"):
                yield e

        result = await _collect(with_retry(_run, max_retries=3, base_delay=0.0))
        assert [e.content for e in result if e.type == "text"] == ["Hello"]

    @pytest.mark.asyncio
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_on_transient_error_then_success(self, mock_sleep):
        """Backend fails transiently once, then succeeds."""
        attempts = {"count": 0}

        async def _run(*_, **__):
            if attempts["count"] == 0:
                attempts["count"] += 1
                raise Exception("rate limit exceeded")
            for e in _make_events("ok"):
                yield e

        result = await _collect(with_retry(_run, max_retries=3, base_delay=0.0, max_delay=30.0))
        assert mock_sleep.call_count == 1
        text_events = [e.content for e in result if e.type == "text"]
        assert text_events == ["ok"]

    @pytest.mark.asyncio
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_not_retry_safe(self, mock_sleep):
        """If retry_safe=False, it skips transient retries entirely."""
        async def _run(*_, **__):
            raise Exception("rate limit exceeded")
            yield  # pragma: no cover

        with pytest.raises(Exception, match="rate limit exceeded"):
            await _collect(with_retry(_run, retry_safe=False, max_retries=3))
        
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    @patch("time.monotonic", side_effect=[0.0, 0.1, 0.1, 0.1, 0.1])
    @patch("asyncio.sleep", new_callable=AsyncMock)
    async def test_global_budget_exhaustion(self, mock_sleep, mock_time):
        """If required wait pushes past global budget, abort immediately."""
        async def _run(*_, **__):
            raise Exception("service unavailable")
            yield  # pragma: no cover

        with patch("random.uniform", return_value=5.0):  # Forcing the wait to be 5s
            with pytest.raises(Exception, match="service unavailable"):
                # Global budget is 2s, but the wait is 5s. Exceeds!
                await _collect(with_retry(_run, max_retries=3, max_total_retry_time=2.0))
        
        # Aborted before sleeping
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_jitter_distribution(self):
        """Wait times should be randomly uniformly distributed up to AWS Full Jitter cap."""
        async def _run(*_, **__):
            raise Exception("timed out")
            yield  # pragma: no cover
            
        sleep_calls = []
        async def _mock_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            
        with patch("pocketpaw.agents.retry.asyncio.sleep", side_effect=_mock_sleep):
            # We mock time.monotonic so the global budget doesn't expire
            with patch("time.monotonic", return_value=1.0):
                # Don't mock random, let it run 
                with pytest.raises(Exception):
                    await _collect(with_retry(_run, max_retries=4, base_delay=1.0, max_delay=3.0, max_total_retry_time=99.0))
                    
        assert len(sleep_calls) == 4
        # At attempt 0: base_delay * 2^0 = 1.0. random.uniform(0, 1) -> cap 1.0
        assert 0.0 <= sleep_calls[0] <= 1.0
        # At attempt 1: base_delay * 2^1 = 2.0. cap 2.0
        assert 0.0 <= sleep_calls[1] <= 2.0
        # At attempt 2: base_delay * 2^2 = 4.0. max_delay is 3.0. cap = 3.0
        assert 0.0 <= sleep_calls[2] <= 3.0
        # At attempt 3 cap = 3.0
        assert 0.0 <= sleep_calls[3] <= 3.0
