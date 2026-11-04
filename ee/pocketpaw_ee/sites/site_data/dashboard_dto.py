# ee/pocketpaw_ee/sites/site_data/dashboard_dto.py — wire models for Sites AI
# dashboards, under /sites/by-pocket/{pocket_id}/data/dashboards.
#
# This is the pinned wire contract the PE lane builds against (tasks doc
# ``2026-11-04-sites-ai-dashboards-tasks.md``): snake_case JSON, integers past 2^53 as
# digit strings (``d1.json_safe``). Fields may be added; none may be renamed or retyped.
#
# Here: the vocabulary (``Viz``, ``WidgetOrigin``, ``WidgetStatus``), a widget's column
# mapping and layout, one widget's query result, and the dashboard request / response
# bodies. ``WidgetMapping`` names result columns only; whether they exist is checked
# against the actual result (``dashboard_sql.to_result``). ``WidgetIn`` ignores the
# server-owned fields a client sends back (origin, deps, status, spec).
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


# --------------------------------------------------------------------------- dashboards

Refresh = Literal["open", "interval"]
UnavailableReason = Literal["not_published", "live_on_cloudflare_only", "no_tables"]
MAX_WIDGETS = 40
MIN_INTERVAL_S = 60
HEAVY_INTERVAL_S = 900


class WidgetIn(BaseModel):
    """A widget as a client saves it. Server-owned fields (``origin``, ``deps``,
    ``status``, ``spec``) are ignored when sent back, so the client can return what it
    read. No ``id`` (or an unknown one) is a new widget."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    viz: Viz
    chart_type: ChartType | None = None
    sql: str = Field(max_length=20_000)  # the gate refuses past 10 KB with its reason
    mapping: WidgetMapping = Field(default_factory=WidgetMapping)
    layout: WidgetLayout
    refresh: Refresh = "open"
    interval_s: int = Field(default=300, ge=MIN_INTERVAL_S, le=86_400)


class DashboardWidget(BaseModel):
    id: str
    title: str
    viz: Viz
    chart_type: ChartType | None
    sql: str
    mapping: WidgetMapping
    layout: WidgetLayout
    refresh: Refresh
    interval_s: int  # >= 60; a heavy widget >= 900
    origin: WidgetOrigin
    deps: list[str]
    status: WidgetStatus
    status_detail: str | None
    spec: dict[str, Any]  # a mini rippleSpec {state: {data}, ui}; read only


class GeneratedInfo(BaseModel):
    model: str
    prompt_version: str
    at: str
    include_stats: bool


class SiteDashboardOut(BaseModel):
    id: str
    site_pocket_id: str
    name: str
    version: int
    widgets: list[DashboardWidget]
    schema_fingerprint: str
    generated: GeneratedInfo | None
    created_by: str
    updated_by: str
    created_at: str
    updated_at: str
    can_manage: bool


class DashboardSummary(BaseModel):
    id: str
    name: str
    widget_count: int
    updated_at: str
    needs_fix_count: int


class DashboardList(BaseModel):
    dashboards: list[DashboardSummary]
    unavailable_reason: UnavailableReason | None = None
    can_manage: bool = False  # caller has sites.dashboard_manage (create / generate)


class CreateDashboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)


class PatchDashboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    widgets: list[WidgetIn] | None = Field(default=None, max_length=MAX_WIDGETS)


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    widget_ids: list[str] | None = Field(default=None, max_length=MAX_WIDGETS)
    force: bool = False


class RunOut(BaseModel):
    results: dict[str, WidgetResult]


class TestWidgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str = Field(max_length=20_000)
    viz: Viz
    chart_type: ChartType | None = None
    mapping: WidgetMapping = Field(default_factory=WidgetMapping)


class TestWidgetOut(BaseModel):
    result: WidgetResult
    status: WidgetStatus
    status_detail: str | None
    spec: dict[str, Any]


class BrokenWidget(BaseModel):
    widget_id: str
    reason: str


class HealthOut(BaseModel):
    schema_changed: bool
    broken: list[BrokenWidget]
    new_tables: list[str]


class RevisionOut(BaseModel):
    version: int
    actor: str
    reason: str
    at: str


class RevisionsOut(BaseModel):
    revisions: list[RevisionOut]
