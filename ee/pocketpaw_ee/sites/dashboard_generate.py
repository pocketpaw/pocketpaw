# ee/pocketpaw_ee/sites/dashboard_generate.py — "Create dashboard for me": the background
# job that reads a site's D1 schema, asks a model for widgets and saves the ones that work.
#
# ``start_generation`` (manage only) checks everything it can up front: the site is
# readable (503 ``unavailable``), it has at least one table a dashboard may use (503
# ``no_tables``, and no model call), the site has room for another dashboard, one job
# per site at a time (``budget.claim_once``, 429 ``rate_limited``), and the workspace's
# daily allowance (10 generations, 50 AI edits per UTC day; 429 ``rate_limited`` with
# ``retry_after`` until midnight). Then it records a ``SiteDashboardJob`` and runs the
# job in the background; ``get_job`` reads its status from any worker. A job still
# queued / running 6 minutes after it started reads as failed.
#
# The job (``design``): schema summary (``dashboard_prompt.summarize``: no sign-in
# tables, masked or PII columns, no values) with row counts, opt-in aggregate stats,
# and for more than 25 tables a first ``pick_tables`` call; then ONE forced
# ``propose_widgets`` call. Every proposed widget goes through the SQL gate and a live
# test run (one D1 batch) and must produce a result its mapping can draw; failures get
# ONE repair call with the gate's / SQLite's error text, and whatever still fails is
# dropped and reported (``dropped``). Survivors are laid out on the 12-column grid by
# size and saved through ``dashboard_service.build_widgets`` (re-gated, EXPLAINed, origin
# ``ai``). Nothing is saved when no widget works or the model doesn't answer; a model
# that never answered refunds the day's allowance.
#
# Model usage is not billed to the workspace wallet (like the Enhance button's side
# completion); the daily allowance is the bound.
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound, RateLimited, ValidationError
from pocketpaw_ee.cloud.models.site_dashboard import SiteDashboardJob
from pocketpaw_ee.sites import dashboard_service as svc
from pocketpaw_ee.sites import data_service
from pocketpaw_ee.sites.site_data import budget, d1
from pocketpaw_ee.sites.site_data import dashboard_prompt as prompt
from pocketpaw_ee.sites.site_data import dashboard_sql as ds
from pocketpaw_ee.sites.site_data.dashboard_dto import (
    DroppedWidget,
    GenerateOut,
    JobOut,
    WidgetIn,
)

logger = logging.getLogger(__name__)

JOB_TTL_S = 300
STALE_AFTER_S = JOB_TTL_S + 60
DAILY_LIMITS = {"generate": 10, "ai_edit": 50}
_SIZES = {
    "metric": (3, 2),
    "stat": (3, 2),
    "gauge": (3, 3),
    "sparkline": (4, 2),
    "chart": (6, 4),
    "funnel": (6, 4),
    "table": (12, 4),
}
_ORDER = ["metric", "stat", "gauge", "sparkline", "chart", "funnel", "table"]
_PLACEHOLDER = {"x": 0, "y": 0, "w": 1, "h": 1}
NO_ANSWER = "The AI couldn't design a dashboard right now. Try again in a minute."
NOTHING_WORKED = "None of the widgets the AI wrote worked on this database."

_tasks: set[asyncio.Task] = set()
_daily_local: dict[str, int] = {}


def reset() -> None:
    _daily_local.clear()


async def drain() -> None:
    """Wait for every background job of this process (tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


# --------------------------------------------------------------------------- daily limits


def _daily_key(workspace_id: str, kind: str, now: datetime) -> str:
    return f"dash:daily:{kind}:{workspace_id or '-'}:{now.strftime('%Y%m%d')}"


async def take_daily(workspace_id: str, kind: str) -> None:
    """Spend one of the workspace's ``kind`` uses today, or 429 ``rate_limited``."""
    limit = DAILY_LIMITS[kind]
    now = datetime.now(UTC)
    key = _daily_key(workspace_id, kind, now)
    midnight = datetime.combine(now.date() + timedelta(days=1), dtime.min, UTC)
    retry_after = max(1, int((midnight - now).total_seconds()))
    client = budget.redis_client()
    if client is not None:
        try:
            used = int(await client.incr(key))
            if used == 1:
                await client.expire(key, 2 * 86_400)
        except Exception:  # noqa: BLE001 — fall back to this process
            logger.warning("site_dashboards: Redis unavailable, counting the day in process")
        else:
            if used > limit:
                await client.decr(key)
                raise svc._rate_limited(retry_after)
            return
    used = _daily_local.get(key, 0) + 1
    if used > limit:
        raise svc._rate_limited(retry_after)
    _daily_local[key] = used


