# ee/pocketpaw_ee/sites/dashboard_service.py — Sites AI dashboards: saved dashboards over a
# site's D1 database, their widgets, runs, health and revision history.
#
# Tenancy: every call resolves the site through ``data_service.resolve_target`` (pocket
# read access, the pocket's workspace, the Site doc's D1 id), and a dashboard is only
# ever looked up by (id, workspace, site pocket), so another site's or workspace's
# dashboard is a 404. Managing (create, save, delete, test, restore) needs EDIT access
# to the pocket here and ``sites.dashboard_manage`` (ADMIN) in the router; viewing and
# running need what reading the Data page needs.
#
# Widget SQL: every save re-checks every widget. A statement the gate refuses outright
# (``dashboard_sql.check``: a write, DDL, PRAGMA, a second statement …) is 422
# ``sql_rejected`` and nothing is saved; one that is safe but can't be planned against
# today's schema (an unknown table) is saved with ``status=needs_fix``. A new or
# changed query is EXPLAINed; a heavy one gets ``status=heavy`` and an interval of at
# least 15 minutes. A content edit to an ``ai`` widget makes it ``ai_edited``; new
# widgets are ``user``. Runs re-gate every widget against the live schema, so nothing
# stored is trusted.
#
# Saves are a compare-and-set on ``version`` (``If-Match``); a stale one is 409
# ``version_conflict`` carrying the current dashboard. The state before each save is
# kept as a revision (the last 20). Runs batch every widget into one D1 call, share
# results through ``dashboard_cache`` and spend the dashboards' own Cloudflare bucket
# (``cf:data:dash:ws:<id>``, per statement); a spent bucket is 429 ``rate_limited`` with
# ``retry_after``. An unpublished or local site is 503 ``unavailable`` with ``reason``.
# Creates, saves, deletes and restores are audited (``site_dashboard.*``).
from __future__ import annotations

import functools
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, ParamSpec, TypeVar

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, NotFound, RateLimited
from pocketpaw_ee.cloud.models.site_dashboard import SiteDashboard, SiteDashboardRevision
from pocketpaw_ee.sites import data_service
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.project_data import auth_table
from pocketpaw_ee.sites.site_data import budget, d1
from pocketpaw_ee.sites.site_data import dashboard_cache as cache
from pocketpaw_ee.sites.site_data import dashboard_sql as ds
from pocketpaw_ee.sites.site_data.dashboard_dto import (
    HEAVY_INTERVAL_S,
    MIN_INTERVAL_S,
    BrokenWidget,
    DashboardList,
    DashboardSummary,
    DashboardWidget,
    GeneratedInfo,
    HealthOut,
    RevisionOut,
    RevisionsOut,
    RunOut,
    SiteDashboardOut,
    TestWidgetOut,
    WidgetIn,
    WidgetMapping,
    WidgetResult,
)
from pocketpaw_ee.sites.site_data.dashboard_spec import compile_widget

logger = logging.getLogger(__name__)

MAX_DASHBOARDS = 20
FORCE_FLOOR_S = 15  # a forced refresh never re-reads a result younger than this
MAX_REVISIONS = 20
SCHEMA_TTL_S = 30.0
_AUDIT_TARGET = "site_dashboard"

P = ParamSpec("P")
T = TypeVar("T")


# --------------------------------------------------------------------------- errors


class VersionConflict(CloudError):
    """409: the dashboard changed since the caller read it. The body carries the
    current dashboard as ``current`` (and ``detail: "version_conflict"``) beside the
    standard envelope."""

    def __init__(self, current: dict[str, Any]) -> None:
        super().__init__(409, "version_conflict", "Someone saved this dashboard first.")
        self.current = current

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["detail"] = "version_conflict"
        base["current"] = self.current
        return base


def unavailable(reason: str) -> CloudError:
    return CloudError(
        503, "unavailable", "This site's data can't be read right now.", {"reason": reason}
    )


def _rate_limited(retry_after: int) -> RateLimited:
    err = RateLimited("rate_limited", f"Dashboards are busy. Try again in {retry_after} s.")
    err.details = {"retry_after": retry_after}
    return err


