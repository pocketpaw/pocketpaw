"""Tests for pocketclaw.analytics.

Covers the collector, store, models, lifecycle singleton, and bus integration.

Created: 2026-02-13
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pocketclaw.analytics.collector import AnalyticsCollector
from pocketclaw.analytics.models import (
    AnalyticsSnapshot,
    AnalyticsSummary,
    DailyActivity,
    ToolCallRecord,
    ToolStats,
)
from pocketclaw.analytics.store import AnalyticsStore

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


@dataclass
class FakeSystemEvent:
    """Minimal stand-in for bus.events.SystemEvent."""

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


# ---------------------------------------------------------------
# Collector unit tests
# ---------------------------------------------------------------


class TestCollector:
    """AnalyticsCollector in isolation (no real bus)."""

    @pytest.fixture()
    def collector(self) -> AnalyticsCollector:
        return AnalyticsCollector()

    @pytest.mark.asyncio
    async def test_records_tool_start(self, collector: AnalyticsCollector):
        event = FakeSystemEvent(event_type="tool_start", data={"name": "shell"})
        await collector.handle_event(event)

        assert collector.total_tool_calls == 1
        assert "shell" in collector._tool_stats
        assert collector._tool_stats["shell"].call_count == 1

    @pytest.mark.asyncio
    async def test_records_tool_result_with_duration(self, collector: AnalyticsCollector):
        # Simulate start then result
        await collector.handle_event(
            FakeSystemEvent(event_type="tool_start", data={"name": "read_file"})
        )
        # Tiny sleep so duration > 0
        await asyncio.sleep(0.01)
        await collector.handle_event(
            FakeSystemEvent(
                event_type="tool_result",
                data={"name": "read_file", "status": "success"},
            )
        )

        assert collector.total_tool_calls == 1
        assert len(collector._recent_calls) == 1
        record = collector._recent_calls[0]
        assert record.name == "read_file"
        assert record.duration_ms is not None
        assert record.duration_ms > 0

    @pytest.mark.asyncio
    async def test_records_tool_error(self, collector: AnalyticsCollector):
        await collector.handle_event(
            FakeSystemEvent(event_type="tool_start", data={"name": "edit_file"})
        )
        await collector.handle_event(
            FakeSystemEvent(
                event_type="tool_result",
                data={"name": "edit_file", "status": "error"},
            )
        )

        assert collector.total_errors == 1
        assert collector._tool_stats["edit_file"].error_count == 1

    @pytest.mark.asyncio
    async def test_records_thinking_as_message(self, collector: AnalyticsCollector):
        await collector.handle_event(
            FakeSystemEvent(event_type="thinking", data={"session_key": "s1"})
        )
        assert collector.total_messages == 1

    @pytest.mark.asyncio
    async def test_records_error_event(self, collector: AnalyticsCollector):
        await collector.handle_event(FakeSystemEvent(event_type="error", data={"message": "boom"}))
        assert collector.total_errors == 1

    @pytest.mark.asyncio
    async def test_summary_aggregation(self, collector: AnalyticsCollector):
        # Generate some data
        for tool in ["shell", "shell", "read_file"]:
            await collector.handle_event(
                FakeSystemEvent(event_type="tool_start", data={"name": tool})
            )
            await collector.handle_event(
                FakeSystemEvent(event_type="tool_result", data={"name": tool, "status": "success"})
            )
        await collector.handle_event(FakeSystemEvent(event_type="thinking", data={}))

        summary = collector.get_summary()
        assert isinstance(summary, AnalyticsSummary)
        assert summary.total_tool_calls == 3
        assert summary.total_messages == 1
        assert summary.total_errors == 0
        assert len(summary.top_tools) == 2
        assert summary.top_tools[0].name == "shell"  # most used
        assert summary.top_tools[0].call_count == 2

    @pytest.mark.asyncio
    async def test_top_tools_sorting(self, collector: AnalyticsCollector):
        # shell: 3 calls, read_file: 1 call
        for _ in range(3):
            await collector.handle_event(
                FakeSystemEvent(event_type="tool_start", data={"name": "shell"})
            )
        await collector.handle_event(
            FakeSystemEvent(event_type="tool_start", data={"name": "read_file"})
        )

        summary = collector.get_summary()
        assert summary.top_tools[0].name == "shell"
        assert summary.top_tools[0].call_count == 3

    @pytest.mark.asyncio
    async def test_tool_details(self, collector: AnalyticsCollector):
        await collector.handle_event(
            FakeSystemEvent(event_type="tool_start", data={"name": "grep"})
        )
        await collector.handle_event(
            FakeSystemEvent(event_type="tool_result", data={"name": "grep", "status": "success"})
        )

        details = collector.get_tool_details()
        assert len(details) == 1
        assert details[0]["name"] == "grep"
        assert details[0]["call_count"] == 1

    @pytest.mark.asyncio
    async def test_daily_activity_bucketing(self, collector: AnalyticsCollector):
        await collector.handle_event(FakeSystemEvent(event_type="tool_start", data={"name": "x"}))
        await collector.handle_event(FakeSystemEvent(event_type="thinking", data={}))
        await collector.handle_event(FakeSystemEvent(event_type="error", data={}))

        timeline = collector.get_timeline()
        assert len(timeline) == 1
        day = timeline[0]
        assert day["tool_calls"] == 1
        assert day["messages"] == 1
        assert day["errors"] == 1

    @pytest.mark.asyncio
    async def test_reset_clears_all(self, collector: AnalyticsCollector):
        await collector.handle_event(FakeSystemEvent(event_type="tool_start", data={"name": "a"}))
        await collector.handle_event(FakeSystemEvent(event_type="thinking", data={}))

        collector.reset()

        assert collector.total_messages == 0
        assert collector.total_tool_calls == 0
        assert collector.total_errors == 0
        assert len(collector._tool_stats) == 0
        assert len(collector._daily) == 0
        assert len(collector._recent_calls) == 0

    @pytest.mark.asyncio
    async def test_concurrent_rapid_events(self, collector: AnalyticsCollector):
        """Many events in quick succession should all be counted."""
        tasks = []
        for i in range(50):
            tasks.append(
                collector.handle_event(
                    FakeSystemEvent(event_type="tool_start", data={"name": f"tool_{i}"})
                )
            )
        await asyncio.gather(*tasks)

        assert collector.total_tool_calls == 50

    @pytest.mark.asyncio
    async def test_snapshot_round_trip(self, collector: AnalyticsCollector):
        await collector.handle_event(
            FakeSystemEvent(event_type="tool_start", data={"name": "shell"})
        )
        await collector.handle_event(FakeSystemEvent(event_type="thinking", data={}))

        snap = collector.to_snapshot()
        assert snap.total_tool_calls == 1
        assert snap.total_messages == 1

        # Load into fresh collector
        new_collector = AnalyticsCollector()
        new_collector.load_snapshot(snap)
        assert new_collector.total_tool_calls == 1
        assert new_collector.total_messages == 1


# ---------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------


class TestStore:
    """AnalyticsStore file persistence."""

    @pytest.fixture()
    def store(self, tmp_path: Path) -> AnalyticsStore:
        return AnalyticsStore(path=tmp_path / "analytics.json")

    def test_save_then_load(self, store: AnalyticsStore):
        snap = AnalyticsSnapshot(
            total_messages=10,
            total_tool_calls=5,
            total_errors=1,
            saved_at=datetime.now(tz=UTC),
        )
        store.save(snap)
        loaded = store.load()

        assert loaded is not None
        assert loaded.total_messages == 10
        assert loaded.total_tool_calls == 5
        assert loaded.total_errors == 1

    def test_load_returns_none_when_missing(self, store: AnalyticsStore):
        assert store.load() is None

    def test_atomic_write_no_corruption(self, store: AnalyticsStore):
        """Save should use temp file → rename, so partial writes don't corrupt."""
        snap = AnalyticsSnapshot(total_messages=42, saved_at=datetime.now(tz=UTC))
        store.save(snap)

        # Verify file is valid JSON
        content = store._path.read_text()
        data = json.loads(content)
        assert data["total_messages"] == 42

    def test_delete(self, store: AnalyticsStore):
        snap = AnalyticsSnapshot(saved_at=datetime.now(tz=UTC))
        store.save(snap)
        assert store._path.exists()
        store.delete()
        assert not store._path.exists()

    def test_load_corrupted_returns_none(self, store: AnalyticsStore):
        store._path.parent.mkdir(parents=True, exist_ok=True)
        store._path.write_text("{{INVALID JSON{{")
        assert store.load() is None


