"""Agent backend retry utility — exponential backoff for transient LLM errors.

This module provides a backend-agnostic ``with_retry`` async generator wrapper
that sits between ``AgentRouter`` and each ``AgentBackend.run()`` call.

Design goals:
* Global Time Budget: Strictly bound retries by elapsed time.
* Full Jitter: Spreads load effectively based on AWS best practices.
* Observability: Structured logging for external metrics systems.
* Idempotency: Supports a `retry_safe` flag for potentially non-idempotent future calls.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from pocketpaw.agents.protocol import AgentEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transient error classifier
# ---------------------------------------------------------------------------

#: HTTP status codes that are safe to retry.
_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({
    429,  # Too Many Requests / rate-limit
    500,  # Internal Server Error (sometimes transient)
    502,  # Bad Gateway
    503,  # Service Unavailable
    529,  # Anthropic-specific "Overloaded"
})

#: Substrings searched in ``str(exc)`` when we cannot inspect the status code.
_RETRYABLE_SUBSTRINGS: tuple[str, ...] = (
    "529",
    "overloaded",
    "rate limit",
    "rate_limit",
    "too many requests",
    "service unavailable",
    "temporarily unavailable",
    "connection reset",
    "connection timeout",
    "read timeout",
    "timed out",
)

#: Status codes that are *permanent* failures — do NOT retry.
_PERMANENT_STATUS_CODES: frozenset[int] = frozenset({
    400,  # Bad Request (e.g. context too long)
    401,  # Unauthorized / bad API key
    403,  # Forbidden
    404,  # Not Found
    422,  # Unprocessable Entity
})


def is_transient_error(exc: BaseException) -> bool:  # noqa: C901
    """Return True if *exc* represents a transient error worth retrying."""
    # Never retry cancellation or keyboard interrupt.
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
        return False

    # ---- httpx ----------------------------------------------------------- #
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in _PERMANENT_STATUS_CODES:
                return False
            return code in _RETRYABLE_STATUS_CODES
        if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError)):
            return True
        if isinstance(exc, httpx.TimeoutException):
            return True
    except ImportError:
        pass

    # ---- anthropic ------------------------------------------------------- #
    try:
        import anthropic

        if isinstance(exc, anthropic.APIStatusError):
            code = exc.status_code
            if code in _PERMANENT_STATUS_CODES:
                return False
            return code in _RETRYABLE_STATUS_CODES
        if isinstance(exc, anthropic.APIConnectionError):
            return True
    except ImportError:
        pass

    # ---- openai ---------------------------------------------------------- #
    try:
        import openai

        if isinstance(exc, openai.APIStatusError):
            code = exc.status_code
            if code in _PERMANENT_STATUS_CODES:
                return False
            return code in _RETRYABLE_STATUS_CODES
        if isinstance(exc, openai.APIConnectionError):
            return True
        if isinstance(exc, openai.APITimeoutError):
            return True
    except ImportError:
        pass

    # ---- stdlib ---------------------------------------------------------- #
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, (ConnectionResetError, ConnectionError, TimeoutError)):
        return True

    # ---- substring fallback ---------------------------------------------- #
    exc_str = str(exc).lower()
    return any(substr in exc_str for substr in _RETRYABLE_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------

async def with_retry(
    run_fn: Callable[..., AsyncIterator[AgentEvent]],
    /,
    *args: Any,
    retry_safe: bool = True,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    max_total_retry_time: float = 60.0,
    backend_name_for_logs: str = "unknown",
    **kwargs: Any,
) -> AsyncIterator[AgentEvent]:
    """Wrap an ``AgentBackend.run()`` call with distributed-safe exponential backoff.

    Features:
    * **Full Jitter**: Calculates backoff as ``random.uniform(0, min(max_delay, base_delay * 2**attempt))``.
    * **Global Budget**: Preemptively aborts if the required backoff exceeds ``max_total_retry_time``.
    * **Idempotency Check**: If ``retry_safe=False``, skips transient retries entirely.
    * **Observability**: Structured logging via ``extra={}`` for easy metric exporting without spamming text logs.

    Args:
        run_fn: The async generator factory to wrap.
        *args: Forwarded to ``run_fn``.
        retry_safe: If False, skips all retries (fallback immediately).
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Initial backoff delay (seconds).
        max_delay: Maximum single backoff delay cap (seconds).
        max_total_retry_time: Maximum total time (seconds) to spend retrying across all attempts.
        backend_name_for_logs: Identifier for observability hooks.
        **kwargs: Forwarded to ``run_fn``.

    Yields:
        AgentEvents, including "thinking" events during retries.
    """
    attempt = 0
    start_time = time.monotonic()

    while True:
        loop_start = time.monotonic()
        try:
            async for event in run_fn(*args, **kwargs):
                yield event
            
            # Record Success Metrics
            if attempt > 0:
                logger.info(
                    "Backend recovered after %d retries", attempt,
                    extra={
                        "event": "retry_recovered",
                        "backend": backend_name_for_logs,
                        "attempts_used": attempt,
                        "latency_ms": int((time.monotonic() - start_time) * 1000)
                    }
                )
            return

        except asyncio.CancelledError:
            raise  # never swallow cancellation
        except Exception as exc:
            elapsed_total = time.monotonic() - start_time
            latency_ms = int((time.monotonic() - loop_start) * 1000)

            if not retry_safe:
                logger.debug("Request declared not retry-safe; skipping retries.")
                raise

            if not is_transient_error(exc):
                # Permanent failure
                logger.debug(
                    "Non-transient error from backend: %s", exc,
                    extra={
                        "event": "backend_error_permanent",
                        "backend": backend_name_for_logs,
                        "error_type": type(exc).__name__,
                        "latency_ms": latency_ms
                    }
                )
                raise

            if attempt >= max_retries:
                logger.warning(
                    "Max retries (%d) exhausted", max_retries,
                    extra={
                        "event": "retry_exhausted_attempts",
                        "backend": backend_name_for_logs,
                        "error_type": type(exc).__name__,
                    }
                )
                raise

            # Calculate AWS-style Full Jitter
            # see: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
            cap = min(max_delay, base_delay * (2 ** attempt))
            wait = random.uniform(0.0, cap)

            # Global Time Budget check
            if elapsed_total + wait > max_total_retry_time:
                logger.warning(
                    "Retry time budget exceeded (elapsed=%.1f, wait=%.1f, max=%.1f)",
                    elapsed_total, wait, max_total_retry_time,
                    extra={
                        "event": "retry_exhausted_budget",
                        "backend": backend_name_for_logs,
                        "elapsed_total": elapsed_total,
                        "intended_wait": wait,
                        "budget": max_total_retry_time
                    }
                )
                # Re-raise the exception since we don't have time to retry
                raise

            attempt += 1
            logger.warning(
                "Transient error [%s] — retrying %s (attempt %d, wait %.2fs)",
                type(exc).__name__, backend_name_for_logs, attempt, wait,
                extra={
                    "event": "retry_triggered",
                    "backend": backend_name_for_logs,
                    "attempt": attempt,
                    "wait_s": wait,
                    "error_type": type(exc).__name__,
                }
            )

            # Emit a thinking event so the Activity panel shows progress
            yield AgentEvent(
                type="thinking",
                content=f"Retrying… (attempt {attempt}/{max_retries}, waiting {wait:.1f}s)",
            )

            await asyncio.sleep(wait)
