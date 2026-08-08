# tests/test_bus_inbound_observers.py
# Created: 2026-08-08 (feat/coupling-t9-channels-in-chat).
#
# The inbound OBSERVER seam on the OSS MessageBus.
#
# Inbound is a QUEUE, not a pub/sub: ``consume_inbound`` pops, so anything that
# "subscribed" to inbound the way ``subscribe_outbound`` works would compete
# with the agent loop and steal messages. The observer seam exists so a
# consumer-of-record (the agent loop) and a watcher (the EE channel↔chat
# mirror) can coexist.
#
# The load-bearing property is NEGATIVE: observing must not change delivery.
# Every test here is really one assertion — the agent still gets its message.

from __future__ import annotations

import asyncio

import pytest

from pocketpaw.bus.events import Channel, InboundMessage
from pocketpaw.bus.queue import MessageBus


def _msg(content: str = "hi", **kw) -> InboundMessage:
    return InboundMessage(
        channel=Channel.TELEGRAM,
        sender_id="sender-1",
        chat_id="chat-1",
        content=content,
        **kw,
    )


async def test_observer_sees_the_message():
    """The basic contract: a registered observer is handed each inbound."""
    bus = MessageBus()
    seen: list[InboundMessage] = []

    async def _obs(m: InboundMessage) -> None:
        seen.append(m)

    bus.subscribe_inbound_observer(_obs)
    await bus.publish_inbound(_msg("hello"))

    assert [m.content for m in seen] == ["hello"]


async def test_observing_does_not_consume():
    """THE POINT OF THE SEAM. After an observer has run, the message is still
    in the queue for the agent loop to consume.

    MUTATION THAT BREAKS THIS: have ``_notify_inbound_observers`` pop from
    ``self._inbound`` (or move the observer fan-out to ``consume_inbound``)
    — ``consume_inbound`` returns None and this fails."""
    bus = MessageBus()
    seen: list[InboundMessage] = []

    async def _obs(m: InboundMessage) -> None:
        seen.append(m)

    bus.subscribe_inbound_observer(_obs)
    await bus.publish_inbound(_msg("hello"))

    assert bus.inbound_pending() == 1
    delivered = await bus.consume_inbound(timeout=0.5)
    assert delivered is not None and delivered.content == "hello"
    assert seen, "the observer should also have seen it"


async def test_a_raising_observer_never_breaks_delivery():
    """An observer is best-effort. A broken one must not propagate into the
    adapter's receive path, and must not starve the agent of the message.

    MUTATION THAT BREAKS THIS: remove the try/except in ``_safe_notify`` —
    ``publish_inbound`` raises and the first assert never runs."""
    bus = MessageBus()

    async def _boom(_m: InboundMessage) -> None:
        raise RuntimeError("observer is broken")

    bus.subscribe_inbound_observer(_boom)

    await bus.publish_inbound(_msg("still delivered"))  # must not raise

    delivered = await bus.consume_inbound(timeout=0.5)
    assert delivered is not None and delivered.content == "still delivered"


async def test_one_broken_observer_does_not_starve_the_others():
    """Per-observer isolation, matching ``publish_outbound``'s fan-out."""
    bus = MessageBus()
    seen: list[str] = []

    async def _boom(_m: InboundMessage) -> None:
        raise RuntimeError("nope")

    async def _ok(m: InboundMessage) -> None:
        seen.append(m.content)

    bus.subscribe_inbound_observer(_boom)
    bus.subscribe_inbound_observer(_ok)
    await bus.publish_inbound(_msg("fan-out"))

    assert seen == ["fan-out"]


async def test_observers_cannot_mutate_what_the_next_one_sees():
    """Each observer gets its own deep copy of the mutable fields.

    MUTATION THAT BREAKS THIS: pass ``message`` straight to every callback
    instead of the ``replace(...)`` copy — the second observer sees the first
    one's mutation and the assert fails."""
    bus = MessageBus()
    second: list[dict] = []

    async def _mutator(m: InboundMessage) -> None:
        m.metadata["injected"] = True

    async def _reader(m: InboundMessage) -> None:
        second.append(dict(m.metadata))

    bus.subscribe_inbound_observer(_mutator)
    bus.subscribe_inbound_observer(_reader)
    await bus.publish_inbound(_msg("x", metadata={"original": 1}))

    assert second == [{"original": 1}], "an observer's mutation leaked sideways"


async def test_unsubscribe_stops_delivery_and_is_safe_when_absent():
    """Unsubscribe-first idempotent registration (the shape every EE bridge
    uses) requires removing an absent callback to be a silent no-op."""
    bus = MessageBus()
    seen: list[str] = []

    async def _obs(m: InboundMessage) -> None:
        seen.append(m.content)

    bus.unsubscribe_inbound_observer(_obs)  # absent: must not raise
    bus.subscribe_inbound_observer(_obs)
    await bus.publish_inbound(_msg("first"))
    bus.unsubscribe_inbound_observer(_obs)
    await bus.publish_inbound(_msg("second"))

    assert seen == ["first"]


async def test_no_observers_is_the_untouched_path():
    """A bus with no observers behaves exactly as it did before the seam —
    the OSS default and every existing test's assumption."""
    bus = MessageBus()
    await bus.publish_inbound(_msg("plain"))

    assert bus.inbound_pending() == 1
    got = await bus.consume_inbound(timeout=0.5)
    assert got is not None and got.content == "plain"


@pytest.mark.parametrize("count", [1, 5])
async def test_every_published_message_reaches_the_observer_in_order(count: int):
    """No drops, no reordering — the mirror files rows in conversation order."""
    bus = MessageBus()
    seen: list[str] = []

    async def _obs(m: InboundMessage) -> None:
        seen.append(m.content)

    bus.subscribe_inbound_observer(_obs)
    for i in range(count):
        await bus.publish_inbound(_msg(f"m{i}"))
        await asyncio.sleep(0)

    assert seen == [f"m{i}" for i in range(count)]
