"""Agent Router — registry-based backend selection.

Uses the backend registry to lazily discover and instantiate the
configured agent backend. Supports optional user-configured fallback
backends if the primary backend fails.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import BackendInfo
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.agents.registry import get_backend_class
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

        # Optional fallback backends
        self._fallback_backends: list[str] = settings.fallback_backends

        self._initialize_backend()

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

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent with optional fallback backends."""

        last_error: str | None = None

        # Primary backend (streaming, no buffering, no error-event fallback)
        if self._backend is not None:
            try:
                async for event in self._backend.run(
                    message,
                    system_prompt=system_prompt,
                    history=history,
                    session_key=session_key,
                ):
                    yield event

                    if event.type == "done":
                        return

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Primary backend '%s' failed: %s",
                    self._active_backend_name,
                    exc,
                )

        # Fallback backends
        for backend_name in self._fallback_backends:
            backend = self._get_fallback_backend(backend_name)

            if backend is None:
                logger.warning("Fallback backend '%s' unavailable", backend_name)
                continue

            logger.info("Attempting fallback backend: %s", backend_name)

            try:
                async for event in backend.run(
                    message,
                    system_prompt=system_prompt,
                    history=history,
                    session_key=session_key,
                ):
                    yield event

                    if event.type == "done":
                        return

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Fallback backend '%s' failed: %s",
                    backend_name,
                    exc,
                )

        # All backends failed
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
