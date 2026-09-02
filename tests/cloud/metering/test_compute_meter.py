# tests/cloud/metering/test_compute_meter.py — proves the BC-3 compute-cost
# metering contract: every completed / terminal chat run debits its workspace
# wallet by (real compute cost x markup) -> integer credits, EXACTLY ONCE, and
# the sweep is idempotent + crash-safe.
#
# The five acceptance criteria:
#   1. A completed run with usage.total_cost_usd = 0.04 -> debits round(0.04*250)
#      = 10 credits; balance drops by 10; a ``compute_spend`` ledger row keyed
#      ``run:{id}`` exists; the run is marked billed=True.
#   2. A run with token counts but NO total_cost_usd falls back to the
#      pricing-table estimate for a known model and debits the computed credits
#      (> 0).
#   3. Running the sweeper TWICE debits ONCE — balance changes once, one ledger
#      row (idempotency: billed flag + run_id key).
#   4. A terminal run with empty usage = {} debits 0 but is still marked
#      billed=True (won't be re-swept).
#   5. allow_negative: a run whose cost exceeds the wallet still bills fully and
#      drives the balance negative (proving metered spend isn't blocked here).
#
# Uses the shared ``mongo_db`` fixture (mongomock-motor + Beanie over
# ALL_DOCUMENTS) from tests/cloud/conftest.py — the same DB-fixture pattern the
# credits / chat-runs tests use. The autouse ``recording_bus`` keeps the
# credits service's ``emit(CreditMovement(...))`` from raising. A fixed
# ``RateCard`` (markup 2.5, credit_usd 0.01) is injected so the rate never
# depends on ambient settings.
#
# Created 2026-06-24 (integration/billing-credits, BC-3): new test module.
# Updated 2026-07-11 (feat/llm-cost-attribution): added two token-attribution
# cases — ``bill_run`` stamps the run's real ``total_tokens`` on the debit ref
# (summed from input + output + cached when no explicit total is given, and the
# explicit backend-supplied total wins when present).
# Updated 2026-09-02 (fix/metering-dated-pricing): five cases for the meter's new
# obligations, and every one of them pins a dollar amount rather than a sign.
# ``_make_run`` gained ``ended_at`` because a run's compute is priced at the
# moment it RAN, and this sweeper bills afterwards out of a backlog — the first
# of the new cases bills the same million tokens at $2.00 and at $3.00 purely
# because they happened on different days. The rest cover the long-context tier,
# the inclusive-prompt reconstruction, and the split of ``unpriced`` out of
# ``none`` so a bill we failed to send stops reading as nothing to bill.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.metering import service as metering
from pocketpaw_ee.cloud.metering.domain import RateCard
from pocketpaw_ee.cloud.metering.sweeper import sweep_unbilled_runs
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

# Pin the rate card so the tests don't depend on ambient POCKETPAW_* settings.
# With these values credits = round(cost_usd * 250).
RATE = RateCard(markup=2.5, credit_usd=0.01)

WS = "ws_metering_test"


