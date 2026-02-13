"""Analytics event collector.

Subscribes to the MessageBus SystemEvent stream and maintains in-memory
counters plus a ring buffer of recent tool calls.  Persistence is handled
by AnalyticsStore (called periodically or on shutdown).

Created: 2026-02-13
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import UTC, datetime

from pocketclaw.analytics.models import (
    AnalyticsSnapshot,
    AnalyticsSummary,
    DailyActivity,
    ToolCallRecord,
    ToolStats,
)

logger = logging.getLogger(__name__)

# Maximum number of recent tool calls kept in the ring buffer.
_MAX_RECENT_CALLS = 500


class AnalyticsCollector:
    """Collects and aggregates analytics events from the message bus.

    Usage::

        collector = AnalyticsCollector()
        bus.subscribe_system(collector.handle_event)
    """

    def __init__(self) -> None:
        # Aggregate counters
        self.total_messages: int = 0
        self.total_tool_calls: int = 0
        self.total_errors: int = 0

        # Per-tool stats  (tool_name → ToolStats)
        self._tool_stats: dict[str, ToolStats] = {}

        # Daily activity  (YYYY-MM-DD → DailyActivity)
        self._daily: dict[str, DailyActivity] = {}

        # Ring buffer of recent tool calls (newest at right)
        self._recent_calls: deque[ToolCallRecord] = deque(maxlen=_MAX_RECENT_CALLS)

        # Track in-flight tool calls to compute duration.
        # Maps (tool_name) → start timestamp in seconds.
        self._inflight: dict[str, float] = {}

        # Server start time (for uptime calculation)
        self._start_time: float = time.monotonic()

        # Event counter for periodic persistence triggers
        self._event_count: int = 0

    # ------------------------------------------------------------------
    # Bus subscriber callback
    # ------------------------------------------------------------------

    async def handle_event(self, event) -> None:  # noqa: ANN001 — SystemEvent
        """Handle a SystemEvent from the message bus.

        Expected event_type values:
          - ``tool_start``  : data.name, data.params
          - ``tool_result`` : data.name, data.status, data.result
          - ``thinking``    : (counted as a message turn)
          - ``error``       : data.message
        """
        etype = event.event_type
        data = event.data or {}

        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")

        if etype == "tool_start":
            tool_name = data.get("name", "unknown")
            self._inflight[tool_name] = time.monotonic()
            self.total_tool_calls += 1
            self._ensure_tool(tool_name)
            self._tool_stats[tool_name].call_count += 1
            self._tool_stats[tool_name].last_used = datetime.now(tz=UTC)
            self._ensure_day(today).tool_calls += 1

        elif etype == "tool_result":
            tool_name = data.get("name", "unknown")
            status = data.get("status", "success")
            start_ts = self._inflight.pop(tool_name, None)
            duration_ms: float | None = None
            if start_ts is not None:
                duration_ms = (time.monotonic() - start_ts) * 1000

            record = ToolCallRecord(
                name=tool_name,
                started_at=datetime.now(tz=UTC),
                duration_ms=duration_ms,
                status=status,
            )
            self._recent_calls.append(record)

            self._ensure_tool(tool_name)
            ts = self._tool_stats[tool_name]
            if status == "error":
                ts.error_count += 1
                self.total_errors += 1
                self._ensure_day(today).errors += 1
            elif duration_ms is not None:
                ts.total_duration_ms += duration_ms

        elif etype == "thinking":
            # Count each thinking event as a processed message turn
            self.total_messages += 1
            self._ensure_day(today).messages += 1

        elif etype == "error":
            self.total_errors += 1
            self._ensure_day(today).errors += 1

        self._event_count += 1

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_summary(self, top_n: int = 10) -> AnalyticsSummary:
        """Build an AnalyticsSummary snapshot."""
        sorted_tools = sorted(
            self._tool_stats.values(),
            key=lambda t: t.call_count,
            reverse=True,
        )
        error_rate = self.total_errors / self.total_tool_calls if self.total_tool_calls > 0 else 0.0
        daily_sorted = sorted(self._daily.values(), key=lambda d: d.date)
        return AnalyticsSummary(
            total_messages=self.total_messages,
            total_tool_calls=self.total_tool_calls,
            total_errors=self.total_errors,
            uptime_seconds=time.monotonic() - self._start_time,
            error_rate=round(error_rate, 4),
            top_tools=sorted_tools[:top_n],
            daily_activity=daily_sorted[-30:],  # last 30 days
            last_updated=datetime.now(tz=UTC),
        )

    def get_tool_details(self) -> list[dict]:
        """Per-tool breakdown for the /api/analytics/tools endpoint."""
        result = []
        for ts in sorted(self._tool_stats.values(), key=lambda t: t.call_count, reverse=True):
            result.append(
                {
                    "name": ts.name,
                    "call_count": ts.call_count,
                    "error_count": ts.error_count,
                    "avg_duration_ms": round(ts.avg_duration_ms, 1),
                    "total_duration_ms": round(ts.total_duration_ms, 1),
                    "last_used": ts.last_used.isoformat() if ts.last_used else None,
                }
            )
        return result

    def get_timeline(self, days: int = 30) -> list[dict]:
        """Daily activity for the /api/analytics/timeline endpoint."""
        daily_sorted = sorted(self._daily.values(), key=lambda d: d.date)
        return [d.model_dump() for d in daily_sorted[-days:]]

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def to_snapshot(self) -> AnalyticsSnapshot:
        """Serialize current state into a persistable snapshot."""
        return AnalyticsSnapshot(
            total_messages=self.total_messages,
            total_tool_calls=self.total_tool_calls,
            total_errors=self.total_errors,
            tool_stats=dict(self._tool_stats),
            daily_activity=dict(self._daily),
            saved_at=datetime.now(tz=UTC),
        )

    def load_snapshot(self, snap: AnalyticsSnapshot) -> None:
        """Restore state from a persisted snapshot."""
        self.total_messages = snap.total_messages
        self.total_tool_calls = snap.total_tool_calls
        self.total_errors = snap.total_errors
        self._tool_stats = dict(snap.tool_stats)
        self._daily = dict(snap.daily_activity)

    def reset(self) -> None:
        """Clear all collected analytics data."""
        self.total_messages = 0
        self.total_tool_calls = 0
        self.total_errors = 0
        self._tool_stats.clear()
        self._daily.clear()
        self._recent_calls.clear()
        self._inflight.clear()
        self._event_count = 0
        self._start_time = time.monotonic()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_tool(self, name: str) -> ToolStats:
        if name not in self._tool_stats:
            self._tool_stats[name] = ToolStats(name=name)
        return self._tool_stats[name]

    def _ensure_day(self, date_str: str) -> DailyActivity:
        if date_str not in self._daily:
            self._daily[date_str] = DailyActivity(date=date_str)
        return self._daily[date_str]
