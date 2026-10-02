# tests/cloud/partners/test_partner_commissions.py — PH-13: a partner's client
# pays the YEAR once through a one-time pay link, the site activates on the
# client-paid rail, and the partner earns a commission in credits.
#
# Created 2026-10-02 (feat/partners-commissions, PH-13). Covers: the list yearly
# price table and the pure commission rule (25%; founding 40% within 24 months of
# the site's first client payment); POST /partners/pay-link (checkout amount,
# currency and metadata, the pending record, reuse of an open link, 403 / 404 /
# 409 refusals, the renewal window); the verified ``payment.succeeded`` matched
# by payment id (activation for 12 months, commission at 25% / 40% / back to 25%,
# INR via settlement, tax-exclusive totals, replays and re-keyed redeliveries,
# no top-up grant); forged metadata (no record) and mismatched amount / currency /
# product / rail (flagged, nothing moves); refund and lost dispute within 60
# days (commission reversed once, site lapsed) and after 60 days (nothing); the
# renewal sweep lapsing a client-paid site without touching the wallet; the
# subscription-payment top-up guard; summary / earnings / sites / offers fields;
# the delete cascade treating the client rail as done.
#
# Harness: the REAL ``standardwebhooks`` signer and ``DodoProvider`` for webhook
# verification (as test_inr_topup.py), the shared ``mongo_db`` fixture, a fake
# provider for the checkout call, and a stub ``get_settings``.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from dateutil.relativedelta import relativedelta
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import ConflictError, Forbidden, NotFound
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing import site_plans
from pocketpaw_ee.cloud.billing.domain import OneTimeCheckout
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.payment import Payment
from pocketpaw_ee.cloud.models.site import PartnerClientPayment, Site
from pocketpaw_ee.cloud.models.workspace import PartnerProfile
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.partners import _calc, service
from pocketpaw_ee.sites import service as sites_service
from soul_protocol.engine.journal import open_journal
from standardwebhooks import Webhook

from pocketpaw.fabric.journal_store import FabricJournalStore

pytestmark = pytest.mark.asyncio

SECRET = "whsec_" + base64.b64encode(b"billing-test-secret-key-32bytes!").decode()
USD_PRODUCT = "prod_credits_usd"
INR_PRODUCT = "prod_credits_inr"
PHONE = "+919876543210"


# ---------------------------------------------------------------- harness


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path) -> SimpleNamespace:
    import pocketpaw.config as config_mod
    from pocketpaw import stores
    from pocketpaw.fabric import read_model
    from pocketpaw.journal_dep import reset_journal_cache

    stub = SimpleNamespace(
        fx_inr_per_usd=89.0,
        dodo_credit_product_id=USD_PRODUCT,
        dodo_credit_product_id_inr=INR_PRODUCT,
        billing_enforced=False,
        sites_billing_enforced=False,
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: stub)
    monkeypatch.setenv("SOUL_DATA_DIR", str(tmp_path / "soul"))
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path / "pocketpaw")
    stores.reset_store_caches()
    read_model.default_journal_store.cache_clear()
    reset_journal_cache()
    yield stub
    read_model.default_journal_store.cache_clear()
    stores.reset_store_caches()
    reset_journal_cache()


@pytest.fixture(autouse=True)
def redeploys(monkeypatch) -> list[str]:
    """Activation's best-effort republish, recorded instead of run."""
    calls: list[str] = []

    async def _redeploy(site_id: str) -> None:
        calls.append(site_id)

    monkeypatch.setattr(sites_service, "redeploy_site", _redeploy)
    return calls


@pytest.fixture
def store(tmp_path):
    journal = open_journal(tmp_path / "journal.db")
    yield FabricJournalStore(journal)
    journal.close()


