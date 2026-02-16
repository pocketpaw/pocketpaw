"""Coordinated singleton lifecycle management.

Provides a central registry for singletons that need graceful shutdown
and/or test-time reset. Typical flow:

* Modules create their singletons and immediately call ``register()``
  with optional ``shutdown`` and/or ``reset`` callbacks.
* Application shutdown paths (e.g. FastAPI lifespan handlers, CLI teardown)
  call ``shutdown_all()`` once to gracefully stop all registered components.
* Test suites call ``reset_all()`` between tests to clear cached instances
  so subsequent ``get_*()`` helpers recreate fresh singletons.

Created: 2026-02-12
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Registry: name → (shutdown_callback_or_None, reset_callback_or_None)
_registry: dict[str, tuple[Callable | None, Callable | None]] = {}


def register(
    name: str,
    *,
    shutdown: Callable[[], Any] | None = None,
    reset: Callable[[], Any] | None = None,
) -> None:
    """Register a singleton's lifecycle callbacks.

    Subsequent calls with the same ``name`` overwrite any existing entry,
    allowing modules to re-register updated callbacks during tests.

    Args:
        name: Unique identifier (e.g. ``"scheduler"``, ``"mcp_manager"``).
        shutdown: Async or sync callable for graceful teardown.
        reset: Sync callable to clear the singleton (for tests).
    """
    _registry[name] = (shutdown, reset)


async def shutdown_all() -> None:
    """Gracefully shut down all registered singletons.

    Iterates over the current registry and invokes each ``shutdown``
    callback, awaiting it when a coroutine is returned. Errors are logged
    but do not prevent remaining shutdown callbacks from running. The
    registry is left intact so that ``reset_all()`` or subsequent calls
    can still see what was registered.
    """
    for name, (shutdown_cb, _) in list(_registry.items()):
        if shutdown_cb is None:
            continue
        try:
            result = shutdown_cb()
            if asyncio.iscoroutine(result):
                await result
            logger.debug("Shut down %s", name)
        except Exception:
            logger.warning("Error shutting down %s", name, exc_info=True)


def reset_all() -> None:
    """Reset all registered singletons to their initial state.

    Intended primarily for test teardown — invokes each ``reset`` callback
    (when provided) and then clears the registry so the next ``get_*()``
    helper call recreates fresh instances.
    """
    for name, (_, reset_cb) in list(_registry.items()):
        if reset_cb is None:
            continue
        try:
            reset_cb()
            logger.debug("Reset %s", name)
        except Exception:
            logger.warning("Error resetting %s", name, exc_info=True)
    _registry.clear()
