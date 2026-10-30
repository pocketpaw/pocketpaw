# tests/cloud/test_activity_buffer.py
# Created: 2026-05-13 (feat/mission-control-facade) — coverage for the
# per-workspace activity ring buffer that feeds Mission Control's live
# ticker. Asserts push, ordering, eviction, TTL pruning, and synchronous
# fan-out to subscribers (used by the SSE bridge + tests).

from __future__ import annotations

import time

import pytest
from pocketpaw_ee.cloud._core.realtime.events import AgentThinking, AgentToolUse
from pocketpaw_ee.cloud.activity.buffer import (
    ActivityEvent,
    Buffer,
    _handle_agent_event,
    get_buffer,
)


def _ev(
    workspace_id: str = "w1",
    kind: str = "tool_call",
    ts: float | None = None,
) -> ActivityEvent:
    return ActivityEvent(
        workspace_id=workspace_id,
        kind=kind,
        agent_id="agent-1",
        summary="ran kb_search",
        pocket_id="p1",
        ts=ts if ts is not None else time.time(),
    )


class TestPushAndGetRecent:
    def test_push_then_get_recent_returns_newest_first(self) -> None:
        b = Buffer()
        now = time.time()
        b.push(_ev(ts=now))
        b.push(_ev(ts=now + 1))
        b.push(_ev(ts=now + 2))
        out = b.get_recent("w1", limit=10)
        expected = [round(now + 2, 3), round(now + 1, 3), round(now, 3)]
        assert [round(e.ts, 3) for e in out] == expected

    def test_get_recent_respects_limit(self) -> None:
        b = Buffer()
        now = time.time()
        for i in range(20):
            b.push(_ev(ts=now + i))
        out = b.get_recent("w1", limit=5)
        assert len(out) == 5
        # newest first => now+19, now+18, ...
        assert [round(e.ts, 3) for e in out] == [round(now + i, 3) for i in [19, 18, 17, 16, 15]]

    def test_empty_workspace_returns_empty_list(self) -> None:
        b = Buffer()
        assert b.get_recent("never-seen-this-workspace") == []

    def test_push_without_workspace_id_is_dropped(self) -> None:
        b = Buffer()
        b.push(_ev(workspace_id="", ts=time.time()))
        assert b.get_recent("") == []


class TestMaxLenEviction:
    def test_overflow_evicts_oldest(self) -> None:
        b = Buffer(max_per_workspace=3, ttl_seconds=10_000)
        now = time.time()
        for i in range(5):
            b.push(_ev(ts=now + i))
        out = b.get_recent("w1", limit=10)
        # Only 3 most recent kept (newest first).
        assert [round(e.ts, 3) for e in out] == [
            round(now + 4, 3),
            round(now + 3, 3),
            round(now + 2, 3),
        ]


class TestTTLPruning:
    def test_old_entries_are_pruned_on_push(self) -> None:
        b = Buffer(ttl_seconds=1)
        # Stale entry, definitely past the 1-second TTL.
        b.push(_ev(ts=time.time() - 60))
        # Fresh entry forces a prune on the way in.
        b.push(_ev(ts=time.time()))
        out = b.get_recent("w1", limit=10)
        assert len(out) == 1

    def test_old_entries_are_pruned_on_read(self) -> None:
        b = Buffer(ttl_seconds=1)
        b.push(_ev(ts=time.time()))
        # Wait past the TTL then peek — the buffer should drop the stale row.
        time.sleep(1.05)
        out = b.get_recent("w1", limit=10)
        assert out == []


class TestWorkspaceIsolation:
    def test_get_recent_does_not_leak_across_workspaces(self) -> None:
        b = Buffer()
        now = time.time()
        b.push(_ev(workspace_id="w1", ts=now))
        b.push(_ev(workspace_id="w2", ts=now))
        assert len(b.get_recent("w1")) == 1
        assert len(b.get_recent("w2")) == 1


class TestSubscribers:
    def test_subscribers_receive_pushed_events_in_order(self) -> None:
        b = Buffer()
        received: list[ActivityEvent] = []
        b.subscribe(lambda e: received.append(e))
        now = time.time()
        b.push(_ev(ts=now))
        b.push(_ev(ts=now + 1))
        assert [round(e.ts, 3) for e in received] == [round(now, 3), round(now + 1, 3)]

    def test_subscriber_failure_does_not_break_push(self) -> None:
        b = Buffer()

        def _bomb(_event: ActivityEvent) -> None:
            raise RuntimeError("boom")

        b.subscribe(_bomb)
        # Push should not raise even with a broken subscriber.
        b.push(_ev(ts=time.time()))
        assert len(b.get_recent("w1")) == 1

    def test_unsubscribe_removes_callback(self) -> None:
        b = Buffer()
        received: list[ActivityEvent] = []
        cb = received.append
        b.subscribe(cb)
        b.unsubscribe(cb)
        b.push(_ev(ts=time.time()))
        assert received == []


class TestGetBufferSingleton:
    def test_get_buffer_returns_same_instance(self) -> None:
        a = get_buffer()
        b = get_buffer()
        assert a is b


class TestHandleAgentEvent:
    @pytest.mark.asyncio
    async def test_thinking_event_lands_with_kind_thinking(self) -> None:
        get_buffer().reset()
        ev = AgentThinking(
            data={"workspace_id": "w-handle-1", "thought": "considering options", "agent_id": "a1"}
        )
        await _handle_agent_event(ev)
        out = get_buffer().get_recent("w-handle-1")
        assert len(out) == 1
        assert out[0].kind == "thinking"
        assert out[0].summary == "considering options"

    @pytest.mark.asyncio
    async def test_tool_use_event_lands_with_kind_tool_call(self) -> None:
        get_buffer().reset()
        ev = AgentToolUse(
            data={"workspace_id": "w-handle-2", "tool": "kb_search", "agent_id": "a1"}
        )
        await _handle_agent_event(ev)
        out = get_buffer().get_recent("w-handle-2")
        assert len(out) == 1
        assert out[0].kind == "tool_call"
        assert out[0].summary == "kb_search"

    @pytest.mark.asyncio
    async def test_event_without_workspace_id_is_dropped(self) -> None:
        get_buffer().reset()
        ev = AgentThinking(data={"thought": "hmm"})  # no workspace_id
        await _handle_agent_event(ev)
        # No workspace_id key registered.
        assert get_buffer().get_recent("") == []
