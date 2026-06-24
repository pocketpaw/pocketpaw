# tests/cloud/billing/test_dodo_webhook.py — proves the BC-2 Gateway-primitive
# contract: a verified Dodo ``payment.succeeded`` webhook grants the right
# credits EXACTLY ONCE, a tampered/bad-signature webhook is rejected with no
# grant, a non-success event never grants, and ``create_topup`` returns a hosted
# checkout url (the Dodo client mocked).
#
# Signing uses the REAL ``standardwebhooks`` library so the verification path is
# exercised end-to-end (no shortcut around the signature check). The dodopayments
# CLIENT is mocked — ``create_topup`` patches ``AsyncDodoPayments`` on the dodo
# provider module; the webhook path never touches the SDK client (verification is
# pure ``standardwebhooks``).
#
# Uses the shared ``mongo_db`` fixture (mongomock-motor + Beanie over
# ALL_DOCUMENTS) from tests/cloud/conftest.py, the same DB pattern the BC-1
# ledger tests use. The autouse ``recording_bus`` installs a RecordingBus so the
# service's ``emit(BillingTopupCaptured(...))`` never raises and tests can assert
# on the captured events.
#
# Created 2026-06-24 (integration/billing-credits, BC-2): new test module.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud._core.errors import BadRequest, ValidationError
from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.payment import Payment
from standardwebhooks import Webhook

WS = "ws_billing_test"
# A valid Standard-Webhooks secret: ``whsec_`` + base64 of a 32-byte key.
SECRET = "whsec_" + base64.b64encode(b"billing-test-secret-key-32bytes!").decode()
PRODUCT_ID = "prod_credits_sku"


def _provider() -> DodoProvider:
    """A DodoProvider wired with the test webhook secret + product id.

    The API key is set so ``create_one_time`` passes its pre-flight; the SDK
    client is mocked per-test where checkout is exercised.
    """
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id=PRODUCT_ID,
    )


def _sign(body: str, *, msg_id: str, ts: datetime | None = None) -> dict[str, str]:
    """Produce valid Standard-Webhooks headers for ``body`` using ``SECRET``."""
    ts = ts or datetime.now(UTC)
    sig = Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body)  # 'v1,<b64>'
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": sig,
    }


def _payment_succeeded_body(
    *, workspace_id: str = WS, total_amount: int = 500, currency: str = "USD"
) -> str:
    """A Dodo ``payment.succeeded`` webhook body (the real envelope shape)."""
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": "payment.succeeded",
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "payment_id": "pay_abc123",
                "metadata": {"workspace_id": workspace_id},
                "total_amount": total_amount,
                "currency": currency,
            },
        }
    )


# ---------------------------------------------------------------------------
# Criterion 1 — a valid signed payment.succeeded grants the right credits to the
# workspace named in the payment metadata.
# ---------------------------------------------------------------------------


async def test_valid_webhook_grants_credits(mongo_db):
    body = _payment_succeeded_body(workspace_id=WS, total_amount=500)
    headers = _sign(body, msg_id="evt_grant_1")

    assert await credits.balance(WS) == 0
    result = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )

    assert result == {"ok": True, "granted": True}
    # 500 cents -> 500 credits (1 credit == 1 cent).
    assert await credits.balance(WS) == 500

    # A Payment record was written for the capture.
    payments = await Payment.find(Payment.workspace == WS).to_list()
    assert len(payments) == 1
    assert payments[0].gateway == "dodo"
    assert payments[0].gateway_event_id == "evt_grant_1"
    assert payments[0].amount_credits == 500
    assert payments[0].status == "succeeded"


async def test_valid_webhook_emits_capture_event(mongo_db, recording_bus):
    body = _payment_succeeded_body(workspace_id=WS, total_amount=250)
    headers = _sign(body, msg_id="evt_emit_1")

    await billing.handle_webhook(payload=body.encode(), headers=headers, provider=_provider())

    captured = [e for e in recording_bus.events if e.type == "billing.topup.captured"]
    assert len(captured) == 1
    data = captured[0].data
    assert data["workspace_id"] == WS
    assert data["gateway"] == "dodo"
    assert data["event_id"] == "evt_emit_1"
    assert data["amount_credits"] == 250


# ---------------------------------------------------------------------------
# Criterion 2 — a tampered payload / bad signature is REJECTED (400) with no
# grant. ValidationError -> 400 via the cloud error handler.
# ---------------------------------------------------------------------------


