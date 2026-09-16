# tests/cloud/platform/test_platform_revenue.py — the platform revenue routes
# (chunk 9 of the Paw Admin PRD): summary, series, subscriptions, mrr.
#
# Calls the route handlers directly (same pattern as test_read_audit.py and
# test_platform_stats.py) — the point under test is the aggregation logic and
# the audit trail, not FastAPI's routing.
#
# Covers:
#   * an empty deployment -> zeros, not an error (no Payment/Subscription rows
#     at all).
#   * the unit boundary that is the whole reason this PR calls out §7.1:
#     Payment figures are WHOLE CREDITS and must never pass through a
#     micro-credit conversion. Seeded with a number that would be wildly wrong
#     if silently divided (or multiplied) by 1,000,000.
#   * the createdAt range filter (served by Payment.ix_created_at).
#   * subscriptions snapshot: by_status, active_by_plan, suspended/in-grace
#     counts, and untracked_active_plans (served by Subscription.ix_status_plan_key).
#   * mrr: the priced / assumed / unpriceable bucketing.
#   * exactly one PlatformAuditEvent row per call.
#
# Created 2026-09-16 (feat/platform-stats-revenue) — chunk 9 of the Paw Admin PRD.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.models.payment import Payment
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.subscription import Subscription
from pocketpaw_ee.cloud.models.user import User as UserDoc
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.platform import revenue as revenue_routes
from starlette.datastructures import Headers
from starlette.requests import Request

pytestmark = pytest.mark.asyncio


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/revenue/summary",
            "headers": Headers(
                raw=[(b"user-agent", b"paw-admin/test"), (b"x-forwarded-for", b"203.0.113.9")]
            ).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


async def _operator() -> UserDoc:
    doc = UserDoc(email="ops@paw.test", hashed_password="x", full_name="Ops", platform_role="support")
    await doc.insert()
    return doc


async def _payment(
    *,
    when: datetime,
    amount_credits: int,
    credits_granted: int | None,
    credits_reversed: int = 0,
    currency: str | None = "USD",
    workspace: str = "ws1",
    event_id: str,
) -> Payment:
    """Insert a Payment back-dated to ``when`` (createdAt is stamped to now() on
    insert, so it is corrected via the raw collection afterward — same idiom
    the credits-ledger tests use for the same reason)."""
    p = Payment(
        workspace=workspace,
        gateway="dodo",
        gateway_event_id=event_id,
        amount_credits=amount_credits,
        credits_granted=credits_granted,
        credits_reversed=credits_reversed,
        currency=currency,
    )
    await p.insert()
    await Payment.get_pymongo_collection().update_one({"_id": p.id}, {"$set": {"createdAt": when}})
    return p


async def _subscription(
    *,
    workspace: str,
    plan_key: str,
    status: str = "active",
    gateway_subscription_id: str,
    grace_until: datetime | None = None,
    suspended_at: datetime | None = None,
) -> Subscription:
    sub = Subscription(
        workspace=workspace,
        gateway="dodo",
        gateway_subscription_id=gateway_subscription_id,
        plan_key=plan_key,
        status=status,
        grace_until=grace_until,
        suspended_at=suspended_at,
    )
    await sub.insert()
    return sub


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


async def test_summary_on_empty_deployment_returns_zeros(mongo_db) -> None:
    operator = await _operator()

    result = await revenue_routes.summary(request=_request(), operator=operator, from_=None, to=None)

    assert result.gross_credits == 0
    assert result.granted_credits == 0
    assert result.reversed_credits == 0
    assert result.payment_count == 0
    assert result.refused_count == 0
    assert result.non_usd_count == 0
    assert result.currencies == []


async def test_summary_reports_whole_credits_not_micro(mongo_db) -> None:
    """A payment of 1,000 credits must come back as 1,000 — not 1,000,000,000
    (multiplied as if it were micro) and not 0.001 (divided as if it were
    micro). This is the exact silent-error class §7.1 warns about."""
    operator = await _operator()
    now = datetime.now(UTC)
    await _payment(when=now, amount_credits=1_000, credits_granted=1_000, event_id="evt-1")

    result = await revenue_routes.summary(request=_request(), operator=operator, from_=None, to=None)

    assert result.gross_credits == 1_000
    assert result.granted_credits == 1_000
    assert result.payment_count == 1


async def test_summary_null_credits_granted_reads_as_fully_granted(mongo_db) -> None:
    """A legacy row with credits_granted=None must read as "granted everything
    paid" (Payment's own contract), not as 0 and not as refused."""
    operator = await _operator()
    now = datetime.now(UTC)
    await _payment(when=now, amount_credits=500, credits_granted=None, event_id="evt-legacy")

    result = await revenue_routes.summary(request=_request(), operator=operator, from_=None, to=None)

    assert result.granted_credits == 500
    assert result.refused_count == 0


async def test_summary_refused_and_non_usd_counts(mongo_db) -> None:
    operator = await _operator()
    now = datetime.now(UTC)
    await _payment(when=now, amount_credits=200, credits_granted=0, event_id="evt-refused")
    await _payment(
        when=now, amount_credits=100, credits_granted=100, currency="EUR", event_id="evt-eur"
    )

    result = await revenue_routes.summary(request=_request(), operator=operator, from_=None, to=None)

    assert result.refused_count == 1
    assert result.non_usd_count == 1
    assert result.currencies == ["EUR", "USD"]


