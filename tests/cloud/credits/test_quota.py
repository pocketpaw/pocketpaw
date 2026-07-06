# tests/cloud/credits/test_quota.py — proves the chunk-2 monthly credit-quota
# reads + assertion in the BC-1 credits service.
#
# Covers:
#   * ``month_to_date_spend`` — sums ONLY the current-UTC-month
#     ``compute_spend`` / ``litellm_spend`` APPLIED debits; excludes the prior
#     month, non-spend causes (top_up / subscription_grant / grant), and
#     ``applied=False`` phantoms. Server-side ``$sum`` (run via async-iteration so
#     it survives the mongomock-motor harness, which cannot ``await`` an
#     aggregation cursor). Tenant-scoped.
#   * ``check_quota`` — raises ``QuotaExceeded`` (402 ``credits.quota_exceeded``)
#     when month-to-date spend >= the effective ceiling; no-op below; no-op when
#     the plan ceiling is None (Enterprise / uncapped); the boundary is ``>=``
#     (exactly-at-ceiling raises).
#   * TOP-UP NETTING (the money-adjacent invariant): a ``top_up`` grant in the
#     current month RAISES the effective ceiling (spend between the plan ceiling
#     and ceiling+topup does NOT raise; above DOES). The recurring
#     ``subscription_grant`` allotment does NOT inflate the ceiling.
#   * a no-plan / unknown-plan workspace FAILS CLOSED to the Free 1000 ceiling
#     (via ``resolve_entitlements``).
#
# Uses the shared ``mongo_db`` fixture (mongomock-motor + Beanie over
# ALL_DOCUMENTS). ``createdAt`` is back-dated via the raw collection (the Insert
# event stamps createdAt=now, so a controlled date can't be set through the
# constructor) — the same seeding style as test_spend_by_model.py. The plan is set
# by inserting a real ``Workspace(plan=...)`` so ``resolve_entitlements`` resolves
# the real catalog ceiling (Free=1000, Go=2250, Enterprise=None).
#
# Created 2026-06-30 (feat/billing-quota-enforcement, chunk 2): new test module —
# locks the monthly credit-quota contract before chunk 3 wires the gate.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.errors import QuotaExceeded
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.workspace import Workspace

pytestmark = pytest.mark.asyncio

WS = "ws_quota"


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed(
    *,
    when: datetime,
    amount_delta: int,
    cause: str,
    applied: bool = True,
    idem: str,
    ws: str = WS,
) -> None:
    """Insert one ledger entry stamped at ``when`` (back-dated via the raw
    collection so the month-window boundary can be asserted)."""
    entry = CreditLedgerEntry(
        workspace=ws,
        kind="grant" if amount_delta > 0 else "spend",
        amount_delta=amount_delta,
        balance_after=0,
        applied=applied,
        conditional=False,
        cause=cause,
        ref={},
        idempotency_key=idem,
    )
    await entry.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": entry.id}, {"$set": {"createdAt": when}}
    )


async def _make_workspace(plan: str, *, slug: str) -> str:
    """Insert a real Workspace doc so ``resolve_entitlements`` reads its plan."""
    ws = Workspace(name="Tenant", slug=slug, owner="u-owner", plan=plan)
    await ws.insert()
    return str(ws.id)


def _now() -> datetime:
    return datetime.now(UTC)


def _this_month(day: int = 15, hour: int = 12) -> datetime:
    """A timestamp inside the CURRENT UTC calendar month."""
    n = _now()
    return datetime(n.year, n.month, day, hour, tzinfo=UTC)


def _last_month() -> datetime:
    """A timestamp in the PREVIOUS UTC calendar month (before this month's 1st)."""
    n = _now()
    month_start = datetime(n.year, n.month, 1, tzinfo=UTC)
    return month_start - timedelta(days=2)


# ===========================================================================
# month_to_date_spend
# ===========================================================================


async def test_mtd_sums_current_month_spend_causes(mongo_db):
    await _seed(when=_this_month(2), amount_delta=-100, cause="compute_spend", idem="a")
    await _seed(when=_this_month(10), amount_delta=-50, cause="litellm_spend", idem="b")

    assert await credits.month_to_date_spend(WS) == 150  # 100 + 50, both spend causes


async def test_mtd_excludes_prior_month(mongo_db):
    await _seed(when=_last_month(), amount_delta=-9999, cause="compute_spend", idem="old")
    await _seed(when=_this_month(5), amount_delta=-40, cause="compute_spend", idem="new")

    assert await credits.month_to_date_spend(WS) == 40  # last month's -9999 excluded


