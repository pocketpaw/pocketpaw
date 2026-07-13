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

from __future__ import annotations

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
) -> ChatRunDoc:
    """Insert a terminal ChatRunDoc carrying the given usage dict."""
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