async def test_summary_range_excludes_payments_outside_window(mongo_db) -> None:
    operator = await _operator()
    now = datetime.now(UTC)
    in_range = now - timedelta(days=5)
    out_of_range = now - timedelta(days=40)
    await _payment(when=in_range, amount_credits=10, credits_granted=10, event_id="evt-in")
    await _payment(when=out_of_range, amount_credits=999, credits_granted=999, event_id="evt-out")

    result = await revenue_routes.summary(
        request=_request(),
        operator=operator,
        from_=(now - timedelta(days=30)).strftime("%Y-%m-%d"),
        to=now.strftime("%Y-%m-%d"),
    )

    assert result.gross_credits == 10
    assert result.payment_count == 1


async def test_summary_writes_exactly_one_audit_row(mongo_db) -> None:
    operator = await _operator()

    await revenue_routes.summary(request=_request(), operator=operator, from_=None, to=None)

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.revenue.read"
    assert rows[0].reason.startswith("read:")
    assert rows[0].target_type == "platform_revenue_summary"


# --------------------------------------------------------------------------
# series
# --------------------------------------------------------------------------


async def test_series_on_empty_deployment_is_zero_filled(mongo_db) -> None:
    operator = await _operator()

    result = await revenue_routes.series(
        request=_request(), operator=operator, from_=None, to=None, bucket="day"
    )

    assert result.bucket == "day"
    assert len(result.buckets) > 0
    assert all(b.gross_credits == 0 for b in result.buckets)


async def test_series_daily_buckets_group_by_day_in_whole_credits(mongo_db) -> None:
    operator = await _operator()
    now = datetime.now(UTC)
    d0 = datetime(now.year, now.month, now.day, 8, 0, tzinfo=UTC) - timedelta(days=2)
    d0_later = d0 + timedelta(hours=6)
    d1 = d0 + timedelta(days=1)

    await _payment(when=d0, amount_credits=300, credits_granted=300, event_id="evt-a")
    await _payment(when=d0_later, amount_credits=200, credits_granted=200, event_id="evt-b")
    await _payment(when=d1, amount_credits=50, credits_granted=50, event_id="evt-c")

    result = await revenue_routes.series(
        request=_request(),
        operator=operator,
        from_=(d0 - timedelta(days=1)).strftime("%Y-%m-%d"),
        to=(d1 + timedelta(days=1)).strftime("%Y-%m-%d"),
        bucket="day",
    )

    by_period = {b.period: b.gross_credits for b in result.buckets}
    assert by_period[d0.strftime("%Y-%m-%d")] == 500
    assert by_period[d1.strftime("%Y-%m-%d")] == 50


async def test_series_auto_selects_day_bucket_for_short_range(mongo_db) -> None:
    operator = await _operator()
    now = datetime.now(UTC)

    result = await revenue_routes.series(
        request=_request(),
        operator=operator,
        from_=(now - timedelta(days=10)).strftime("%Y-%m-%d"),
        to=now.strftime("%Y-%m-%d"),
        bucket=None,
    )

    assert result.bucket == "day"


async def test_series_auto_selects_month_bucket_for_long_range(mongo_db) -> None:
    operator = await _operator()
    now = datetime.now(UTC)

    result = await revenue_routes.series(
        request=_request(),
        operator=operator,
        from_=(now - timedelta(days=400)).strftime("%Y-%m-%d"),
        to=now.strftime("%Y-%m-%d"),
        bucket=None,
    )

    assert result.bucket == "month"


# --------------------------------------------------------------------------
# subscriptions
# --------------------------------------------------------------------------


async def test_subscriptions_snapshot_on_empty_deployment(mongo_db) -> None:
    operator = await _operator()

    result = await revenue_routes.subscriptions(request=_request(), operator=operator)

    assert result.by_status == {"active": 0, "on_hold": 0, "expired": 0, "cancelled": 0}
    assert result.active_by_plan == {}
    assert result.suspended_count == 0
    assert result.in_grace_count == 0
    assert result.untracked_active_plans == 0


async def test_subscriptions_snapshot_counts_by_status_and_plan(mongo_db) -> None:
    operator = await _operator()
    await _subscription(workspace="ws1", plan_key="pro", status="active", gateway_subscription_id="s1")
    await _subscription(workspace="ws2", plan_key="go", status="active", gateway_subscription_id="s2")
    await _subscription(workspace="ws3", plan_key="pro", status="on_hold", gateway_subscription_id="s3")
    await _subscription(workspace="ws4", plan_key="go", status="cancelled", gateway_subscription_id="s4")

    result = await revenue_routes.subscriptions(request=_request(), operator=operator)

    assert result.by_status["active"] == 2
    assert result.by_status["on_hold"] == 1
    assert result.by_status["cancelled"] == 1
    assert result.by_status["expired"] == 0
    # active_by_plan is restricted to status == active only.
    assert result.active_by_plan == {"pro": 1, "go": 1}


