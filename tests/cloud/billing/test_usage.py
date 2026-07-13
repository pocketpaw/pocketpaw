# tests/cloud/billing/test_usage.py — proves the per-workspace USAGE transform
# (the billing usage-graph seam). The frontend renders a daily usage graph broken
# down by model; this module locks the transform CREDIT LEDGER -> the
# WorkspaceUsage contract.
#
# Asserts:
#   * each charged ledger movement (``compute_spend`` / ``litellm_spend``, with the
#     run's ``model`` on ``ref``) is folded into a daily bucket with a per-model
#     {credits, tokens, requests} block; credits come STRAIGHT off the ledger
#     (markup already applied at debit time — no conversion here), so the chart
#     matches the wallet by construction.
#   * ``models`` is the sorted distinct union across the range; ``total_credits``
#     is the sum over every bucket; each bucket's ``total_credits`` sums its models.
#   * ``tokens`` is 0 (the ledger ref carries no token count) and ``requests`` is
#     the count of charged entries in the (day, model) group.
#   * default date range = last 30 days when start/end omitted, and the resolved
#     range is echoed onto the response.
#   * a workspace with no spend in the window -> empty buckets + empty models +
#     total_credits 0 (HTTP 200, not an error).
#   * the explicit-range validator + clamp (format / inverted / oversized-span)
#     surface a clean ValidationError before the read.
#
# Uses the shared ``mongo_db`` fixture (Beanie over ALL_DOCUMENTS) so the read
# path queries real persisted ``CreditLedgerEntry`` rows — the same DB-fixture
# pattern the credits / provisioning tests use. ``createdAt`` is back-dated via the
# raw collection because ``TimestampedDocument``'s @before_event(Insert) stamps
# createdAt=now, so a past date can't be set through the constructor.
#
# Created 2026-06-29 (feat/billing-usage-endpoint): new test module.
# Changed 2026-06-29 (fix/billing-usage-ledger-source): REWROTE the data-source
# tests to seed the CREDIT LEDGER instead of a LiteLLM proxy fake — the graph is
# now sourced from the wallet's own ledger, not the proxy /user/daily/activity
# (the proxy was empty in the default off-mode, so the chart showed "No usage to
# chart yet" despite real spend). Dropped the FakeDailyActivity / ensure_tenant_key
# / proxy-transform machinery; kept the source-independent validator tests verbatim
# and re-expressed the default-30-day-window test against the ledger.

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing import usage
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

WS = "ws_usage_test"
SONNET = "anthropic/claude-3-5-sonnet"
GPT = "openai/gpt-4o"


async def _seed_spend(
    ws: str,
    *,
    day: tuple[int, int, int],
    model: str | None,
    credits: int,
    idem: str,
    cause: str = "compute_spend",
) -> None:
    """Insert one APPLIED debit ledger entry stamped on a controlled date.

    Mirrors what ``metering.service.bill_run`` writes: a negative ``amount_delta``
    under ``cause`` with the run's ``model`` in ``ref``. Inserts first (the Insert
    event forces createdAt=now), then back-dates ``createdAt`` via the raw
    collection so the (day, model) bucketing can be asserted. ``model=None`` seeds a
    debit with no ``ref.model`` (the "unknown" bucket).
    """
    ref = {"model": model} if model is not None else {}
    entry = CreditLedgerEntry(
        workspace=ws,
        kind="spend",
        amount_delta=-credits,
        balance_after=0,
        applied=True,
        conditional=False,
        cause=cause,
        ref=ref,
        idempotency_key=idem,
    )
    await entry.insert()
    when = datetime(day[0], day[1], day[2], 12, 0, tzinfo=UTC)
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": entry.id}, {"$set": {"createdAt": when}}
    )


# ---------------------------------------------------------------------------
# The transform — credit ledger -> the WorkspaceUsage contract.
# ---------------------------------------------------------------------------


async def test_usage_transforms_ledger_into_buckets_by_model(mongo_db):
    # Day 1: two models. Day 2: sonnet only.
    await _seed_spend(WS, day=(2026, 6, 1), model=SONNET, credits=10, idem="r1")
    await _seed_spend(WS, day=(2026, 6, 1), model=GPT, credits=5, idem="r2")
    await _seed_spend(WS, day=(2026, 6, 2), model=SONNET, credits=25, idem="r3")

    result = await usage.get_workspace_usage(WS, start_date="2026-06-01", end_date="2026-06-02")

    # Echoed range.
    assert result.start_date == "2026-06-01"
    assert result.end_date == "2026-06-02"

    # Distinct model list, sorted (alphabetical: "anthropic/..." before "openai/...").
    assert result.models == sorted([GPT, SONNET])
    assert result.models == [SONNET, GPT]

    # Two daily buckets.
    assert [b.date for b in result.buckets] == ["2026-06-01", "2026-06-02"]

    b1 = result.buckets[0]
    assert b1.by_model[SONNET].credits == 10
    assert b1.by_model[SONNET].tokens == 0  # ledger carries no token count
    assert b1.by_model[SONNET].requests == 1
    assert b1.by_model[GPT].credits == 5
    assert b1.by_model[GPT].requests == 1
    assert b1.total_credits == 15  # 10 + 5

    b2 = result.buckets[1]
    assert set(b2.by_model.keys()) == {SONNET}
    assert b2.by_model[SONNET].credits == 25
    assert b2.total_credits == 25

    # Grand total over every bucket — matches the wallet's spend over the window.
    assert result.total_credits == 40  # 15 + 25


