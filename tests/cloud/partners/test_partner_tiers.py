# tests/cloud/partners/test_partner_tiers.py — PH-15: volume tiers and milestone
# rewards for Paw Partners.
#
# Created 2026-10-02 (feat/partners-tiers). Covers: the tier / discount /
# commission / milestone tables (pure); the 10th active sold site lifting a
# partner to silver right after the sale (the 10th itself is charged at bronze,
# the 11th at the discounted price, offers show it, the debit key family is
# unchanged); a client payment lifting the tier and the NEXT payment earning 30%,
# a founding partner keeping 40%; a lapse holding the tier until the monthly
# review, which runs once a month and has a kill switch; an operator-set tier
# standing until a recompute moves it; renewals at the tier price, also after the
# profile is gone; milestone rewards granted once on lifetime distinct sites
# (re-runs, redeliveries and refunds never re-grant or claw back); the /me,
# summary, earnings and rewards shapes; 403 for a non-partner; tenancy.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from dateutil.relativedelta import relativedelta
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.partners import _calc, service
from soul_protocol.engine.journal import open_journal

from pocketpaw.fabric.journal_store import FabricJournalStore
from tests.cloud.partners.test_partner_commissions import (
    INR_PRODUCT,
    USD_PRODUCT,
    FakeProvider,
    _link,
    _pay,
    _refund,
)
from tests.cloud.partners.test_partner_commissions import _partner as _pay_partner
from tests.cloud.partners.test_partners import (
    _balance,
    _client,
    _ctx,
    _free_site,
    _fund,
    _partner_ws,
    _sell_seams,
    _sold_site,
    _workspace,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_real_journal(tmp_path, monkeypatch):
    from pocketpaw import stores
    from pocketpaw.fabric import read_model
    from pocketpaw.journal_dep import reset_journal_cache

    monkeypatch.setenv("SOUL_DATA_DIR", str(tmp_path / "soul"))
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path / "pocketpaw")
    stores.reset_store_caches()
    read_model.default_journal_store.cache_clear()
    reset_journal_cache()
    yield
    read_model.default_journal_store.cache_clear()
    stores.reset_store_caches()
    reset_journal_cache()


@pytest.fixture
def store(tmp_path):
    journal = open_journal(tmp_path / "journal.db")
    yield FabricJournalStore(journal)
    journal.close()


@pytest.fixture
def pay_settings(monkeypatch):
    """The webhook path's settings (credit products, FX), as test_partner_commissions."""
    import pocketpaw.config as config_mod

    stub = SimpleNamespace(
        fx_inr_per_usd=89.0,
        dodo_credit_product_id=USD_PRODUCT,
        dodo_credit_product_id_inr=INR_PRODUCT,
        billing_enforced=False,
        sites_billing_enforced=False,
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: stub)

    async def _redeploy(site_id: str) -> None:
        return None

    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setattr(sites_service, "redeploy_site", _redeploy)


async def _seed_active(wid: str, n: int, *, status: str = "active") -> list[Site]:
    """``n`` sold sites on an active paid year (no ledger rows: not lifetime sales)."""
    out = []
    for _ in range(n):
        doc = Site(
            workspace=wid,
            pocket_id=f"pk_{uuid4().hex}",
            owner="u1",
            name="Seeded",
            url="http://local/seeded/",
            deployed=True,
            plan_tier="site_year",
            subscription_status=status,
            billing_rail="credits",
            renewal_date=datetime.now(UTC) + timedelta(days=200),
            period_paid_usd=29,
            partner_client_id="c-seed",
        )
        await doc.insert()
        out.append(doc)
    return out


async def _tier(wid: str) -> str:
    return (await WorkspaceDoc.get(wid)).partner.tier


async def _set_tier(wid: str, tier: str) -> None:
    ws = await WorkspaceDoc.get(wid)
    ws.partner.tier = tier
    await ws.save()


