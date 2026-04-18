"""Internal async event bus for cross-domain side effects.

Provides a simple in-process pub/sub so that domains can react to events
from other domains without importing each other directly.  For example,
an "invite.accepted" event can trigger notification creation and
auto-adding a user to a group — all without the invite domain knowing
about notifications or groups.

Usage::

    from ee.cloud.shared.events import event_bus

    async def on_invite_accepted(data: dict[str, Any]) -> None:
        await create_notification(data["user_id"], ...)

    event_bus.subscribe("invite.accepted", on_invite_accepted)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class EventBus:
    """Simple in-process async pub/sub event bus."""

    def __init__(self) -> None:
        self._handlers: defaultdict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler) -> None:
        """Register *handler* to be called when *event* is emitted."""
        self._handlers[event].append(handler)

    def unsubscribe(self, event: str, handler: Handler) -> None:
        """Remove *handler* from *event*.  No-op if not subscribed."""
        try:
            self._handlers[event].remove(handler)
        except ValueError:
            pass

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        """Call every handler registered for *event*.

        Each handler is awaited in subscription order.  If a handler raises,
        the exception is logged and the remaining handlers still run.
        """
        for handler in self._handlers[event]:
            try:
                await handler(data)
            except Exception:
                logger.exception(
                    "Event handler %s failed for event %r",
                    getattr(handler, "__name__", handler),
                    event,
                )


# Module-level singleton used throughout the cloud module.
event_bus = EventBus()