def busy_as_rate_limited(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Map a spent Cloudflare bucket to the dashboards' 429 ``rate_limited``."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await fn(*args, **kwargs)
        except budget.DataBusy as exc:
            raise _rate_limited(int((exc.details or {}).get("retry_after") or 60)) from exc

    return wrapper


# --------------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime | None) -> str:
    if moment is None:
        return ""
    # Milliseconds: what Mongo keeps, so a value reads the same before and after a save.
    if moment.tzinfo is None:  # Mongo hands datetimes back naive, in UTC
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_widget_id() -> str:
    return "w_" + uuid.uuid4().hex[:12]


def client(target: data_service.DataTarget, injected: Any | None) -> budget.Budgeted:
    return budget.Budgeted(
        injected or sites_service._data_cf_client(), target.workspace_id, dashboard=True
    )


async def view_target(
    *, workspace_id: str, user_id: str, pocket_id: str, _cloudflare: Any | None = None
) -> data_service.DataTarget:
    return await data_service.resolve_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )


async def manage_target(
    *, workspace_id: str, user_id: str, pocket_id: str, _cloudflare: Any | None = None
) -> data_service.DataTarget:
    return await data_service._editable_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )


async def find(target: data_service.DataTarget, dashboard_id: str) -> SiteDashboard:
    from beanie import PydanticObjectId

    try:
        oid = PydanticObjectId(dashboard_id)
    except Exception as exc:  # noqa: BLE001 — a malformed id is just unknown
        raise NotFound("site_dashboard", dashboard_id) from exc
    doc = await SiteDashboard.find_one(
        {"_id": oid, "workspace_id": target.workspace_id, "site_pocket_id": target.pocket_id}
    )
    if doc is None:
        raise NotFound("site_dashboard", dashboard_id)
    return doc


# A short per-process schema cache for runs: introspection reads sqlite_master on every
# call, and a dashboard refresh shouldn't pay that each time.
_schemas: dict[str, tuple[float, d1.Schema]] = {}


def reset() -> None:
    _schemas.clear()
    cache.reset()


async def run_schema(cf: budget.Budgeted, database_id: str) -> d1.Schema:
    hit = _schemas.get(database_id)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    schema = await d1.introspect(cf, database_id)
    if len(_schemas) > 256:
        _schemas.pop(next(iter(_schemas)))
    _schemas[database_id] = (time.monotonic() + SCHEMA_TTL_S, schema)
    return schema


def forget_schema(database_id: str) -> None:
    _schemas.pop(database_id, None)


async def _audit(
    target: data_service.DataTarget, user_id: str, action: str, dash_id: str, **meta: Any
) -> None:
    await data_service._audit_event(
        target,
        user_id,
        action,
        dash_id,
        {"pocket_id": target.pocket_id, **meta},
        target_type=_AUDIT_TARGET,
    )


def _mapping(w: dict[str, Any]) -> WidgetMapping:
    try:
        return WidgetMapping.model_validate(w.get("mapping") or {})
    except Exception:  # noqa: BLE001 — a stored mapping that no longer validates
        return WidgetMapping()


def widget_out(w: dict[str, Any]) -> DashboardWidget:
    mapping = _mapping(w)
    return DashboardWidget(
        id=w["id"],
        title=w["title"],
        viz=w["viz"],
        chart_type=w.get("chart_type"),
        sql=w["sql"],
        mapping=mapping,
        layout=w["layout"],
        refresh=w.get("refresh", "open"),
        interval_s=int(w.get("interval_s") or 300),
        origin=w.get("origin", "user"),
        deps=list(w.get("deps") or []),
        status=w.get("status", "ok"),
        status_detail=w.get("status_detail"),
        spec=compile_widget(w["viz"], w.get("chart_type"), w["title"], mapping),
    )


