# tests/cloud/billing/test_inr_topup.py — PH-4: Paw Partners top up the wallet in
# INR. A verified INR ``payment.succeeded`` grants credits (from Dodo's USD
# settlement figure when present, else the configured FX rate) plus a SEPARATE
# ``bulk_bonus`` ledger line at Rs.25,000 (+10%) and Rs.1,00,000 (+20%); both
# lines are exactly-once; any other non-USD currency still grants nothing; and a
# refund / lost dispute of an INR top-up reverses pro rata, exactly base + bonus
# on a full refund and never more than was granted.
#
# Same harness as test_dodo_webhook.py / test_reversals.py: the REAL
# ``standardwebhooks`` signer, the shared ``mongo_db`` fixture, and a mocked
# ``AsyncDodoPayments`` for the checkout path. Settings are pinned per test with a
# stub ``get_settings`` so the FX rate never comes from a developer's config.json.
#
# Created 2026-10-02 (feat/partners-inr-topup, PH-4): new test module.

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing.dto import CreateTopupRequest
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.payment import Payment
from standardwebhooks import Webhook

WS = "ws_inr_partner"
SECRET = "whsec_" + base64.b64encode(b"billing-test-secret-key-32bytes!").decode()
PRODUCT_ID = "prod_credits_sku"
PRODUCT_ID_INR = "prod_credits_sku_inr"
PAYMENT_ID = "pay_inr_1"

RS_10K = 1_000_000  # paise
RS_25K = 2_500_000
RS_1L = 10_000_000


@pytest.fixture(autouse=True)
def _fx_89(monkeypatch):
    import pocketpaw.config as config_mod

    monkeypatch.setattr(config_mod, "get_settings", lambda: SimpleNamespace(fx_inr_per_usd=89.0))


def _provider(**kw) -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id=PRODUCT_ID,
        credit_product_id_inr=kw.get("inr_product", PRODUCT_ID_INR),
    )


def _sign(body: str, *, msg_id: str) -> dict[str, str]:
    ts = datetime.now(UTC)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body),
    }


def _envelope(event_type: str, data: dict) -> str:
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": data,
        }
    )


def _payment_body(*, paise: int, currency: str = "INR", settlement: tuple | None = None) -> str:
    data = {
        "payment_id": PAYMENT_ID,
        "metadata": {"workspace_id": WS},
        "total_amount": paise,
        "currency": currency,
    }
    if settlement is not None:
        data["settlement_amount"], data["settlement_currency"] = settlement
    return _envelope("payment.succeeded", data)


async def _deliver(body: str, event_id: str) -> dict:
    return await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id=event_id), provider=_provider()
    )


async def _lines(cause: str) -> list[int]:
    entries, _ = await credits.history(WS, cause=cause)
    return [e.amount_delta for e in entries]


# --- grants ------------------------------------------------------------------


async def test_rs_25k_grants_fx_base_plus_a_separate_10pct_bonus_line(mongo_db):
    result = await _deliver(_payment_body(paise=RS_25K), "evt_inr_25k")

    assert result == {"ok": True, "granted": True}
    # 2_500_000 paise // 89 == 28_089 credits; 10% bonus floored == 2_808.
    assert await _lines("top_up") == [28_089]
    assert await _lines("bulk_bonus") == [2_808]
    assert await credits.balance(WS) == 28_089 + 2_808

    row = await Payment.find_one(Payment.gateway_event_id == "evt_inr_25k")
    assert row.currency == "INR"
    assert row.amount_credits == RS_25K  # what was PAID, in paise
    assert row.credits_granted == 28_089 + 2_808  # the reversal cap


async def test_rs_1l_earns_a_20pct_bonus(mongo_db):
    await _deliver(_payment_body(paise=RS_1L), "evt_inr_1l")

    assert await _lines("top_up") == [112_359]
    assert await _lines("bulk_bonus") == [22_471]


