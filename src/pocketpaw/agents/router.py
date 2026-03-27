"""Agent Router — registry-based backend selection.

Uses the backend registry to lazily discover and instantiate the
configured agent backend. Supports optional user-configured fallback
backends if the primary backend fails. Integrates Circuit Breakers
and distributed retry policies.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import BackendInfo
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.registry import get_backend_class
from pocketpaw.agents.retry import with_retry
from pocketpaw.agents.circuit_breaker import CircuitBreaker, CircuitOpenException
from pocketpaw.config import Settings

logger = logging.getLogger(__name__)


class AgentRouter:
    """Routes agent requests to the selected backend via the registry."""

    def __init__(self, settings: Settings):
        self.settings = settings

        # Primary backend instance (required by existing tests)
        self._backend = None
        self._active_backend_name: str | None = None

        # Cache for fallback backend instances
        self._fallback_instances: dict[str, Any] = {}
        
        # Protects individual backends from retry storms
        self._circuit_breakers: dict[str, CircuitBreaker] = {}

        # Optional fallback backends
        self._fallback_backends: list[str] = settings.fallback_backends

        self._initialize_backend()

    def _get_circuit_breaker(self, backend_name: str) -> CircuitBreaker:
        """Get or create the circuit breaker for a given backend."""
        if backend_name not in self._circuit_breakers:
            self._circuit_breakers[backend_name] = CircuitBreaker(
                backend_name=backend_name,
                failure_threshold=getattr(self.settings, "agent_circuit_breaker_threshold", 5),
                recovery_timeout=getattr(self.settings, "agent_circuit_breaker_timeout", 60.0),
            )
        return self._circuit_breakers[backend_name]

    def _initialize_backend(self) -> None:
        """Initialize the primary backend."""

        backend_name = self.settings.agent_backend
        cls = get_backend_class(backend_name)

        if cls is None:
            logger.warning(
                "Backend '%s' unavailable — falling back to claude_agent_sdk",
                backend_name,
            )
            cls = get_backend_class("claude_agent_sdk")
            backend_name = "claude_agent_sdk"

        if cls is None:
            logger.error("No agent backend could be loaded")
            self._active_backend_name = None
            return

        try:
            self._backend = cls(self.settings)
            self._active_backend_name = backend_name

            info = cls.info()
            logger.info("🚀 Backend: %s", info.display_name)

        except Exception as exc:
            logger.error("Failed to initialize '%s' backend: %s", backend_name, exc)
            self._active_backend_name = None

    def _get_fallback_backend(self, backend_name: str):
        """Return cached fallback backend or create it."""

        if backend_name in self._fallback_instances:
            return self._fallback_instances[backend_name]

        cls = get_backend_class(backend_name)
        if cls is None:
            return None

        try:
            backend = cls(self.settings)
            self._fallback_instances[backend_name] = backend
            return backend
        except Exception as exc:
            logger.warning(
                "Failed to initialize fallback backend '%s': %s",
                backend_name,
                exc,
            )
            return None

    def _wrap_backend_execution(self, backend: Any, backend_name: str, message: str, **kwargs: Any) -> AsyncIterator[AgentEvent]:
        """Wrap backend execution with Distributed Retry + Circuit Breaker layers."""
        cb = self._get_circuit_breaker(backend_name)

        # Retry config mapping (using getattr for safety if user forgot to config_patch)
        max_retries = getattr(self.settings, "agent_max_retries", 3)
        base_delay = getattr(self.settings, "agent_retry_base_delay", 1.0)
        max_delay = getattr(self.settings, "agent_retry_max_delay", 30.0)
        max_total_retry_time = getattr(self.settings, "agent_max_total_retry_time", 60.0)

        # 1. Innermost layer: Retry wrapper catches transient errors locally.
        retriable_gen = with_retry(
            backend.run, 
            message,
            retry_safe=True,  # Default LLM chats are safe to retry
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=max_delay,
            max_total_retry_time=max_total_retry_time,
            backend_name_for_logs=backend_name,
            **kwargs
        )

        # 2. Outermost layer: Circuit Breaker tracks definitive wins/losses 
        #    after retries. Rejects instantly if OPEN.
        return cb.wrap_generator(retriable_gen)

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent through primary backend -> fallback backends until success."""

        last_error: str | None = None
        run_kwargs = {
            "system_prompt": system_prompt,
            "history": history,
            "session_key": session_key
        }

        # Iteration sequence: [primary] + [fallbacks]
        backends_to_try = []
        if self._backend and self._active_backend_name:
            backends_to_try.append((self._active_backend_name, self._backend))
        
        for name in self._fallback_backends:
            fallback_backend = self._get_fallback_backend(name)
            if fallback_backend:
                backends_to_try.append((name, fallback_backend))

        # Core Routing Pipeline
        for backend_name, backend in backends_to_try:
            try:
                # Returns fully wrapped generator representing this backend's execution.
                gen = self._wrap_backend_execution(backend, backend_name, message, **run_kwargs)
                
                async for event in gen:
                    yield event
                    if event.type == "done":
                        return  # Success, terminate

            except CircuitOpenException:
                # Fast fallback: logged by CircuitBreaker already.
                last_error = f"Circuit breaker OPEN for {backend_name}. Skipping."
                logger.info(last_error)
                continue

            except Exception as exc:
                # Final failure after retries were exhausted (or unretriable error).
                last_error = f"[{backend_name}] Failed definitively: {exc}"
                logger.warning(
                    "Backend '%s' failed in router pipeline: %s", 
                    backend_name, exc,
                    extra={"event": "router_pipeline_failure", "backend": backend_name}
                )

        # If we reach here, all configured backends failed or skipped.
        yield AgentEvent(
            type="error",
            content=last_error or "All configured backends failed",
        )
        yield AgentEvent(type="done", content="")

    async def stop(self) -> None:
        """Stop all backend instances."""

        if self._backend:
            try:
                await self._backend.stop()
            except Exception as exc:
                logger.debug("Error stopping primary backend: %s", exc)

        for backend in self._fallback_instances.values():
            try:
                await backend.stop()
            except Exception as exc:
                logger.debug("Error stopping fallback backend: %s", exc)

    def get_backend_info(self) -> BackendInfo | None:
        """Return metadata about the active backend."""

        if self._backend is None:
            return None

        return self._backend.info()