async def refund_daily(workspace_id: str, kind: str) -> None:
    key = _daily_key(workspace_id, kind, datetime.now(UTC))
    client = budget.redis_client()
    if client is not None:
        try:
            await client.decr(key)
            return
        except Exception:  # noqa: BLE001
            logger.warning("site_dashboards: could not refund %s", key)
    if _daily_local.get(key, 0) > 0:
        _daily_local[key] -= 1


# --------------------------------------------------------------------------- layout


def lay_out(widgets: list[WidgetIn]) -> list[WidgetIn]:
    """``widgets`` ordered small to large and packed onto the 12-column grid."""
    ordered = sorted(widgets, key=lambda w: _ORDER.index(w.viz))
    x = y = row_h = 0
    out: list[WidgetIn] = []
    for w in ordered:
        cw, ch = _SIZES[w.viz]
        if x + cw > 12:
            x, y, row_h = 0, y + row_h, 0
        out.append(w.model_copy(update={"layout": {"x": x, "y": y, "w": cw, "h": ch}}))
        x += cw
        row_h = max(row_h, ch)
    return [WidgetIn.model_validate(w.model_dump()) for w in out]


# --------------------------------------------------------------------------- design


def _title(raw: Any) -> str:
    title = raw.get("title") if isinstance(raw, dict) else None
    return (str(title).strip() or "Untitled")[:120] if title else "Untitled"


def _brief(w: WidgetIn) -> dict[str, Any]:
    return {
        "title": w.title,
        "viz": w.viz,
        "chart_type": w.chart_type,
        "sql": w.sql,
        "mapping": w.mapping.model_dump(exclude_none=True),
    }


def parse(raw: Any) -> WidgetIn | None:
    """A model-proposed widget as a ``WidgetIn`` (placeholder layout), or None."""
    if not isinstance(raw, dict):
        return None
    fields = {k: raw[k] for k in ("title", "viz", "chart_type", "sql", "mapping") if raw.get(k)}
    if isinstance(fields.get("title"), str):
        fields["title"] = fields["title"].strip()[:120]
    try:
        return WidgetIn.model_validate({**fields, "layout": _PLACEHOLDER})
    except Exception:  # noqa: BLE001 — a malformed proposal is dropped, not fatal
        return None


async def validate(
    cf: budget.Budgeted, database_id: str, schema: d1.Schema, proposals: list[tuple[int, Any]]
) -> tuple[list[tuple[int, WidgetIn]], list[dict[str, Any]], list[DroppedWidget]]:
    """``(working, failed, dropped)``: each proposal gated and test-run (one batch).
    ``failed`` entries can be repaired; ``dropped`` ones can't."""
    candidates: list[tuple[int, WidgetIn, ds.GatedQuery]] = []
    failed: list[dict[str, Any]] = []
    dropped: list[DroppedWidget] = []
    for index, raw in proposals:
        w = parse(raw)
        if w is None:
            dropped.append(DroppedWidget(title=_title(raw), reason="The widget wasn't valid."))
            continue
        try:
            q = ds.gate(w.sql, w.viz, schema)
        except ds.SqlRejected as exc:
            failed.append({"index": index, "widget": _brief(w), "error": exc.message})
            continue
        candidates.append((index, w, q))
    raws = await ds.run_batch(cf, database_id, [q for _i, _w, q in candidates])
    working: list[tuple[int, WidgetIn]] = []
    for (index, w, q), raw in zip(candidates, raws, strict=True):
        result = ds.to_result(q, w.viz, w.mapping, raw)
        if result.ok:
            working.append((index, w))
        else:
            failed.append({"index": index, "widget": _brief(w), "error": result.error or ""})
    return working, failed, dropped


def _rows(counts: dict[str, int | None], name: str) -> int:
    n = counts.get(name, 0)
    return d1.COUNT_CAP + 1 if n is None else n