async def _rewards(wid: str) -> list[tuple[str, int]]:
    entries, _ = await credits.history(wid, cause=service.PARTNER_REWARD_CAUSE)
    return sorted((e.idempotency_key, e.amount_delta) for e in entries if e.applied)


async def _sold_on_ledger(wid: str, site_id: str, *, tier: str = "site_year", at=None) -> None:
    """A real wallet ``site_plan`` debit for ``site_id`` (what a sale or renewal leaves)."""
    await billing.charge_site_plan_credits(
        workspace_id=wid,
        site_id=site_id,
        tier_key=tier,
        amount_usd=29,
        period_start=at or datetime.now(UTC),
    )


# ---------------------------------------------------------------- pure tables


async def test_tier_boundaries_and_next_tier() -> None:
    assert [_calc.tier_for(n) for n in (-1, 0, 9, 10, 24, 25, 500)] == [
        "bronze",
        "bronze",
        "bronze",
        "silver",
        "silver",
        "gold",
        "gold",
    ]
    assert _calc.next_tier(0) == {"name": "silver", "at": 10, "remaining": 10}
    assert _calc.next_tier(9) == {"name": "silver", "at": 10, "remaining": 1}
    assert _calc.next_tier(10) == {"name": "gold", "at": 25, "remaining": 15}
    assert _calc.next_tier(25) is None
    assert [_calc.rank(t) for t in ("bronze", "silver", "gold", "platinum")] == [0, 1, 2, 0]


async def test_discounts_floor_the_whole_usd_price() -> None:
    assert [_calc.wholesale_discount_bps(t) for t in ("bronze", "silver", "gold", "?")] == [
        0,
        1_000,
        2_000,
        0,
    ]
    price = site_plans.partner_price_usd
    assert [price("site_year", "IN", t) for t in ("bronze", "silver", "gold")] == [17, 15, 13]
    assert [price("site_year", "US", t) for t in ("bronze", "silver", "gold")] == [29, 26, 23]
    assert [price("staff_year", "IN", t) for t in ("bronze", "silver", "gold")] == [56, 50, 44]
    assert [price("staff_year", "US", t) for t in ("bronze", "silver", "gold")] == [89, 80, 71]
    assert price("site_year", "IN") == 17  # no tier = bronze
    # A removed partner's renewal keeps any price the rung is really sold at.
    assert site_plans.partner_prices_usd("site_year") == {29, 26, 23, 17, 15, 13}
    assert site_plans.partner_prices_usd("site") == frozenset()


async def test_commission_rates_by_tier_and_founding() -> None:
    first = datetime(2026, 1, 15, tzinfo=UTC)
    after = first + relativedelta(months=24)

    def rate(tier: str, *, founding: bool = False, at: datetime = first) -> int:
        return _calc.commission_rate_bps(
            founding=founding, first_client_payment_at=first, paid_at=at, tier=tier
        )

    assert [rate(t) for t in ("bronze", "silver", "gold", "?")] == [2_500, 3_000, 3_500, 2_500]
    assert [rate(t, founding=True) for t in ("bronze", "silver", "gold")] == [4_000] * 3
    assert [rate(t, founding=True, at=after) for t in ("bronze", "silver", "gold")] == [
        2_500,
        3_000,
        3_500,
    ]


async def test_milestones_reached_on_lifetime_sites() -> None:
    assert _calc.milestones_reached(0) == []
    assert _calc.milestones_reached(1) == [1]
    assert _calc.milestones_reached(9) == [1]
    assert _calc.milestones_reached(10) == [1, 10]
    assert _calc.milestones_reached(49) == [1, 10, 25]
    assert _calc.milestones_reached(50) == [1, 10, 25, 50]
    assert dict(_calc.MILESTONES) == {1: 200, 10: 1_000, 25: 3_000, 50: 7_500}


# ---------------------------------------------------------------- tier up on a sale