async def test_tampered_payload_is_rejected_no_grant(mongo_db):
    body = _payment_succeeded_body(workspace_id=WS, total_amount=500)
    headers = _sign(body, msg_id="evt_tamper_1")

    # Tamper with the body AFTER signing — the signature no longer matches.
    tampered = body.replace('"total_amount": 500', '"total_amount": 999999').encode()

    with pytest.raises(BadRequest) as exc:
        await billing.handle_webhook(payload=tampered, headers=headers, provider=_provider())
    assert exc.value.status_code == 400
    assert exc.value.code == "billing.webhook_invalid_signature"

    # No credits granted, no payment recorded.
    assert await credits.balance(WS) == 0
    assert await Payment.find(Payment.workspace == WS).to_list() == []


async def test_wrong_secret_is_rejected_no_grant(mongo_db):
    body = _payment_succeeded_body(workspace_id=WS, total_amount=500)
    # Sign with a DIFFERENT secret than the provider verifies with.
    other_secret = "whsec_" + base64.b64encode(b"a-totally-different-32byte-key!!!").decode()
    ts = datetime.now(UTC)
    bad_sig = Webhook(other_secret).sign(msg_id="evt_bad", timestamp=ts, data=body)
    headers = {
        "webhook-id": "evt_bad",
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": bad_sig,
    }

    with pytest.raises(BadRequest) as exc:
        await billing.handle_webhook(payload=body.encode(), headers=headers, provider=_provider())
    assert exc.value.status_code == 400
    assert exc.value.code == "billing.webhook_invalid_signature"
    assert await credits.balance(WS) == 0


async def test_missing_signature_headers_rejected(mongo_db):
    body = _payment_succeeded_body()
    with pytest.raises(BadRequest) as exc:
        await billing.handle_webhook(
            payload=body.encode(),
            headers={"content-type": "application/json"},
            provider=_provider(),
        )
    assert exc.value.status_code == 400
    assert exc.value.code == "billing.webhook_invalid_signature"
    assert await credits.balance(WS) == 0


async def test_stale_timestamp_rejected(mongo_db):
    body = _payment_succeeded_body()
    # 10 minutes old — outside Standard-Webhooks' 5-minute tolerance.
    stale = datetime.now(UTC) - timedelta(minutes=10)
    headers = _sign(body, msg_id="evt_stale", ts=stale)
    with pytest.raises(BadRequest) as exc:
        await billing.handle_webhook(payload=body.encode(), headers=headers, provider=_provider())
    assert exc.value.status_code == 400
    assert exc.value.code == "billing.webhook_invalid_signature"
    assert await credits.balance(WS) == 0


# ---------------------------------------------------------------------------
# Criterion 3 — grants EXACTLY ONCE; replay of the same event_id is a no-op
# (balance unchanged, no second emit), via BC-1's unique idempotency key.
# ---------------------------------------------------------------------------


async def test_replay_same_event_id_is_noop(mongo_db, recording_bus):
    body = _payment_succeeded_body(workspace_id=WS, total_amount=500)
    headers = _sign(body, msg_id="evt_replay_1")

    first = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )
    assert first == {"ok": True, "granted": True}
    assert await credits.balance(WS) == 500

    # Re-deliver the SAME event (same webhook-id) — re-signed but identical id.
    second = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )
    assert second == {"ok": True, "granted": False}  # replay no-op
    assert await credits.balance(WS) == 500  # unchanged — no double-grant

    # Exactly one capture event emitted (the replay did not re-emit).
    captured = [e for e in recording_bus.events if e.type == "billing.topup.captured"]
    assert len(captured) == 1

    # Exactly one Payment row (the replay's insert collided on the unique index).
    payments = await Payment.find(Payment.workspace == WS).to_list()
    assert len(payments) == 1


async def test_replay_with_resigned_headers_still_noop(mongo_db):
    """A replay re-signed with a fresh timestamp (valid signature) but the SAME
    webhook-id still grants nothing — idempotency keys on the event id, not the
    signature."""
    body = _payment_succeeded_body(workspace_id=WS, total_amount=300)
    h1 = _sign(body, msg_id="evt_dup_id", ts=datetime.now(UTC) - timedelta(minutes=1))
    h2 = _sign(body, msg_id="evt_dup_id", ts=datetime.now(UTC))  # fresh sig, same id

    await billing.handle_webhook(payload=body.encode(), headers=h1, provider=_provider())
    assert await credits.balance(WS) == 300
    res2 = await billing.handle_webhook(payload=body.encode(), headers=h2, provider=_provider())
    assert res2["granted"] is False
    assert await credits.balance(WS) == 300


# ---------------------------------------------------------------------------
# Criterion 4 — create_topup returns a checkout url (Dodo client mocked).
# ---------------------------------------------------------------------------


