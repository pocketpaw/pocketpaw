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
# Updated 2026-09-11 (fix/billing-reversal-claim-first, T-4): added the three
#   racing-reversal tests at the foot of the module. The per-payment cap was a
#   read-modify-write, so a refund and a lost dispute on one payment could each
#   read ``credits_reversed`` before either claimed it and each take the whole
#   grant — 1000 clawed back against a 500 grant, wallet at -500. The existing
#   ``test_a_refund_then_a_lost_dispute_cannot_double_claw`` misses it because it
#   awaits the first delivery to completion before starting the second.
#   mongomock's awaits never actually suspend, so ``asyncio.gather`` alone runs
#   the two deliveries end to end; ``_interleave_at_the_read_and_the_debit``
#   restores the suspension production's real I/O has at the row read and the
#   debit, and nothing else about the deliveries is faked.
# Updated 2026-09-11 (fix/billing-reversal-claim-first, T-4 review): four more
#   tests for the window claim-first opens. A failed debit must RELEASE its
#   claim so the redelivery heals; a debit that raised AFTER the balance moved
#   must KEEP it (releasing there hands the remainder to the next reversal on
#   top of credits already taken); a hard kill — modelled as ``CancelledError``,
#   which no ``except Exception`` catches — must alarm on redelivery rather than
#   log "already applied", which is the one state where that line is false; and
#   a genuine redelivery must stay quiet, or the alarm trains people to ignore
#   it. Mutation plan: ``tests/mutations/billing_reversal_claim.json``.

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime

import pytest
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


async def test_a_partial_refund_with_no_readable_amount_takes_nothing(mongo_db, caplog):
    """The gateway saying "partial" and naming no amount we can read is a
    CONTRADICTION, not a default. Falling through to the full grant there takes
    credits the buyer still paid for on the strength of a number we admit we
    could not parse, so the reversal refuses and alarms instead."""
    await _grant_topup(amount=500)

    body = _refund_body(amount=None, is_partial=True)
    with caplog.at_level("ERROR"):
        result = await billing.handle_webhook(
            payload=body.encode(),
            headers=_sign(body, msg_id="evt_refund_part_unreadable"),
            provider=_provider(),
        )

    assert result == {"ok": True, "granted": False, "reversed": 0}
    assert await credits.balance(WS) == 500
    assert any("PARTIAL" in r.getMessage() for r in caplog.records)


async def test_a_full_refund_with_no_stated_amount_reverses_everything(mongo_db):
    """The other side of that guard: ``is_partial`` false and no amount is not a
    contradiction, it is how a full refund reads. Reverse the whole grant."""
    await _grant_topup(amount=500)

    body = _refund_body(amount=None, is_partial=False)
    result = await billing.handle_webhook(
        payload=body.encode(), headers=_sign(body, msg_id="evt_refund_full"), provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 500}
    assert await credits.balance(WS) == 0


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


# ---------------------------------------------------------------------------
# T-4 — two DIFFERENT reversals on one payment, interleaved in ONE event loop.
#
# The ``$ne`` filter on ``reversal_event_ids`` guards a REDELIVERY of the same
# event. It does nothing for a refund and a lost dispute arriving together,
# which is the routine case (Verifi RDR resolves disputes by refunding). Both
# read ``credits_reversed`` before either has claimed it, both compute the same
# ``remaining``, both debit under distinct idempotency keys, and the running
# total lands at twice the grant with the wallet driven negative.
#
# This needs NO second process. The read, the awaited debit and the claim are
# ordered so two deliveries interleave at an ``await`` inside a single loop, so
# a single-worker deployment gives no protection at all. mongomock's awaits
# never actually suspend, so the two helpers below restore the suspension that
# production's real I/O has at exactly those two points — nothing else about
# the deliveries is faked.
# ---------------------------------------------------------------------------


def _interleave_at_the_read_and_the_debit(monkeypatch) -> None:
    """Make ``Payment.find_one`` and ``credits.debit`` yield to the loop.

    Both are network round-trips in production and neither is under mongomock,
    so without this ``asyncio.gather`` runs the two deliveries end to end, one
    after the other, and no interleaving is possible to observe.
    """
    real_find_one = Payment.find_one
    real_debit = credits.debit

    async def _find_one_that_yields(*args, **kwargs):
        doc = await real_find_one(*args, **kwargs)
        await asyncio.sleep(0)
        return doc

    async def _debit_that_yields(*args, **kwargs):
        await asyncio.sleep(0)
        return await real_debit(*args, **kwargs)

    monkeypatch.setattr(Payment, "find_one", _find_one_that_yields)
    monkeypatch.setattr(billing.credits_service, "debit", _debit_that_yields)