async def test_the_tenth_active_site_lifts_to_silver_and_the_next_sale_is_discounted(
    mongo_db, store, monkeypatch
) -> None:
    _sell_seams(monkeypatch)
    wid = await _partner_ws("us-shop", country="US")
    ctx = _ctx(wid)
    await _fund(wid, 100_000)
    await _seed_active(wid, 9)
    cid = await _client(ctx, store)

    me = await service.get_profile(ctx)
    assert (me.tier, me.active_sites, me.lifetime_sites_sold) == ("bronze", 9, 0)
    assert me.next_tier.model_dump() == {"name": "silver", "at": 10, "remaining": 1}

    tenth = await _free_site(wid)
    await service.sell(
        ctx, body={"client_id": cid, "site_id": tenth, "sku": "site_year"}, store=store
    )
    # The 10th sale itself is charged at bronze; the 1st lifetime sale earns 200.
    assert await _balance(wid) == 100_000 - 2900 + 200
    assert await _tier(wid) == "silver"
    key = billing.site_plan_debit_key(tenth, "site_year", datetime.now(UTC))
    assert (await credits.find_by_key(wid, key)).amount_delta == -2900

    me = await service.get_profile(ctx)
    assert me.model_dump(exclude={"joined_at"}) == {
        "status": "active",
        "tier": "silver",
        "footer_name": "us-shop Prints",
        "billing_country": "US",
        "founding": False,
        "active_sites": 10,
        "lifetime_sites_sold": 1,
        "next_tier": {"name": "gold", "at": 25, "remaining": 15},
        "benefits": {"wholesale_discount_pct": 10.0, "commission_pct": 30.0},
        "slug": None,
        "display_name": None,
        "city": None,
        "country": None,
        "services": [],
        "bio": None,
        "contact_url": None,
        "public": False,
    }
    offers = {o.sku: o.price_credits for o in await service.list_offers(ctx)}
    assert offers == {"site_year": 2600, "staff_year": 8000}

    eleventh = await _free_site(wid)
    before = await _balance(wid)
    await service.sell(
        ctx, body={"client_id": cid, "site_id": eleventh, "sku": "site_year"}, store=store
    )
    assert before - await _balance(wid) == 2600
    assert (await Site.get(eleventh)).period_paid_usd == 26
    key = billing.site_plan_debit_key(eleventh, "site_year", datetime.now(UTC))
    assert (await credits.find_by_key(wid, key)).amount_delta == -2600


async def test_a_renewal_charges_the_tier_price_and_keeps_it_without_a_profile(
    mongo_db,
) -> None:
    from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

    wid = await _partner_ws("in-shop", country="IN")
    await _set_tier(wid, "silver")
    await _fund(wid, 5000)
    doc = await _sold_site(wid, tier="site_year", renewal_date=datetime.now(UTC) - timedelta(1))

    assert (await sweep_site_renewals())["renewed"] == 1
    assert await _balance(wid) == 5000 - 1500
    assert (await Site.get(doc.id)).period_paid_usd == 15

    ws = await WorkspaceDoc.get(wid)
    ws.partner = None
    await ws.save()
    fresh = await Site.get(doc.id)
    # Another due day: the debit key is dated, so the same day would replay.
    fresh.renewal_date = datetime.now(UTC) - timedelta(days=2)
    await fresh.save()
    assert (await sweep_site_renewals())["renewed"] == 1
    assert await _balance(wid) == 5000 - 1500 - 1500, "the discounted price last paid"


# ---------------------------------------------------------------- tier up on a payment


