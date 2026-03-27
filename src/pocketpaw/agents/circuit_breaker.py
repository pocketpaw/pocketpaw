"""Circuit Breaker for Agent Backends.

Provides a state machine (CLOSED, OPEN, HALF-OPEN) to prevent retry storms
and fail fast when an LLM provider is definitively down. Designed specifically
to wrap async generators (like AgentBackend.run).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from enum import Enum
from typing import Any, Callable

from pocketpaw.agents.protocol import AgentEvent

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """The state of a circuit breaker."""
    CLOSED = "CLOSED"           # Normal operation, requests allowed
    OPEN = "OPEN"               # Failing, requests immediately rejected
    HALF_OPEN = "HALF_OPEN"     # Probing to see if service recovered


class CircuitOpenException(Exception):
    """Raised when a request is attempted while the circuit is OPEN."""
    def __init__(self, backend_name: str) -> None:
        super().__init__(f"Circuit breaker is OPEN for backend: {backend_name}")
        self.backend_name = backend_name


class CircuitBreaker:
    """A distributed systems circuit breaker for an LLM backend."""

    def __init__(
        self,
        backend_name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        """Initialize the circuit breaker.

        Args:
            backend_name: The name of the backend this breaker protects.
            failure_threshold: Number of consecutive failures before tripping OPEN.
            recovery_timeout: Seconds to wait in OPEN before transitioning to HALF-OPEN.
        """
        self.backend_name = backend_name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> CircuitState:
        """Get the current state, handling automatic transition to HALF-OPEN."""
        if self._state == CircuitState.OPEN:
            now = time.monotonic()
            if now - self._last_failure_time >= self._recovery_timeout:
                self._transition_to(CircuitState.HALF_OPEN)
        return self._state

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state and log the change."""
        if self._state == new_state:
            return
        
        old_state = self._state
        self._state = new_state
        logger.warning(
            "CircuitBreaker[%s]: %s -> %s",
            self.backend_name,
            old_state.value,
            new_state.value,
            extra={
                "event": "circuit_breaker_transition",
                "backend": self.backend_name,
                "old_state": old_state.value,
                "new_state": new_state.value,
            },
        )

    def record_success(self) -> None:
        """Record a successful request. Resets the breaker if HALF-OPEN or failures > 0."""
        if self._failure_count > 0 or self._state != CircuitState.CLOSED:
            self._failure_count = 0
            self._transition_to(CircuitState.CLOSED)

    def record_failure(self) -> None:
        """Record a failed request. Trips the breaker if threshold is reached."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            # A single failure in HALF-OPEN immediately trips back to OPEN
            self._transition_to(CircuitState.OPEN)
        elif self._state == CircuitState.CLOSED and self._failure_count >= self._failure_threshold:
            # Reached failure threshold while CLOSED
            self._transition_to(CircuitState.OPEN)

    async def wrap_generator(
        self,
        generator: AsyncIterator[AgentEvent],
    ) -> AsyncIterator[AgentEvent]:
        """Wrap an async generator, applying circuit breaker logic.

        If the circuit is OPEN, immediately raises CircuitOpenException.
        Yields items from the generator. If iteration completes successfully,
        records a success. If an exception escapes the generator, records a failure.
        
        Note: Cancellation (asyncio.CancelledError) is NOT recorded as a failure
        as it represents client-side disconnects, not backend unhealthiness.
        """
        current_state = self.state

        if current_state == CircuitState.OPEN:
            # Fail fast without hitting the network
            raise CircuitOpenException(self.backend_name)

        # In HALF-OPEN, we ideally only want 1 concurrent request to probe.
        # But for an agent backend, typically only a few concurrent streams happen.
        # We allow it through. If it fails, it trips OPEN immediately.
        
        try:
            async for event in generator:
                yield event
            # Fully consumed without errors -> success!
            self.record_success()
        except asyncio.CancelledError:
            # Client disconnected mid-stream or task cancelled; not a backend fault.
            raise
        except Exception:
            # Any escaped error implies the backend failed definitively
            # (transient errors would have been caught/retried by with_retry).
            self.record_failure()
            raise