async def test_mtd_excludes_non_spend_causes(mongo_db):
    # Real spend.
    await _seed(when=_this_month(3), amount_delta=-30, cause="compute_spend", idem="spend")
    # Grants under non-spend causes — must not subtract from / count toward spend.
    await _seed(when=_this_month(3), amount_delta=5000, cause="top_up", idem="topup")
    await _seed(when=_this_month(3), amount_delta=1500, cause="subscription_grant", idem="allot")
    await _seed(when=_this_month(3), amount_delta=1000, cause="genesis", idem="gen")

    assert await credits.month_to_date_spend(WS) == 30  # only the compute_spend debit


async def test_mtd_excludes_unapplied_phantom(mongo_db):
    await _seed(when=_this_month(4), amount_delta=-20, cause="compute_spend", idem="real")
    await _seed(
        when=_this_month(4),
        amount_delta=-1000,
        cause="compute_spend",
        applied=False,  # phantom: committed but never moved the wallet
        idem="phantom",
    )

    assert await credits.month_to_date_spend(WS) == 20  # phantom -1000 excluded


async def test_mtd_zero_when_no_spend(mongo_db):
    assert await credits.month_to_date_spend("ws-never-spent") == 0


async def test_mtd_is_tenant_scoped(mongo_db):
    await _seed(when=_this_month(6), amount_delta=-25, cause="compute_spend", idem="mine")
    await _seed(
        when=_this_month(6),
        amount_delta=-999,
        cause="compute_spend",
        idem="theirs",
        ws="other_ws",
    )

    assert await credits.month_to_date_spend(WS) == 25  # not 25 + 999


async def test_mtd_clamps_stray_positive_to_zero(mongo_db):
    # A (should-never-happen) net-positive under a spend cause must not read as
    # negative spend — clamp to 0.
    await _seed(when=_this_month(7), amount_delta=500, cause="compute_spend", idem="stray")

    assert await credits.month_to_date_spend(WS) == 0


# ===========================================================================
# check_quota — basic raise / no-op / None
# ===========================================================================


async def test_check_quota_raises_at_ceiling_boundary(mongo_db):
    # Free plan → ceiling 1000. Spend EXACTLY 1000 → boundary is >=, so it raises.
    ws = await _make_workspace("free", slug="q-free-boundary")
    await _seed(when=_this_month(2), amount_delta=-1000, cause="compute_spend", idem="s", ws=ws)

    with pytest.raises(QuotaExceeded) as exc:
        await credits.check_quota(ws)
    assert exc.value.status_code == 402
    assert exc.value.code == "credits.quota_exceeded"
    assert exc.value.ceiling == 1000
    assert exc.value.spent == 1000


async def test_check_quota_raises_above_ceiling(mongo_db):
    ws = await _make_workspace("free", slug="q-free-above")
    await _seed(when=_this_month(2), amount_delta=-1200, cause="compute_spend", idem="s", ws=ws)

    with pytest.raises(QuotaExceeded):
        await credits.check_quota(ws)


async def test_check_quota_noop_below_ceiling(mongo_db):
    # Free ceiling 1000, spend 999 → just under → no raise (returns None).
    ws = await _make_workspace("free", slug="q-free-under")
    await _seed(when=_this_month(2), amount_delta=-999, cause="compute_spend", idem="s", ws=ws)

    assert await credits.check_quota(ws) is None


async def test_check_quota_noop_when_uncapped_enterprise(mongo_db):
    # Enterprise → ceiling None → no cap to enforce, even with huge spend.
    ws = await _make_workspace("enterprise", slug="q-ent")
    await _seed(
        when=_this_month(2), amount_delta=-5_000_000, cause="compute_spend", idem="s", ws=ws
    )

    assert await credits.check_quota(ws) is None


async def test_check_quota_uses_paid_tier_ceiling(mongo_db):
    # Go plan → ceiling 2250 (1500 allotment × 1.5). 2000 spent → under → no raise.
    ws = await _make_workspace("go", slug="q-go-under")
    await _seed(when=_this_month(2), amount_delta=-2000, cause="compute_spend", idem="s", ws=ws)
    assert await credits.check_quota(ws) is None
    # 2250 spent → at the ceiling → raises.
    await _seed(when=_this_month(3), amount_delta=-250, cause="compute_spend", idem="s2", ws=ws)
    with pytest.raises(QuotaExceeded):
        await credits.check_quota(ws)