async def test_a_client_payment_lifts_the_tier_and_the_next_one_earns_30(
    mongo_db, store, pay_settings
) -> None:
    wid = await _pay_partner("us-shop")
    await _seed_active(wid, 9)
    prov = FakeProvider()
    await _link(wid, store, provider=prov)

    ack = await _pay("pay_link_1", amount=22_800)
    assert ack["commission_credits"] == 5_700  # rated before the lift: 25%
    assert await _tier(wid) == "silver"

    _, second = await _link(wid, store, provider=prov)
    ack = await _pay("pay_link_2", amount=22_800)
    assert ack["commission_credits"] == 6_840  # 30%
    rec = (await Site.get(second)).partner_payments[0]
    assert (rec.rate_bps, rec.commission_credits) == (3_000, 6_840)


async def test_a_founding_partner_keeps_40_within_24_months_at_any_tier(
    mongo_db, store, pay_settings
) -> None:
    wid = await _pay_partner("us-shop", founding=True)
    await _seed_active(wid, 25)
    await service.refresh_standing(wid)
    assert await _tier(wid) == "gold"
    await _link(wid, store)
    assert (await _pay("pay_link_1", amount=22_800))["commission_credits"] == 9_120  # 40%


# ---------------------------------------------------------------- tier down, monthly


async def test_a_lapse_keeps_the_tier_until_the_monthly_review(mongo_db, monkeypatch) -> None:
    wid = await _partner_ws("in-shop")
    sites = await _seed_active(wid, 10)
    await service.refresh_standing(wid)
    assert await _tier(wid) == "silver"

    sites[0].subscription_status = "cancelled"
    await sites[0].save()
    await service.refresh_standing(wid)  # a sale or payment never lowers it
    assert await _tier(wid) == "silver"

    monkeypatch.setenv("POCKETPAW_PARTNER_TIER_SWEEP_ENABLED", "0")
    assert await service.sweep_partner_tiers() == {"reviewed": 0}
    assert await _tier(wid) == "silver"
    monkeypatch.delenv("POCKETPAW_PARTNER_TIER_SWEEP_ENABLED")

    now = datetime(2026, 10, 2, 12, tzinfo=UTC)
    assert await service.sweep_partner_tiers(now=now) == {"reviewed": 1}
    assert await _tier(wid) == "bronze"
    reviewed = (await WorkspaceDoc.get(wid)).partner.tier_reviewed_at
    assert reviewed.replace(tzinfo=UTC) == now

    # Back up at once; down again only at the NEXT month's review.
    sites[0].subscription_status = "active"
    await sites[0].save()
    await service.refresh_standing(wid)
    assert await _tier(wid) == "silver"
    sites[0].subscription_status = "cancelled"
    await sites[0].save()
    assert await service.sweep_partner_tiers(now=now + timedelta(days=20)) == {"reviewed": 0}
    assert await _tier(wid) == "silver"
    assert await service.sweep_partner_tiers(now=datetime(2026, 11, 1, tzinfo=UTC)) == {
        "reviewed": 1
    }
    assert await _tier(wid) == "bronze"


async def test_the_review_skips_inactive_partners_and_other_workspaces(mongo_db) -> None:
    suspended = await _partner_ws("sus-shop", status="suspended")
    await _set_tier(suspended, "gold")
    plain = str((await _workspace("plain")).id)
    assert await service.sweep_partner_tiers() == {"reviewed": 0}
    assert await _tier(suspended) == "gold"
    await service.refresh_standing(plain)  # no profile: a no-op, never raises
    assert (await WorkspaceDoc.get(plain)).partner is None


async def test_an_operator_tier_stands_until_a_recompute_moves_it(mongo_db) -> None:
    wid = await _partner_ws("in-shop")
    await _set_tier(wid, "gold")  # what the operator PUT writes
    await service.refresh_standing(wid)
    assert await _tier(wid) == "gold"
    await service.sweep_partner_tiers()
    assert await _tier(wid) == "bronze"


async def test_the_tier_write_is_compare_and_set(mongo_db) -> None:
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    wid = await _partner_ws("in-shop")
    assert not await workspace_service.set_partner_tier(wid, expected="silver", tier="gold")
    assert await _tier(wid) == "bronze"
    assert await workspace_service.set_partner_tier(wid, expected="bronze", tier="gold")
    assert await _tier(wid) == "gold"
    assert not await workspace_service.set_partner_tier("not-an-id", expected="x", tier="gold")


