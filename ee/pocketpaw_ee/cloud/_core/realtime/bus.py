"""EventBus protocol and the in-process implementation.

Services call ``emit(event)`` (see ``emit.py``), which delegates to the active
bus. ``InProcessBus.publish`` does three things, in order:

  1. Resolves the audience via ``AudienceResolver`` and delivers to the sockets
     THIS process holds through ``ConnectionManager.send_to_user``. The fan-out
     is concurrent and capped (``fanout.map_bounded``) because publish runs
     inline in the emitting request; a serial loop would charge that request
     the sum of every recipient's send latency.
  2. When cross-process broadcast is on (``POCKETPAW_REALTIME_BUS=redis-streams``,
     see ``broadcast.py``), relays the same frame and audience to every other
     web process so they reach the sockets they hold. Off, this is one bool check.
  3. Runs the in-process handlers registered via ``subscribe``. These are
     NEVER relayed: handlers carry side effects (agent runs, push, calendar
     writes) that must fire once, on the process that published. Each
     handler's failure is logged and contained.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from pocketpaw_ee.cloud._core.realtime import broadcast
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.broadcast import Channel
from pocketpaw_ee.cloud._core.realtime.events import Event
from pocketpaw_ee.cloud._core.realtime.fanout import map_bounded

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

    def __init__(
        self, *, resolver: AudienceResolver, conn_manager, channel: Channel | None = None
    ) -> None:
        self._resolver = resolver
        self._conn = conn_manager
        # Explicit channel for tests that simulate several processes in one;
        # otherwise the process's own channel, present only when broadcast is on.
        self._channel = channel
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

            async def _deliver(uid: str) -> None:
                # Containment stays per recipient, exactly as the serial loop
                # had it: one unreachable member must not abort delivery to the
                # rest. Keeping the try INSIDE the coroutine (rather than
                # reaching for gather's return_exceptions) also keeps
                # cancellation honest — see fanout.map_bounded.
                try:
                    await self._conn.send_to_user(uid, payload)
                except Exception:
                    logger.warning(
                        "ws send failed; user=%s event=%s", uid, event.type, exc_info=True
                    )

            # Concurrent, capped. The old serial loop charged the emitting
            # request the sum of every member's send latency; a single
            # back-pressured socket burning its full 5s timeout therefore
            # delayed every member after it in the list.
            await map_bounded(list(audience), _deliver)

            # Other web processes hold the rest of the audience's sockets.
            channel = self._channel or broadcast.active_channel()
            if channel is not None:
                await channel.publish_group(
                    _relay_scope(event),
                    list(audience),
                    event.type,
                    payload.model_dump(mode="json")["data"],
                )

        # Local in-process handlers — run regardless of WebSocket audience so
        # bus listeners (e.g. the upload indexer) fire even when no client is
        # subscribed. Each handler's failure is contained.
        for handler in self._handlers.get(event.type, []):
            try:
                await handler(event)
            except Exception:
                logger.exception("local handler failed for event %s", event.type)


def _relay_scope(event: Event) -> str:
    """The scope a relayed frame is ordered within on the receiving process,
    read the same way ``xproc`` reads a bus envelope's lane."""
    # Lazy: xproc imports this module at load time.
    from pocketpaw_ee.cloud._core.realtime.xproc import _BUS_SCOPE_FIELDS

    data = event.data if isinstance(event.data, dict) else {}
    for field in _BUS_SCOPE_FIELDS:
        value = data.get(field)
        if value:
            return str(value)
    return event.type


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
