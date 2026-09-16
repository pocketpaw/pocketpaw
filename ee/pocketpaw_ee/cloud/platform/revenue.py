"""Platform revenue routes (chunk 9 of the Paw Admin PRD).

Created: 2026-09-16 (feat/platform-stats-revenue) — bundled with chunk 8
(stats.py) as one read-only aggregations PR.

Four read-only, platform-wide endpoints — no workspace path parameter, gated
``platform.revenue.read`` (SUPPORT), each writing exactly one
``PlatformAuditEvent`` via ``audit.record_read``. Unlike the dashboard
(stats.py), none of these need a rollup: they are direct ``$match``/``$group``
aggregations over live ``Payment``/``Subscription`` data, served by the two
indexes added in this same PR (errata C1 in
docs/design/drafts/2026-09-15-paw-admin-prd-corrections.md) —
``Payment.ix_created_at`` and ``Subscription.ix_status_plan_key``.

UNIT WARNING — READ BEFORE TOUCHING THIS FILE. Three incompatible units
coexist on this screen (design doc §7.1):

  * ``CreditLedgerEntry.amount_delta_micro`` — MICRO-credits
    (``credits.domain.MICRO_PER_CREDIT`` = 1_000_000). Not read here at all.
  * ``Payment.amount_credits`` / ``credits_granted`` / ``credits_reversed`` —
    WHOLE credits. This is the one that bites: passing a Payment figure
    through ``micro_to_credits`` would silently divide it by a further
    million. Every dollar figure this module returns from ``Payment`` stays
    in whole credits, full stop — no conversion call appears anywhere below.
  * ``PlanTier.price_usd_monthly`` — whole USD dollars. MRR converts this to
    **cents** (``* 100``) once, at computation time, because "$9" as a float
    is a rounding hazard the moment two plans get summed.

If you are about to import ``micro_to_credits`` or ``MICRO_PER_CREDIT`` into
this file, stop: nothing here should need it.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.billing.plans import get_plan
from pocketpaw_ee.cloud.billing.service import _BILLABLE_STATUSES, _PAID_UP_STATUSES
from pocketpaw_ee.cloud.models.payment import Payment
from pocketpaw_ee.cloud.models.subscription import Subscription
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.workspace import Workspace
from pocketpaw_ee.cloud.platform import audit

router = APIRouter(prefix="/revenue", tags=["platform"])

_STATUS_VALUES = ("active", "on_hold", "expired", "cancelled")


async def _aggregate(model, pipeline: list[dict]) -> list[dict]:
    """Run a pymongo aggregation, awaiting the cursor when the driver needs it.

    Same cross-driver idiom as ``credits.service._sum_amount_delta``: prod's
    async pymongo client returns a coroutine from ``.aggregate()``; mongomock's
    test double returns a directly-iterable cursor.
    """
    cursor = model.get_pymongo_collection().aggregate(pipeline)
    if inspect.isawaitable(cursor):
        cursor = await cursor
    return [row async for row in cursor]


def _parse_day(value: str | None, default: datetime) -> datetime:
    if value is None:
        return default
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def _default_to(now: datetime) -> datetime:
    """The upper bound to use when the caller omits ``to``: "up to right now".

    Padded by a hair past ``now`` — Windows' ~15.6ms clock resolution means a
    payment inserted moments before this call can carry the exact same
    timestamp as ``now``, and the range's upper bound is EXCLUSIVE, so an
    unpadded default would drop it. The pad is small enough to never cross a
    calendar day boundary in practice and does not affect any caller-supplied
    ``to``.
    """
    return now + timedelta(seconds=1)


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


class RevenueSummaryOut(BaseModel):
    range_from: str
    range_to: str
    gross_credits: int
    granted_credits: int
    reversed_credits: int
    payment_count: int
    refused_count: int
    non_usd_count: int
    currencies: list[str]
    as_of: datetime


@router.get("/summary", response_model=RevenueSummaryOut)
async def summary(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.revenue.read"))],
    from_: Annotated[str | None, Query(alias="from", description="YYYY-MM-DD, inclusive")] = None,
    to: Annotated[str | None, Query(description="YYYY-MM-DD, exclusive")] = None,
) -> RevenueSummaryOut:
    """Platform-wide payment totals over ``[from, to)``. All figures are WHOLE CREDITS.

    Defaults to the trailing 30 UTC days. ``credits_granted``/``credits_reversed``
    are nullable on legacy rows — see ``Payment``'s own docstring for why a
    missing ``credits_granted`` reads as "granted everything paid" rather than
    zero, and this pipeline mirrors that with ``$ifNull``.
    """
    now = datetime.now(UTC)
    to_dt = _parse_day(to, _default_to(now))
    from_dt = _parse_day(from_, to_dt - timedelta(days=30))

    match = {"createdAt": {"$gte": from_dt, "$lt": to_dt}}
    rows = await _aggregate(
        Payment,
        [
            {"$match": match},
            {
                "$group": {
                    "_id": None,
                    "gross_credits": {"$sum": "$amount_credits"},
                    "granted_credits": {
                        "$sum": {"$ifNull": ["$credits_granted", "$amount_credits"]}
                    },
                    "reversed_credits": {"$sum": {"$ifNull": ["$credits_reversed", 0]}},
                    "payment_count": {"$sum": 1},
                    "refused_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": [{"$ifNull": ["$credits_granted", -1]}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "non_usd_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$ne": ["$currency", None]},
                                        {"$ne": ["$currency", "USD"]},
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "currencies": {"$addToSet": "$currency"},
                }
            },
        ],
    )

    if rows:
        row = rows[0]
        currencies = sorted(c for c in (row.get("currencies") or []) if c)
        result = RevenueSummaryOut(
            range_from=from_dt.strftime("%Y-%m-%d"),
            range_to=to_dt.strftime("%Y-%m-%d"),
            gross_credits=int(row.get("gross_credits") or 0),
            granted_credits=int(row.get("granted_credits") or 0),
            reversed_credits=int(row.get("reversed_credits") or 0),
            payment_count=int(row.get("payment_count") or 0),
            refused_count=int(row.get("refused_count") or 0),
            non_usd_count=int(row.get("non_usd_count") or 0),
            currencies=currencies,
            as_of=now,
        )
    else:
        result = RevenueSummaryOut(
            range_from=from_dt.strftime("%Y-%m-%d"),
            range_to=to_dt.strftime("%Y-%m-%d"),
            gross_credits=0,
            granted_credits=0,
            reversed_credits=0,
            payment_count=0,
            refused_count=0,
            non_usd_count=0,
            currencies=[],
            as_of=now,
        )

    await audit.record_read(
        operator=operator,
        action="platform.revenue.read",
        query=f"summary from={result.range_from} to={result.range_to}",
        target_type="platform_revenue_summary",
        request=request,
    )
    return result


# --------------------------------------------------------------------------
# series
# --------------------------------------------------------------------------


class RevenueBucketOut(BaseModel):
    period: str
    gross_credits: int
    reversed_credits: int


class RevenueSeriesOut(BaseModel):
    bucket: str
    buckets: list[RevenueBucketOut]


def _iso_week_monday(day: datetime) -> str:
    monday = day - timedelta(days=day.weekday())
    return monday.strftime("%Y-%m-%d")


def _month_key(day: datetime) -> str:
    return day.strftime("%Y-%m")


@router.get("/series", response_model=RevenueSeriesOut)
async def series(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.revenue.read"))],
    from_: Annotated[str | None, Query(alias="from", description="YYYY-MM-DD, inclusive")] = None,
    to: Annotated[str | None, Query(description="YYYY-MM-DD, exclusive")] = None,
    bucket: Annotated[str | None, Query(description="day | week | month")] = None,
) -> RevenueSeriesOut:
    """Zero-filled revenue time series over ``[from, to)``, in WHOLE CREDITS.

    The DB does the grouping by day (bounded to at most ``to - from`` rows
    regardless of payment volume) — coarser buckets, when requested or
    auto-selected by range width, are folded in Python from the daily rows,
    which is cheap because it is bounded by day-count, not payment-count.

    Bucket auto-selection (design doc §7.6): day under 90 days, week up to a
    year, month beyond that.
    """
    now = datetime.now(UTC)
    to_dt = _parse_day(to, _default_to(now))
    from_dt = _parse_day(from_, to_dt - timedelta(days=30))
    span_days = max((to_dt - from_dt).days, 1)

    resolved_bucket = bucket
    if resolved_bucket is None:
        if span_days < 90:
            resolved_bucket = "day"
        elif span_days <= 365:
            resolved_bucket = "week"
        else:
            resolved_bucket = "month"

    rows = await _aggregate(
        Payment,
        [
            {"$match": {"createdAt": {"$gte": from_dt, "$lt": to_dt}}},
            {
                "$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$createdAt"}},
                    "gross_credits": {"$sum": "$amount_credits"},
                    "reversed_credits": {"$sum": {"$ifNull": ["$credits_reversed", 0]}},
                }
            },
        ],
    )
    by_day: dict[str, dict[str, int]] = {
        r["_id"]: {
            "gross_credits": int(r.get("gross_credits") or 0),
            "reversed_credits": int(r.get("reversed_credits") or 0),
        }
        for r in rows
    }

    # Zero-filled daily axis across the full requested range.
    daily_periods: list[str] = []
    d = from_dt
    while d < to_dt:
        daily_periods.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    if resolved_bucket == "day":
        buckets = [
            RevenueBucketOut(
                period=p,
                gross_credits=by_day.get(p, {}).get("gross_credits", 0),
                reversed_credits=by_day.get(p, {}).get("reversed_credits", 0),
            )
            for p in daily_periods
        ]
    else:
        key_fn = _iso_week_monday if resolved_bucket == "week" else _month_key
        folded: dict[str, dict[str, int]] = {}
        order: list[str] = []
        for p in daily_periods:
            day_dt = datetime.strptime(p, "%Y-%m-%d").replace(tzinfo=UTC)
            key = key_fn(day_dt)
            if key not in folded:
                folded[key] = {"gross_credits": 0, "reversed_credits": 0}
                order.append(key)
            src = by_day.get(p, {})
            folded[key]["gross_credits"] += src.get("gross_credits", 0)
            folded[key]["reversed_credits"] += src.get("reversed_credits", 0)
        buckets = [
            RevenueBucketOut(
                period=k,
                gross_credits=folded[k]["gross_credits"],
                reversed_credits=folded[k]["reversed_credits"],
            )
            for k in order
        ]

    result = RevenueSeriesOut(bucket=resolved_bucket, buckets=buckets)

    await audit.record_read(
        operator=operator,
        action="platform.revenue.read",
        query=(
            f"series from={from_dt.strftime('%Y-%m-%d')} "
            f"to={to_dt.strftime('%Y-%m-%d')} bucket={resolved_bucket}"
        ),
        target_type="platform_revenue_series",
        request=request,
    )
    return result


# --------------------------------------------------------------------------
# subscriptions
# --------------------------------------------------------------------------


class SubscriptionsSnapshotOut(BaseModel):
    by_status: dict[str, int]
    active_by_plan: dict[str, int]
    suspended_count: int
    in_grace_count: int
    untracked_active_plans: int
    as_of: datetime


@router.get("/subscriptions", response_model=SubscriptionsSnapshotOut)
async def subscriptions(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.revenue.read"))],
) -> SubscriptionsSnapshotOut:
    """A snapshot of subscription state right now — no time range.

    Served by ``Subscription.ix_status_plan_key``. ``untracked_active_plans``
    catches workspaces that carry a paid ``Workspace.plan`` (e.g. hand-granted
    by an operator, or a data-migration artifact) with no billable
    ``Subscription`` row backing it — a real gap the revenue screen would
    otherwise never surface, since every other figure here is derived FROM the
    ``Subscription`` collection and so is blind to a workspace missing from it
    entirely.
    """
    now = datetime.now(UTC)

    status_rows = await _aggregate(
        Subscription,
        [{"$group": {"_id": "$status", "count": {"$sum": 1}}}],
    )
    by_status = dict.fromkeys(_STATUS_VALUES, 0)
    for r in status_rows:
        by_status[r["_id"]] = int(r.get("count") or 0)

    active_plan_rows = await _aggregate(
        Subscription,
        [
            {"$match": {"status": "active"}},
            {"$group": {"_id": "$plan_key", "count": {"$sum": 1}}},
        ]
    )
    active_by_plan = {r["_id"]: int(r.get("count") or 0) for r in active_plan_rows}

    suspended_count = await Subscription.find({"suspended_at": {"$ne": None}}).count()
    in_grace_count = await Subscription.find(
        {"grace_until": {"$ne": None, "$gt": now}}
    ).count()

    billable_workspaces = {
        s.workspace
        for s in await Subscription.find(
            {"status": {"$in": list(_BILLABLE_STATUSES)}}
        ).to_list()
    }
    paid_workspaces = await Workspace.find(
        {"plan": {"$ne": "free"}, "deleted_at": None}
    ).to_list()
    untracked_active_plans = sum(
        1 for w in paid_workspaces if str(w.id) not in billable_workspaces
    )

    result = SubscriptionsSnapshotOut(
        by_status=by_status,
        active_by_plan=active_by_plan,
        suspended_count=suspended_count,
        in_grace_count=in_grace_count,
        untracked_active_plans=untracked_active_plans,
        as_of=now,
    )

    await audit.record_read(
        operator=operator,
        action="platform.revenue.read",
        query="subscriptions snapshot",
        target_type="platform_revenue_subscriptions",
        request=request,
    )
    return result


# --------------------------------------------------------------------------
# mrr
# --------------------------------------------------------------------------


class MrrOut(BaseModel):
    mrr_usd_cents: int
    priced_subscriptions: int
    assumed_subscriptions: int
    unpriceable_subscriptions: int
    assumption: str
    as_of: datetime


@router.get("/mrr", response_model=MrrOut)
async def mrr(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.revenue.read"))],
) -> MrrOut:
    """Estimated monthly recurring revenue, in USD CENTS. Cannot be exact.

    Design doc §7.2 names this the sharpest gap on the screen: MRR from
    ``Subscription.plan_key`` × the CURRENT catalog price is an estimate, not a
    ledger fact — proration, coupons, and historical pricing at signup are all
    invisible to it. Population is ``_PAID_UP_STATUSES`` (``{active}``), not the
    wider ``_BILLABLE_STATUSES``: "MRR" means currently collecting, and an
    ``on_hold`` subscription is not.

    Three buckets, all counted so the estimate's own shakiness is visible
    instead of silently absorbed into one number:

      * ``priced_subscriptions``   — ``plan_key`` resolves to a catalog tier
        with a real ``price_usd_monthly``. Contributes that price.
      * ``unpriceable_subscriptions`` — resolves to a real tier, but its price
        is "talk to us" (``price_usd_monthly is None`` — currently only
        ``enterprise``). Contributes $0; the true figure is simply unknown.
      * ``assumed_subscriptions`` — ``plan_key`` is not in the current catalog
        at all (a stale/legacy key). Falls back to ``BASE_PLAN_KEY``'s price
        ($0 today) rather than inventing a number for a plan that no longer
        exists — "fail closed, don't invent revenue," the same precedent
        ``resolve_entitlements`` sets for an unknown plan.
    """
    now = datetime.now(UTC)
    rows = await Subscription.find({"status": {"$in": list(_PAID_UP_STATUSES)}}).to_list()

    mrr_cents = 0
    priced = 0
    assumed = 0
    unpriceable = 0
    base_plan = get_plan("free")

    for sub in rows:
        plan = get_plan(sub.plan_key)
        if plan is None:
            assumed += 1
            if base_plan is not None and base_plan.price_usd_monthly is not None:
                mrr_cents += base_plan.price_usd_monthly * 100
        elif plan.price_usd_monthly is None:
            unpriceable += 1
        else:
            priced += 1
            mrr_cents += plan.price_usd_monthly * 100

    result = MrrOut(
        mrr_usd_cents=mrr_cents,
        priced_subscriptions=priced,
        assumed_subscriptions=assumed,
        unpriceable_subscriptions=unpriceable,
        assumption="usd_monthly",
        as_of=now,
    )

    await audit.record_read(
        operator=operator,
        action="platform.revenue.read",
        query="mrr estimate",
        target_type="platform_revenue_mrr",
        request=request,
    )
    return result


__all__ = ["router"]
