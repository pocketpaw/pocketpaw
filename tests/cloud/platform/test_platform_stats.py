# tests/cloud/platform/test_platform_stats.py — the platform dashboard read
# (chunk 8 of the Paw Admin PRD).
#
# Calls ``stats.dashboard(...)`` directly, matching the pattern in
# test_read_audit.py: the point under test is "does the handler compute and
# audit correctly", not FastAPI's routing/dependency wiring (that is covered
# generically by test_platform_guard.py + test_platform_matrix.py, which
# already cover ``platform.stats.read`` since it was pre-registered in
# PLATFORM_ACTIONS).
#
# Covers:
#   * an empty deployment (job never ran) -> zeros everywhere, not an error or
#     a division by zero — the first-class state this screen must handle from
#     day one, before the nightly rollup job exists.
#   * a unit-boundary case that would fail on a micro/whole-credit mixup: a
#     rollup seeded with a specific spend_micro must come back unconverted.
#   * the live "today" block reads ONLY the current UTC day from
#     CreditLedgerEntry, ignoring older entries and the requested range.
#   * exactly one PlatformAuditEvent row per dashboard load.
#
# Created 2026-09-16 (feat/platform-stats-revenue) — chunk 8 of the Paw Admin PRD.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.credits.domain import MICRO_PER_CREDIT
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.platform_rollup import ModelSpendRollup, PlatformDailyRollup
from pocketpaw_ee.cloud.models.user import User as UserDoc
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.platform import stats as stats_routes
from starlette.datastructures import Headers
from starlette.requests import Request

pytestmark = pytest.mark.asyncio


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/stats/dashboard",
            "headers": Headers(
                raw=[(b"user-agent", b"paw-admin/test"), (b"x-forwarded-for", b"203.0.113.9")]
            ).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


async def _operator() -> UserDoc:
    doc = UserDoc(
        email="ops@paw.test", hashed_password="x", full_name="Ops", platform_role="support"
    )
    await doc.insert()
    return doc


async def _rollup(
    *,
    day: str,
    workspace: str,
    spend_micro: int,
    runs: int = 1,
    tokens: int = 100,
    plan: str = "free",
    deleted: bool = False,
    by_model: list[ModelSpendRollup] | None = None,
) -> PlatformDailyRollup:
    row = PlatformDailyRollup(
        day=day,
        workspace=workspace,
        spend_micro=spend_micro,
        runs=runs,
        tokens=tokens,
        plan=plan,
        deleted=deleted,
        by_model=by_model or [],
        generated_at=datetime.now(UTC),
    )
    await row.insert()
    return row


async def _seed_ledger(
    *, workspace: str, when: datetime, spend_micro: int, cause: str = "compute_spend"
) -> None:
    """Insert an applied spend entry back-dated to ``when`` (createdAt is
    stamped to now() on insert, so it is corrected via the raw collection —
    same idiom as tests/cloud/credits/test_spend_by_model.py)."""
    entry = CreditLedgerEntry(
        workspace=workspace,
        kind="spend",
        amount_delta_micro=-spend_micro,
        balance_after_micro=0,
        applied=True,
        conditional=False,
        cause=cause,
        ref={},
        idempotency_key=f"idem-{workspace}-{when.isoformat()}-{spend_micro}",
    )
    await entry.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": entry.id}, {"$set": {"createdAt": when}}
    )


async def test_empty_deployment_returns_zeros_not_errors(mongo_db) -> None:
    operator = await _operator()

    result = await stats_routes.dashboard(
        request=_request(), operator=operator, start=None, end=None
    )

    assert result.rollup_count == 0
    assert result.newest_rollup_day is None
    assert result.as_of is None
    assert result.covered_days == 0
    assert len(result.uncovered_days) == result.requested_days
    assert result.totals.spend_micro == 0
    assert result.totals.runs == 0
    assert result.totals.tokens == 0
    assert result.totals.tenants_with_spend == 0
    assert all(p.spend_micro == 0 for p in result.series)
    assert result.models.rows == []
    assert result.tenants.rows == []
    assert result.tenants.platform_total_micro == 0
    assert all(t.tenants == 0 for t in result.plans.tiers)
    assert result.plans.day is None
    # The live block still runs (it does not depend on the rollup at all) and
    # must report zero spend on a genuinely empty ledger, not fail.
    assert result.today is not None
    assert result.today.spend_micro == 0
    assert result.today.partial is True
    assert result.errors == []


async def test_dashboard_writes_exactly_one_audit_row(mongo_db) -> None:
    operator = await _operator()

    await stats_routes.dashboard(request=_request(), operator=operator, start=None, end=None)

    events = await PlatformAuditEvent.find_all().to_list()
    assert len(events) == 1
    event = events[0]
    assert event.action == "platform.stats.read"
    assert event.reason.startswith("read:")
    assert event.status == "applied"
    assert event.target_type == "platform_dashboard"
    assert event.actor_id == str(operator.id)


