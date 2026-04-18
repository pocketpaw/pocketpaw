# tests/cloud/test_decision_traces.py — Tests for ReasoningTrace + TraceCollector
# + FabricObjectSnapshot store ops (Move 2 PR-A).
# Created: 2026-04-13 — Locks the context-manager lifecycle, event aggregation
# across fabric/soul/kb/tool-call event types, deduplication on exit, and the
# SQLite persistence path for decision-time fabric snapshots.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ee.instinct.store import InstinctStore
from ee.instinct.trace import FabricObjectSnapshot, ReasoningTrace, ToolCallRef
from ee.instinct.trace_collector import TraceCollector

# ---------------------------------------------------------------------------
# Lightweight bus + event stand-ins (avoids pulling the real MessageBus)
# ---------------------------------------------------------------------------


@dataclass
class FakeEvent:
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


class FakeBus:
    """Minimal subscribe/unsubscribe interface matching MessageBus."""

    def __init__(self) -> None:
        self.subscribers: list[Any] = []

    def subscribe_system(self, cb: Any) -> None:
        self.subscribers.append(cb)

    def unsubscribe_system(self, cb: Any) -> None:
        if cb in self.subscribers:
            self.subscribers.remove(cb)

    async def publish(self, event: FakeEvent) -> None:
        for cb in list(self.subscribers):
            await cb(event)


# ---------------------------------------------------------------------------
# ReasoningTrace — shape
# ---------------------------------------------------------------------------


class TestReasoningTraceModel:
    def test_defaults_produce_empty_collections(self) -> None:
        trace = ReasoningTrace()
        assert trace.fabric_queries == []
        assert trace.soul_memories == []
        assert trace.kb_articles == []
        assert trace.tool_calls == []
        assert trace.token_counts == {}

    def test_round_trip_serialization(self) -> None:
        trace = ReasoningTrace(
            fabric_queries=["obj_1", "obj_2"],
            soul_memories=["mem_a"],
            kb_articles=["kb_42"],
            tool_calls=[ToolCallRef(tool="fabric_query", args_hash="abc", result_preview="...")],
            prompt_version="v1",
            backend="claude_agent_sdk",
            model="claude-opus-4-6",
            token_counts={"prompt": 120, "completion": 45},
        )
        restored = ReasoningTrace.model_validate(trace.model_dump())
        assert restored == trace


# ---------------------------------------------------------------------------
# TraceCollector lifecycle
# ---------------------------------------------------------------------------


class TestCollectorLifecycle:
    @pytest.mark.asyncio
    async def test_subscribes_on_enter_and_unsubscribes_on_exit(self) -> None:
        bus = FakeBus()
        assert bus.subscribers == []

        async with TraceCollector(bus):
            assert len(bus.subscribers) == 1

        assert bus.subscribers == []

    @pytest.mark.asyncio
    async def test_unsubscribes_even_when_body_raises(self) -> None:
        bus = FakeBus()

        with pytest.raises(RuntimeError, match="boom"):
            async with TraceCollector(bus):
                raise RuntimeError("boom")

        assert bus.subscribers == []

    @pytest.mark.asyncio
    async def test_carries_prompt_version_backend_and_model(self) -> None:
        bus = FakeBus()
        async with TraceCollector(
            bus,
            prompt_version="pv_123",
            backend="claude_agent_sdk",
            model="claude-opus-4-6",
        ) as collector:
            pass
        assert collector.trace.prompt_version == "pv_123"
        assert collector.trace.backend == "claude_agent_sdk"
        assert collector.trace.model == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# TraceCollector — event aggregation
# ---------------------------------------------------------------------------


