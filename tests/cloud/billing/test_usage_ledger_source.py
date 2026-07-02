# tests/cloud/billing/test_usage_ledger_source.py — REGRESSION for the
# "No usage to chart yet" bug: the per-workspace usage graph must render spend
# that exists in the CREDIT LEDGER even when the workspace has NO LiteLLM virtual
# key. In the default metering mode (BC-3) a finished run's compute cost is
# debited to the ledger as a ``compute_spend`` movement and NEVER routed through
# the LiteLLM proxy — so a workspace can carry real spend (the dashboard's
# "Recent activity" list is full) while the proxy's /user/daily/activity is empty.
# The chart was sourced from the proxy, so it showed the empty state despite the
# wallet being well into the negative. This test pins the corrected contract: the
# graph is built from the wallet's own ledger, so the chart and the wallet agree.
#
# Seeds APPLIED debit entries directly (the read path is what's under test), one
# per (day, model). ``createdAt`` is back-dated via the raw collection because
# ``TimestampedDocument``'s @before_event(Insert) stamps createdAt=now, so a past
# date can't be set through the constructor.
#
# Created 2026-06-29 (fix/billing-usage-ledger-source): RED anchor for
# re-sourcing get_workspace_usage from the credit ledger instead of the LiteLLM
# proxy daily-activity.

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.billing import usage
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

WS = "ws_usage_ledger_test"
SONNET = "anthropic/claude-3-5-sonnet"
GPT = "openai/gpt-4o"


async def _seed_spend(
    ws: str,
    *,
    day: tuple[int, int, int],
    model: str,
    credits: int,
    idem: str,
    cause: str = "compute_spend",
) -> None:
    """Insert one APPLIED debit ledger entry stamped on a controlled date.

    Mirrors what ``metering.service.bill_run`` writes: a negative ``amount_delta``
    under ``cause`` with the run's ``model`` in ``ref``. Inserts first (the Insert
    event forces createdAt=now), then back-dates ``createdAt`` via the raw
    collection so the (day, model) bucketing can be asserted.
    """
    entry = CreditLedgerEntry(
        workspace=ws,
        kind="spend",
        amount_delta=-credits,
        balance_after=0,
        applied=True,
        conditional=False,
        cause=cause,
        ref={"model": model},
        idempotency_key=idem,
    )
    await entry.insert()
    when = datetime(day[0], day[1], day[2], 12, 0, tzinfo=UTC)
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": entry.id}, {"$set": {"createdAt": when}}
    )


async def test_usage_renders_ledger_compute_spend_without_litellm_key(mongo_db):
    """A workspace with compute_spend in the ledger but NO LiteLLM key renders its
    spend on the chart (the bug returned an empty contract here)."""
    # The exact bug scenario: real spend, no provisioned proxy key.
    assert await provisioning.get_tenant_key(WS) is None

    # Day 1: two models. Day 2: sonnet only.
    await _seed_spend(WS, day=(2026, 6, 1), model=SONNET, credits=10, idem="run:r1")
    await _seed_spend(WS, day=(2026, 6, 1), model=GPT, credits=5, idem="run:r2")
    await _seed_spend(WS, day=(2026, 6, 2), model=SONNET, credits=25, idem="run:r3")

    result = await usage.get_workspace_usage(WS, start_date="2026-06-01", end_date="2026-06-02")

    # Pre-fix: no key -> the proxy source returns models=[], buckets=[] ("No usage
    # to chart yet"). Post-fix: the ledger spend renders and matches the wallet.
    assert result.models == [SONNET, GPT]  # sorted distinct union
    assert [b.date for b in result.buckets] == ["2026-06-01", "2026-06-02"]

    b1 = result.buckets[0]
    assert b1.by_model[SONNET].credits == 10
    assert b1.by_model[GPT].credits == 5
    assert b1.total_credits == 15

    b2 = result.buckets[1]
    assert set(b2.by_model.keys()) == {SONNET}
    assert b2.by_model[SONNET].credits == 25
    assert b2.total_credits == 25

    # The grand total equals the wallet's compute spend over the window.
    assert result.total_credits == 40


async def test_usage_includes_litellm_spend_cause_after_cutover(mongo_db):
    """After an off->live cutover the ledger carries ``litellm_spend`` entries; the
    graph reads both causes so it still matches the wallet (no double-count — a run
    is billed under exactly one cause)."""
    assert await provisioning.get_tenant_key(WS) is None

    await _seed_spend(
        WS, day=(2026, 6, 1), model=SONNET, credits=8, idem="run:bc3", cause="compute_spend"
    )
    await _seed_spend(
        WS, day=(2026, 6, 2), model=SONNET, credits=12, idem="ingest:live", cause="litellm_spend"
    )

    result = await usage.get_workspace_usage(WS, start_date="2026-06-01", end_date="2026-06-02")

    assert result.models == [SONNET]
    assert result.total_credits == 20  # 8 (compute_spend) + 12 (litellm_spend)


async def test_usage_excludes_non_spend_causes(mongo_db):
    """A top_up / grant movement is NOT compute usage and must not appear on the
    usage-by-model graph (only spend causes are charted)."""
    assert await provisioning.get_tenant_key(WS) is None

    await _seed_spend(
        WS, day=(2026, 6, 1), model=SONNET, credits=10, idem="run:spend", cause="compute_spend"
    )
    # A positive grant under a non-spend cause (seeded as a movement on the ledger).
    grant = CreditLedgerEntry(
        workspace=WS,
        kind="grant",
        amount_delta=5000,
        balance_after=0,
        applied=True,
        conditional=False,
        cause="top_up",
        ref={},
        idempotency_key="topup:1",
    )
    await grant.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": grant.id}, {"$set": {"createdAt": datetime(2026, 6, 1, 12, 0, tzinfo=UTC)}}
    )

    result = await usage.get_workspace_usage(WS, start_date="2026-06-01", end_date="2026-06-01")

    # Only the compute_spend movement is charted; the top_up is ignored.
    assert result.models == [SONNET]
    assert result.total_credits == 10