# ---------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------


class TestModels:
    """Pydantic model validation."""

    def test_tool_call_record_defaults(self):
        record = ToolCallRecord(name="shell", started_at=datetime.now(tz=UTC))
        assert record.status == "success"
        assert record.duration_ms is None

    def test_tool_stats_avg_duration(self):
        ts = ToolStats(name="grep", call_count=3, error_count=1, total_duration_ms=200.0)
        # 3 calls, 1 error → 2 successful → avg = 200/2 = 100
        assert ts.avg_duration_ms == 100.0

    def test_tool_stats_avg_duration_all_errors(self):
        ts = ToolStats(name="fail", call_count=2, error_count=2, total_duration_ms=0)
        assert ts.avg_duration_ms == 0.0

    def test_daily_activity_defaults(self):
        da = DailyActivity(date="2026-02-13")
        assert da.messages == 0
        assert da.tool_calls == 0
        assert da.errors == 0

    def test_analytics_summary_serialization(self):
        summary = AnalyticsSummary(
            total_messages=100,
            total_tool_calls=50,
            total_errors=5,
            error_rate=0.1,
        )
        data = summary.model_dump(mode="json")
        assert data["total_messages"] == 100
        assert data["error_rate"] == 0.1

    def test_snapshot_round_trip(self):
        snap = AnalyticsSnapshot(
            total_messages=7,
            tool_stats={"shell": ToolStats(name="shell", call_count=3)},
            daily_activity={"2026-02-13": DailyActivity(date="2026-02-13", messages=7)},
            saved_at=datetime.now(tz=UTC),
        )
        json_str = snap.model_dump_json()
        restored = AnalyticsSnapshot.model_validate_json(json_str)
        assert restored.total_messages == 7
        assert restored.tool_stats["shell"].call_count == 3


# ---------------------------------------------------------------
# Singleton lifecycle test
# ---------------------------------------------------------------


class TestSingleton:
    """get_analytics_collector() singleton and lifecycle reset."""

    def test_singleton_returns_same_instance(self):
        import pocketclaw.analytics as analytics_mod

        # Reset first
        analytics_mod._collector = None

        c1 = analytics_mod.get_analytics_collector()
        c2 = analytics_mod.get_analytics_collector()
        assert c1 is c2

        # Cleanup
        analytics_mod._collector = None

    def test_reset_clears_singleton(self):
        import pocketclaw.analytics as analytics_mod

        analytics_mod._collector = None
        c1 = analytics_mod.get_analytics_collector()
        analytics_mod._collector = None  # simulate reset
        c2 = analytics_mod.get_analytics_collector()
        assert c1 is not c2

        # Cleanup
        analytics_mod._collector = None