def dashboard_out(doc: SiteDashboard, can_manage: bool) -> SiteDashboardOut:
    generated = GeneratedInfo.model_validate(doc.generated) if doc.generated else None
    return SiteDashboardOut(
        id=str(doc.id),
        site_pocket_id=doc.site_pocket_id,
        name=doc.name,
        version=doc.version,
        widgets=[widget_out(w) for w in doc.widgets],
        schema_fingerprint=doc.schema_fingerprint,
        generated=generated,
        created_by=doc.created_by,
        updated_by=doc.updated_by,
        created_at=iso(doc.created_at),
        updated_at=iso(doc.updated_at),
        can_manage=can_manage,
    )


def refresh_floor(w: dict[str, Any]) -> int:
    """How long a widget's result is shared: its interval (at least 60 s, at least
    15 minutes when heavy), whatever the client asks for."""
    ttl = max(MIN_INTERVAL_S, int(w.get("interval_s") or 300))
    if w.get("status") == "heavy":
        ttl = max(ttl, HEAVY_INTERVAL_S)
    return ttl


def _content(w: dict[str, Any]) -> tuple:
    return (w.get("title"), w.get("viz"), w.get("chart_type"), w.get("sql"), w.get("mapping"))


async def _assess(
    rec: dict[str, Any],
    prev: dict[str, Any] | None,
    schema: d1.Schema | None,
    cf: budget.Budgeted | None,
    database_id: str,
) -> None:
    """Fill ``deps`` / ``status`` / ``status_detail`` of a widget being saved."""
    same_query = prev is not None and (prev.get("sql"), prev.get("viz")) == (
        rec["sql"],
        rec["viz"],
    )
    if schema is None:  # the site can't be read now: keep what we knew
        keep = prev if same_query else None
        rec["deps"] = list((keep or {}).get("deps") or [])
        rec["status"] = (keep or {}).get("status", "ok")
        rec["status_detail"] = (keep or {}).get("status_detail")
        return
    try:
        q = ds.gate(rec["sql"], rec["viz"], schema)
    except ds.SqlRejected as exc:
        rec.update(deps=[], status="needs_fix", status_detail=exc.message)
        return
    rec["deps"] = list(q.deps)
    if same_query and prev is not None and prev.get("status") != "needs_fix":
        heavy = prev.get("status_detail") if prev.get("status") == "heavy" else None
    else:
        try:
            heavy = await ds.heavy(cf, database_id, q, schema) if cf is not None else None
        except budget.DataBusy:  # advice only: a big save must not fail on its EXPLAINs
            heavy = None
    rec["status"] = "heavy" if heavy else "ok"
    rec["status_detail"] = heavy
    if heavy:
        rec["interval_s"] = max(int(rec["interval_s"]), HEAVY_INTERVAL_S)


async def build_widgets(
    incoming: list[WidgetIn],
    existing: list[dict[str, Any]],
    schema: d1.Schema | None,
    cf: budget.Budgeted | None,
    database_id: str,
    *,
    new_origin: str = "user",
) -> list[dict[str, Any]]:
    """The stored widgets for a save of ``incoming`` over ``existing``, or 422
    ``sql_rejected`` (nothing saved) when a widget's SQL is refused outright."""
    by_id = {w["id"]: w for w in existing}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, w in enumerate(incoming):
        try:
            ds.check(w.sql)
        except ds.SqlRejected as exc:
            exc.details = {**(exc.details or {}), "widget_index": index, "widget_id": w.id}
            raise
        prev = by_id.get(w.id) if w.id and w.id not in seen else None
        wid = prev["id"] if prev else new_widget_id()
        seen.add(wid)
        layout = w.layout.model_dump()
        layout["w"] = min(layout["w"], 12 - layout["x"])
        rec: dict[str, Any] = {
            "id": wid,
            "title": w.title.strip() or "Untitled",
            "viz": w.viz,
            "chart_type": (w.chart_type or "bar") if w.viz == "chart" else None,
            "sql": w.sql,
            "mapping": w.mapping.model_dump(exclude_none=True),
            "layout": layout,
            "refresh": w.refresh,
            "interval_s": int(w.interval_s),
        }
        if prev is None:
            rec["origin"] = new_origin
        else:
            origin = prev.get("origin", "user")
            rec["origin"] = (
                "ai_edited" if origin == "ai" and _content(prev) != _content(rec) else origin
            )
        await _assess(rec, prev, schema, cf, database_id)
        out.append(rec)
    return out