# ===========================================================================
# check_quota — fail-closed on no/unknown plan
# ===========================================================================


async def test_check_quota_no_plan_fails_closed_to_free_1000(mongo_db):
    # A workspace id with NO Workspace doc → resolve_entitlements falls back to the
    # Free base tier → ceiling 1000 (never None/uncapped). Spend 1000 → raises.
    await _seed(
        when=_this_month(2),
        amount_delta=-1000,
        cause="compute_spend",
        idem="s",
        ws="ws-no-plan",
    )
    with pytest.raises(QuotaExceeded) as exc:
        await credits.check_quota("ws-no-plan")
    assert exc.value.ceiling == 1000


async def test_check_quota_unknown_plan_fails_closed_to_free_1000(mongo_db):
    # An unknown/typo'd plan string also resolves to the Free ceiling.
    ws = await _make_workspace("bogus_tier", slug="q-unknown")
    await _seed(when=_this_month(2), amount_delta=-1000, cause="compute_spend", idem="s", ws=ws)
    with pytest.raises(QuotaExceeded) as exc:
        await credits.check_quota(ws)
    assert exc.value.ceiling == 1000


# ===========================================================================
# TOP-UP NETTING (the money-adjacent invariant)
# ===========================================================================


async def test_topup_raises_effective_ceiling_below_extended_cap_noop(mongo_db):
    # Free ceiling 1000 + a 500-credit top_up THIS month → effective ceiling 1500.
    # Spend 1200 (between 1000 and 1500) → NO raise (the top-up extended the cap).
    ws = await _make_workspace("free", slug="q-topup-under")
    await _seed(when=_this_month(1), amount_delta=500, cause="top_up", idem="tu", ws=ws)
    await _seed(when=_this_month(2), amount_delta=-1200, cause="compute_spend", idem="s", ws=ws)

    assert await credits.check_quota(ws) is None


async def test_topup_raises_effective_ceiling_above_extended_cap_raises(mongo_db):
    # Free 1000 + 500 top_up → effective 1500. Spend 1500 → at the EXTENDED ceiling
    # → raises (boundary >=), and the reported ceiling is the EXTENDED 1500.
    ws = await _make_workspace("free", slug="q-topup-over")
    await _seed(when=_this_month(1), amount_delta=500, cause="top_up", idem="tu", ws=ws)
    await _seed(when=_this_month(2), amount_delta=-1500, cause="compute_spend", idem="s", ws=ws)

    with pytest.raises(QuotaExceeded) as exc:
        await credits.check_quota(ws)
    assert exc.value.ceiling == 1500  # plan 1000 + top-up 500
    assert exc.value.spent == 1500


async def test_subscription_grant_does_not_inflate_ceiling(mongo_db):
    # The recurring monthly allotment (cause="subscription_grant") is the ceiling's
    # OWN baseline — it must NOT raise the effective cap. Free 1000 + a
    # subscription_grant of 5000 → effective ceiling STAYS 1000. Spend 1000 →
    # raises with ceiling == 1000 (the allotment did not extend it).
    ws = await _make_workspace("free", slug="q-allot")
    await _seed(
        when=_this_month(1), amount_delta=5000, cause="subscription_grant", idem="al", ws=ws
    )
    await _seed(when=_this_month(2), amount_delta=-1000, cause="compute_spend", idem="s", ws=ws)

    with pytest.raises(QuotaExceeded) as exc:
        await credits.check_quota(ws)
    assert exc.value.ceiling == 1000  # NOT 1000 + 5000


async def test_prior_month_topup_does_not_extend_this_month(mongo_db):
    # A top-up granted LAST month must not raise THIS month's ceiling — the cap is
    # per current-UTC-month. Free 1000 + last-month top_up 5000 → effective 1000.
    # Spend 1000 this month → raises with ceiling 1000.
    ws = await _make_workspace("free", slug="q-topup-prior")
    await _seed(when=_last_month(), amount_delta=5000, cause="top_up", idem="oldtu", ws=ws)
    await _seed(when=_this_month(2), amount_delta=-1000, cause="compute_spend", idem="s", ws=ws)

    with pytest.raises(QuotaExceeded) as exc:
        await credits.check_quota(ws)
    assert exc.value.ceiling == 1000  # last month's top-up excluded
