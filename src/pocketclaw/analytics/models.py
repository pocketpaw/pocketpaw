"""Pydantic models for analytics data.

Created: 2026-02-13
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ToolCallRecord(BaseModel):
    """A single recorded tool invocation."""

    name: str
    started_at: datetime
    duration_ms: float | None = None
    status: str = "success"  # "success" | "error"


class ToolStats(BaseModel):
    """Aggregated per-tool statistics."""

    name: str
    call_count: int = 0
    error_count: int = 0
    total_duration_ms: float = 0.0
    last_used: datetime | None = None

    @property
    def avg_duration_ms(self) -> float:
        successful = self.call_count - self.error_count
        if successful <= 0:
            return 0.0
        return self.total_duration_ms / successful


class DailyActivity(BaseModel):
    """Activity counts for a single day."""

    date: str  # ISO format YYYY-MM-DD
    messages: int = 0
    tool_calls: int = 0
    errors: int = 0


class AnalyticsSummary(BaseModel):
    """Top-level analytics summary returned by the API."""

    total_messages: int = 0
    total_tool_calls: int = 0
    total_errors: int = 0
    uptime_seconds: float = 0.0
    error_rate: float = Field(default=0.0, description="Errors / total tool calls")
    top_tools: list[ToolStats] = Field(default_factory=list)
    daily_activity: list[DailyActivity] = Field(default_factory=list)
    last_updated: datetime | None = None


class AnalyticsSnapshot(BaseModel):
    """Serializable snapshot for file-based persistence."""

    total_messages: int = 0
    total_tool_calls: int = 0
    total_errors: int = 0
    tool_stats: dict[str, ToolStats] = Field(default_factory=dict)
    daily_activity: dict[str, DailyActivity] = Field(default_factory=dict)
    saved_at: datetime | None = None