class FakeProvider:
    """Stands in for ``create_one_time``; mints a payment id per call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_one_time(self, **kw: Any) -> OneTimeCheckout:
        self.calls.append(kw)
        n = len(self.calls)
        return OneTimeCheckout(checkout_url=f"https://pay.test/{n}", gateway_ref=f"pay_link_{n}")


def _ctx(workspace_id: str) -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace_id,
        request_id="r1",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _partner(
    slug: str, *, country: str = "US", founding: bool = False, status: str = "active"
) -> str:
    ws = WorkspaceDoc(name=slug, slug=slug, owner="u1", plan="go")
    ws.partner = PartnerProfile(
        status=status, footer_name=f"{slug} Prints", billing_country=country, founding=founding
    )
    await ws.insert()
    return str(ws.id)


async def _site(wid: str, **fields: Any) -> Site:
    doc = Site(
        workspace=wid,
        pocket_id=f"pk_{uuid4().hex}",
        owner="u1",
        name="Ravi Stores",
        url="http://local/ravi/",
        **{"plan_tier": "free", "deployed": True, **fields},
    )
    await doc.insert()
    return doc


async def _client(wid: str, store) -> str:
    return (
        await service.create_client(
            _ctx(wid), body={"name": "Ravi", "whatsapp": PHONE}, store=store
        )
    ).id


async def _link(wid: str, store, *, sku: str = "staff_year", provider=None) -> tuple[Any, str]:
    site = await _site(wid)
    out = await service.create_pay_link(
        _ctx(wid),
        body={"client_id": await _client(wid, store), "site_id": str(site.id), "sku": sku},
        store=store,
        provider=provider or FakeProvider(),
    )
    return out, str(site.id)


def _dodo() -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id=USD_PRODUCT,
        credit_product_id_inr=INR_PRODUCT,
    )


async def _deliver(event_type: str, data: dict, event_id: str) -> dict:
    body = json.dumps(
        {
            "business_id": "biz_1",
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": data,
        }
    )
    ts = datetime.now(UTC)
    headers = {
        "webhook-id": event_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": Webhook(SECRET).sign(msg_id=event_id, timestamp=ts, data=body),
    }
    return await billing.handle_webhook(payload=body.encode(), headers=headers, provider=_dodo())


async def _pay(
    payment_id: str,
    *,
    amount: int,
    currency: str = "USD",
    product: str = USD_PRODUCT,
    meta: dict | None = None,
    event_id: str | None = None,
    **extra: Any,
) -> dict:
    data = {
        "payment_id": payment_id,
        "metadata": meta if meta is not None else {},
        "total_amount": amount,
        "currency": currency,
        "product_cart": [{"product_id": product, "quantity": 1}],
        **extra,
    }
    return await _deliver("payment.succeeded", data, event_id or f"evt_{payment_id}")


async def _refund(payment_id: str, *, event_id: str, event_type: str = "refund.succeeded"):
    data = {
        "refund_id": "ref_1",
        "payment_id": payment_id,
        "business_id": "biz_1",
        "status": "succeeded",
        "is_partial": False,
        "currency": "USD",
        "metadata": {},
    }
    if event_type == "dispute.lost":
        data = {"dispute_id": "dis_1", "payment_id": payment_id, "amount": "100", "currency": "USD"}
    return await _deliver(event_type, data, event_id)


async def _lines(wid: str, cause: str) -> list[int]:
    entries, _ = await credits.history(wid, cause=cause)
    return [e.amount_delta for e in entries if e.applied]


async def _record(site_id: str) -> PartnerClientPayment:
    return (await Site.get(site_id)).partner_payments[-1]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------- pure rules


async def test_client_list_prices_per_country() -> None:
    assert site_plans.partner_client_price("site_year", "IN") == ("INR", 358_800)
    assert site_plans.partner_client_price("staff_year", "in") == ("INR", 1_198_800)
    assert site_plans.partner_client_price("site_year", "US") == ("USD", 8_400)
    assert site_plans.partner_client_price("staff_year", "") == ("USD", 22_800)
    with pytest.raises(KeyError):
        site_plans.partner_client_price("staff", "US")


async def test_commission_rate_rule() -> None:
    first = datetime(2026, 1, 15, tzinfo=UTC)
    rate = _calc.commission_rate_bps
    assert rate(founding=False, first_client_payment_at=first, paid_at=first, tier="x") == 2500
    assert rate(founding=True, first_client_payment_at=first, paid_at=first, tier="x") == 4000
    month_24 = first + relativedelta(months=24) - timedelta(seconds=1)
    assert rate(founding=True, first_client_payment_at=first, paid_at=month_24, tier="x") == 4000
    month_25 = first + relativedelta(months=24)
    assert rate(founding=True, first_client_payment_at=first, paid_at=month_25, tier="x") == 2500
    assert _calc.commission_credits(22_800, 2500) == 5_700
    assert _calc.commission_credits(22_800, 4000) == 9_120
    assert _calc.commission_credits(13_469, 2500) == 3_367  # floored


# ---------------------------------------------------------------- pay link


async def test_pay_link_opens_a_list_price_checkout_and_records_it(mongo_db, store) -> None:
    wid = await _partner("in-shop", country="IN")
    prov = FakeProvider()
    out, site_id = await _link(wid, store, sku="site_year", provider=prov)

    assert out.model_dump() == {
        "checkout_url": "https://pay.test/1",
        "site_id": site_id,
        "sku": "site_year",
        "amount_minor": 358_800,
        "currency": "INR",
    }
    [call] = prov.calls
    assert call["amount_credits"] == 358_800 and call["currency"] == "INR"
    assert call["metadata"]["kind"] == "partner_client_payment"
    assert call["metadata"]["site_id"] == site_id
    assert call["metadata"]["workspace_id"] == wid
    rec = await _record(site_id)
    assert (rec.payment_id, rec.status, rec.sku, rec.amount_minor, rec.currency) == (
        "pay_link_1",
        "pending",
        "site_year",
        358_800,
        "INR",
    )
    site = await Site.get(site_id)
    # Nothing is sold until it is paid: no client stamp, no plan.
    assert site.partner_client_id is None and site.plan_tier == "free"
    assert site.subscription_status == "none"


async def test_an_open_link_is_handed_back_not_minted_twice(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    prov = FakeProvider()
    out, site_id = await _link(wid, store, provider=prov)
    client_id = (await Site.get(site_id)).partner_payments[0].client_id
    again = await service.create_pay_link(
        _ctx(wid),
        body={"client_id": client_id, "site_id": site_id, "sku": "staff_year"},
        store=store,
        provider=prov,
    )
    assert again.checkout_url == out.checkout_url
    assert len(prov.calls) == 1
    assert len((await Site.get(site_id)).partner_payments) == 1


async def test_pay_link_refusals(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    other = await _partner("other-shop")
    plain = str((await (WorkspaceDoc(name="p", slug="p", owner="u1")).insert()).id)
    cid = await _client(wid, store)
    other_cid = await _client(other, store)
    prov = FakeProvider()

    async def link(ws: str, site: Site, client: str = cid):
        return await service.create_pay_link(
            _ctx(ws),
            body={"client_id": client, "site_id": str(site.id), "sku": "staff_year"},
            store=store,
            provider=prov,
        )

    with pytest.raises(Forbidden):
        await link(plain, await _site(plain))
    with pytest.raises(NotFound):  # another workspace's site
        await link(wid, await _site(other))
    with pytest.raises(NotFound):  # another workspace's client
        await link(wid, await _site(wid), other_cid)
    soon = datetime.now(UTC) + timedelta(days=200)
    refused = [
        # paid by the wallet, yearly and monthly
        await _site(
            wid,
            plan_tier="staff_year",
            subscription_status="active",
            billing_rail="credits",
            renewal_date=soon,
        ),
        await _site(
            wid,
            plan_tier="staff",
            subscription_status="active",
            billing_rail="credits",
            renewal_date=soon,
        ),
        # paid by a client, outside the renewal window
        await _site(
            wid,
            plan_tier="staff_year",
            subscription_status="active",
            billing_rail="client",
            renewal_date=soon,
        ),
        await _site(wid, billing_rail="plan", plan_tier="staff", subscription_status="active"),
        await _site(wid, deployed=False),
        await _site(wid, foreign_origin=True),
    ]
    for site in refused:
        with pytest.raises(ConflictError) as exc:
            await link(wid, site)
        assert exc.value.status_code == 409
    assert prov.calls == []

    # The renewal: a client-paid site inside its last 30 days, same plan.
    due = datetime.now(UTC) + timedelta(days=20)
    renewing = await _site(
        wid,
        plan_tier="staff_year",
        subscription_status="active",
        billing_rail="client",
        renewal_date=due,
    )
    assert (await link(wid, renewing)).amount_minor == 22_800


# ---------------------------------------------------------------- the payment


async def test_a_matched_payment_activates_the_year_and_pays_25_percent(
    mongo_db, store, redeploys
) -> None:
    wid = await _partner("us-shop")
    out, site_id = await _link(wid, store)

    ack = await _pay("pay_link_1", amount=22_800, meta={"workspace_id": wid})
    assert ack == {"ok": True, "granted": False, "commission_credits": 5_700}

    site = await Site.get(site_id)
    rec = site.partner_payments[0]
    assert (site.plan_tier, site.subscription_status, site.billing_rail) == (
        "staff_year",
        "active",
        "client",
    )
    assert site.partner_client_id == rec.client_id
    step = _aware(site.renewal_date) - (_aware(rec.paid_at) + relativedelta(months=12))
    assert abs(step.total_seconds()) < 1
    assert (rec.status, rec.commission_credits, rec.rate_bps) == ("paid", 5_700, 2_500)
    assert await _lines(wid, "partner_commission") == [5_700]
    assert await _lines(wid, "top_up") == []  # the client's money is never a top-up
    assert await credits.balance(wid) == 5_700
    payment = await Payment.find_one(Payment.gateway_ref == "pay_link_1")
    assert payment.workspace == wid and payment.credits_granted == 0
    assert redeploys == [site_id]


async def test_a_founding_partner_earns_40_percent(mongo_db, store) -> None:
    wid = await _partner("founder", founding=True)
    _, site_id = await _link(wid, store)
    await _pay("pay_link_1", amount=22_800)
    assert await _lines(wid, "partner_commission") == [9_120]
    assert (await _record(site_id)).rate_bps == 4_000


@pytest.mark.parametrize(("months_ago", "expected"), [(23, 4_000), (25, 2_500)])
async def test_the_founding_rate_runs_24_months_from_the_first_client_payment(
    mongo_db, store, months_ago, expected
) -> None:
    wid = await _partner("founder", founding=True)
    first = datetime.now(UTC) - relativedelta(months=months_ago)
    site = await _site(wid)
    site.partner_payments = [
        PartnerClientPayment(
            payment_id="pay_old",
            sku="staff_year",
            amount_minor=22_800,
            currency="USD",
            client_id="c1",
            created_at=first,
            status="paid",
            paid_at=first,
        )
    ]
    await site.save()
    cid = await _client(wid, store)
    await service.create_pay_link(
        _ctx(wid),
        body={"client_id": cid, "site_id": str(site.id), "sku": "staff_year"},
        store=store,
        provider=FakeProvider(),
    )
    await _pay("pay_link_1", amount=22_800)
    assert (await _record(str(site.id))).rate_bps == expected


async def test_a_replay_or_rekeyed_redelivery_pays_and_extends_once(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    _, site_id = await _link(wid, store)
    await _pay("pay_link_1", amount=22_800, event_id="evt_a")
    renewal = (await Site.get(site_id)).renewal_date

    await _pay("pay_link_1", amount=22_800, event_id="evt_a")  # replay
    ack = await _pay("pay_link_1", amount=22_800, event_id="evt_b")  # new delivery id
    assert ack["commission_credits"] == 0
    assert await _lines(wid, "partner_commission") == [5_700]
    assert (await Site.get(site_id)).renewal_date == renewal


async def test_an_inr_payment_pays_commission_on_the_usd_settlement(mongo_db, store) -> None:
    wid = await _partner("in-shop", country="IN")
    _, site_id = await _link(wid, store)
    await _pay(
        "pay_link_1",
        amount=1_198_800,
        currency="INR",
        product=INR_PRODUCT,
        settlement_amount=13_400,
        settlement_currency="USD",
    )
    assert await _lines(wid, "partner_commission") == [3_350]  # 25% of $134.00
    assert (await Site.get(site_id)).subscription_status == "active"
    assert await _lines(wid, "top_up") == [] and await _lines(wid, "bulk_bonus") == []


async def test_tax_on_top_is_accepted_and_kept_out_of_the_commission(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    _, site_id = await _link(wid, store)
    await _pay("pay_link_1", amount=22_800 + 2_052, tax=2_052)
    assert await _lines(wid, "partner_commission") == [5_700]
    assert (await Site.get(site_id)).subscription_status == "active"


async def test_forged_metadata_without_a_record_never_earns_or_activates(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    site = await _site(wid)
    meta = {"workspace_id": wid, "site_id": str(site.id)}
    # No kind: an ordinary top-up on the credits product, exactly as before.
    await _pay("pay_static_1", amount=22_800, meta=meta)
    assert await _lines(wid, "top_up") == [22_800]
    # Claiming to be a client payment with no record behind it: recorded, not granted.
    await _pay("pay_static_2", amount=22_800, meta={**meta, "kind": "partner_client_payment"})
    assert await _lines(wid, "top_up") == [22_800]
    assert await Payment.find_one(Payment.gateway_ref == "pay_static_2") is not None
    assert await _lines(wid, "partner_commission") == []
    fresh = await Site.get(site.id)
    assert fresh.subscription_status == "none" and fresh.plan_tier == "free"


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"amount": 22_700}, "amount"),
        ({"amount": 22_800, "currency": "EUR"}, "currency"),
        # Same digits in rupees on the USD product: never a $228 payment.
        ({"amount": 22_800, "currency": "INR"}, "currency"),
        ({"amount": 22_800, "product": "prod_other"}, "product"),
        ({"amount": 22_800, "product": INR_PRODUCT}, "product"),
    ],
)
async def test_a_payment_that_does_not_match_its_link_moves_nothing(
    mongo_db, store, redeploys, kw, reason
) -> None:
    wid = await _partner("us-shop")
    _, site_id = await _link(wid, store)
    await _pay("pay_link_1", **kw)
    rec = await _record(site_id)
    assert (rec.status, rec.flag_reason) == ("flagged", reason)
    site = await Site.get(site_id)
    assert site.subscription_status == "none" and site.plan_tier == "free"
    assert await credits.balance(wid) == 0
    assert redeploys == []


async def test_a_site_the_wallet_bought_meanwhile_is_flagged(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    _, site_id = await _link(wid, store)
    await Site.get_pymongo_collection().update_one(
        {"_id": (await Site.get(site_id)).id},
        {
            "$set": {
                "plan_tier": "staff_year",
                "subscription_status": "active",
                "billing_rail": "credits",
                "renewal_date": datetime.now(UTC),
            }
        },
    )
    await _pay("pay_link_1", amount=22_800)
    assert (await _record(site_id)).flag_reason == "site_already_paid"
    assert (await Site.get(site_id)).billing_rail == "credits"
    assert await credits.balance(wid) == 0


async def test_a_renewal_payment_extends_from_the_running_year(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    due = datetime.now(UTC) + timedelta(days=10)
    site = await _site(
        wid,
        plan_tier="staff_year",
        subscription_status="active",
        billing_rail="client",
        renewal_date=due,
    )
    cid = await _client(wid, store)
    await service.create_pay_link(
        _ctx(wid),
        body={"client_id": cid, "site_id": str(site.id), "sku": "staff_year"},
        store=store,
        provider=FakeProvider(),
    )
    await _pay("pay_link_1", amount=22_800)
    step = _aware((await Site.get(site.id)).renewal_date) - (due + relativedelta(months=12))
    assert abs(step.total_seconds()) < 1


# ---------------------------------------------------------------- clawback


async def test_a_refund_within_60_days_reverses_once_and_lapses(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    _, site_id = await _link(wid, store)
    await _pay("pay_link_1", amount=22_800)

    await _refund("pay_link_1", event_id="evt_ref")
    await _refund("pay_link_1", event_id="evt_ref")  # redelivery
    await _refund("pay_link_1", event_id="evt_dis", event_type="dispute.lost")

    assert await _lines(wid, "partner_commission_reversal") == [-5_700]
    assert await credits.balance(wid) == 0
    site = await Site.get(site_id)
    assert site.partner_payments[0].status == "reversed"
    assert (site.plan_tier, site.subscription_status, site.renewal_date) == ("free", "none", None)
    assert site.deployed is True


async def test_a_refund_after_60_days_claws_nothing_back(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    _, site_id = await _link(wid, store)
    await _pay("pay_link_1", amount=22_800)
    old = datetime.now(UTC) - timedelta(days=61)
    await Site.get_pymongo_collection().update_one(
        {"_id": (await Site.get(site_id)).id},
        {"$set": {"partner_payments.0.paid_at": old}},
    )
    await _refund("pay_link_1", event_id="evt_late")
    assert await _lines(wid, "partner_commission_reversal") == []
    assert await credits.balance(wid) == 5_700
    site = await Site.get(site_id)
    assert site.subscription_status == "active" and site.partner_payments[0].status == "paid"


# ---------------------------------------------------------------- sweep


async def test_the_sweep_lapses_a_client_paid_year_and_never_debits(mongo_db) -> None:
    from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

    wid = await _partner("us-shop")
    await credits.grant(workspace=wid, amount=50_000, cause="top_up", idempotency_key="seed")
    past = datetime.now(UTC) - timedelta(days=1)
    due = await _site(
        wid,
        plan_tier="staff_year",
        subscription_status="active",
        billing_rail="client",
        renewal_date=past,
        partner_client_id="c1",
    )
    later = await _site(
        wid,
        plan_tier="site_year",
        subscription_status="active",
        billing_rail="client",
        renewal_date=past + timedelta(days=90),
    )

    counts = await sweep_site_renewals()
    assert counts["lapsed"] == 1 and counts["renewed"] == 0
    assert await credits.balance(wid) == 50_000
    assert await _lines(wid, "site_plan") == []
    lapsed = await Site.get(due.id)
    assert (lapsed.plan_tier, lapsed.subscription_status, lapsed.renewal_date) == (
        "free",
        "none",
        None,
    )
    assert lapsed.deployed is True
    assert (await Site.get(later.id)).subscription_status == "active"


# ---------------------------------------------------------------- top-up guard


async def test_a_subscription_payment_is_recorded_not_granted(mongo_db) -> None:
    ack = await _pay(
        "pay_sub_1", amount=4_900, meta={"workspace_id": "ws_sub"}, subscription_id="sub_1"
    )
    assert ack == {"ok": True, "granted": False}
    assert await Payment.find_one(Payment.gateway_ref == "pay_sub_1") is not None
    assert await credits.balance("ws_sub") == 0


# ---------------------------------------------------------------- read views


async def test_summary_earnings_sites_and_offers_show_the_client_side(mongo_db, store) -> None:
    wid = await _partner("us-shop")
    ctx = _ctx(wid)
    _, site_id = await _link(wid, store)
    await _pay("pay_link_1", amount=22_800)
    s2 = await _site(wid)
    cid = (await Site.get(site_id)).partner_client_id
    await service.create_pay_link(
        ctx,
        body={"client_id": cid, "site_id": str(s2.id), "sku": "site_year"},
        store=store,
        provider=FakeProvider(),
    )
    # FakeProvider restarts its count, so this link's id collides; give it its own.
    await Site.get_pymongo_collection().update_one(
        {"_id": s2.id}, {"$set": {"partner_payments.0.payment_id": "pay_link_s2"}}
    )
    await _pay("pay_link_s2", amount=8_400)
    await _refund("pay_link_s2", event_id="evt_ref_s2")

    got = await service.summary(ctx, store=store)
    assert got.commission_credits_30d == 5_700 and got.commission_credits_total == 5_700
    [row] = await service.earnings(ctx, months=1)
    assert row.commission_credits == 5_700
    modes = {s.site_id: s.billing_mode for s in await service.list_sites(ctx, store=store)}
    assert modes == {site_id: "client", str(s2.id): "client"}
    offers = {
        o.sku: (o.client_price_minor, o.client_currency) for o in await service.list_offers(ctx)
    }
    assert offers == {"site_year": (8_400, "USD"), "staff_year": (22_800, "USD")}


async def test_deleting_a_client_paid_site_stops_cleanly() -> None:
    from pocketpaw_ee.sites import delete_cascade

    site = SimpleNamespace(
        id="s1", subscription_status="active", billing_rail="client", renewal_date=datetime.now(UTC)
    )
    assert await delete_cascade._stop_billing(site=site, deps=None) == delete_cascade.OUTCOME_DONE


def _pending(pid: str, at: datetime) -> PartnerClientPayment:
    return PartnerClientPayment(
        payment_id=pid,
        sku="staff_year",
        amount_minor=22_800,
        currency="USD",
        client_id="c1",
        checkout_url=f"https://pay.test/{pid}",
        created_at=at,
    )


async def test_two_renewals_landing_together_buy_two_years(mongo_db) -> None:
    import asyncio

    wid = await _partner("us-shop")
    now = datetime.now(UTC)
    due = now + timedelta(days=10)
    site = await _site(
        wid,
        plan_tier="staff_year",
        subscription_status="active",
        billing_rail="client",
        renewal_date=due,
        partner_client_id="c1",
        partner_payments=[_pending("pay_r1", now), _pending("pay_r2", now)],
    )
    await asyncio.gather(_pay("pay_r1", amount=22_800), _pay("pay_r2", amount=22_800))
    fresh = await Site.get(site.id)
    assert [p.status for p in fresh.partner_payments] == ["paid", "paid"]
    step = _aware(fresh.renewal_date) - (due + relativedelta(months=24))
    assert abs(step.total_seconds()) < 1
    assert await _lines(wid, "partner_commission") == [5_700, 5_700]


async def test_activation_never_reapplies_a_settled_record(mongo_db) -> None:
    wid = await _partner("us-shop")
    now = datetime.now(UTC)
    site = await _site(wid, partner_payments=[_pending("pay_x", now)])
    kw = {"site_id": str(site.id), "payment_id": "pay_x", "commission_credits": 1, "rate_bps": 1}
    assert await sites_service.activate_client_paid_site(paid_at=now, **kw) == "activated"
    renewal = (await Site.get(site.id)).renewal_date
    later = now + timedelta(days=5)
    assert await sites_service.activate_client_paid_site(paid_at=later, **kw) == "paid"
    assert (await Site.get(site.id)).renewal_date == renewal
    assert (
        await sites_service.activate_client_paid_site(
            **{**kw, "payment_id": "pay_none"}, paid_at=now
        )
        == "missing"
    )


async def test_a_renewal_that_read_a_stale_year_retries_instead_of_losing_one(
    mongo_db, monkeypatch
) -> None:
    """Forces the interleaving: payment 1 reads the site, payment 2 lands, then
    payment 1 writes. Without the compare-and-set on the renewal date read,
    payment 1 would overwrite payment 2's year and a paid year would vanish."""
    wid = await _partner("us-shop")
    now = datetime.now(UTC)
    due = now + timedelta(days=10)
    site = await _site(
        wid,
        plan_tier="staff_year",
        subscription_status="active",
        billing_rail="client",
        renewal_date=due,
        partner_payments=[_pending("pay_r1", now), _pending("pay_r2", now)],
    )
    real = sites_service._SiteDoc.find_one
    raced = []

    async def find_one(*args, **kw):
        doc = await real(*args, **kw)
        if not raced:
            raced.append(True)
            await sites_service.activate_client_paid_site(
                site_id=str(site.id),
                payment_id="pay_r2",
                paid_at=now,
                commission_credits=0,
                rate_bps=0,
            )
        return doc

    monkeypatch.setattr(sites_service._SiteDoc, "find_one", find_one)
    outcome = await sites_service.activate_client_paid_site(
        site_id=str(site.id), payment_id="pay_r1", paid_at=now, commission_credits=0, rate_bps=0
    )
    assert outcome == "activated"
    step = _aware((await Site.get(site.id)).renewal_date) - (due + relativedelta(months=24))
    assert abs(step.total_seconds()) < 1