class TestCollectorAggregation:
    @pytest.mark.asyncio
    async def test_captures_fabric_queries(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("fabric_query", {"object_id": "obj_acme"}))
            await bus.publish(FakeEvent("fabric_query", {"object_id": "obj_pricing"}))
        assert collector.trace.fabric_queries == ["obj_acme", "obj_pricing"]

    @pytest.mark.asyncio
    async def test_captures_soul_memories(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("soul_recall", {"memory_id": "mem_1"}))
            await bus.publish(FakeEvent("soul_recall", {"memory_id": "mem_2"}))
        assert collector.trace.soul_memories == ["mem_1", "mem_2"]

    @pytest.mark.asyncio
    async def test_captures_kb_articles(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("kb_inject", {"article_id": "kb_pricing"}))
        assert collector.trace.kb_articles == ["kb_pricing"]

    @pytest.mark.asyncio
    async def test_captures_tool_calls_with_duration(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("tool_start", {"tool": "fabric_query"}))
            await bus.publish(
                FakeEvent(
                    "tool_end",
                    {"tool": "fabric_query", "args": {"q": "acme"}, "result": "1 row"},
                ),
            )
        assert len(collector.trace.tool_calls) == 1
        call = collector.trace.tool_calls[0]
        assert call.tool == "fabric_query"
        assert call.result_preview == "1 row"
        assert call.args_hash  # non-empty
        assert call.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_tool_result_alias_event_also_captured(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("tool_start", {"tool": "kb_search"}))
            await bus.publish(
                FakeEvent(
                    "tool_result",
                    {"tool": "kb_search", "args": {"q": "discount"}, "result": "ok"},
                ),
            )
        assert len(collector.trace.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_long_tool_result_is_truncated_with_ellipsis(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("tool_start", {"tool": "fabric_query"}))
            await bus.publish(
                FakeEvent(
                    "tool_end",
                    {"tool": "fabric_query", "args": {}, "result": "x" * 500},
                ),
            )
        preview = collector.trace.tool_calls[0].result_preview
        assert len(preview) == 200
        assert preview.endswith("...")

    @pytest.mark.asyncio
    async def test_duplicate_tool_calls_with_same_args_are_merged(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            for _ in range(3):
                await bus.publish(FakeEvent("tool_start", {"tool": "fabric_query"}))
                await bus.publish(
                    FakeEvent(
                        "tool_end",
                        {"tool": "fabric_query", "args": {"q": "acme"}, "result": "ok"},
                    ),
                )
        assert len(collector.trace.tool_calls) == 1

    @pytest.mark.asyncio
    async def test_tool_calls_with_different_args_are_kept_separate(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("tool_start", {"tool": "fabric_query"}))
            await bus.publish(
                FakeEvent(
                    "tool_end",
                    {"tool": "fabric_query", "args": {"q": "acme"}, "result": "ok"},
                ),
            )
            await bus.publish(FakeEvent("tool_start", {"tool": "fabric_query"}))
            await bus.publish(
                FakeEvent(
                    "tool_end",
                    {"tool": "fabric_query", "args": {"q": "beta"}, "result": "ok"},
                ),
            )
        assert len(collector.trace.tool_calls) == 2

    @pytest.mark.asyncio
    async def test_reference_lists_are_deduplicated_on_exit(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("fabric_query", {"object_id": "obj_acme"}))
            await bus.publish(FakeEvent("fabric_query", {"object_id": "obj_acme"}))
            await bus.publish(FakeEvent("soul_recall", {"memory_id": "mem_1"}))
            await bus.publish(FakeEvent("soul_recall", {"memory_id": "mem_1"}))
        assert collector.trace.fabric_queries == ["obj_acme"]
        assert collector.trace.soul_memories == ["mem_1"]

    @pytest.mark.asyncio
    async def test_unknown_event_types_are_ignored(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("unknown_thing", {"object_id": "nope"}))
            await bus.publish(FakeEvent("another_thing", {"data": 1}))
        assert collector.trace.fabric_queries == []
        assert collector.trace.tool_calls == []

    @pytest.mark.asyncio
    async def test_malformed_event_data_is_skipped(self) -> None:
        bus = FakeBus()
        async with TraceCollector(bus) as collector:
            await bus.publish(FakeEvent("fabric_query", {"object_id": 123}))
            await bus.publish(FakeEvent("soul_recall", {}))
            await bus.publish(FakeEvent("tool_end", {"args": {"q": "x"}}))
        assert collector.trace.fabric_queries == []
        assert collector.trace.soul_memories == []
        assert collector.trace.tool_calls == []


# ---------------------------------------------------------------------------
# FabricObjectSnapshot store ops
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "trace_test.db")


class TestFabricSnapshotStore:
    @pytest.mark.asyncio
    async def test_record_and_read_snapshot(self, store: InstinctStore) -> None:
        snapshot = FabricObjectSnapshot(
            object_id="obj_acme",
            audit_id="aud_42",
            object_type="Customer",
            snapshot={"arr": 180000, "tier": "enterprise"},
        )
        saved = await store.record_fabric_snapshot(snapshot)
        assert saved.id == snapshot.id

        rows = await store.get_snapshots_for_audit("aud_42")
        assert len(rows) == 1
        assert rows[0].object_id == "obj_acme"
        assert rows[0].snapshot["arr"] == 180000
        assert rows[0].object_type == "Customer"

    @pytest.mark.asyncio
    async def test_snapshots_for_audit_orders_oldest_first(self, store: InstinctStore) -> None:
        first = FabricObjectSnapshot(object_id="a", audit_id="aud_1")
        second = FabricObjectSnapshot(object_id="b", audit_id="aud_1")
        await store.record_fabric_snapshot(first)
        await store.record_fabric_snapshot(second)

        rows = await store.get_snapshots_for_audit("aud_1")
        assert [r.object_id for r in rows] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_snapshots_for_object_orders_newest_first(self, store: InstinctStore) -> None:
        older = FabricObjectSnapshot(object_id="obj_x", audit_id="aud_1")
        newer = FabricObjectSnapshot(object_id="obj_x", audit_id="aud_2")
        await store.record_fabric_snapshot(older)
        await store.record_fabric_snapshot(newer)

        rows = await store.get_snapshots_for_object("obj_x")
        assert [r.audit_id for r in rows] == ["aud_2", "aud_1"]
