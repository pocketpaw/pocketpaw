"""Tests for the internal async event bus."""

from __future__ import annotations

from ee.cloud.shared.events import EventBus, event_bus


async def test_subscribe_and_emit() -> None:
    """Subscribe a handler, emit an event, verify handler called with data."""
    bus = EventBus()
    received: list[dict] = []

    async def handler(data: dict) -> None:
        received.append(data)

    bus.subscribe("user.created", handler)
    await bus.emit("user.created", {"user_id": "u1"})

    assert len(received) == 1
    assert received[0] == {"user_id": "u1"}


async def test_multiple_handlers() -> None:
    """Two handlers on same event, both called in order."""
    bus = EventBus()
    order: list[str] = []

    async def first(data: dict) -> None:
        order.append("first")

    async def second(data: dict) -> None:
        order.append("second")

    bus.subscribe("invite.accepted", first)
    bus.subscribe("invite.accepted", second)
    await bus.emit("invite.accepted", {"invite_id": "inv1"})

    assert order == ["first", "second"]


async def test_emit_unknown_event_does_nothing() -> None:
    """Emitting an event with no handlers should not raise."""
    bus = EventBus()
    await bus.emit("nonexistent.event", {"key": "value"})


async def test_unsubscribe() -> None:
    """Subscribe then unsubscribe; emit should not call the handler."""
    bus = EventBus()
    called = False

    async def handler(data: dict) -> None:
        nonlocal called
        called = True

    bus.subscribe("room.deleted", handler)
    bus.unsubscribe("room.deleted", handler)
    await bus.emit("room.deleted", {"room_id": "r1"})

    assert called is False


async def test_handler_error_does_not_stop_others() -> None:
    """First handler raises, second handler still called."""
    bus = EventBus()
    results: list[str] = []

    async def failing_handler(data: dict) -> None:
        raise RuntimeError("boom")

    async def good_handler(data: dict) -> None:
        results.append("ok")

    bus.subscribe("msg.sent", failing_handler)
    bus.subscribe("msg.sent", good_handler)
    await bus.emit("msg.sent", {"msg_id": "m1"})

    assert results == ["ok"]


async def test_module_level_singleton() -> None:
    """The module-level event_bus is an EventBus instance."""
    assert isinstance(event_bus, EventBus)