async def summary_for(
    cf: budget.Budgeted,
    database_id: str,
    schema: d1.Schema,
    *,
    include_stats: bool,
    focus: str | None,
    model: str,
) -> dict[str, Any] | None:
    """The schema JSON the design call sees (None: the pick call didn't answer)."""
    tables = prompt.usable_tables(schema)
    counts = await d1.count_tables(cf, database_id, schema, [t.name for t in tables])
    if len(tables) > prompt.ONE_STAGE_TABLES:
        args = await prompt.call_tool(
            system=prompt.PICK_SYSTEM,
            user=prompt.pick_message(prompt.brief(tables, counts), focus),
            tool=prompt.PICK_TOOL,
            model=model,
        )
        if args is None:
            return None
        by_fold = {d1.fold(t.name): t for t in tables}
        picked: list[d1.Table] = []
        for name in args.get("tables") or []:
            t = by_fold.get(d1.fold(str(name)))
            if t is not None and t not in picked:
                picked.append(t)
        if not picked:  # fall back to the busiest tables
            picked = sorted(tables, key=lambda t: -_rows(counts, t.name))
        tables = picked[: prompt.PICK_MAX]
    stats = None
    if include_stats:
        statements = prompt.stats_statements(tables)
        if statements:
            results = await cf.query_d1_batch(database_id=database_id, statements=statements)
            stats = prompt.read_stats(tables, results)
    return prompt.summarize(tables, counts, stats)


async def design(
    cf: budget.Budgeted,
    database_id: str,
    schema: d1.Schema,
    *,
    include_stats: bool = False,
    focus: str | None = None,
    stage: Callable[[str], Awaitable[None]] | None = None,
    extra_user: str = "",
) -> tuple[list[WidgetIn], list[DroppedWidget], bool]:
    """``(working widgets, dropped, answered)``: the model's design, tested, with one
    repair round. ``answered`` is False when the model never replied."""

    async def _stage(name: str) -> None:
        if stage is not None:
            await stage(name)

    model = prompt.model_name()
    await _stage("schema")
    summary = await summary_for(
        cf, database_id, schema, include_stats=include_stats, focus=focus, model=model
    )
    if summary is None:
        return [], [], False
    await _stage("design")
    user = prompt.design_message(summary, focus)
    if extra_user:
        user = user + "\n" + extra_user
    args = await prompt.call_tool(
        system=prompt.SYSTEM, user=user, tool=prompt.WIDGETS_TOOL, model=model
    )
    if args is None:
        return [], [], False
    proposals = list(enumerate((args.get("widgets") or [])[: prompt.MAX_WIDGETS]))
    await _stage("testing")
    working, failed, dropped = await validate(cf, database_id, schema, proposals)
    if failed:
        await _stage("repair")
        fixed = await prompt.call_tool(
            system=prompt.SYSTEM,
            user=prompt.repair_message(summary, failed),
            tool=prompt.WIDGETS_TOOL,
            model=model,
        )
        by_index: dict[int, Any] = {}
        for item in (fixed or {}).get("widgets") or []:
            if isinstance(item, dict) and isinstance(item.get("index"), int):
                by_index.setdefault(item["index"], item)
        retry = [(f["index"], by_index[f["index"]]) for f in failed if f["index"] in by_index]
        again, still, more = await validate(cf, database_id, schema, retry)
        working += again
        dropped += more
        dropped += [DroppedWidget(title=f["widget"]["title"], reason=f["error"]) for f in still]
        dropped += [
            DroppedWidget(title=f["widget"]["title"], reason=f["error"])
            for f in failed
            if f["index"] not in by_index
        ]
    working.sort(key=lambda p: p[0])
    return [w for _i, w in working][: prompt.MAX_WIDGETS], dropped, True


# --------------------------------------------------------------------------- jobs


def _job_out(job: SiteDashboardJob) -> JobOut:
    status, error = job.status, job.error
    started = job.created_at if job.created_at.tzinfo else job.created_at.replace(tzinfo=UTC)
    stale = datetime.now(UTC) - started > timedelta(seconds=STALE_AFTER_S)
    if status in ("queued", "running") and stale:
        status, error = "failed", "The job took too long. Try again."
    return JobOut(
        status=status,  # type: ignore[arg-type]
        stage=job.stage,
        dashboard_id=job.dashboard_id,
        proposal=job.proposal,
        dropped=[DroppedWidget.model_validate(d) for d in job.dropped],
        error=error,
    )


async def _update(job: SiteDashboardJob, **fields: Any) -> None:
    for k, v in fields.items():
        setattr(job, k, v)
    job.updated_at = datetime.now(UTC)
    await job.save()