# ---------------------------------------------------------------- milestones


async def test_milestones_are_granted_once_on_lifetime_distinct_sites(mongo_db) -> None:
    wid = await _partner_ws("in-shop")
    other = await _partner_ws("other-shop")
    await _fund(wid, 1_000_000)
    await _fund(other, 1_000_000)
    for i in range(9):
        await _sold_on_ledger(wid, f"s{i}")
        await _sold_on_ledger(other, f"o{i}")
    await _sold_on_ledger(other, "o9")  # the other partner's 10th is not ours
    await _sold_on_ledger(wid, "monthly", tier="site")  # not a partner rung
    renewal = datetime.now(UTC) + relativedelta(years=1)
    await _sold_on_ledger(wid, "s0", at=renewal)  # a renewal is the same site

    await service.refresh_standing(wid)
    await service.refresh_standing(wid)
    assert await _rewards(wid) == [(f"partner_reward:{wid}:1", 200)]

    await _sold_on_ledger(wid, "s9")
    await service.refresh_standing(wid)
    await service.refresh_standing(wid)
    assert await _rewards(wid) == [
        (f"partner_reward:{wid}:1", 200),
        (f"partner_reward:{wid}:10", 1_000),
    ]
    await service.refresh_standing(other)
    assert len(await _rewards(other)) == 2  # its own 1st and 10th, keyed to it

    ctx = _ctx(wid)
    ladder = [r.model_dump() for r in await service.rewards(ctx)]
    assert [(r["sites"], r["credits"]) for r in ladder] == list(_calc.MILESTONES)
    assert all(isinstance(r["reached_at"], datetime) for r in ladder[:2])
    assert [r["reached_at"] for r in ladder[2:]] == [None, None]

    got = (await service.summary(ctx)).model_dump()
    assert (got["rewards_credits_30d"], got["rewards_credits_total"]) == (1_200, 1_200)
    assert got["lifetime_sites_sold"] == 10
    [month] = await service.earnings(ctx, months=1)
    assert month.rewards_credits == 1_200


async def test_a_refunded_sale_never_re_grants_or_claws_back_a_milestone(
    mongo_db, store, pay_settings
) -> None:
    wid = await _pay_partner("us-shop")
    prov = FakeProvider()
    await _link(wid, store, provider=prov)
    await _pay("pay_link_1", amount=22_800)
    await _pay("pay_link_1", amount=22_800)  # replay
    await _pay("pay_link_1", amount=22_800, event_id="evt_rekeyed")  # redelivery
    assert await _rewards(wid) == [(f"partner_reward:{wid}:1", 200)]

    await _refund("pay_link_1", event_id="evt_ref")
    await service.refresh_standing(wid)
    assert await _rewards(wid) == [(f"partner_reward:{wid}:1", 200)]
    assert (await service.summary(_ctx(wid))).lifetime_sites_sold == 1  # never lowered

    await _link(wid, store, provider=prov)
    await _pay("pay_link_2", amount=22_800)
    assert await _rewards(wid) == [(f"partner_reward:{wid}:1", 200)]
    assert (await service.summary(_ctx(wid))).lifetime_sites_sold == 2


# ---------------------------------------------------------------- access


async def test_rewards_and_standing_need_a_partner(mongo_db) -> None:
    plain = _ctx(str((await _workspace("plain")).id))
    suspended = _ctx(await _partner_ws("sus-shop", status="suspended"))
    for ctx in (plain, suspended):
        with pytest.raises(Forbidden):
            await service.rewards(ctx)
    # /me stays readable for a suspended partner (404 without a profile).
    me: Any = await service.get_profile(suspended)
    assert (me.status, me.active_sites, me.next_tier.name) == ("suspended", 0, "silver")