async def test_create_topup_returns_checkout_url(mongo_db, monkeypatch):
    # Mock the async DodoPayments client so no network call happens. The provider
    # builds the client via ``AsyncDodoPayments`` imported on the dodo module.
    fake_response = MagicMock()
    fake_response.payment_link = "https://checkout.dodopayments.test/pay/abc123"
    fake_response.payment_id = "pay_xyz789"

    fake_client = MagicMock()
    fake_client.payments.create = AsyncMock(return_value=fake_response)

    import pocketpaw_ee.cloud.billing.providers.dodo as dodo_mod

    monkeypatch.setattr(dodo_mod, "AsyncDodoPayments", MagicMock(return_value=fake_client))

    result = await billing.create_topup(
        workspace_id=WS, user_id="u1", amount_credits=1000, provider=_provider()
    )
    assert result == {"checkout_url": "https://checkout.dodopayments.test/pay/abc123"}

    # The Dodo client was called with the workspace_id on metadata and the
    # pay-what-you-want amount on the cart (1000 credits == 1000 cents).
    _, kwargs = fake_client.payments.create.call_args
    assert kwargs["payment_link"] is True
    assert kwargs["metadata"]["workspace_id"] == WS
    assert kwargs["product_cart"][0]["product_id"] == PRODUCT_ID
    assert kwargs["product_cart"][0]["amount"] == 1000


async def test_create_topup_rejects_non_positive_amount(mongo_db):
    with pytest.raises(ValidationError):
        await billing.create_topup(
            workspace_id=WS, user_id="u1", amount_credits=0, provider=_provider()
        )
    with pytest.raises(ValidationError):
        await billing.create_topup(
            workspace_id=WS, user_id="u1", amount_credits=-50, provider=_provider()
        )


# ---------------------------------------------------------------------------
# Criterion 5 — a non-success event (payment.failed) never grants.
# ---------------------------------------------------------------------------


async def test_payment_failed_event_does_not_grant(mongo_db, recording_bus):
    body = json.dumps(
        {
            "business_id": "biz_1",
            "type": "payment.failed",
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "payment_id": "pay_fail_1",
                "metadata": {"workspace_id": WS},
                "total_amount": 500,
                "currency": "USD",
            },
        }
    )
    headers = _sign(body, msg_id="evt_failed_1")

    result = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )
    assert result == {"ok": True, "granted": False}
    assert await credits.balance(WS) == 0
    assert await Payment.find(Payment.workspace == WS).to_list() == []
    assert [e for e in recording_bus.events if e.type == "billing.topup.captured"] == []


async def test_success_event_without_workspace_id_does_not_grant(mongo_db):
    """A verified success event whose metadata lacks workspace_id is acked (200)
    but grants nothing — it can't be routed."""
    body = json.dumps(
        {
            "business_id": "biz_1",
            "type": "payment.succeeded",
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "payment_id": "pay_noroute",
                "metadata": {},
                "total_amount": 500,
                "currency": "USD",
            },
        }
    )
    headers = _sign(body, msg_id="evt_noroute_1")
    result = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )
    assert result == {"ok": True, "granted": False}


# ---------------------------------------------------------------------------
# Provider unit checks — verify_and_parse_webhook normalization + config guards.
# ---------------------------------------------------------------------------


async def test_provider_parses_verified_event_fields():
    body = _payment_succeeded_body(workspace_id="ws_parse", total_amount=750, currency="EUR")
    headers = _sign(body, msg_id="evt_parse_1")
    event = _provider().verify_and_parse_webhook(payload=body.encode(), headers=headers)
    assert event.event_id == "evt_parse_1"  # from the webhook-id header
    assert event.type == "payment.succeeded"
    assert event.workspace_id == "ws_parse"
    assert event.amount_credits == 750
    assert event.currency == "EUR"


async def test_provider_webhook_unconfigured_raises():
    prov = DodoProvider(
        api_key="k", environment="test_mode", webhook_secret=None, credit_product_id=PRODUCT_ID
    )
    body = _payment_succeeded_body()
    headers = _sign(body, msg_id="evt_x")
    with pytest.raises(ValidationError) as exc:
        prov.verify_and_parse_webhook(payload=body.encode(), headers=headers)
    assert exc.value.code == "billing.webhook_unconfigured"


async def test_create_topup_product_unconfigured_raises(mongo_db):
    prov = DodoProvider(
        api_key="k", environment="test_mode", webhook_secret=SECRET, credit_product_id=None
    )
    with pytest.raises(ValidationError) as exc:
        await billing.create_topup(workspace_id=WS, user_id="u1", amount_credits=100, provider=prov)
    assert exc.value.code == "billing.product_unconfigured"
