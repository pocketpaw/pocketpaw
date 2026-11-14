"""Platform-wide stats: the operator dashboard and the early-access launch read.

Read-only and platform-wide, not per-tenant: no workspace path parameter,
because both screens roll up every tenant. Gated at ``platform.stats.read``
(SUPPORT), and every load is audited once (see ``dashboard`` for why one call,
not five).

``GET /stats/dashboard`` reads ``platform_daily_rollup``. ``CreditLedgerEntry``
has only workspace-prefixed indexes, so "platform spend last month" would be a
full scan of the hot metering write path; the rollup is the only way to ask it.
Nothing writes the rollup yet (the nightly job is a separate follow-up), so an
empty collection is a normal state: ``rollup_count == 0`` and ``as_of`` None,
never a 500. Its live "today" block scans the ledger for the current UTC day
only, and no parameter can widen that window.

``GET /stats/early-access`` is the launch read EA-16 draws: signups, credits
burned on unpaid workspaces, notify-me and waitlist per UTC day, the share of
granted users at or below zero, today's early-access meter against the ceiling,
and the launch controls. All live queries, no rollup; ``days`` is capped at 90
because the burn series is the same unindexed ledger scan, bounded by its window.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from bson import ObjectId
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.billing.plans import _TIER_ORDER, BASE_PLAN_KEY
from pocketpaw_ee.cloud.credits.service import _PAID_CAUSES, _SPEND_CAUSES
from pocketpaw_ee.cloud.launch_controls import service as launch_controls_service
from pocketpaw_ee.cloud.launch_controls.dto import LaunchControlsResponse
from pocketpaw_ee.cloud.metering import service as metering_service
from pocketpaw_ee.cloud.models.credit import CreditBalance, CreditLedgerEntry
from pocketpaw_ee.cloud.models.platform_rollup import PlatformDailyRollup
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.waitlist import WaitlistEntry
from pocketpaw_ee.cloud.models.workspace import Workspace
from pocketpaw_ee.cloud.platform import audit
from pocketpaw_ee.guards.abac import PLAN_FEATURES

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


def _spend_match(since: datetime) -> dict:
    """Applied spend debits created at or after ``since``: the one definition of
    "spend" both the live today block and the early-access burn series read."""
    return {
        "cause": {"$in": list(_SPEND_CAUSES)},
        "applied": True,
        "amount_delta_micro": {"$lt": 0},
        "createdAt": {"$gte": since},
    }


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
    cursor = CreditLedgerEntry.get_pymongo_collection().aggregate(
        [
            {"$match": _spend_match(day_start)},
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


# --- early-access launch read --------------------------------------------------


class EarlyAccessDayOut(BaseModel):
    date: str
    signups: int
    credits_burned: int
    notify: int
    waitlist: int


class EarlyAccessTodayOut(BaseModel):
    meter: int
    ceiling: int


class EarlyAccessStatsOut(BaseModel):
    days: list[EarlyAccessDayOut]
    cap_share: float
    today: EarlyAccessTodayOut | None
    controls: LaunchControlsResponse
    errors: list[str]


async def _unpaid_burn_by_day(since: datetime) -> dict[str, int]:
    """Micro-credits spent per UTC day on unpaid workspaces since ``since``.

    Unpaid mirrors the database legs of ``guards.is_platform_funded_free``: a
    workspace whose plan resolves to ``free`` (no plan, or one outside the
    catalog, counts as free) and whose ledger holds no applied paid credits.
    The ``payments_open`` leg is left out so the series still means something
    after payments open.
    """
    # global-read: platform stats, every tenant by design.
    paid = await CreditLedgerEntry.get_pymongo_collection().distinct(
        "workspace", {"cause": {"$in": list(_PAID_CAUSES)}, "applied": True}
    )
    # Paid means a catalog tier other than the base one. A missing, unknown or
    # retired plan string resolves to free in ``entitlements_from_plan``, so it
    # stays in the series here too.
    paid_tiers = [k for k in PLAN_FEATURES if k != BASE_PLAN_KEY]
    # global-read: platform stats, every tenant by design.
    non_free = [
        str(w)
        for w in await Workspace.get_pymongo_collection().distinct(
            "_id", {"plan": {"$in": paid_tiers}}
        )
    ]
    match = _spend_match(since) | {"workspace": {"$nin": [*paid, *non_free]}}
    cursor = CreditLedgerEntry.get_pymongo_collection().aggregate(
        [
            {"$match": match},
            {
                "$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$createdAt"}},
                    "micro": {"$sum": {"$subtract": [0, "$amount_delta_micro"]}},
                }
            },
        ]
    )
    if inspect.isawaitable(cursor):
        cursor = await cursor
    return {row["_id"]: int(row["micro"]) async for row in cursor}


async def _cap_share() -> float:
    """Granted users whose granted workspace is at or below 0, over all granted."""
    # global-read: platform stats, every granted user.
    granted = await User.get_pymongo_collection().distinct(
        "early_access_workspace_id", {"early_access_granted_at": {"$ne": None}}
    )
    granted = [w for w in granted if w]
    if not granted:
        return 0.0
    # global-read: platform stats, the granted workspaces' wallets.
    funded = await CreditBalance.find(
        {"workspace": {"$in": granted}, "balance_micro": {"$gt": 0}}
    ).count()
    return (len(granted) - funded) / len(granted)


def _count_by_day(stamps: list[datetime | None], counts: dict[str, dict[str, int]], key: str):
    for stamp in stamps:
        bucket = counts.get(_day_str(stamp.date())) if stamp else None
        if bucket is not None:
            bucket[key] += 1


@router.get("/early-access", response_model=EarlyAccessStatsOut)
async def early_access(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.stats.read"))],
    days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> EarlyAccessStatsOut:
    """Early-access launch numbers for the trailing ``days`` UTC days, today
    included, every day present (zero-filled). Live queries, audited once."""
    now = datetime.now(UTC)
    since = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=days - 1)
    counts = {
        d: {"signups": 0, "credits_burned": 0, "notify": 0, "waitlist": 0}
        for d in _date_range(_day_str(since.date()), _day_str(now.date()))
    }
    errors: list[str] = []

    # Signups: users by ObjectId time; a kiosk guest row is not a signup.
    # global-read: platform stats, every user.
    signup_ids = await User.get_pymongo_collection().distinct(
        "_id", {"_id": {"$gte": ObjectId.from_datetime(since)}, "is_guest": {"$ne": True}}
    )
    _count_by_day([oid.generation_time for oid in signup_ids], counts, "signups")

    # global-read: platform stats, every user.
    notified = User.get_pymongo_collection().find(
        {"notify_plans_at": {"$gte": since}}, {"notify_plans_at": 1}
    )
    _count_by_day([doc["notify_plans_at"] async for doc in notified], counts, "notify")

    # global-read: platform stats, the waitlist has no tenant.
    joined = WaitlistEntry.get_pymongo_collection().find(
        {"created_at": {"$gte": since}}, {"created_at": 1}
    )
    _count_by_day([doc["created_at"] async for doc in joined], counts, "waitlist")

    for day, micro in (await _unpaid_burn_by_day(since)).items():
        if day in counts:
            counts[day]["credits_burned"] = metering_service.credits_ceil(micro)

    cap_share = 0.0
    try:
        cap_share = await _cap_share()
    except Exception as exc:  # noqa: BLE001 - one broken block must not sink the page
        logger.exception("platform.stats.early_access: cap share failed")
        errors.append(f"cap_share: {exc}")

    # Read fresh, like the launch-controls GET, not the 30 s per-process snapshot.
    launch_controls_service.invalidate_cache()
    controls = await launch_controls_service.get_launch_controls()
    if controls.read_failed:
        errors.append("controls: read failed, showing defaults")

    today: EarlyAccessTodayOut | None = None
    try:
        today = EarlyAccessTodayOut(
            meter=await metering_service.early_access_credits_used_today(),
            ceiling=controls.daily_free_ceiling_credits,
        )
    except Exception as exc:  # noqa: BLE001 - one broken block must not sink the page
        logger.exception("platform.stats.early_access: meter read failed")
        errors.append(f"today: {exc}")

    result = EarlyAccessStatsOut(
        days=[EarlyAccessDayOut(date=d, **v) for d, v in counts.items()],
        cap_share=cap_share,
        today=today,
        controls=LaunchControlsResponse.model_validate(controls, from_attributes=True),
        errors=errors,
    )
    await audit.record_read(
        operator=operator,
        action="platform.stats.read",
        query=f"early-access days={days}",
        target_type="platform_early_access",
        request=request,
    )
    return result


__all__ = ["router"]