async def _snapshot(doc: SiteDashboard) -> None:
    """Keep ``doc`` as it is now as a revision, and only the newest 20."""
    try:
        await SiteDashboardRevision(
            workspace_id=doc.workspace_id,
            dashboard_id=str(doc.id),
            version=doc.version,
            name=doc.name,
            widgets=doc.widgets,
            actor=doc.updated_by,
            reason=getattr(doc, "reason", "") or "edit",
            at=doc.updated_at,
        ).insert()
    except DuplicateKeyError:
        pass  # a concurrent save already kept this version
    old = (
        await SiteDashboardRevision.find({"dashboard_id": str(doc.id)})
        .sort("-version")
        .skip(MAX_REVISIONS)
        .to_list()
    )
    for rev in old:
        await rev.delete()


async def save(
    doc: SiteDashboard,
    expected_version: int,
    fields: dict[str, Any],
    *,
    actor: str,
    reason: str,
    can_manage: bool = True,
) -> SiteDashboard:
    """Compare-and-set the dashboard at ``expected_version``, keeping a revision of
    what it replaces; 409 ``version_conflict`` when it moved on."""
    if doc.version != expected_version:
        raise VersionConflict(dashboard_out(doc, can_manage).model_dump(mode="json"))
    await _snapshot(doc)
    coll = SiteDashboard.get_pymongo_collection()
    updated = await coll.find_one_and_update(
        {"_id": doc.id, "version": expected_version},
        {
            "$set": {
                **fields,
                "version": expected_version + 1,
                "updated_by": actor,
                "updated_at": _now(),
                "reason": reason,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    current = await SiteDashboard.get(doc.id)
    if updated is None or current is None:
        if current is None:
            raise NotFound("site_dashboard", str(doc.id))
        raise VersionConflict(dashboard_out(current, can_manage).model_dump(mode="json"))
    return current


# --------------------------------------------------------------------------- CRUD


@busy_as_rate_limited
async def list_dashboards(
    *, workspace_id: str, user_id: str, pocket_id: str, _cloudflare: Any | None = None
) -> DashboardList:
    target = await view_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    docs = (
        await SiteDashboard.find({"workspace_id": workspace_id, "site_pocket_id": pocket_id})
        .sort("-updated_at")
        .to_list()
    )
    reason: str | None = target.reason or None
    if reason is None and not docs:
        schema = await run_schema(client(target, _cloudflare), target.d1_id)
        if not schema.tables:
            reason = "no_tables"
    return DashboardList(
        dashboards=[
            DashboardSummary(
                id=str(d.id),
                name=d.name,
                widget_count=len(d.widgets),
                updated_at=iso(d.updated_at),
                needs_fix_count=sum(1 for w in d.widgets if w.get("status") == "needs_fix"),
            )
            for d in docs
        ],
        unavailable_reason=reason,  # type: ignore[arg-type]
    )


async def check_room(workspace_id: str, pocket_id: str) -> None:
    """409 ``sites.dashboard_limit`` when the site already has the most dashboards."""
    count = await SiteDashboard.find(
        {"workspace_id": workspace_id, "site_pocket_id": pocket_id}
    ).count()
    if count >= MAX_DASHBOARDS:
        raise ConflictError(
            "sites.dashboard_limit", f"A site can have up to {MAX_DASHBOARDS} dashboards."
        )


async def insert_dashboard(
    target: data_service.DataTarget,
    user_id: str,
    name: str,
    *,
    widgets: list[dict[str, Any]] | None = None,
    generated: dict[str, Any] | None = None,
    schema: d1.Schema | None = None,
) -> SiteDashboard:
    """Store a new dashboard (the caller checked access and room) and audit it."""
    now = _now()
    doc = SiteDashboard(
        workspace_id=target.workspace_id,
        site_pocket_id=target.pocket_id,
        name=name.strip() or "Dashboard",
        widgets=widgets or [],
        schema_fingerprint=schema.fingerprint if schema else "",
        known_tables=sorted(schema.tables) if schema else [],
        generated=generated,
        reason="generate" if generated else "create",
        created_by=user_id,
        updated_by=user_id,
        created_at=now,
        updated_at=now,
    )
    await doc.insert()
    await keep_within_cap(doc)
    await _audit(
        target,
        user_id,
        "site_dashboard.create",
        str(doc.id),
        widgets=len(doc.widgets),
        generated=bool(generated),
    )
    return doc


async def keep_within_cap(doc: SiteDashboard) -> None:
    """After an insert: if concurrent creates pushed the site past the cap, the newest
    ones (this among them) are removed again and refused with 409."""
    docs = (
        await SiteDashboard.find(
            {"workspace_id": doc.workspace_id, "site_pocket_id": doc.site_pocket_id}
        )
        .sort("+_id")
        .to_list()
    )
    if [d.id for d in docs].index(doc.id) >= MAX_DASHBOARDS:
        await doc.delete()
        raise ConflictError(
            "sites.dashboard_limit", f"A site can have up to {MAX_DASHBOARDS} dashboards."
        )


@busy_as_rate_limited
async def create_dashboard(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    name: str,
    widgets: list[dict[str, Any]] | None = None,
    _cloudflare: Any | None = None,
) -> SiteDashboard:
    """A new dashboard (blank unless given ``widgets``)."""
    target = await manage_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    await check_room(workspace_id, pocket_id)
    schema = None
    if not target.reason:
        schema = await d1.introspect(client(target, _cloudflare), target.d1_id)
    return await insert_dashboard(target, user_id, name, widgets=widgets, schema=schema)


async def get_dashboard(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    _cloudflare: Any | None = None,
) -> SiteDashboard:
    target = await view_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    return await find(target, dashboard_id)


@busy_as_rate_limited
async def patch_dashboard(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    if_match: int,
    name: str | None = None,
    widgets: list[WidgetIn] | None = None,
    _cloudflare: Any | None = None,
) -> SiteDashboard:
    """Rename and / or replace the widget list (``widgets`` is the FULL list)."""
    target = await manage_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    doc = await find(target, dashboard_id)
    if doc.version != if_match:
        raise VersionConflict(dashboard_out(doc, True).model_dump(mode="json"))
    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name.strip() or doc.name
    if widgets is not None:
        schema: d1.Schema | None = None
        cf: budget.Budgeted | None = None
        if not target.reason:
            cf = client(target, _cloudflare)
            schema = await d1.introspect(cf, target.d1_id)
            fields["schema_fingerprint"] = schema.fingerprint
            fields["known_tables"] = sorted(schema.tables)
        fields["widgets"] = await build_widgets(widgets, doc.widgets, schema, cf, target.d1_id)
    if not fields:
        return doc
    saved = await save(doc, if_match, fields, actor=user_id, reason="edit")
    await _audit(
        target,
        user_id,
        "site_dashboard.update",
        str(doc.id),
        version=saved.version,
        widgets=len(saved.widgets),
        renamed=name is not None,
    )
    return saved


async def delete_dashboard(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    _cloudflare: Any | None = None,
) -> None:
    target = await manage_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    doc = await find(target, dashboard_id)
    await SiteDashboardRevision.find({"dashboard_id": str(doc.id)}).delete()
    await doc.delete()
    await _audit(target, user_id, "site_dashboard.delete", dashboard_id, name=doc.name)


# --------------------------------------------------------------------------- runs


@busy_as_rate_limited
async def run_dashboard(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    widget_ids: list[str] | None = None,
    force: bool = False,
    can_manage: bool = False,
    _cloudflare: Any | None = None,
) -> RunOut:
    """Each widget's result: from the shared cache while it is younger than the widget's
    interval, the rest in ONE D1 batch (re-run one by one if the batch fails). ``force``
    counts only for a manager, and never re-reads a result younger than 15 s."""
    force = force and can_manage
    target = await view_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    doc = await find(target, dashboard_id)
    if target.reason:
        raise unavailable(target.reason)
    wanted = set(widget_ids or [])
    widgets = [w for w in doc.widgets if not wanted or w["id"] in wanted]
    results: dict[str, WidgetResult] = {}
    if not widgets:
        return RunOut(results=results)
    cf = client(target, _cloudflare)
    schema = await run_schema(cf, target.d1_id)
    epoch = await cache.epoch(target.d1_id)

    # key -> (gated query, ttl, [(widget, mapping)])
    pending: dict[str, tuple[ds.GatedQuery, int, list[tuple[dict[str, Any], WidgetMapping]]]] = {}
    for w in widgets:
        mapping = _mapping(w)
        try:
            q = ds.gate(w["sql"], w["viz"], schema)
        except ds.SqlRejected as exc:
            results[w["id"]] = ds.error_result(exc.message)
            continue
        key = cache.key(target.d1_id, q, schema.fingerprint, epoch)
        ttl = refresh_floor(w)
        if key not in pending:
            hit = await cache.get(key)
            if hit is not None and (not force or cache.age_s(hit[1]) < FORCE_FLOOR_S):
                raw, at = hit
                results[w["id"]] = ds.to_result(q, w["viz"], mapping, raw, cached_at=at)
                continue
        entry = pending.setdefault(key, (q, ttl, []))
        entry[2].append((w, mapping))

    if pending:
        keys = list(pending)
        raws = await ds.run_batch(cf, target.d1_id, [pending[k][0] for k in keys])
        at = iso(_now())
        for key, raw in zip(keys, raws, strict=True):
            q, ttl, users = pending[key]
            if isinstance(raw, dict):
                await cache.put(key, raw, at, ttl)
            for w, mapping in users:
                results[w["id"]] = ds.to_result(q, w["viz"], mapping, raw, cached_at=at)
    return RunOut(results={w["id"]: results[w["id"]] for w in widgets if w["id"] in results})


@busy_as_rate_limited
async def test_widget(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    sql: str,
    viz: str,
    chart_type: str | None,
    mapping: WidgetMapping,
    _cloudflare: Any | None = None,
) -> TestWidgetOut:
    """Run a draft widget once, fresh: its result, status and spec. 422
    ``sql_rejected`` when the gate refuses the SQL outright."""
    target = await manage_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    await find(target, dashboard_id)
    if target.reason:
        raise unavailable(target.reason)
    ds.check(sql)
    chart = (chart_type or "bar") if viz == "chart" else None
    cf = client(target, _cloudflare)
    schema = await d1.introspect(cf, target.d1_id)
    try:
        q = ds.gate(sql, viz, schema)
    except ds.SqlRejected as exc:
        return TestWidgetOut(
            result=ds.error_result(exc.message),
            status="needs_fix",
            status_detail=exc.message,
            spec=compile_widget(viz, chart, "", mapping),
        )
    raw = await ds.run_one(cf, target.d1_id, q)
    result = ds.to_result(q, viz, mapping, raw, cached_at=iso(_now()))
    # Recorded like a console read: it ran owner-typed SQL against the live database.
    await data_service._audit_event(
        target,
        user_id,
        "site_data.sql",
        target.pocket_id,
        {
            "kind": "read",
            "verb": "SELECT",
            "sql": sql[:2_048],
            "database_id": target.d1_id,
            "source": "dashboard_test",
            "dashboard_id": dashboard_id,
            "rows": len(result.rows),
            "error": None if result.ok else result.error,
        },
        target_type=data_service._SQL_QUERY_TARGET,
    )
    spec = compile_widget(viz, chart, "", mapping, result.columns)
    if not result.ok:
        return TestWidgetOut(
            result=result, status="needs_fix", status_detail=result.error, spec=spec
        )
    heavy = await ds.heavy(cf, target.d1_id, q, schema)
    return TestWidgetOut(
        result=result, status="heavy" if heavy else "ok", status_detail=heavy, spec=spec
    )


# --------------------------------------------------------------------------- health


def broken_widgets(doc: SiteDashboard, schema: d1.Schema) -> list[BrokenWidget]:
    """Widgets today's schema breaks: the gate refuses them, or a column they
    mention is gone. Pure: no D1 call."""
    columns = {
        d1.fold(t.name): {d1.fold(c.name) for c in t.columns} for t in schema.tables.values()
    }
    out: list[BrokenWidget] = []
    for w in doc.widgets:
        try:
            ds.gate(w["sql"], w["viz"], schema)
        except ds.SqlRejected as exc:
            out.append(BrokenWidget(widget_id=w["id"], reason=exc.message))
            continue
        for dep in w.get("deps") or []:
            table, _, column = dep.partition(".")
            if not column:
                continue
            have = columns.get(d1.fold(table))
            if have is None or d1.fold(column) not in have:
                out.append(BrokenWidget(widget_id=w["id"], reason=f"The column {dep} is gone."))
                break
    return out


def new_tables(doc: SiteDashboard, schema: d1.Schema) -> list[str]:
    """User tables added since the dashboard last saw the schema (sign-in tables
    left out)."""
    if not doc.known_tables:
        return []
    known = {d1.fold(n) for n in doc.known_tables}
    return sorted(
        name
        for name, t in schema.tables.items()
        if d1.fold(name) not in known and not auth_table(name) and not d1.is_login(t)
    )


@busy_as_rate_limited
async def health(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    _cloudflare: Any | None = None,
) -> HealthOut:
    target = await view_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    doc = await find(target, dashboard_id)
    if target.reason:
        raise unavailable(target.reason)
    schema = await d1.introspect(client(target, _cloudflare), target.d1_id)
    return HealthOut(
        schema_changed=bool(doc.schema_fingerprint)
        and schema.fingerprint != doc.schema_fingerprint,
        broken=broken_widgets(doc, schema),
        new_tables=new_tables(doc, schema),
    )


# --------------------------------------------------------------------------- revisions


async def list_revisions(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    _cloudflare: Any | None = None,
) -> RevisionsOut:
    """The current version first, then the kept ones, newest first."""
    target = await view_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    doc = await find(target, dashboard_id)
    revs = (
        await SiteDashboardRevision.find({"dashboard_id": str(doc.id)})
        .sort("-version")
        .limit(MAX_REVISIONS)
        .to_list()
    )
    current = RevisionOut(
        version=doc.version,
        actor=doc.updated_by,
        reason=getattr(doc, "reason", "") or "create",
        at=iso(doc.updated_at),
    )
    return RevisionsOut(
        revisions=[current]
        + [
            RevisionOut(version=r.version, actor=r.actor, reason=r.reason, at=iso(r.at))
            for r in revs
            if r.version != doc.version
        ]
    )


@busy_as_rate_limited
async def restore_revision(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    dashboard_id: str,
    version: int,
    _cloudflare: Any | None = None,
) -> SiteDashboard:
    """Save the widgets of an earlier version as a new version (re-checked against
    today's schema)."""
    target = await manage_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    doc = await find(target, dashboard_id)
    if version == doc.version:
        return doc
    rev = await SiteDashboardRevision.find_one({"dashboard_id": str(doc.id), "version": version})
    if rev is None:
        raise NotFound("site_dashboard_revision", str(version))
    fields: dict[str, Any] = {"name": rev.name, "widgets": [dict(w) for w in rev.widgets]}
    if not target.reason:
        cf = client(target, _cloudflare)
        schema = await d1.introspect(cf, target.d1_id)
        for w in fields["widgets"]:
            await _assess(w, w, schema, cf, target.d1_id)
        fields["schema_fingerprint"] = schema.fingerprint
        fields["known_tables"] = sorted(schema.tables)
    saved = await save(doc, doc.version, fields, actor=user_id, reason=f"restore:{version}")
    await _audit(
        target,
        user_id,
        "site_dashboard.restore",
        str(doc.id),
        restored=version,
        version=saved.version,
    )
    return saved