async def test_usage_aggregates_repeated_day_model_requests(mongo_db):
    # Two charged runs for the same (day, model) accumulate: credits sum and the
    # request count is the number of entries.
    await _seed_spend(WS, day=(2026, 6, 1), model=SONNET, credits=4, idem="r1")
    await _seed_spend(WS, day=(2026, 6, 1), model=SONNET, credits=6, idem="r2")

    result = await usage.get_workspace_usage(WS, start_date="2026-06-01", end_date="2026-06-01")

    assert result.models == [SONNET]
    b1 = result.buckets[0]
    assert b1.by_model[SONNET].credits == 10  # 4 + 6
    assert b1.by_model[SONNET].requests == 2  # two entries
    assert result.total_credits == 10


async def test_usage_defaults_to_last_30_days_when_range_omitted(mongo_db):
    today = datetime.now(UTC).date()
    expected_start = (today - timedelta(days=29)).isoformat()  # inclusive 30-day window
    expected_end = today.isoformat()

    # Seed a spend INSIDE the default window (today) and one OUTSIDE it (40 days ago)
    # so the default-window selection is actually exercised, not just the echo.
    inside = today
    outside = today - timedelta(days=40)
    await _seed_spend(
        WS, day=(inside.year, inside.month, inside.day), model=SONNET, credits=7, idem="in"
    )
    await _seed_spend(
        WS, day=(outside.year, outside.month, outside.day), model=SONNET, credits=99, idem="out"
    )

    result = await usage.get_workspace_usage(WS)

    assert result.start_date == expected_start
    assert result.end_date == expected_end
    # Only the in-window spend is charted; the 40-day-old row is excluded.
    assert result.total_credits == 7
    assert [b.date for b in result.buckets] == [inside.isoformat()]


async def test_usage_empty_when_no_spend_in_window(mongo_db):
    # No ledger rows for WS -> empty contract, HTTP 200 (not an error).
    result = await usage.get_workspace_usage(WS, start_date="2026-06-01", end_date="2026-06-30")

    assert result.buckets == []
    assert result.models == []
    assert result.total_credits == 0
    assert result.start_date == "2026-06-01"
    assert result.end_date == "2026-06-30"


async def test_usage_invalid_workspace_rejected(mongo_db):
    with pytest.raises(ValidationError):
        await usage.get_workspace_usage("", start_date="2026-06-01", end_date="2026-06-02")


# --- HARDENING (fix/billing-usage-validate): the explicit-range validator + clamp.
#     A caller-supplied range is format-checked, inverted-checked, and span-clamped
#     before it reaches the read (a clean 400, and a bounded window). These are
#     source-independent — kept verbatim across the ledger re-source. ---


def test_resolve_explicit_range_rejects_malformed_dates():
    # A non-YYYY-MM-DD string fails the format gate; a well-formed but impossible
    # calendar date (month 13) fails fromisoformat. Both surface ValidationError.
    with pytest.raises(ValidationError):
        usage._resolve_explicit_range("garbage", "2026-06-30")
    with pytest.raises(ValidationError):
        usage._resolve_explicit_range("2026-13-01", "2026-06-30")


def test_resolve_explicit_range_rejects_inverted():
    with pytest.raises(ValidationError):
        usage._resolve_explicit_range("2026-06-30", "2026-06-01")


def test_resolve_explicit_range_clamps_oversized_span():
    # A multi-year span clamps to the most recent _MAX_WINDOW_DAYS ending at end_date.
    start, end = usage._resolve_explicit_range("2023-01-01", "2026-06-30")
    assert end == "2026-06-30"
    assert start == (date(2026, 6, 30) - timedelta(days=usage._MAX_WINDOW_DAYS - 1)).isoformat()


def test_resolve_explicit_range_passes_valid_window_through():
    assert usage._resolve_explicit_range("2026-06-01", "2026-06-30") == (
        "2026-06-01",
        "2026-06-30",
    )