async def test_totals_report_raw_micro_credits_not_converted(mongo_db) -> None:
    """A rollup seeded with a whole-credit-looking number must come back
    UNCHANGED — this is the test that fails on a silent micro_to_credits()
    call or any other 1,000,000x conversion error."""
    operator = await _operator()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    # 7 credits worth of spend, expressed correctly in micro.
    seeded_micro = 7 * MICRO_PER_CREDIT
    await _rollup(day=today, workspace="ws1", spend_micro=seeded_micro, runs=3, tokens=500)

    result = await stats_routes.dashboard(
        request=_request(), operator=operator, start=today, end=today
    )

    assert result.totals.spend_micro == seeded_micro
    assert result.totals.spend_micro == 7_000_000
    assert result.rollup_count == 1
    assert result.newest_rollup_day == today


async def test_series_zero_fills_and_sums_across_workspaces(mongo_db) -> None:
    operator = await _operator()
    end = datetime.now(UTC).date()
    start = end - timedelta(days=2)
    day0 = start.strftime("%Y-%m-%d")
    day1 = (start + timedelta(days=1)).strftime("%Y-%m-%d")
    day2 = end.strftime("%Y-%m-%d")

    await _rollup(day=day0, workspace="ws1", spend_micro=1_000_000)
    await _rollup(day=day0, workspace="ws2", spend_micro=2_000_000)
    # day1 has no rollups at all -> must appear zero-filled, not be skipped.

    result = await stats_routes.dashboard(
        request=_request(),
        operator=operator,
        start=day0,
        end=day2,
    )

    by_day = {p.day: p for p in result.series}
    assert by_day[day0].spend_micro == 3_000_000
    assert by_day[day1].spend_micro == 0
    assert by_day[day2].spend_micro == 0
    assert day1 in result.uncovered_days
    assert day2 in result.uncovered_days
    assert day0 not in result.uncovered_days
    assert result.covered_days == 1
    assert result.totals.tenants_with_spend == 2


async def test_tenants_share_bps_uses_full_platform_denominator(mongo_db) -> None:
    operator = await _operator()
    ws1 = await WorkspaceDoc(name="Acme", slug="acme", owner="u1").insert()
    ws2 = await WorkspaceDoc(name="Beta", slug="beta", owner="u2").insert()
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    await _rollup(day=today, workspace=str(ws1.id), spend_micro=3_000_000, plan="pro")
    await _rollup(day=today, workspace=str(ws2.id), spend_micro=1_000_000, plan="free")

    result = await stats_routes.dashboard(
        request=_request(), operator=operator, start=today, end=today
    )

    assert result.tenants.platform_total_micro == 4_000_000
    rows_by_ws = {r.workspace: r for r in result.tenants.rows}
    assert rows_by_ws[str(ws1.id)].share_bps == 7500
    assert rows_by_ws[str(ws2.id)].share_bps == 2500
    assert rows_by_ws[str(ws1.id)].name == "Acme"
    assert rows_by_ws[str(ws1.id)].plan == "pro"


async def test_today_block_ignores_older_entries_and_requested_range(mongo_db) -> None:
    operator = await _operator()
    now = datetime.now(UTC)
    today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    yesterday = today_start - timedelta(hours=2)

    await _seed_ledger(workspace="ws1", when=yesterday, spend_micro=9_000_000)
    await _seed_ledger(
        workspace="ws1", when=today_start + timedelta(minutes=5), spend_micro=1_500_000
    )

    # Even a very old / very wide requested range must not affect "today".
    result = await stats_routes.dashboard(
        request=_request(), operator=operator, start="2000-01-01", end="2000-01-02"
    )

    assert result.today is not None
    assert result.today.spend_micro == 1_500_000
    assert result.today.day == now.strftime("%Y-%m-%d")


async def test_today_block_excludes_non_spend_causes_and_unapplied(mongo_db) -> None:
    operator = await _operator()
    now = datetime.now(UTC)
    today_start = datetime(now.year, now.month, now.day, tzinfo=UTC)

    # A grant (not a spend cause) must not count as spend.
    grant = CreditLedgerEntry(
        workspace="ws1",
        kind="grant",
        amount_delta_micro=5_000_000,
        applied=True,
        cause="top_up",
        idempotency_key="grant-1",
    )
    await grant.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": grant.id}, {"$set": {"createdAt": today_start + timedelta(minutes=1)}}
    )

    # An unapplied phantom spend must not count either.
    phantom = CreditLedgerEntry(
        workspace="ws1",
        kind="spend",
        amount_delta_micro=-2_000_000,
        applied=False,
        cause="compute_spend",
        idempotency_key="phantom-1",
    )
    await phantom.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": phantom.id}, {"$set": {"createdAt": today_start + timedelta(minutes=2)}}
    )

    result = await stats_routes.dashboard(
        request=_request(), operator=operator, start=None, end=None
    )

    assert result.today is not None
    assert result.today.spend_micro == 0
    assert result.today.runs == 0
