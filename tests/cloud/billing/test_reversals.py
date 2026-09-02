# tests/cloud/billing/test_reversals.py — proves M1 / M1a / F8: a verified Dodo
# ``refund.succeeded`` or ``dispute.lost`` claws the granted credits back out of
# the workspace wallet EXACTLY ONCE, the other five ``dispute.*`` lifecycle
# events move no money, and every verified ``payment.succeeded`` now writes a
# ``Payment`` row whether or not it granted (so the reversal has something to
# join to).
#
# Signing uses the REAL ``standardwebhooks`` library, mirroring
# ``test_dodo_webhook.py`` — the verification path is exercised end to end and
# no test reaches around the signature check. No Dodo SDK client is needed: the
# reversal path is pure webhook parse + local join + credit debit.
#
# The two shapes under test are the real Dodo envelopes:
#   * ``refund.succeeded`` -> ``data`` is a ``Refund``: payment_id, amount (int
#     lowest denomination, OPTIONAL), currency, is_partial, metadata.
#   * ``dispute.lost``     -> ``data`` is a ``Dispute``: payment_id, amount (a
#     STRING), currency, dispute_id — and NO metadata, which is precisely why
#     ``payment_id`` is the only join key back to a workspace.
#
# Created 2026-09-02 (fix/billing-reversals-and-dunning, M1 + M1a + F8): new
#   test module.

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from pocketpaw_ee.cloud.billing import service as billing
from pocketpaw_ee.cloud.billing.providers.dodo import DodoProvider
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.payment import Payment
from standardwebhooks import Webhook

WS = "ws_reversal_test"
SECRET = "whsec_" + base64.b64encode(b"billing-test-secret-key-32bytes!").decode()
PRODUCT_ID = "prod_credits_sku"
PAYMENT_ID = "pay_reversible_1"


def _provider() -> DodoProvider:
    return DodoProvider(
        api_key="dodo_test_key",
        environment="test_mode",
        webhook_secret=SECRET,
        credit_product_id=PRODUCT_ID,
    )


def _sign(body: str, *, msg_id: str) -> dict[str, str]:
    ts = datetime.now(UTC)
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(ts.timestamp())),
        "webhook-signature": Webhook(SECRET).sign(msg_id=msg_id, timestamp=ts, data=body),
    }


def _payment_body(
    *,
    workspace_id: str = WS,
    total_amount: int = 500,
    currency: str = "USD",
    payment_id: str = PAYMENT_ID,
) -> str:
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": "payment.succeeded",
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "payment_id": payment_id,
                "metadata": {"workspace_id": workspace_id},
                "total_amount": total_amount,
                "currency": currency,
            },
        }
    )


def _refund_body(
    *,
    payment_id: str = PAYMENT_ID,
    amount: int | None = None,
    currency: str = "USD",
    is_partial: bool = False,
    event_type: str = "refund.succeeded",
) -> str:
    """A Dodo ``refund.*`` webhook body — ``data`` is a ``Refund`` object."""
    data: dict = {
        "refund_id": "ref_1",
        "payment_id": payment_id,
        "business_id": "biz_1",
        "status": "succeeded",
        "is_partial": is_partial,
        "currency": currency,
        "metadata": {},
    }
    if amount is not None:
        data["amount"] = amount
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": data,
        }
    )


def _dispute_body(
    *,
    payment_id: str = PAYMENT_ID,
    amount: str = "500",
    currency: str = "USD",
    event_type: str = "dispute.lost",
) -> str:
    """A Dodo ``dispute.*`` webhook body — ``data`` is a ``Dispute``: the amount
    is a STRING and there is NO metadata anywhere on it."""
    return json.dumps(
        {
            "business_id": "biz_1",
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": {
                "dispute_id": "dis_1",
                "payment_id": payment_id,
                "business_id": "biz_1",
                "amount": amount,
                "currency": currency,
                "dispute_stage": "dispute",
                "dispute_status": "lost",
            },
        }
    )


async def _grant_topup(*, amount: int = 500, event_id: str = "evt_pay_1", **kw) -> None:
    """Drive a real verified ``payment.succeeded`` so the Payment row and the
    grant both exist exactly as production would have written them."""
    body = _payment_body(total_amount=amount, **kw)
    await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id=event_id), provider=_provider()
    )


# ---------------------------------------------------------------------------
# M1a / F8 — every verified success is recorded, granted or not. Without this
# the reversal has no row to join and a refunded non-USD charge is invisible.
# ---------------------------------------------------------------------------