async def test_rs_10k_earns_no_bonus(mongo_db):
    await _deliver(_payment_body(paise=RS_10K), "evt_inr_10k")

    assert await _lines("top_up") == [11_235]
    assert await _lines("bulk_bonus") == []
    assert await credits.balance(WS) == 11_235


async def test_usd_settlement_figure_wins_over_the_fx_rate(mongo_db):
    await _deliver(_payment_body(paise=RS_25K, settlement=(28_000, "USD")), "evt_inr_settle")

    assert await _lines("top_up") == [28_000]
    assert await _lines("bulk_bonus") == [2_800]


async def test_a_non_usd_settlement_falls_back_to_the_fx_rate(mongo_db):
    await _deliver(_payment_body(paise=RS_25K, settlement=(RS_25K, "INR")), "evt_inr_settle_inr")

    assert await _lines("top_up") == [28_089]


async def test_replaying_an_inr_event_grants_nothing_new_on_either_line(mongo_db, recording_bus):
    body = _payment_body(paise=RS_25K)
    first = await _deliver(body, "evt_inr_replay")
    second = await _deliver(body, "evt_inr_replay")

    assert first["granted"] is True
    assert second["granted"] is False
    assert await _lines("top_up") == [28_089]
    assert await _lines("bulk_bonus") == [2_808]
    assert await credits.balance(WS) == 30_897
    captured = [e for e in recording_bus.events if e.type == "billing.topup.captured"]
    assert len(captured) == 1
    assert captured[0].data["amount_credits"] == 30_897


async def test_usd_topup_is_unchanged_and_earns_no_bonus(mongo_db):
    await _deliver(_payment_body(paise=5_000_000, currency="USD"), "evt_usd_big")

    assert await _lines("top_up") == [5_000_000]
    assert await _lines("bulk_bonus") == []


async def test_eur_grants_nothing_and_logs_without_the_amount(mongo_db, caplog):
    caplog.set_level(logging.INFO, logger="pocketpaw_ee.cloud.billing.service")
    result = await _deliver(_payment_body(paise=2_345_678, currency="EUR"), "evt_eur")

    assert result == {"ok": True, "granted": False}
    assert await credits.balance(WS) == 0
    row = await Payment.find_one(Payment.gateway_event_id == "evt_eur")
    assert row.credits_granted == 0
    assert "evt_eur" in caplog.text and "EUR" in caplog.text
    assert "2345678" not in caplog.text


# --- reversals ---------------------------------------------------------------


def _refund(amount: int | None, *, is_partial: bool) -> str:
    data = {
        "refund_id": "ref_1",
        "payment_id": PAYMENT_ID,
        "business_id": "biz_1",
        "status": "succeeded",
        "is_partial": is_partial,
        "currency": "INR",
        "metadata": {},
    }
    if amount is not None:
        data["amount"] = amount
    return _envelope("refund.succeeded", data)


async def test_full_inr_refund_reverses_exactly_base_plus_bonus(mongo_db):
    await _deliver(_payment_body(paise=RS_25K), "evt_inr_pay")

    result = await _deliver(_refund(RS_25K, is_partial=False), "evt_inr_refund_full")

    assert result["reversed"] == 30_897
    assert await credits.balance(WS) == 0


async def test_full_inr_refund_without_a_stated_amount_reverses_base_plus_bonus(mongo_db):
    await _deliver(_payment_body(paise=RS_25K), "evt_inr_pay")

    result = await _deliver(_refund(None, is_partial=False), "evt_inr_refund_nostated")

    assert result["reversed"] == 30_897
    assert await credits.balance(WS) == 0


