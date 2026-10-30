"""InMemoryStreamTransport — Tier 0 dev fallback when Redis is unset."""

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

pytestmark = pytest.mark.asyncio


async def test_append_then_read_yields_in_order():
    t = InMemoryStreamTransport()
    e1 = await t.append_event("r1", "chunk", {"i": 1})
    e2 = await t.append_event("r1", "chunk", {"i": 2})
    e3 = await t.append_event("r1", "stream_end", {"assistant_message_id": "m"})

    events = [ev async for ev in t.read_events("r1", after="0", block_ms=10)]
    assert [ev.entry_id for ev in events] == [e1, e2, e3]
    assert events[-1].is_terminal


async def test_read_after_cursor_skips_already_seen():
    t = InMemoryStreamTransport()
    e1 = await t.append_event("r1", "chunk", {"i": 1})
    await t.append_event("r1", "chunk", {"i": 2})
    await t.append_event("r1", "stream_end", {})

    events = [ev async for ev in t.read_events("r1", after=e1, block_ms=10)]
    assert [ev.data for ev in events] == [{"i": 2}, {}]


async def test_read_blocks_then_returns_on_terminal():
    t = InMemoryStreamTransport()

    async def produce():
        await asyncio.sleep(0.05)
        await t.append_event("r1", "chunk", {"i": 1})
        await asyncio.sleep(0.05)
        await t.append_event("r1", "stream_end", {})

    produce_task = asyncio.create_task(produce())
    events = [ev async for ev in t.read_events("r1", after="0", block_ms=500)]
    await produce_task

    assert len(events) == 2
    assert events[-1].is_terminal


async def test_read_returns_on_timeout_without_events():
    t = InMemoryStreamTransport()
    events = [ev async for ev in t.read_events("r1", after="0", block_ms=20)]
    assert events == []


async def test_cancel_flag_isolated_per_run():
    t = InMemoryStreamTransport()
    await t.request_cancel("r1")
    assert await t.is_cancelled("r1") is True
    assert await t.is_cancelled("r2") is False


async def test_stream_exists_tracks_appends():
    t = InMemoryStreamTransport()
    assert await t.stream_exists("r1") is False
    await t.append_event("r1", "chunk", {})
    assert await t.stream_exists("r1") is True


async def test_set_ttl_evicts_after_delay():
    t = InMemoryStreamTransport()
    await t.append_event("r1", "stream_end", {})
    await t.set_ttl("r1", 0)  # asyncio.sleep(0) yields once
    await asyncio.sleep(0.01)
    assert await t.stream_exists("r1") is False


async def test_concurrent_reader_wakes_on_append():
    """Regression for the clear-then-check race: an append landing between
    drain-end and event.clear() must not be lost."""
    t = InMemoryStreamTransport()

    async def reader():
        return [ev async for ev in t.read_events("r1", after="0", block_ms=200)]

    reader_task = asyncio.create_task(reader())
    # Let the reader hit its first wait.
    await asyncio.sleep(0.01)
    await t.append_event("r1", "stream_end", {})

    events = await reader_task
    assert len(events) == 1 and events[0].is_terminal
