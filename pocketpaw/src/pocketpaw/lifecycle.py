"""Coordinated singleton lifecycle management.

Provides a central registry for singletons that need graceful shutdown
and/or test-time reset. Modules register their cleanup callbacks via
``register()``, and the app teardown path calls ``shutdown_all()``.

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

    Args:
        name: Unique identifier (e.g. ``"scheduler"``, ``"mcp_manager"``).
        shutdown: Async or sync callable for graceful teardown.
        reset: Sync callable to clear the singleton (for tests).
    """
    _registry[name] = (shutdown, reset)


async def shutdown_all() -> None:
    """Gracefully shut down all registered singletons.

    Calls each registered shutdown callback, awaiting async ones.
    Errors are logged but don't prevent other shutdowns from running.
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

    Intended for test teardown — clears cached instances so the next
    ``get_*()`` call creates a fresh one.
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
