# tests/cloud/credits/test_sub_cent_display.py — what a sub-cent charge looks like
# by the time it reaches a screen.
#
# THE BUG. A proxy call costing $0.0015 is 375_000 micro-credits: a real debit, and
# 0.375 of a credit. The wire carried only whole credits, where it truncates to 0.
# The billing page derived BOTH the amount and the sign from that zero:
#
#     {entry.amount_delta >= 0 ? "+" : "−"}{usd(Math.abs(entry.amount_delta))}
#
# so a charge rendered as ``+$0.00``. Not merely rounded away — signed the WRONG
# WAY, claiming money went in while the balance fell. Observed live 2026-09-04 as a
# column of ``+$0.00`` rows against a dropping balance, which is what a customer
# would open a dispute about.
#
# The whole-credit fields cannot be fixed in place. A cent is the smallest thing
# they can express and these charges are smaller than a cent, so any value they
# carry is wrong. The exact micro figures ship alongside instead, and the UI takes
# the sign and the amount from those.
#
# WHY TEST IT HERE rather than only in the frontend. The frontend can only render
# what it is given, and the sign was destroyed on this side of the wire. These
# assert the field is present, exact, and correctly signed for the movements that
# actually occur in production — sub-cent proxy spend above all.
#
# Created 2026-09-04 (fix/sub-cent-ledger-display): new test module.

from __future__ import annotations

from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.credits.dto import ledger_entry_to_dto

WS = "ws_sub_cent"

# A real row from the production proxy: $0.0015 of compute at the 2.5x markup.
_SUB_CENT_DEBIT_MICRO = 375_000


async def _wire_rows(limit: int = 50):
    entries, _ = await credits.history(WS, limit=limit)
    return [ledger_entry_to_dto(e) for e in entries]


async def test_a_sub_cent_charge_is_negative_on_the_wire(mongo_db):
    """The headline. A debit must never reach the UI looking like a credit."""
    await credits.grant(WS, 700, idempotency_key="seed", cause="top_up")
    await credits.debit(
        WS,
        idempotency_key="litellm:call-1",
        amount_micro=_SUB_CENT_DEBIT_MICRO,
        cause="litellm_spend",
        allow_negative=True,
    )

    row = (await _wire_rows())[0]

    assert row.amount_delta_micro == -_SUB_CENT_DEBIT_MICRO
    assert row.amount_delta_micro < 0, "a charge must be signed negative"

    # And the field the UI used to read is exactly why it could not be trusted:
    # zero, which any `>= 0` test reads as money coming IN.
    assert row.amount_delta == 0


async def test_the_exact_amount_survives_to_the_wire(mongo_db):
    """$0.0015 is renderable as $0.0038 of credit, but only if the exact figure
    reaches the client. Truncation happens for display, never in transport."""
    await credits.grant(WS, 700, idempotency_key="seed", cause="top_up")
    await credits.debit(
        WS,
        idempotency_key="litellm:call-1",
        amount_micro=_SUB_CENT_DEBIT_MICRO,
        cause="litellm_spend",
        allow_negative=True,
    )

    row = (await _wire_rows())[0]

    # 375_000 micro = 0.375 credits = $0.00375, signed negative because it is a charge.
    assert row.amount_delta_micro == -375_000
    assert abs(row.amount_delta_micro) / 1_000_000 == 0.375


async def test_a_genuine_zero_is_distinguishable_from_a_tiny_charge(mongo_db):
    """``record_no_movement`` writes a real zero — a source event accounted for
    without touching the wallet. It and a sub-cent debit were the SAME number on
    the wire, so nothing downstream could tell "we saw this and charged nothing"
    from "we charged you". They differ now."""
    await credits.grant(WS, 700, idempotency_key="seed", cause="top_up")
    await credits.record_no_movement(WS, idempotency_key="litellm:free-call", cause="litellm_spend")
    await credits.debit(
        WS,
        idempotency_key="litellm:paid-call",
        amount_micro=_SUB_CENT_DEBIT_MICRO,
        cause="litellm_spend",
        allow_negative=True,
    )

    rows = {r.idempotency_key: r for r in await _wire_rows()}

    assert rows["litellm:free-call"].amount_delta_micro == 0
    assert rows["litellm:paid-call"].amount_delta_micro == -_SUB_CENT_DEBIT_MICRO
    # Indistinguishable in the old field — both zero.
    assert rows["litellm:free-call"].amount_delta == rows["litellm:paid-call"].amount_delta


async def test_a_grant_is_still_positive(mongo_db):
    """The sign has to be right in both directions, or fixing the debit just moves
    the bug. A top-up is money in."""
    await credits.grant(WS, 500, idempotency_key="topup-1", cause="top_up")

    row = (await _wire_rows())[0]

    assert row.amount_delta_micro == 500 * 1_000_000
    assert row.amount_delta == 500


async def test_the_running_balance_is_exact_too(mongo_db):
    """``balance_after`` has the same problem in miniature: a wallet holding 699.6
    credits reads as 699, so a column of running balances would appear not to move
    across several sub-cent charges."""
    await credits.grant(WS, 700, idempotency_key="seed", cause="top_up")
    for i in range(3):
        await credits.debit(
            WS,
            idempotency_key=f"litellm:call-{i}",
            amount_micro=_SUB_CENT_DEBIT_MICRO,
            cause="litellm_spend",
            allow_negative=True,
        )

    rows = [r for r in await _wire_rows() if r.cause == "litellm_spend"]
    micro = [r.balance_after_micro for r in rows]

    # Newest first, each 375_000 micro below the one before it. Every charge moves
    # the running balance, which is the whole point.
    assert micro == [
        698_875_000,
        699_250_000,
        699_625_000,
    ], f"each charge must show a distinct running balance, got {micro}"

    # The whole-credit view cannot say that. Three charges totalling 1.125 credits
    # straddle one cent boundary, so it shows two distinct numbers for three
    # movements — two rows appear not to have moved the balance at all.
    whole = [r.balance_after for r in rows]
    assert whole == [698, 699, 699], f"expected the truncated view to stall, got {whole}"
    assert len(set(whole)) < len(set(micro))