async def test_subscriptions_snapshot_suspended_and_grace_counts(mongo_db) -> None:
    operator = await _operator()
    now = datetime.now(UTC)
    await _subscription(
        workspace="ws1",
        plan_key="pro",
        status="on_hold",
        gateway_subscription_id="s1",
        suspended_at=now,
    )
    await _subscription(
        workspace="ws2",
        plan_key="pro",
        status="on_hold",
        gateway_subscription_id="s2",
        grace_until=now + timedelta(days=3),
    )
    # Already-expired grace must not count as "in grace" any more.
    await _subscription(
        workspace="ws3",
        plan_key="pro",
        status="expired",
        gateway_subscription_id="s3",
        grace_until=now - timedelta(days=1),
    )

    result = await revenue_routes.subscriptions(request=_request(), operator=operator)

    assert result.suspended_count == 1
    assert result.in_grace_count == 1


async def test_subscriptions_untracked_active_plans(mongo_db) -> None:
    """A workspace on a paid plan with no billable Subscription row backing it
    is a real gap this snapshot must surface."""
    operator = await _operator()
    tracked_ws = await WorkspaceDoc(name="Tracked", slug="tracked", owner="u1", plan="pro").insert()
    untracked_ws = await WorkspaceDoc(name="Untracked", slug="untracked", owner="u2", plan="pro").insert()
    await WorkspaceDoc(name="FreeCo", slug="freeco", owner="u3", plan="free").insert()
    await WorkspaceDoc(
        name="DeletedPaid", slug="deletedpaid", owner="u4", plan="pro", deleted_at=datetime.now(UTC)
    ).insert()

    await _subscription(
        workspace=str(tracked_ws.id), plan_key="pro", status="active", gateway_subscription_id="s-tracked"
    )
    # untracked_ws and the deleted paid workspace get no Subscription row.

    result = await revenue_routes.subscriptions(request=_request(), operator=operator)

    assert result.untracked_active_plans == 1


async def test_subscriptions_writes_exactly_one_audit_row(mongo_db) -> None:
    operator = await _operator()

    await revenue_routes.subscriptions(request=_request(), operator=operator)

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.revenue.read"
    assert rows[0].target_type == "platform_revenue_subscriptions"


# --------------------------------------------------------------------------
# mrr
# --------------------------------------------------------------------------


async def test_mrr_on_empty_deployment_is_zero(mongo_db) -> None:
    operator = await _operator()

    result = await revenue_routes.mrr(request=_request(), operator=operator)

    assert result.mrr_usd_cents == 0
    assert result.priced_subscriptions == 0
    assert result.assumed_subscriptions == 0
    assert result.unpriceable_subscriptions == 0


async def test_mrr_sums_priced_plans_in_usd_cents(mongo_db) -> None:
    operator = await _operator()
    # go = $9/mo, pro = $19/mo.
    await _subscription(workspace="ws1", plan_key="go", status="active", gateway_subscription_id="s1")
    await _subscription(workspace="ws2", plan_key="pro", status="active", gateway_subscription_id="s2")

    result = await revenue_routes.mrr(request=_request(), operator=operator)

    assert result.mrr_usd_cents == (9 + 19) * 100
    assert result.priced_subscriptions == 2
    assert result.unpriceable_subscriptions == 0
    assert result.assumed_subscriptions == 0


async def test_mrr_excludes_on_hold_from_paid_up_population(mongo_db) -> None:
    """MRR means currently collecting — on_hold is billable but not paid up."""
    operator = await _operator()
    await _subscription(workspace="ws1", plan_key="pro", status="on_hold", gateway_subscription_id="s1")

    result = await revenue_routes.mrr(request=_request(), operator=operator)

    assert result.mrr_usd_cents == 0
    assert result.priced_subscriptions == 0


async def test_mrr_enterprise_is_unpriceable_not_zero_revenue(mongo_db) -> None:
    operator = await _operator()
    await _subscription(
        workspace="ws1", plan_key="enterprise", status="active", gateway_subscription_id="s1"
    )

    result = await revenue_routes.mrr(request=_request(), operator=operator)

    assert result.unpriceable_subscriptions == 1
    assert result.priced_subscriptions == 0
    assert result.mrr_usd_cents == 0


async def test_mrr_unknown_plan_key_is_assumed_not_invented(mongo_db) -> None:
    """A stale/legacy plan_key not in the current catalog falls back to the
    base plan's price ($0) rather than being silently dropped or priced up."""
    operator = await _operator()
    await _subscription(
        workspace="ws1", plan_key="team", status="active", gateway_subscription_id="s1"
    )

    result = await revenue_routes.mrr(request=_request(), operator=operator)

    assert result.assumed_subscriptions == 1
    assert result.priced_subscriptions == 0
    assert result.unpriceable_subscriptions == 0
    assert result.mrr_usd_cents == 0


async def test_mrr_writes_exactly_one_audit_row(mongo_db) -> None:
    operator = await _operator()

    await revenue_routes.mrr(request=_request(), operator=operator)

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.revenue.read"
    assert rows[0].target_type == "platform_revenue_mrr"