async def test_non_usd_success_records_a_payment_row_and_grants_nothing(mongo_db):
    """The M1a ordering fix. A non-USD ``payment.succeeded`` is still acked with
    no grant (B6 owns the currency posture) but it now leaves a ``Payment`` row,
    so a later refund of it can be joined back to this workspace."""
    body = _payment_body(total_amount=750, currency="JPY")
    result = await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_jpy_row"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False}
    assert await credits.balance(WS) == 0  # grant behaviour unchanged

    rows = await Payment.find(Payment.workspace == WS).to_list()
    assert len(rows) == 1
    assert rows[0].gateway_ref == PAYMENT_ID
    assert rows[0].currency == "JPY"
    # Paid 750; granted NOTHING. The split is what stops a reversal of this
    # payment clawing back 750 credits the workspace never received.
    assert rows[0].amount_credits == 750
    assert rows[0].credits_granted == 0


async def test_granted_payment_records_what_it_granted(mongo_db):
    await _grant_topup(amount=500)
    row = await Payment.find_one(Payment.workspace == WS)
    assert row is not None
    assert row.amount_credits == 500
    assert row.credits_granted == 500
    assert row.credits_reversed == 0


# ---------------------------------------------------------------------------
# M1 — refund.succeeded and dispute.lost reverse; the balance may go negative.
# ---------------------------------------------------------------------------


async def test_refund_succeeded_claws_back_the_granted_credits(mongo_db):
    await _grant_topup(amount=500)
    assert await credits.balance(WS) == 500

    body = _refund_body()
    result = await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_refund_1"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 500}
    assert await credits.balance(WS) == 0

    row = await Payment.find_one(Payment.workspace == WS)
    assert row is not None
    assert row.credits_reversed == 500


async def test_dispute_lost_claws_back_the_granted_credits(mongo_db):
    """``Dispute`` carries no metadata at all, so this proves the join runs
    entirely through ``payment_id`` -> ``Payment.gateway_ref``."""
    await _grant_topup(amount=500)

    body = _dispute_body()
    result = await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_dispute_1"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 500}
    assert await credits.balance(WS) == 0


async def test_reversal_drives_the_balance_negative_when_the_credits_were_spent(mongo_db):
    """The decided posture: a spent-then-reversed workspace goes NEGATIVE rather
    than having the balance written off. A negative balance blocks further spend
    (``check_balance`` raises at <= 0) until it is settled."""
    await _grant_topup(amount=500)
    await credits.debit(workspace=WS, amount=400, cause="compute_spend", idempotency_key="run-1")
    assert await credits.balance(WS) == 100

    body = _refund_body()
    await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_refund_neg"), provider=_provider()
    )

    assert await credits.balance(WS) == -400


async def test_a_partial_refund_reverses_only_what_came_back(mongo_db):
    """``Refund`` carries ``is_partial`` + ``amount``. Reversing the whole grant
    on a partial refund would take credits the buyer still paid for."""
    await _grant_topup(amount=500)

    body = _refund_body(amount=200, is_partial=True)
    result = await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_refund_part"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 200}
    assert await credits.balance(WS) == 300


# ---------------------------------------------------------------------------
# M1 — idempotency. A redelivery is a no-op, and the per-payment cap stops a
# refund and a lost dispute on ONE payment clawing the same credits twice.
# ---------------------------------------------------------------------------


async def test_replayed_reversal_event_is_a_noop(mongo_db):
    await _grant_topup(amount=500)
    body = _refund_body()
    headers = _sign(body, msg_id="evt_refund_replay")

    first = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )
    assert first == {"ok": True, "granted": False, "reversed": 500}
    assert await credits.balance(WS) == 0

    second = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )
    assert second == {"ok": True, "granted": False, "reversed": 0}
    # Balance unchanged — the redelivery clawed back nothing a second time.
    assert await credits.balance(WS) == 0

    row = await Payment.find_one(Payment.workspace == WS)
    assert row is not None
    assert row.credits_reversed == 500  # counted once, not twice


