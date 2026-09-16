"""Platform-wide stats rollup — the operator dashboard (chunk 8 of the Paw Admin PRD).

Created: 2026-09-16 (feat/platform-stats-revenue) — chunk 8, bundled with
chunk 9 (revenue.py) as one read-only aggregations PR.

Read-only, and platform-wide, not per-tenant: there is no workspace path
parameter here, because the whole point of this screen is a rollup over every
tenant. Gated at ``platform.stats.read`` (SUPPORT), and every load is audited
once — see the docstring on ``dashboard`` for why ONE call, not five.

WHY A ROLLUP COLLECTION EXISTS AT ALL. Verified against the live models
(``ee/pocketpaw_ee/cloud/models/credit.py``): ``CreditLedgerEntry`` carries
exactly three indexes and every one is workspace-prefixed (``workspace``,
``(workspace, idempotency_key)``, ``(workspace, createdAt)``). A query shaped
``{createdAt: {$gte, $lt}}`` with no workspace term — which is exactly what
"platform spend last month" is — can use none of them, so it is a full
collection scan of the hot metering write path. ``platform_daily_rollup``
(``cloud/models/platform_rollup.py``) is the only way to ask that question; see
the design doc's §3.1 for the full verification.

SCOPE OF THIS PR: THE READ SIDE ONLY. The master PRD's chunk-8 row also lists a
nightly arq job and a backfill script that WRITE ``platform_daily_rollup``.
Neither is built here — this PR's deliverables are the model, the dashboard
read endpoint, and tests, and a nightly-job PR is a natural, separately
reviewable follow-up rather than something to fold in unannounced. Until that
job ships, this endpoint runs against an EMPTY collection in every real
deployment, which is exactly why "the job has never run" is treated as a normal
state below (``rollup_count == 0``, ``newest_rollup_day`` and ``as_of`` both
``None``) rather than an error or a 500 — the screen must be able to say "no
data yet" truthfully from day one, before the job exists.

The live "today" block is the one exception to "reads from the rollup only": it
queries ``CreditLedgerEntry`` directly, for the current UTC day alone, and no
query parameter can widen that window (§3.4) — that is the one unindexed
platform-wide scan this screen accepts, because it is bounded to a single day.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.billing.plans import _TIER_ORDER, BASE_PLAN_KEY
from pocketpaw_ee.cloud.credits.service import _SPEND_CAUSES
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.platform_rollup import PlatformDailyRollup
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.workspace import Workspace
from pocketpaw_ee.cloud.platform import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stats", tags=["platform"])

_DEFAULT_WINDOW_DAYS = 30
_MODEL_ROWS_CAP = 20
_TENANT_ROWS_CAP = 10
_BPS = 10_000  # basis points denominator for share_bps


def _day_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _date_range(start: str, end: str) -> list[str]:
    """Every ``YYYY-MM-DD`` day from ``start`` to ``end`` inclusive.

    ``day`` strings sort and compare lexicographically the same as the dates
    they name (ISO order), which is what lets a Mongo ``$gte``/``$lte`` on the
    string field serve a real date range without ever parsing it back.
    """
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    out = []
    d = start_d
    while d <= end_d:
        out.append(_day_str(d))
        d += timedelta(days=1)
    return out


class RangeOut(BaseModel):
    start: str
    end: str


class TotalsOut(BaseModel):
    spend_micro: int
    runs: int
    tokens: int
    tenants_with_spend: int


class SeriesPointOut(BaseModel):
    day: str
    spend_micro: int
    runs: int
    tokens: int


class ModelRowOut(BaseModel):
    model: str
    spend_micro: int
    runs: int
    tokens: int


class ModelsOut(BaseModel):
    rows: list[ModelRowOut]
    truncated_to: int


class TenantRowOut(BaseModel):
    workspace: str
    name: str
    slug: str
    plan: str
    deleted: bool
    spend_micro: int
    runs: int
    share_bps: int


class TenantsOut(BaseModel):
    rows: list[TenantRowOut]
    platform_total_micro: int


class PlanTierCountOut(BaseModel):
    plan: str
    tenants: int


class PlansOut(BaseModel):
    day: str | None
    tiers: list[PlanTierCountOut]


class TodayOut(BaseModel):
    day: str
    as_of: datetime
    spend_micro: int
    runs: int
    partial: bool


class DashboardOut(BaseModel):
    as_of: datetime | None
    newest_rollup_day: str | None
    rollup_count: int
    requested_days: int
    covered_days: int
    uncovered_days: list[str]
    range: RangeOut
    totals: TotalsOut
    series: list[SeriesPointOut]
    models: ModelsOut
    tenants: TenantsOut
    plans: PlansOut
    today: TodayOut | None
    errors: list[str]


async def _today_block() -> TodayOut:
    """Live spend for the current UTC day, across every tenant.

    THE ONE LIVE, UNINDEXED, PLATFORM-WIDE READ THIS SCREEN MAKES. Bounded to a
    single UTC day and to that alone — the window is computed here, server-side,
    and there is no query parameter that can widen it (§3.4). A bookmarked URL
    or a curious operator cannot turn this into the global scan the rollup
    exists to avoid.
    """
    now = datetime.now(UTC)
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    query = {
        "cause": {"$in": list(_SPEND_CAUSES)},
        "applied": True,
        "amount_delta_micro": {"$lt": 0},
        "createdAt": {"$gte": day_start},
    }
    cursor = CreditLedgerEntry.get_pymongo_collection().aggregate(
        [
            {"$match": query},
            {
                "$group": {
                    "_id": None,
                    "spend_micro": {"$sum": {"$subtract": [0, "$amount_delta_micro"]}},
                    "runs": {"$sum": 1},
                }
            },
        ]
    )
    if inspect.isawaitable(cursor):
        cursor = await cursor
    spend_micro = 0
    runs = 0
    async for row in cursor:
        spend_micro = int(row.get("spend_micro") or 0)
        runs = int(row.get("runs") or 0)
        break
    return TodayOut(
        day=_day_str(now.date()),
        as_of=now,
        spend_micro=max(spend_micro, 0),
        runs=runs,
        # Always true: this is a running day, never a closed one. The reader
        # must never mistake "today so far" for "today, final".
        partial=True,
    )


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.stats.read"))],
    start: Annotated[str | None, Query(description="YYYY-MM-DD, inclusive")] = None,
    end: Annotated[str | None, Query(description="YYYY-MM-DD, inclusive")] = None,
) -> DashboardOut:
    """The whole platform dashboard, in one call.

    ONE ENDPOINT, NOT FIVE. Spine §6.9: every cross-tenant read writes a
    ``PlatformAuditEvent``, so five endpoints backing one page load would render
    as five actions in the operator's own trail — the trail would then lie
    about how much they did. It also buys a single watermark: if a rollup lands
    mid-page-load, five separate calls could show two widgets from two
    different days with nothing on the page able to tell the reader. One
    response carries one ``as_of`` by construction.

    Defaults to the trailing 30 UTC days (today exclusive of the range's own
    "today" live block, which is always the current day regardless of
    ``start``/``end``).
    """
    end_day = end or _day_str(datetime.now(UTC).date())
    start_day = start or _day_str(
        datetime.strptime(end_day, "%Y-%m-%d").date() - timedelta(days=_DEFAULT_WINDOW_DAYS - 1)
    )
    requested_days = _date_range(start_day, end_day)
    # Re-derive start/end in case a caller passed them reversed — _date_range
    # already normalised the order; keep the range block honest about it.
    range_start, range_end = requested_days[0], requested_days[-1]

    errors: list[str] = []

    # Newest rollup + collection size are GLOBAL facts, independent of the
    # requested range — they answer "has the job ever run", not "did it cover
    # what I asked for". Collapsing the two would make an outage look like an
    # empty range with nothing to see.
    rollup_count = await PlatformDailyRollup.find_all().count()
    newest = await PlatformDailyRollup.find_all().sort("-generated_at").limit(1).to_list()
    newest_rollup_day = newest[0].day if newest else None
    as_of = newest[0].generated_at if newest else None

    rows = await PlatformDailyRollup.find(
        {"day": {"$gte": range_start, "$lte": range_end}}
    ).to_list()

    covered_day_set = {r.day for r in rows}
    covered_days = len(covered_day_set)
    uncovered_days = [d for d in requested_days if d not in covered_day_set]

    # --- totals + per-day series -------------------------------------------------
    by_day: dict[str, dict[str, int]] = {
        d: {"spend_micro": 0, "runs": 0, "tokens": 0} for d in requested_days
    }
    per_workspace_spend: dict[str, int] = {}
    per_workspace_latest: dict[str, PlatformDailyRollup] = {}
    per_model: dict[str, dict[str, int]] = {}

    for r in rows:
        bucket = by_day.get(r.day)
        if bucket is not None:
            bucket["spend_micro"] += r.spend_micro
            bucket["runs"] += r.runs
            bucket["tokens"] += r.tokens

        per_workspace_spend[r.workspace] = per_workspace_spend.get(r.workspace, 0) + r.spend_micro
        latest = per_workspace_latest.get(r.workspace)
        if latest is None or r.day >= latest.day:
            per_workspace_latest[r.workspace] = r

        for m in r.by_model:
            slot = per_model.setdefault(m.model, {"spend_micro": 0, "runs": 0, "tokens": 0})
            slot["spend_micro"] += m.spend_micro
            slot["runs"] += m.runs
            slot["tokens"] += m.tokens

    totals = TotalsOut(
        spend_micro=sum(v["spend_micro"] for v in by_day.values()),
        runs=sum(v["runs"] for v in by_day.values()),
        tokens=sum(v["tokens"] for v in by_day.values()),
        tenants_with_spend=sum(1 for v in per_workspace_spend.values() if v > 0),
    )
    series = [
        SeriesPointOut(day=d, spend_micro=v["spend_micro"], runs=v["runs"], tokens=v["tokens"])
        for d, v in sorted(by_day.items())
    ]

    # --- models (top N by spend, server-capped) ----------------------------------
    model_rows_all = sorted(
        (
            ModelRowOut(model=m, spend_micro=v["spend_micro"], runs=v["runs"], tokens=v["tokens"])
            for m, v in per_model.items()
        ),
        key=lambda r: r.spend_micro,
        reverse=True,
    )
    models = ModelsOut(rows=model_rows_all[:_MODEL_ROWS_CAP], truncated_to=_MODEL_ROWS_CAP)

    # --- tenants (top N by spend; share_bps against the FULL platform total) ----
    platform_total_micro = sum(per_workspace_spend.values())
    top_workspace_ids = sorted(
        per_workspace_spend, key=lambda w: per_workspace_spend[w], reverse=True
    )[:_TENANT_ROWS_CAP]
    workspace_docs: dict[str, Workspace] = {}
    if top_workspace_ids:
        try:
            from beanie import PydanticObjectId

            object_ids = [PydanticObjectId(w) for w in top_workspace_ids]
            for w in await Workspace.find({"_id": {"$in": object_ids}}).to_list():
                workspace_docs[str(w.id)] = w
        except Exception as exc:  # noqa: BLE001 — degrade, don't 500 the whole page
            errors.append(f"tenants: could not resolve workspace names ({exc})")

    tenant_rows = []
    for wid in top_workspace_ids:
        spend = per_workspace_spend[wid]
        share = round(spend / platform_total_micro * _BPS) if platform_total_micro > 0 else 0
        ws_doc = workspace_docs.get(wid)
        latest_rollup = per_workspace_latest.get(wid)
        tenant_rows.append(
            TenantRowOut(
                workspace=wid,
                name=ws_doc.name if ws_doc else "",
                slug=ws_doc.slug if ws_doc else "",
                plan=latest_rollup.plan if latest_rollup else BASE_PLAN_KEY,
                deleted=(
                    latest_rollup.deleted
                    if latest_rollup
                    else bool(ws_doc.deleted_at)
                    if ws_doc
                    else False
                ),
                spend_micro=spend,
                runs=sum(r.runs for r in rows if r.workspace == wid),
                share_bps=share,
            )
        )
    tenants = TenantsOut(rows=tenant_rows, platform_total_micro=platform_total_micro)

    # --- plan mix, as of the newest rollup day -----------------------------------
    plan_counts: dict[str, int] = dict.fromkeys(_TIER_ORDER, 0)
    plans_day = newest_rollup_day
    if plans_day is not None:
        plan_rows = await PlatformDailyRollup.find({"day": plans_day}).to_list()
        seen_workspaces: set[str] = set()
        for r in plan_rows:
            if r.workspace in seen_workspaces:
                continue
            seen_workspaces.add(r.workspace)
            plan_counts[r.plan] = plan_counts.get(r.plan, 0) + 1
    plans = PlansOut(
        day=plans_day,
        tiers=[PlanTierCountOut(plan=p, tenants=plan_counts.get(p, 0)) for p in _TIER_ORDER],
    )

    # --- live "today" block -------------------------------------------------------
    today: TodayOut | None
    try:
        today = await _today_block()
    except Exception as exc:  # noqa: BLE001 — one broken block must not sink the page
        logger.exception("platform.stats.dashboard: today block failed")
        errors.append(f"today: {exc}")
        today = None

    result = DashboardOut(
        as_of=as_of,
        newest_rollup_day=newest_rollup_day,
        rollup_count=rollup_count,
        requested_days=len(requested_days),
        covered_days=covered_days,
        uncovered_days=uncovered_days,
        range=RangeOut(start=range_start, end=range_end),
        totals=totals,
        series=series,
        models=models,
        tenants=tenants,
        plans=plans,
        today=today,
        errors=errors,
    )

    # Audited once for the whole page load — see the module + function
    # docstrings for why this must not be five separate audit rows.
    await audit.record_read(
        operator=operator,
        action="platform.stats.read",
        query=f"start={range_start} end={range_end}",
        target_type="platform_dashboard",
        request=request,
    )

    return result


__all__ = ["router"]
