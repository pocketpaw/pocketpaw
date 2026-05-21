# bus.py — EventBus protocol and InProcessBus implementation.
# Updated: 2026-04-30 — Added in-process subscriber API (Stage 1.B of
#   "Files as Knowledge"). publish() now fans out to local handlers as well
#   as WebSocket clients. Failures are isolated per-handler so one bad
#   listener can't block the rest of the dispatch.
"""EventBus protocol and in-process implementation.

Services call ``emit(event)`` (see ``emit.py``) which delegates to the active
bus. The default ``InProcessBus`` does two things:

  1. Resolves the audience via ``AudienceResolver`` and fans out through
     ``ConnectionManager.send_to_user`` (existing WebSocket path).
  2. Calls any in-process handlers registered via ``subscribe(event_type, h)``.

In-process handlers were added in Stage 1.B of the Files-as-Knowledge plan
so the upload pipeline can wire a ``FileReady`` listener without leaving
the bus singleton or pulling in an extra event runtime.

A future ``RedisBus`` (Task 33) will use the same protocol so call sites are
unaffected.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.events import Event

logger = logging.getLogger(__name__)


# An in-process handler accepts the published Event and runs async. The
# concrete handler may narrow the parameter type; we type the registry
# loosely so the bus doesn't need a generic per-event-class registry.
Handler = Callable[[Event], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...

    def subscribe(self, event_type: str, handler: Handler) -> None: ...


class InProcessBus:
    """Fan out events to sockets on the same process.

    Also supports local in-process subscribers registered via
    :meth:`subscribe`. Subscribers are keyed by ``event.type`` (the literal
    string set by each :class:`Event` subclass) and are invoked after the
    WebSocket fan-out. Each subscriber's exception is logged and swallowed
    so one broken handler does not stop the others.
    """

    def __init__(self, *, resolver: AudienceResolver, conn_manager) -> None:
        self._resolver = resolver
        self._conn = conn_manager
        self._handlers: dict[str, list[Handler]] = {}

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register an in-process handler for the given event type.

        ``event_type`` must match the literal string set by the matching
        :class:`Event` subclass (e.g. ``"file.ready"``). Multiple handlers
        per type are allowed and run in registration order.
        """
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: Event) -> None:
        # WsOutbound is imported lazily: ee.cloud.chat.schemas is the lowest
        # reachable node that also sits on the message-send import chain, so
        # pytest collection orderings that load services before realtime can
        # see a partially-initialised bus if we import at module top. Tested:
        # reverts to ImportError under pytest collection of test_bus.py.
        from pocketpaw_ee.cloud.chat.schemas import WsOutbound

        try:
            audience = await self._resolver.audience(event)
        except Exception:
            logger.exception("audience resolution failed for event %s", event.type)
            audience = []

        if audience:
            payload = WsOutbound(type=event.type, data=event.data)
            for uid in audience:
                try:
                    await self._conn.send_to_user(uid, payload)
                except Exception:
                    logger.warning(
                        "ws send failed; user=%s event=%s", uid, event.type, exc_info=True
                    )

        # Local in-process handlers — run regardless of WebSocket audience so
        # bus listeners (e.g. the upload indexer) fire even when no client is
        # subscribed. Each handler's failure is contained.
        for handler in self._handlers.get(event.type, []):
            try:
                await handler(event)
            except Exception:
                logger.exception("local handler failed for event %s", event.type)


# --- module-level singleton ---------------------------------------------------

_bus: EventBus | None = None


def set_bus(bus: EventBus) -> None:
    global _bus
    _bus = bus


def get_bus() -> EventBus:
    assert _bus is not None, "EventBus not initialized — call init_realtime()"
    return _bus


_resolver: AudienceResolver | None = None


def set_resolver(resolver: AudienceResolver) -> None:
    global _resolver
    _resolver = resolver


def get_resolver() -> AudienceResolver:
    assert _resolver is not None, "AudienceResolver not initialized — call init_realtime()"
    return _resolver