async def get_job(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    job_id: str,
    _cloudflare: Any | None = None,
) -> JobOut:
    from beanie import PydanticObjectId

    await svc.view_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    try:
        oid = PydanticObjectId(job_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("site_dashboard_job", job_id) from exc
    job = await SiteDashboardJob.find_one(
        {"_id": oid, "workspace_id": workspace_id, "site_pocket_id": pocket_id}
    )
    if job is None:
        raise NotFound("site_dashboard_job", job_id)
    return _job_out(job)


def _slot(target: data_service.DataTarget) -> str:
    return f"dash:gen:{target.workspace_id or '-'}:{target.pocket_id}"


async def _claim(target: data_service.DataTarget) -> str:
    slot = _slot(target)
    if not await budget.claim_once(slot, JOB_TTL_S):
        raise svc._rate_limited(30)
    return slot


Runner = Callable[[SiteDashboardJob], Awaitable[None]]


async def _launch(
    job: SiteDashboardJob, target: data_service.DataTarget, slot: str, runner: Runner
) -> None:
    """Run ``runner`` as the job: failures become a clean ``failed`` status; the
    site's slot is always given back."""

    async def _go() -> None:
        try:
            await _update(job, status="running")
            await asyncio.wait_for(runner(job), timeout=JOB_TTL_S)
        except budget.DataBusy:
            await _update(job, status="failed", error="Your site's data is busy. Try again soon.")
        except TimeoutError:
            await _update(job, status="failed", error="The job took too long. Try again.")
        except Exception:  # noqa: BLE001 — a job must always end with a status
            logger.exception("site_dashboards: job %s failed", job.id)
            await _update(job, status="failed", error="Something went wrong. Try again.")
        finally:
            await budget.release(slot)

    task = asyncio.create_task(_go())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def new_job(
    target: data_service.DataTarget, user_id: str, mode: str, dashboard_id: str | None = None
) -> SiteDashboardJob:
    now = datetime.now(UTC)
    job = SiteDashboardJob(
        workspace_id=target.workspace_id,
        site_pocket_id=target.pocket_id,
        mode=mode,
        dashboard_id=dashboard_id,
        created_by=user_id,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=1),
    )
    await job.insert()
    return job


@svc.busy_as_rate_limited
async def start_generation(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    mode: str = "new",
    dashboard_id: str | None = None,
    include_stats: bool = False,
    focus: str | None = None,
    _cloudflare: Any | None = None,
) -> GenerateOut:
    """Queue a generation job; ``{job_id}``."""
    if mode != "new":
        raise ValidationError(
            "sites.dashboard_mode_unsupported", "Only new dashboards can be generated yet."
        )
    target = await svc.manage_target(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id, _cloudflare=_cloudflare
    )
    if target.reason:
        raise svc.unavailable(target.reason)
    await svc.check_room(workspace_id, pocket_id)
    cf = svc.client(target, _cloudflare)
    schema = await d1.introspect(cf, target.d1_id)
    if not any(prompt.usable_columns(t) for t in prompt.usable_tables(schema)):
        raise svc.unavailable("no_tables")
    slot = await _claim(target)
    try:
        await take_daily(workspace_id, "generate")
    except RateLimited:
        await budget.release(slot)
        raise
    job = await new_job(target, user_id, mode)
    await svc._audit(
        target,
        user_id,
        "site_dashboard.generate",
        str(job.id),
        mode=mode,
        include_stats=include_stats,
        focus=bool(focus),
    )

    async def _run(job: SiteDashboardJob) -> None:
        async def _stage(name: str) -> None:
            await _update(job, stage=name)

        widgets, dropped, answered = await design(
            cf, target.d1_id, schema, include_stats=include_stats, focus=focus, stage=_stage
        )
        dropped_out = [d.model_dump() for d in dropped]
        if not answered:
            await refund_daily(workspace_id, "generate")
            await _update(job, status="failed", error=NO_ANSWER, dropped=dropped_out)
            return
        if not widgets:
            await _update(job, status="failed", error=NOTHING_WORKED, dropped=dropped_out)
            return
        await _update(job, stage="saving")
        stored = await svc.build_widgets(
            lay_out(widgets), [], schema, cf, target.d1_id, new_origin="ai"
        )
        doc = await svc.insert_dashboard(
            target,
            user_id,
            "Overview",
            widgets=stored,
            schema=schema,
            generated={
                "model": prompt.model_name(),
                "prompt_version": prompt.PROMPT_VERSION,
                "at": svc.iso(datetime.now(UTC)),
                "include_stats": include_stats,
            },
        )
        await _update(job, status="done", stage=None, dashboard_id=str(doc.id), dropped=dropped_out)

    await _launch(job, target, slot, _run)
    return GenerateOut(job_id=str(job.id))
