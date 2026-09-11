# tests/cloud/billing/test_usage_sub_credit_micro.py — REGRESSION for the usage
# chart reading ZERO against a wallet the customer can watch draining.
#
# The wallet stores micro-credits (1_000_000 micro == 1 credit == $0.01) and a
# typical chat run costs about $0.0015 of compute — 375_000 micro, which is 0
# whole credits. ``credits.service.spend_by_model`` truncated to whole credits
# PER (day, model) GROUP before the figure ever reached the chart, so a day of
# ordinary light usage across two or three models rendered as a row of zeros.
# Truncating per group also makes the error COMPOUND rather than cancel: the
# day total and the grand total are sums of already-truncated values, so the
# chart can report 0 for a day on which the wallet moved a credit and a half.
#
# This is the same defect PR #2071 fixed for the history rows, where
# ``LedgerEntryResponse`` gained ``amount_delta_micro`` because a real debit
# arrived on the wire as ``amount_delta: 0``. The chart simply did not get the
# same treatment.
#
# Seeds APPLIED debits directly (the READ path is what's under test), back-dating
# ``createdAt`` through the raw collection because ``TimestampedDocument``'s
# @before_event(Insert) stamps createdAt=now. The expected total is aggregated
# straight off the collection so the assertion does not depend on the code it is
# checking.
#
# Created 2026-09-11 (fix/billing-usage-chart-micro): RED anchor for rendering
# the usage chart from micro-credits.

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.billing import usage
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

WS = "ws_usage_sub_credit_test"
SONNET = "anthropic/claude-3-5-sonnet"
GPT = "openai/gpt-4o"

# One ordinary chat run: $0.0015 of compute. Three-eighths of a cent, and 0 whole
# credits however you round it down.
RUN_MICRO = 375_000


async def _seed_micro(
    ws: str,
    *,
    day: tuple[int, int, int],
    model: str,
    micro: int,
    idem: str,
) -> None:
    """Insert one APPLIED debit of ``micro`` micro-credits, stamped on ``day``."""
    entry = CreditLedgerEntry(
        workspace=ws,
        kind="spend",
        amount_delta_micro=-micro,
        balance_after_micro=0,
        applied=True,
        conditional=False,
        cause="compute_spend",
        ref={"model": model},
        idempotency_key=idem,
    )
    await entry.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": entry.id},
        {"$set": {"createdAt": datetime(day[0], day[1], day[2], 12, 0, tzinfo=UTC)}},
    )


async def _ledger_spend_micro(ws: str) -> int:
    """The workspace's total debited micro-credits, read straight off the ledger.

    Deliberately NOT via ``spend_by_model`` — that is the read under test.
    """
    cursor = CreditLedgerEntry.get_pymongo_collection().aggregate(
        [
            {"$match": {"workspace": ws, "applied": True, "amount_delta_micro": {"$lt": 0}}},
            {"$group": {"_id": None, "micro": {"$sum": {"$subtract": [0, "$amount_delta_micro"]}}}},
        ]
    )
    if hasattr(cursor, "__await__"):
        cursor = await cursor
    async for doc in cursor:
        return int(doc["micro"])
    return 0


async def _seed_a_light_day(ws: str) -> None:
    """A day of ordinary light usage: two runs on each of two models.

    4 x 375_000 == 1_500_000 micro == 1.5 credits off the wallet. Each (day, model)
    group holds 750_000 micro, which truncates to 0 — so the pre-fix chart reported
    nothing at all for a day the customer was charged a credit and a half for.
    """
    await _seed_micro(ws, day=(2026, 9, 1), model=SONNET, micro=RUN_MICRO, idem="run:s1")
    await _seed_micro(ws, day=(2026, 9, 1), model=SONNET, micro=RUN_MICRO, idem="run:s2")
    await _seed_micro(ws, day=(2026, 9, 1), model=GPT, micro=RUN_MICRO, idem="run:g1")
    await _seed_micro(ws, day=(2026, 9, 1), model=GPT, micro=RUN_MICRO, idem="run:g2")


async def test_sub_credit_day_charts_non_zero_and_reconciles_with_the_ledger(mongo_db):
    """A day of sub-credit spend across two models must not render as zeros, and
    the chart's exact total must equal the ledger's.

    Pre-fix this failed twice over: every ``credits_micro`` field was absent, and
    ``total_credits`` was 0 against a ledger that had moved 1_500_000 micro.
    """
    await _seed_a_light_day(WS)
    expected_micro = await _ledger_spend_micro(WS)
    assert expected_micro == 4 * RUN_MICRO  # the wallet really did move 1.5 credits

    result = await usage.get_workspace_usage(WS, start_date="2026-09-01", end_date="2026-09-01")

    assert [b.date for b in result.buckets] == ["2026-09-01"]
    bucket = result.buckets[0]
    assert set(bucket.by_model) == {SONNET, GPT}

    # NON-ZERO: the exact per-model figures survive to the chart.
    assert bucket.by_model[SONNET].credits_micro == 2 * RUN_MICRO
    assert bucket.by_model[GPT].credits_micro == 2 * RUN_MICRO

    # RECONCILES: the day and the grand total equal what the ledger actually
    # debited — no truncation compounds across the (day, model) groups.
    assert bucket.total_credits_micro == expected_micro
    assert result.total_credits_micro == expected_micro


async def test_whole_credit_totals_are_truncated_once_not_per_group(mongo_db):
    """The whole-credit fields the existing clients read must be derived from the
    micro total, not summed from per-group truncations.

    1_500_000 micro is 1 whole credit. Pre-fix each group truncated to 0 first and
    the day reported 0 — the chart disagreeing with the wallet by the whole charge.
    """
    await _seed_a_light_day(WS)

    result = await usage.get_workspace_usage(WS, start_date="2026-09-01", end_date="2026-09-01")

    bucket = result.buckets[0]
    assert bucket.total_credits == 1
    assert result.total_credits == 1
    # Per-model whole credits genuinely are 0 here (750_000 micro is under a
    # credit); that is honest display rounding, and it is exactly why the micro
    # fields have to exist for anything that reasons about the money.
    assert bucket.by_model[SONNET].credits == 0


async def test_spend_across_days_totals_from_micro(mongo_db):
    """Two light days each under a credit still add up on the grand total.

    Guards the fold across buckets as well as within one: 750_000 + 750_000 is a
    whole credit even though neither day is.
    """
    await _seed_micro(WS, day=(2026, 9, 1), model=SONNET, micro=2 * RUN_MICRO, idem="run:d1")
    await _seed_micro(WS, day=(2026, 9, 2), model=SONNET, micro=2 * RUN_MICRO, idem="run:d2")
    expected_micro = await _ledger_spend_micro(WS)

    result = await usage.get_workspace_usage(WS, start_date="2026-09-01", end_date="2026-09-02")

    assert [b.total_credits_micro for b in result.buckets] == [2 * RUN_MICRO, 2 * RUN_MICRO]
    assert result.total_credits_micro == expected_micro
    assert result.total_credits == 1