async def test_partial_inr_refunds_reverse_pro_rata_and_never_exceed_the_grant(mongo_db):
    await _deliver(_payment_body(paise=RS_25K), "evt_inr_pay")

    # Rs.5,000 of Rs.25,000 back -> one fifth of 30_897, floored. NOT 500_000
    # credits, which is what reading paise as credits would have taken.
    first = await _deliver(_refund(500_000, is_partial=True), "evt_inr_part_1")
    assert first["reversed"] == 6_179
    assert await credits.balance(WS) == 30_897 - 6_179

    # The remaining Rs.20,000: four fifths floored. The pair rounds down, so
    # one credit stays — the under-reversal direction.
    second = await _deliver(_refund(2_000_000, is_partial=True), "evt_inr_part_2")
    assert second["reversed"] == 24_717
    assert await credits.balance(WS) == 1

    # A refund overstating what was paid can never take past the grant.
    third = await _deliver(_refund(RS_25K, is_partial=True), "evt_inr_part_3")
    assert third["reversed"] == 1
    assert await credits.balance(WS) == 0
    row = await Payment.find_one(Payment.gateway_ref == PAYMENT_ID)
    assert row.credits_reversed == row.credits_granted == 30_897


async def test_lost_dispute_on_an_inr_topup_reverses_base_plus_bonus(mongo_db):
    await _deliver(_payment_body(paise=RS_1L), "evt_inr_pay_1l")
    body = _envelope(
        "dispute.lost",
        {
            "dispute_id": "dis_1",
            "payment_id": PAYMENT_ID,
            "business_id": "biz_1",
            "amount": str(RS_1L),
            "currency": "INR",
            "dispute_stage": "dispute",
            "dispute_status": "lost",
        },
    )

    result = await _deliver(body, "evt_inr_dispute")

    assert result["reversed"] == 112_359 + 22_471
    assert await credits.balance(WS) == 0


# --- checkout ----------------------------------------------------------------


def _mock_dodo(monkeypatch) -> MagicMock:
    fake_response = MagicMock(payment_link="https://checkout.test/pay/x", payment_id="pay_x")
    fake_client = MagicMock()
    fake_client.payments.create = AsyncMock(return_value=fake_response)
    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=fake_client))
    return fake_client


async def test_inr_topup_checkout_charges_paise_on_the_inr_product(mongo_db, monkeypatch):
    client = _mock_dodo(monkeypatch)

    await billing.create_topup(
        workspace_id=WS, user_id="u1", amount_credits=RS_25K, currency="INR", provider=_provider()
    )

    _, kwargs = client.payments.create.call_args
    assert kwargs["product_cart"][0] == {
        "product_id": PRODUCT_ID_INR,
        "quantity": 1,
        "amount": RS_25K,
    }
    assert kwargs["billing_currency"] == "INR"


async def test_usd_topup_checkout_does_not_pin_a_currency(mongo_db, monkeypatch):
    client = _mock_dodo(monkeypatch)

    await billing.create_topup(
        workspace_id=WS, user_id="u1", amount_credits=1000, provider=_provider()
    )

    _, kwargs = client.payments.create.call_args
    assert kwargs["product_cart"][0]["product_id"] == PRODUCT_ID
    assert "billing_currency" not in kwargs


async def test_inr_topup_without_an_inr_product_is_refused(mongo_db):
    with pytest.raises(ValidationError) as exc:
        await billing.create_topup(
            workspace_id=WS,
            user_id="u1",
            amount_credits=RS_25K,
            currency="INR",
            provider=_provider(inr_product=None),
        )
    assert exc.value.code == "billing.product_unconfigured"


async def test_unsupported_topup_currency_is_refused(mongo_db):
    with pytest.raises(ValidationError):
        await billing.create_topup(
            workspace_id=WS, user_id="u1", amount_credits=100, currency="EUR", provider=_provider()
        )


def test_topup_dto_ceiling_is_per_currency():
    assert CreateTopupRequest(amount_credits=RS_1L, currency="INR").currency == "INR"
    assert CreateTopupRequest(amount_credits=100_000_000, currency="INR")
    with pytest.raises(ValueError):
        CreateTopupRequest(amount_credits=100_000_001, currency="INR")
    with pytest.raises(ValueError):
        CreateTopupRequest(amount_credits=1_000_001)
    with pytest.raises(ValueError):
        CreateTopupRequest(amount_credits=100, currency="EUR")
