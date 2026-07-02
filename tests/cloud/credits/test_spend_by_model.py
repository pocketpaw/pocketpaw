# tests/cloud/credits/test_spend_by_model.py — proves the BC-1 credits service's
# ``spend_by_model`` read: the (UTC day, model) spend breakdown the billing usage
# graph is sourced from. The wallet's own ledger is the universal meter, so this
# read is what makes the "Usage by model" chart reconcile with the wallet.
#
# Covers:
#   * the cause filter — only the spend causes (compute_spend / litellm_spend) feed
#     it; a top_up / grant under another cause is excluded.
#   * the ``applied is False`` exclusion — a committed-but-unapplied phantom never
#     moved the wallet, so it must not appear on the chart.
#   * the EXCLUSIVE ``until`` boundary — an entry stamped exactly at ``until`` is
#     out; one at ``since`` is in (inclusive since / exclusive until, identical to
#     ``sum_debits_by_cause`` so adjacent windows never double-count).
#   * multi-day / multi-model aggregation and the per-group request count.
#   * a debit with no ``ref.model`` -> the "unknown" bucket (its credits still count
#     so the total reconciles with the wallet).
#
# Uses the shared ``mongo_db`` fixture (mongomock-motor + Beanie over ALL_DOCUMENTS).
# ``createdAt`` is back-dated via the raw collection (the Insert event stamps
# createdAt=now, so a controlled date can't be set through the constructor).
#
# Created 2026-06-29 (fix/billing-usage-ledger-source): new test module — unit
# coverage for the ledger read that re-sources the billing usage graph.

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.credits.domain import ModelSpendRow
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

WS = "ws_spend_by_model"
SONNET = "anthropic/claude-3-5-sonnet"
GPT = "openai/gpt-4o"


async def _seed(
    *,
    when: datetime,
    amount_delta: int,
    model: str | None = SONNET,
    cause: str = "compute_spend",
    applied: bool = True,
    idem: str,
    ws: str = WS,
) -> None:
    """Insert one ledger entry stamped at ``when`` (back-dated via the raw
    collection so the window boundaries can be asserted)."""
    ref = {"model": model} if model is not None else {}
    entry = CreditLedgerEntry(
        workspace=ws,
        kind="spend",
        amount_delta=amount_delta,
        balance_after=0,
        applied=applied,
        conditional=False,
        cause=cause,
        ref=ref,
        idempotency_key=idem,
    )
    await entry.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": entry.id}, {"$set": {"createdAt": when}}
    )


def _by_key(rows: list[ModelSpendRow]) -> dict[tuple[str, str], ModelSpendRow]:
    return {(r.day, r.model): r for r in rows}


async def test_groups_by_day_and_model_with_request_counts(mongo_db):
    # Day 1: two sonnet runs + one gpt run. Day 2: one sonnet run.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC), amount_delta=-4, model=SONNET, idem="a"
    )
    await _seed(
        when=datetime(2026, 6, 1, 18, 0, tzinfo=UTC), amount_delta=-6, model=SONNET, idem="b"
    )
    await _seed(when=datetime(2026, 6, 1, 12, 0, tzinfo=UTC), amount_delta=-5, model=GPT, idem="c")
    await _seed(
        when=datetime(2026, 6, 2, 8, 0, tzinfo=UTC), amount_delta=-25, model=SONNET, idem="d"
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 3, tzinfo=UTC),
    )
    by = _by_key(rows)

    assert by[("2026-06-01", SONNET)].credits == 10  # 4 + 6
    assert by[("2026-06-01", SONNET)].requests == 2  # two entries
    assert by[("2026-06-01", GPT)].credits == 5
    assert by[("2026-06-01", GPT)].requests == 1
    assert by[("2026-06-02", SONNET)].credits == 25
    assert by[("2026-06-02", SONNET)].requests == 1
    # Exactly three groups.
    assert len(rows) == 3


async def test_reads_both_spend_causes(mongo_db):
    # compute_spend (off-mode) + litellm_spend (post-cutover) both feed the chart.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        amount_delta=-8,
        model=SONNET,
        cause="compute_spend",
        idem="bc3",
    )
    await _seed(
        when=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        amount_delta=-12,
        model=SONNET,
        cause="litellm_spend",
        idem="live",
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )
    by = _by_key(rows)

    assert by[("2026-06-01", SONNET)].credits == 20  # 8 + 12
    assert by[("2026-06-01", SONNET)].requests == 2


async def test_excludes_non_spend_causes(mongo_db):
    # A grant / top_up movement is not compute usage — it must not chart.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        amount_delta=-10,
        model=SONNET,
        cause="compute_spend",
        idem="spend",
    )
    # A positive grant under a non-spend cause.
    await _seed(
        when=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
        amount_delta=5000,
        model=None,
        cause="top_up",
        idem="topup",
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert rows[0].model == SONNET
    assert rows[0].credits == 10


async def test_excludes_unapplied_phantom(mongo_db):
    # A committed-but-unapplied entry never moved the wallet — exclude it.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        amount_delta=-10,
        model=SONNET,
        applied=True,
        idem="real",
    )
    await _seed(
        when=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        amount_delta=-1000,
        model=SONNET,
        applied=False,  # phantom
        idem="phantom",
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert rows[0].credits == 10  # the phantom -1000 is NOT counted


async def test_until_is_exclusive_since_is_inclusive(mongo_db):
    # An entry exactly at ``since`` is IN; one exactly at ``until`` is OUT.
    since = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)
    await _seed(when=since, amount_delta=-3, model=SONNET, idem="at_since")
    await _seed(when=until, amount_delta=-7, model=SONNET, idem="at_until")

    rows = await credits.spend_by_model(WS, since=since, until=until)

    assert len(rows) == 1
    # Only the at-``since`` entry (2026-06-01) is in window; the at-``until`` one
    # (2026-06-02 00:00) is excluded.
    assert rows[0].day == "2026-06-01"
    assert rows[0].credits == 3


async def test_missing_ref_model_buckets_unknown(mongo_db):
    # A charged debit with no ``ref.model`` still counts, under "unknown", so the
    # total reconciles with the wallet.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC), amount_delta=-15, model=None, idem="nm"
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert rows[0].model == "unknown"
    assert rows[0].credits == 15


async def test_is_tenant_scoped(mongo_db):
    # Another workspace's spend must not leak into this workspace's chart.
    await _seed(when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC), amount_delta=-10, idem="mine")
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC), amount_delta=-999, idem="theirs", ws="other_ws"
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert rows[0].credits == 10  # not 999