async def _make_run(
    *,
    run_id: str,
    workspace: str = WS,
    status: str = "completed",
    usage: dict | None = None,
    billed: bool = False,
    ended_at: datetime | None = None,
) -> ChatRunDoc:
    """Insert a terminal ChatRunDoc carrying the given usage dict.

    ``ended_at`` pins WHEN the run happened, which is what its compute is priced
    at. Left ``None`` the doc falls back to ``createdAt`` (now), which is right
    for a run that just finished and wrong for every backlogged one - the whole
    reason the meter takes a timestamp.
    """
    doc = ChatRunDoc(
        run_id=run_id,
        workspace=workspace,
        context_type="dm",
        scope_id="scope-1",
        session_key="sk-1",
        user_id="u1",
        agent_id="a1",
        client_message_id=f"cmid-{run_id}",
        user_message_id=f"umid-{run_id}",
        status=status,  # type: ignore[arg-type]
        usage=usage if usage is not None else {},
        billed=billed,
        **({"ended_at": ended_at} if ended_at is not None else {}),
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# Criterion 1 — reported total_cost_usd drives the debit; ledger + flag land.
# ---------------------------------------------------------------------------


async def test_reported_cost_debits_expected_credits(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    run = await _make_run(
        run_id="run-1",
        usage={"total_cost_usd": 0.04, "model": "claude-sonnet-4-20250514"},
    )

    result = await metering.bill_run(run, rate_card=RATE)

    # round(0.04 * 250) == 10 credits.
    assert result.credits_charged == 10
    assert result.debited is True
    assert await credits.balance(WS) == 990  # 1000 - 10

    # A compute_spend ledger row keyed run:{id} exists, signed-negative.
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == WS).to_list()
    spend = [e for e in entries if e.idempotency_key == "run:run-1"]
    assert len(spend) == 1
    assert spend[0].amount_delta == -10
    assert spend[0].cause == "compute_spend"
    assert spend[0].ref.get("run_id") == "run-1"

    # The run is flipped billed.
    reloaded = await ChatRunDoc.find_one(ChatRunDoc.run_id == "run-1")
    assert reloaded is not None
    assert reloaded.billed is True


# ---------------------------------------------------------------------------
# Criterion 2 — no reported cost -> fall back to the pricing-table estimate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reported", [None, 0])
async def test_estimate_fallback_when_no_reported_cost(mongo_db, reported):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    # gpt-4o is in _PRICING (input $2.5/1M, output $10/1M). 1000 in + 1000 out
    # -> (1000*2.5 + 1000*10)/1e6 = 0.0125 USD -> round(0.0125 * 250) = 3.
    usage = {
        "input_tokens": 1000,
        "output_tokens": 1000,
        "model": "gpt-4o",
    }
    if reported is not None:
        usage["total_cost_usd"] = reported
    run = await _make_run(run_id=f"run-est-{reported}", usage=usage)

    before = await credits.balance(WS)
    result = await metering.bill_run(run, rate_card=RATE)

    assert result.cost_source == "estimated"
    assert result.credits_charged > 0
    assert result.credits_charged == 3
    assert await credits.balance(WS) == before - result.credits_charged


# ---------------------------------------------------------------------------
# Criterion 3 — running the sweeper TWICE debits ONCE (idempotency).
# ---------------------------------------------------------------------------


async def test_sweep_twice_debits_once(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _make_run(
        run_id="run-sweep",
        usage={"total_cost_usd": 0.04, "model": "gpt-4o"},
    )

    first = await sweep_unbilled_runs(rate_card=RATE)
    assert first == 1
    balance_after_first = await credits.balance(WS)
    assert balance_after_first == 990  # 1000 - round(0.04*250)=10

    # Second sweep: the run is now billed=True, so it isn't even re-queried.
    second = await sweep_unbilled_runs(rate_card=RATE)
    assert second == 0
    assert await credits.balance(WS) == balance_after_first

    # Exactly one compute_spend ledger row for this run.
    spend = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "run:run-sweep",
    ).to_list()
    assert len(spend) == 1


async def test_sweep_idempotent_even_if_flag_lost(mongo_db):
    """Belt-and-braces: even if the billed flag is lost, the run_id key blocks a
    second debit. Force ``billed`` back to False and re-sweep — the ledger key
    makes the re-bill a no-op, so the balance never moves twice."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    run = await _make_run(
        run_id="run-flagloss",
        usage={"total_cost_usd": 0.04, "model": "gpt-4o"},
    )

    await sweep_unbilled_runs(rate_card=RATE)
    balance_once = await credits.balance(WS)
    assert balance_once == 990

    # Simulate a lost flag write (crash between debit and save).
    run.billed = False
    await run.save()

    swept = await sweep_unbilled_runs(rate_card=RATE)
    assert swept == 1  # it was re-picked up...
    assert await credits.balance(WS) == balance_once  # ...but the debit was a no-op

    spend = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "run:run-flagloss",
    ).to_list()
    assert len(spend) == 1  # still exactly one ledger row


# ---------------------------------------------------------------------------
# Criterion 4 — empty usage -> debit 0 but still mark billed.
# ---------------------------------------------------------------------------


async def test_empty_usage_bills_zero_but_marks_billed(mongo_db):
    await credits.grant(WS, 500, cause="top_up", idempotency_key="seed")
    run = await _make_run(run_id="run-empty", status="failed", usage={})

    result = await metering.bill_run(run, rate_card=RATE)

    assert result.credits_charged == 0
    assert result.debited is False
    assert result.cost_source == "none"
    assert await credits.balance(WS) == 500  # unchanged

    # No ledger row for this run.
    spend = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "run:run-empty",
    ).to_list()
    assert len(spend) == 0

    # But the run is marked billed so the sweep won't re-visit it.
    reloaded = await ChatRunDoc.find_one(ChatRunDoc.run_id == "run-empty")
    assert reloaded is not None
    assert reloaded.billed is True

    # And a sweep over an all-billed table is a no-op.
    assert await sweep_unbilled_runs(rate_card=RATE) == 0


# ---------------------------------------------------------------------------
# Criterion 5 — allow_negative: a run costlier than the wallet bills fully and
# drives the balance below zero (metered spend is never blocked here).
# ---------------------------------------------------------------------------


async def test_overage_drives_balance_negative(mongo_db):
    # Tiny wallet: 5 credits.
    await credits.grant(WS, 5, cause="top_up", idempotency_key="seed")
    # cost 0.04 -> 10 credits, which exceeds the 5-credit wallet.
    run = await _make_run(
        run_id="run-overage",
        usage={"total_cost_usd": 0.04, "model": "gpt-4o"},
    )

    result = await metering.bill_run(run, rate_card=RATE)

    assert result.credits_charged == 10
    assert result.debited is True
    # 5 - 10 == -5: the overage is recorded as a legitimate negative balance.
    assert result.balance_after == -5
    assert await credits.balance(WS) < 0
    assert await credits.balance(WS) == -5

    reloaded = await ChatRunDoc.find_one(ChatRunDoc.run_id == "run-overage")
    assert reloaded is not None
    assert reloaded.billed is True


# ---------------------------------------------------------------------------
# Extra — the sweep bills the non-completed terminal states too (a cancelled /
# interrupted / failed run consumed tokens), and skips non-terminal runs.
# ---------------------------------------------------------------------------


async def test_sweep_covers_terminal_states_and_skips_active(mongo_db):
    await credits.grant(WS, 10_000, cause="top_up", idempotency_key="seed")
    # Four billable terminals.
    for i, status in enumerate(["completed", "cancelled", "interrupted", "failed"]):
        await _make_run(
            run_id=f"run-term-{i}",
            status=status,
            usage={"total_cost_usd": 0.04, "model": "gpt-4o"},
        )
    # Two non-terminal runs that must NOT be billed.
    await _make_run(run_id="run-queued", status="queued", usage={"total_cost_usd": 0.04})
    await _make_run(run_id="run-running", status="running", usage={"total_cost_usd": 0.04})

    billed = await sweep_unbilled_runs(rate_card=RATE)
    assert billed == 4  # only the four terminals

    # The active runs stay unbilled.
    for rid in ("run-queued", "run-running"):
        doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == rid)
        assert doc is not None and doc.billed is False


# ---------------------------------------------------------------------------
# Token attribution — bill_run stamps the run's real total_tokens on the debit
# ref so the ledger-sourced usage graph can surface real volume (not a hardcoded
# 0). The token counts ride ChatRunDoc.usage in every mode — NOT the LiteLLM path.
# ---------------------------------------------------------------------------


async def test_bill_run_stamps_total_tokens_on_ref(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    run = await _make_run(
        run_id="run-tok",
        usage={
            "total_cost_usd": 0.04,
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 1200,
            "output_tokens": 300,
            "cached_input_tokens": 100,
        },
    )

    await metering.bill_run(run, rate_card=RATE)

    spend = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "run:run-tok",
    ).to_list()
    assert len(spend) == 1
    # input + output + cached = 1200 + 300 + 100.
    assert spend[0].ref.get("total_tokens") == 1600


async def test_bill_run_prefers_explicit_total_tokens(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    run = await _make_run(
        run_id="run-tt",
        usage={
            "total_cost_usd": 0.04,
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 999,  # backend supplied an explicit total — trust it
        },
    )

    await metering.bill_run(run, rate_card=RATE)

    spend = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "run:run-tt",
    ).to_list()
    assert len(spend) == 1
    assert spend[0].ref.get("total_tokens") == 999


# ---------------------------------------------------------------------------
# Prices are effective-dated, and this meter runs late (2026-09-02).
# ---------------------------------------------------------------------------


async def test_a_backlogged_run_bills_at_the_rate_it_actually_ran_at(mongo_db):
    """The crux of the dated-pricing fix, in the one place it costs money.

    ``claude-sonnet-5`` was $2.00/MTok through 2026-08-31 and $3.00 from
    2026-09-01. This sweeper bills AFTER the run, 200 at a tick, so a backlog
    spans hours or days. Pricing at ``now()`` bills August's run at September's
    rate - a 50% overcharge that looks like a correct bill from every angle
    except the date.
    """
    await credits.grant(WS, 100_000, cause="top_up", idempotency_key="seed-dated")
    usage = {"input_tokens": 1_000_000, "output_tokens": 0, "model": "claude-sonnet-5"}

    before = await _make_run(
        run_id="run-dated-before",
        usage=usage,
        ended_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )
    after = await _make_run(
        run_id="run-dated-after",
        usage=usage,
        ended_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )

    old_bill = await metering.bill_run(before, rate_card=RATE)
    new_bill = await metering.bill_run(after, rate_card=RATE)

    # $2.00 and $3.00 for the same million tokens, because they ran on different
    # days. round(2.00 * 250) = 500 credits; round(3.00 * 250) = 750.
    assert old_bill.cost_usd == 2.00
    assert old_bill.credits_charged == 500
    assert new_bill.cost_usd == 3.00
    assert new_bill.credits_charged == 750


async def test_a_cached_anthropic_payload_is_read_as_an_inclusive_prompt(mongo_db):
    """The payload ``pydantic_ai`` writes carries the UNCACHED remainder in
    ``input_tokens`` with the cache lines beside it, so the meter has to add
    them back before pricing. Reading the remainder as the total subtracts the
    cache twice and undercounts every cached turn.

    10k inclusive prompt (1k fresh + 8k read + 1k write) and 1k out on
    ``claude-sonnet-5``: 0.003 + 0.0024 + 0.00375 + 0.015 = $0.02415.
    round(0.02415 * 250) = 6 credits.
    """
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed-cached")
    run = await _make_run(
        run_id="run-cached",
        usage={
            "input_tokens": 1_000,
            "output_tokens": 1_000,
            "cached_input_tokens": 8_000,
            "cache_read_tokens": 8_000,
            "cache_write_tokens": 1_000,
            "model": "claude-sonnet-5",
        },
        ended_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    result = await metering.bill_run(run, rate_card=RATE)

    assert result.cost_source == "estimated"
    assert result.cost_usd == 0.02415
    assert result.credits_charged == 6


async def test_an_unpriced_model_is_its_own_source_and_not_silence(mongo_db):
    """C4. A run that consumed real tokens on a model nothing can price used to
    be indistinguishable from a run with no usage at all: both billed 0, both
    logged at DEBUG, both reported ``source="none"``. One of those is nothing to
    bill and the other is a bill we are failing to send."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed-unpriced")
    run = await _make_run(
        run_id="run-unpriced",
        usage={
            "input_tokens": 50_000,
            "output_tokens": 5_000,
            "model": "some-other-vendor-model",
        },
        ended_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    empty = await _make_run(
        run_id="run-empty-usage",
        usage={},
        ended_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    unpriced = await metering.bill_run(run, rate_card=RATE)
    nothing = await metering.bill_run(empty, rate_card=RATE)

    assert unpriced.cost_source == "unpriced"
    assert unpriced.credits_charged == 0
    assert nothing.cost_source == "none"
    # Both still marked billed - the sweep must not re-visit either forever.
    for run_id in ("run-unpriced", "run-empty-usage"):
        doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == run_id)
        assert doc is not None and doc.billed is True


async def test_the_sweep_names_the_models_it_could_not_price(mongo_db, caplog):
    """An unpriced run bills 0 credits, which writes no ledger row, so the tick
    is the last place the fact exists. One warning per tick naming the distinct
    models - not one per run, or a backlog on a single bad id reads as two
    hundred problems."""
    import logging

    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed-sweep-unpriced")
    for i in range(3):
        await _make_run(
            run_id=f"run-sweep-unpriced-{i}",
            usage={
                "input_tokens": 1_000,
                "output_tokens": 100,
                "model": "some-other-vendor-model",
            },
            ended_at=datetime(2026, 9, 1, tzinfo=UTC),
        )

    with caplog.at_level(logging.WARNING):
        await sweep_unbilled_runs(rate_card=RATE, mode="off")

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    tick_lines = [m for m in warnings if "could price them" in m]
    assert len(tick_lines) == 1, warnings
    assert "some-other-vendor-model" in tick_lines[0]


async def test_a_long_context_run_bills_the_long_context_tier(mongo_db):
    """C6. ``claude-sonnet-4-5`` is $3.00/MTok up to 200k prompt tokens and
    $6.00 above. A single flat rate per model cannot express that, so every long
    prompt billed at half price. 250k in / 0 out = $1.50 -> 375 credits."""
    await credits.grant(WS, 100_000, cause="top_up", idempotency_key="seed-longctx")
    run = await _make_run(
        run_id="run-longctx",
        usage={"input_tokens": 250_000, "output_tokens": 0, "model": "claude-sonnet-4-5"},
        ended_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    result = await metering.bill_run(run, rate_card=RATE)

    assert result.cost_usd == 1.50
    assert result.credits_charged == 375


# ===========================================================================
# BILL AT COMPLETION — the wallet should not be a sweep interval behind.
# ===========================================================================


async def test_bill_run_now_charges_without_waiting_for_a_sweep(mongo_db):
    # The point of the whole thing: a finished run is charged immediately, so a
    # customer who opens billing straight after chatting sees their own message.
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _make_run(run_id="r-now", usage={"total_cost_usd": 0.04})

    result = await metering.bill_run_now("r-now")

    assert result is not None
    assert result.debited is True
    assert result.credits_charged == 10  # round(0.04 * 250)
    assert await credits.balance(WS) == 990

    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == "r-now")
    assert doc.billed is True


async def test_the_sweeper_does_not_charge_again_for_a_run_billed_at_completion(mongo_db):
    """The money invariant. Completion billing is an optimisation ON TOP of the
    sweep, not a replacement, so both running must still charge exactly once."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _make_run(run_id="r-once", usage={"total_cost_usd": 0.04})

    await metering.bill_run_now("r-once")
    assert await credits.balance(WS) == 990

    # The sweep reaches it later and must move nothing.
    swept = await sweep_unbilled_runs(rate_card=RATE)
    assert swept == 0
    assert await credits.balance(WS) == 990

    entries = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "run:r-once",
    ).to_list()
    assert len(entries) == 1


async def test_an_already_billed_run_is_left_alone(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _make_run(run_id="r-done", usage={"total_cost_usd": 0.04}, billed=True)

    assert await metering.bill_run_now("r-done") is None
    assert await credits.balance(WS) == 1000


async def test_a_run_still_in_flight_is_left_to_the_sweep(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _make_run(run_id="r-running", status="running", usage={"total_cost_usd": 0.04})

    assert await metering.bill_run_now("r-running") is None
    assert await credits.balance(WS) == 1000


async def test_a_missing_run_is_not_an_error(mongo_db):
    # A crash between the terminal write and this call leaves nothing to bill.
    assert await metering.bill_run_now("r-nope") is None


async def test_live_mode_gates_completion_billing_off(mongo_db, monkeypatch):
    """The single-meter guarantee has to hold at completion too, not just on the
    tick. Otherwise flipping to ``live`` would silently double-charge every run:
    LiteLLM from proxy spend, and this from the run doc."""
    import pocketpaw_ee.cloud.llm_provisioning.service as provisioning

    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _make_run(run_id="r-live", usage={"total_cost_usd": 0.04})
    monkeypatch.setattr(provisioning, "spend_mode", lambda: "live")

    assert await metering.bill_run_now("r-live") is None
    assert await credits.balance(WS) == 1000

    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == "r-live")
    assert doc.billed is False, "live mode must not flip the flag either"


async def test_a_billing_failure_never_propagates_into_the_run(mongo_db, monkeypatch):
    """A run that already succeeded must not be failed by its own accounting.
    Swallowing here is safe precisely because the sweeper retries."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _make_run(run_id="r-boom", usage={"total_cost_usd": 0.04})

    async def _explode(*a, **k):
        raise RuntimeError("ledger unreachable (simulated)")

    monkeypatch.setattr(metering, "bill_run", _explode)

    assert await metering.bill_run_now("r-boom") is None  # no raise

    # Still unbilled, so the sweep will pick it up.
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == "r-boom")
    assert doc.billed is False


async def test_a_run_missed_at_completion_is_still_swept(mongo_db):
    # Completion billing is best-effort; the sweeper remains the backstop and
    # must still charge a run that never reached bill_run_now.
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _make_run(run_id="r-missed", usage={"total_cost_usd": 0.04})

    swept = await sweep_unbilled_runs(rate_card=RATE)

    assert swept == 1
    assert await credits.balance(WS) == 990
