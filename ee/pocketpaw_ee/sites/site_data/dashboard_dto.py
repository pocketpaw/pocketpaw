# ee/pocketpaw_ee/sites/site_data/dashboard_dto.py — wire models for Sites AI
# dashboards, under /sites/by-pocket/{pocket_id}/data/dashboards.
#
# This is the pinned wire contract the PE lane builds against (tasks doc
# ``2026-11-04-sites-ai-dashboards-tasks.md``): snake_case JSON, integers past 2^53 as
# digit strings (``d1.json_safe``). Fields may be added; none may be renamed or retyped.
#
# Here: the vocabulary (``Viz``, ``WidgetOrigin``, ``WidgetStatus``), a widget's column
# mapping, and one widget's query result. ``WidgetMapping`` names result columns only;
# whether they exist is checked against the actual result (``dashboard_sql.to_result``).
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Viz = Literal["metric", "stat", "chart", "table", "sparkline", "gauge", "funnel"]
ChartType = Literal["bar", "line", "area", "pie", "donut"]
WidgetOrigin = Literal["ai", "user", "ai_edited"]
WidgetStatus = Literal["ok", "needs_fix", "heavy"]
Format = Literal["number", "currency", "percent", "duration", "date", "text"]

VIZ: tuple[str, ...] = ("metric", "stat", "chart", "table", "sparkline", "gauge", "funnel")
CHART_TYPES: tuple[str, ...] = ("bar", "line", "area", "pie", "donut")

_COL = Field(default=None, min_length=1, max_length=128)


class WidgetMapping(BaseModel):
    """Which result columns a widget draws. Unset fields fall back to the result's
    column order (label = first, value = second, or first for a single value)."""

    model_config = ConfigDict(extra="forbid")

    label_col: str | None = _COL
    value_col: str | None = _COL
    series_cols: list[str] | None = Field(default=None, max_length=8)
    columns: list[str] | None = Field(default=None, max_length=30)  # table: visible, in order
    format: Format | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class WidgetLayout(BaseModel):
    """A tile on a 12-column grid."""

    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0, le=11)
    y: int = Field(ge=0, le=1000)
    w: int = Field(ge=1, le=12)
    h: int = Field(ge=1, le=8)


class WidgetResult(BaseModel):
    """One widget's query result. ``data`` is already shaped for its spec: a chart,
    sparkline or funnel ``[{label, value, series?}]``, a metric / stat / gauge
    ``{value, delta?}``, a table its rows. ``error`` is D1's message with ids removed."""

    ok: bool
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    data: Any = None
    truncated: bool = False
    cached_at: str | None = None
    error: str | None = None
    # A chart value was an integer past 2^53, drawn as the nearest float.
    precision_loss: bool = False