async def test_a_racing_refund_and_lost_dispute_cannot_claw_back_more_than_the_grant(
    mongo_db, monkeypatch, caplog
):
    """The T-4 over-clawback. Granted 500; a refund and a lost dispute land
    together and the pair must still only ever take 500."""
    await _grant_topup(amount=500)
    assert await credits.balance(WS) == 500

    _interleave_at_the_read_and_the_debit(monkeypatch)

    refund = _refund_body()
    dispute = _dispute_body()
    with caplog.at_level("ERROR"):
        results = await asyncio.gather(
            billing.handle_webhook(
                payload=refund.encode(),
                headers=_sign(refund, msg_id="evt_race_refund"),
                provider=_provider(),
            ),
            billing.handle_webhook(
                payload=dispute.encode(),
                headers=_sign(dispute, msg_id="evt_race_dispute"),
                provider=_provider(),
            ),
        )

    # Exactly one of the two took the money; the other took nothing.
    assert sorted(r["reversed"] for r in results) == [0, 500]

    row = await Payment.find_one(Payment.workspace == WS)
    assert row is not None
    assert row.credits_reversed == 500  # NOT 1000
    assert len(row.reversal_event_ids) == 1

    # 500 granted, 500 clawed back, nothing spent — the wallet lands at zero and
    # the customer is not locked out by a shortfall they never owed.
    assert await credits.balance(WS) == 0

    # Nothing compares the running total against the grant after the fact, so
    # the loser has to alarm here or an attempted over-reversal is silent until
    # ``check_balance`` locks the customer out.
    assert any(
        "evt_race_dispute" in r.getMessage() or "evt_race_refund" in r.getMessage()
        for r in caplog.records
        if r.levelname == "ERROR"
    )


async def test_a_racing_pair_of_partial_refunds_cannot_exceed_the_grant(mongo_db, monkeypatch):
    """The same race with amounts that each fit under the cap on their own.
    300 + 300 against a 500 grant must settle at 500, never 600."""
    await _grant_topup(amount=500)

    _interleave_at_the_read_and_the_debit(monkeypatch)

    first = _refund_body(amount=300, is_partial=True)
    second = _refund_body(amount=300, is_partial=True)
    results = await asyncio.gather(
        billing.handle_webhook(
            payload=first.encode(),
            headers=_sign(first, msg_id="evt_race_part_1"),
            provider=_provider(),
        ),
        billing.handle_webhook(
            payload=second.encode(),
            headers=_sign(second, msg_id="evt_race_part_2"),
            provider=_provider(),
        ),
    )

    row = await Payment.find_one(Payment.workspace == WS)
    assert row is not None
    assert row.credits_reversed <= 500
    assert sum(r["reversed"] for r in results) == row.credits_reversed
    assert await credits.balance(WS) >= 0


async def test_a_racing_reversal_that_loses_the_claim_debits_nothing(mongo_db, monkeypatch):
    """The heart of the fix: the loser must be turned away BEFORE the money
    moves. A loser that debits first and discovers the cap afterwards has
    already taken the credits — the ledger is append-only and the wallet is
    already short."""
    await _grant_topup(amount=500)

    _interleave_at_the_read_and_the_debit(monkeypatch)

    refund = _refund_body()
    dispute = _dispute_body()
    await asyncio.gather(
        billing.handle_webhook(
            payload=refund.encode(),
            headers=_sign(refund, msg_id="evt_race_ledger_r"),
            provider=_provider(),
        ),
        billing.handle_webhook(
            payload=dispute.encode(),
            headers=_sign(dispute, msg_id="evt_race_ledger_d"),
            provider=_provider(),
        ),
    )

    # One reversal debit in the ledger, not two. The ledger is append-only, so a
    # loser that debits and only then discovers the cap has already taken the
    # credits — there is no entry here to take back.
    entries, _ = await credits.history(WS, limit=200)
    reversals = [e for e in entries if e.cause == billing._REVERSAL_CAUSE]
    assert len(reversals) == 1
    assert reversals[0].amount_delta == -500


# ---------------------------------------------------------------------------
# T-4 — the crash window the claim-first ordering opens, and what tells a human.
#
# Claiming before debiting means a failure BETWEEN the two leaves a reversal
# recorded on the payment row that never took the credits. The redelivery is
# refused by the ``$ne`` filter — the dead delivery put its own id there — so
# without a second distinguishing fact it reads as a routine replay and logs
# "already applied", which is a lie in this one state: the customer is holding
# credits they were refunded for and the row says otherwise.
#
# The window is reachable without a process kill. ``webhooks.py`` wraps
# ``handle_webhook`` in no try/except, so an exception inside the debit is a
# 500; Dodo redelivers; the redelivery is acked 200 and the gateway stops.
# ---------------------------------------------------------------------------