async def test_a_refund_then_a_lost_dispute_cannot_double_claw(mongo_db):
    """Verifi RDR resolves disputes BY refunding, so a payment carrying both a
    refund and a dispute is routine. The per-payment cap is what keeps the
    second one from taking credits the first already took."""
    await _grant_topup(amount=500)

    refund = _refund_body()
    await billing.handle_webhook(
        payload=refund.encode(), headers=_sign(refund, msg_id="evt_r1"), provider=_provider()
    )
    assert await credits.balance(WS) == 0

    dispute = _dispute_body()
    result = await billing.handle_webhook(
        payload=dispute.encode(), headers=_sign(dispute, msg_id="evt_d1"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 0}
    assert await credits.balance(WS) == 0  # NOT -500


async def test_reversing_a_non_usd_payment_takes_no_credits(mongo_db):
    """A non-USD charge granted nothing, so refunding it must claw back nothing.
    The cap does this for free — no separate currency gate on the reversal."""
    await _grant_topup(amount=750, currency="JPY", event_id="evt_jpy_pay")
    assert await credits.balance(WS) == 0

    body = _refund_body(currency="JPY")
    result = await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_jpy_refund"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 0}
    assert await credits.balance(WS) == 0


# ---------------------------------------------------------------------------
# M1 — the other five dispute.* events are lifecycle. Reversing on
# ``dispute.opened`` would double-count once the dispute is later won.
# ---------------------------------------------------------------------------


async def test_dispute_lifecycle_events_move_no_money(mongo_db):
    for i, event_type in enumerate(
        (
            "dispute.opened",
            "dispute.challenged",
            "dispute.accepted",
            "dispute.cancelled",
            "dispute.expired",
            "dispute.won",
        )
    ):
        await _grant_topup(amount=500, event_id=f"evt_pay_lifecycle_{i}")
        before = await credits.balance(WS)

        body = _dispute_body(event_type=event_type)
        result = await billing.handle_webhook(
            payload=body.encode(),
            headers=_sign(body, msg_id=f"evt_lifecycle_{i}"),
            provider=_provider(),
        )

        assert result == {"ok": True, "granted": False, "reversed": 0}, event_type
        assert await credits.balance(WS) == before, event_type


async def test_refund_failed_moves_no_money(mongo_db):
    await _grant_topup(amount=500)
    body = _refund_body(event_type="refund.failed")
    result = await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_refund_failed"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 0}
    assert await credits.balance(WS) == 500


# ---------------------------------------------------------------------------
# M1 — a reversal we cannot join is an ALARM, never a silent guess.
# ---------------------------------------------------------------------------


async def test_reversal_for_an_unknown_payment_is_acked_and_logged(mongo_db, caplog):
    body = _refund_body(payment_id="pay_never_seen")
    with caplog.at_level("ERROR"):
        result = await billing.handle_webhook(
            payload=body.encode(),
            headers=_sign(body, msg_id="evt_refund_orphan"),
            provider=_provider(),
        )

    assert result == {"ok": True, "granted": False, "reversed": 0}
    assert any("pay_never_seen" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Legacy rows — written before ``credits_granted`` existed. They were ONLY ever
# written when the grant applied, so a missing field means "granted == paid".
# Reading it as 0 would make every pre-existing payment un-reversible.
# ---------------------------------------------------------------------------


async def test_a_legacy_row_without_credits_granted_is_still_reversible(mongo_db):
    await credits.grant(workspace=WS, amount=500, cause="top_up", idempotency_key="legacy-grant")
    # A row exactly as the pre-change code wrote it: no credits_granted field.
    await Payment.get_pymongo_collection().insert_one(
        {
            "workspace": WS,
            "gateway": "dodo",
            "gateway_ref": PAYMENT_ID,
            "gateway_event_id": "evt_legacy",
            "amount_credits": 500,
            "currency": "USD",
            "status": "succeeded",
            "createdAt": datetime.now(UTC),
            "updatedAt": datetime.now(UTC),
        }
    )

    body = _refund_body()
    result = await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_refund_legacy"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 500}
    assert await credits.balance(WS) == 0


# ---------------------------------------------------------------------------
# Provider unit checks — the normalized ReversalEvent shape.
# ---------------------------------------------------------------------------


async def test_provider_normalizes_a_refund_delivery():
    from pocketpaw_ee.cloud.billing.domain import ReversalEvent

    body = _refund_body(amount=250, is_partial=True)
    event = _provider().verify_and_parse_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_parse_refund")
    )
    assert isinstance(event, ReversalEvent)
    assert event.event_id == "evt_parse_refund"
    assert event.type == "refund.succeeded"
    assert event.payment_id == PAYMENT_ID
    assert event.amount_credits == 250
    assert event.is_partial is True
    assert event.currency == "USD"


async def test_provider_normalizes_a_dispute_delivery_whose_amount_is_a_string():
    from pocketpaw_ee.cloud.billing.domain import ReversalEvent

    body = _dispute_body(amount="500")
    event = _provider().verify_and_parse_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_parse_dispute")
    )
    assert isinstance(event, ReversalEvent)
    assert event.type == "dispute.lost"
    assert event.payment_id == PAYMENT_ID
    assert event.amount_credits == 500  # parsed out of the string
    assert event.workspace_id == ""  # Dispute carries no metadata


async def test_provider_refuses_to_guess_a_malformed_amount():
    """A money field that does not parse yields 0 — "the gateway named no
    amount" — which the service reads as a FULL reversal, capped at the grant.
    It never invents a partial figure out of an unparseable string."""
    body = _dispute_body(amount="not-a-number")
    event = _provider().verify_and_parse_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_parse_bad")
    )
    assert event.amount_credits == 0