async def test_a_failed_debit_releases_the_claim_so_the_redelivery_heals(mongo_db, monkeypatch):
    """An ordinary Mongo hiccup inside the debit (``socketTimeoutMS`` is 30s)
    must leave NO trace on the payment row, so the gateway's redelivery re-drives
    the reversal from scratch. The release is keyed on this delivery's own event
    id, so it cannot free a claim some other reversal made."""
    await _grant_topup(amount=500)

    async def _mongo_went_away(*_args, **_kwargs):
        raise RuntimeError("connection closed mid-debit")

    monkeypatch.setattr(billing.credits_service, "debit", _mongo_went_away)

    body = _refund_body()
    headers = _sign(body, msg_id="evt_debit_failed")
    with pytest.raises(RuntimeError):
        await billing.handle_webhook(payload=body.encode(), headers=headers, provider=_provider())

    row = await Payment.find_one(Payment.workspace == WS)
    assert row is not None
    assert row.credits_reversed == 0
    assert row.reversal_event_ids == []
    assert await credits.balance(WS) == 500  # no money moved either way

    monkeypatch.undo()
    result = await billing.handle_webhook(
        payload=body.encode(), headers=headers, provider=_provider()
    )

    assert result == {"ok": True, "granted": False, "reversed": 500}
    assert await credits.balance(WS) == 0


async def test_a_claim_whose_debit_never_ran_alarms_instead_of_reporting_it_applied(
    mongo_db, monkeypatch, caplog
):
    """The hard-kill half, which no compensating release can cover: a SIGTERM
    during a deploy cancels the task mid-debit, and ``CancelledError`` is a
    ``BaseException`` so no ``except Exception`` runs. The claim stands, the
    credits were never taken, and the redelivery is refused by the ``$ne``
    filter. That state MUST alarm — it is the one case where "already applied"
    is false, and nothing else in the system compares the two."""
    await _grant_topup(amount=500)

    async def _killed_mid_debit(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(billing.credits_service, "debit", _killed_mid_debit)

    body = _refund_body()
    headers = _sign(body, msg_id="evt_orphan_claim")
    with pytest.raises(asyncio.CancelledError):
        await billing.handle_webhook(payload=body.encode(), headers=headers, provider=_provider())

    monkeypatch.undo()

    # The orphan: the row says 500 was reversed, the wallet says otherwise.
    row = await Payment.find_one(Payment.workspace == WS)
    assert row is not None
    assert row.credits_reversed == 500
    assert row.reversal_event_ids == ["evt_orphan_claim"]
    assert await credits.balance(WS) == 500

    with caplog.at_level("INFO"):
        result = await billing.handle_webhook(
            payload=body.encode(), headers=headers, provider=_provider()
        )

    # Still acked and still no double-debit — the guard itself is unchanged.
    assert result == {"ok": True, "granted": False, "reversed": 0}
    assert await credits.balance(WS) == 500

    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("evt_orphan_claim" in m for m in errors), "the orphan claim must alarm"
    # And it must NOT report the routine outcome, which is what makes the line a
    # lie: no credits were taken and none ever will be for this delivery.
    infos = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert not any("already applied" in m for m in infos)


async def test_a_genuine_redelivery_still_reports_the_routine_outcome(mongo_db, caplog):
    """The other side of that branch. When the debit DID land, the redelivery is
    ordinary and must stay quiet — an ERROR here would train a human to ignore
    the one that matters."""
    await _grant_topup(amount=500)
    body = _refund_body()
    headers = _sign(body, msg_id="evt_real_replay")

    await billing.handle_webhook(payload=body.encode(), headers=headers, provider=_provider())
    assert await credits.balance(WS) == 0

    with caplog.at_level("INFO"):
        result = await billing.handle_webhook(
            payload=body.encode(), headers=headers, provider=_provider()
        )

    assert result == {"ok": True, "granted": False, "reversed": 0}
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("already applied" in r.getMessage() for r in caplog.records)


async def test_a_debit_that_moved_the_money_before_raising_keeps_its_claim(mongo_db, monkeypatch):
    """The release has to be guarded, not reflexive. ``credits.debit`` stamps the
    ledger entry and emits AFTER the balance ``$inc`` has landed, so it can raise
    with the money already gone. Releasing the claim there would hand the
    remainder back to the next reversal on top of credits already taken — the
    exact over-reversal this ordering exists to prevent."""
    await _grant_topup(amount=500)
    real_debit = credits.debit

    async def _raise_after_the_money_moved(*args, **kwargs):
        await real_debit(*args, **kwargs)
        raise RuntimeError("emit failed after the balance moved")

    monkeypatch.setattr(billing.credits_service, "debit", _raise_after_the_money_moved)

    refund = _refund_body()
    with pytest.raises(RuntimeError):
        await billing.handle_webhook(
            payload=refund.encode(),
            headers=_sign(refund, msg_id="evt_late_raise"),
            provider=_provider(),
        )

    monkeypatch.undo()

    # The money moved, so the claim must STAND.
    row = await Payment.find_one(Payment.workspace == WS)
    assert row is not None
    assert row.credits_reversed == 500
    assert row.reversal_event_ids == ["evt_late_raise"]
    assert await credits.balance(WS) == 0

    # And because it stands, a lost dispute on the same payment now finds the
    # grant fully reversed and takes nothing, instead of clawing a second 500.
    dispute = _dispute_body()
    result = await billing.handle_webhook(
        payload=dispute.encode(),
        headers=_sign(dispute, msg_id="evt_late_dispute"),
        provider=_provider(),
    )
    assert result == {"ok": True, "granted": False, "reversed": 0}
    assert await credits.balance(WS) == 0
