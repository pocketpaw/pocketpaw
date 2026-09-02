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
# Updated 2026-07-11 (feat/llm-cost-attribution): ``_seed`` gained a ``tokens``
# param (stamps ``ref.total_tokens``) and two cases cover the new token sum —
# real per-group volume across token-bearing debits + a legacy debit adding 0,
# and an all-legacy group reporting tokens=0.
# Updated 2026-09-02 (fix/credits-spend-by-model-aggregation): the fold moved off
# a pull-the-window-into-Python loop onto a server-side ``$match`` + ``$group``.
# Five cases were added to hold that migration honest, three of them covering
# behaviour the suite never asserted before:
#   * a non-negative ``amount_delta`` under a spend cause is SKIPPED, not clamped
#     — it must not reach credits, the request count, the token sum, or invent a
#     (day, model) group. The old suite never seeded one, so a pipeline that
#     clamped instead of skipping would have passed.
#   * the UTC day boundary — 23:59:59Z and the next 00:00:00Z land in different
#     day buckets. This is the case ``$dateToString`` could silently get wrong if
#     it were ever given a non-UTC timezone.
#   * one kitchen-sink window asserting the EXACT row set across multiple days,
#     multiple models, a missing model, a missing token count and an excluded
#     positive delta — the aggregate equivalent of the old fold's output.
#   * ``spend_by_model`` no longer loads documents: with ``CreditLedgerEntry.find``
#     sabotaged to raise, the read still returns correct rows.
#   * the pipeline document itself is asserted, so the ``$group`` can't quietly
#     regress into a client-side fold that still passes the behavioural cases.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.credits.domain import ModelSpendRow
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

WS = "ws_spend_by_model"
SONNET = "anthropic/claude-3-5-sonnet"
GPT = "openai/gpt-4o"

# Distinguishes "no ``total_tokens`` key at all" (a legacy debit) from a key
# explicitly set to null — two different branches of the token fold.
_NO_TOKENS = object()


async def _seed(
    *,
    when: datetime,
    amount_delta: int,
    model: str | None = SONNET,
    cause: str = "compute_spend",
    applied: bool = True,
    idem: str,
    ws: str = WS,
    tokens: object = _NO_TOKENS,
) -> None:
    """Insert one ledger entry stamped at ``when`` (back-dated via the raw
    collection so the window boundaries can be asserted).

    When ``tokens`` is given it is stamped on the ref as ``total_tokens`` (the real
    volume the metering path records) — including an explicit ``None``. Omitting
    it entirely models a legacy debit whose ref predates token attribution
    (contributes 0 to the group's token sum)."""
    ref = {"model": model} if model is not None else {}
    if tokens is not _NO_TOKENS:
        ref["total_tokens"] = tokens
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


async def test_sums_total_tokens_per_group(mongo_db):
    # Two token-bearing debits + one legacy debit (no total_tokens) in one group:
    # the group's tokens is the sum of the real figures, the legacy one adds 0.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        amount_delta=-4,
        model=SONNET,
        idem="t1",
        tokens=1000,
    )
    await _seed(
        when=datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
        amount_delta=-6,
        model=SONNET,
        idem="t2",
        tokens=500,
    )
    await _seed(
        when=datetime(2026, 6, 1, 20, 0, tzinfo=UTC),
        amount_delta=-2,
        model=SONNET,
        idem="t3",  # legacy: no total_tokens on the ref
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )
    by = _by_key(rows)

    row = by[("2026-06-01", SONNET)]
    assert row.tokens == 1500  # 1000 + 500 + 0 (legacy)
    assert row.requests == 3
    assert row.credits == 12  # 4 + 6 + 2 — unaffected by tokens


async def test_tokens_default_zero_when_no_ref_tokens(mongo_db):
    # A group made entirely of legacy debits reports tokens=0 (credits still real).
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC), amount_delta=-7, model=GPT, idem="legacy"
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert rows[0].tokens == 0
    assert rows[0].credits == 7


async def test_non_negative_delta_is_skipped_not_clamped(mongo_db):
    # Spend is debit-only. A stray zero-or-positive ``amount_delta`` under a spend
    # cause is a bad row: it must not reach the credits sum, must not inflate the
    # request count, must not contribute its tokens, and must not conjure a group
    # of its own on a day that had no real spend.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        amount_delta=-10,
        model=SONNET,
        idem="real",
        tokens=100,
    )
    await _seed(  # same (day, model) group as the real debit
        when=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        amount_delta=50,
        model=SONNET,
        idem="stray_positive",
        tokens=9999,
    )
    await _seed(  # a group that would exist ONLY because of the bad row
        when=datetime(2026, 6, 2, 10, 0, tzinfo=UTC),
        amount_delta=0,
        model=GPT,
        idem="stray_zero",
        tokens=9999,
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 3, tzinfo=UTC),
    )

    assert len(rows) == 1  # the zero-delta row invented no second group
    assert rows[0] == ModelSpendRow(
        day="2026-06-01", model=SONNET, credits=10, requests=1, tokens=100
    )


async def test_day_bucket_splits_on_the_utc_midnight_boundary(mongo_db):
    # One second before midnight UTC and midnight itself are different days. A
    # fold that resolved the day in any other timezone would merge them.
    await _seed(
        when=datetime(2026, 6, 1, 23, 59, 59, tzinfo=UTC),
        amount_delta=-11,
        model=SONNET,
        idem="late",
        tokens=2,
    )
    await _seed(
        when=datetime(2026, 6, 2, 0, 0, 0, tzinfo=UTC),
        amount_delta=-13,
        model=SONNET,
        idem="early",
        tokens=4,
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 3, tzinfo=UTC),
    )
    by = _by_key(rows)

    assert len(rows) == 2
    assert by[("2026-06-01", SONNET)].credits == 11
    assert by[("2026-06-01", SONNET)].tokens == 2
    assert by[("2026-06-02", SONNET)].credits == 13
    assert by[("2026-06-02", SONNET)].tokens == 4


async def test_folds_a_mixed_window_into_the_exact_row_set(mongo_db):
    # Every branch of the fold in one window: two days, two models, a debit with
    # no ``ref.model``, a debit with no ``ref.total_tokens``, and a positive delta
    # that must not appear anywhere. Asserting the EXACT list (not spot values)
    # is what makes this a byte-for-byte contract rather than a smoke test.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        amount_delta=-4,
        model=SONNET,
        idem="d1a",
        tokens=1000,
    )
    await _seed(
        when=datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
        amount_delta=-6,
        model=SONNET,
        idem="d1b",  # legacy: no total_tokens
    )
    await _seed(
        when=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        amount_delta=-5,
        model=GPT,
        cause="litellm_spend",
        idem="d1c",
        tokens=250,
    )
    await _seed(
        when=datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
        amount_delta=7,  # refund-shaped row under a spend cause: excluded
        model=GPT,
        idem="d1d",
        tokens=9999,
    )
    await _seed(
        when=datetime(2026, 6, 2, 8, 0, tzinfo=UTC),
        amount_delta=-25,
        model=None,  # unattributed spend still charts, under "unknown"
        idem="d2a",
        tokens=42,
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 3, tzinfo=UTC),
    )

    assert rows == [
        ModelSpendRow(day="2026-06-01", model=SONNET, credits=10, requests=2, tokens=1000),
        ModelSpendRow(day="2026-06-01", model=GPT, credits=5, requests=1, tokens=250),
        ModelSpendRow(day="2026-06-02", model="unknown", credits=25, requests=1, tokens=42),
    ]
    # The wallet's own total for the window: 4 + 6 + 5 + 25. The chart reconciles.
    assert sum(r.credits for r in rows) == 40


async def test_reads_without_loading_ledger_documents(mongo_db, monkeypatch):
    # The point of the change: the grouping happens in Mongo. Sabotage the
    # document-loading entry point and the read must still work — if anything
    # reintroduces a ``find(...).to_list()`` fold this fails loudly.
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        amount_delta=-8,
        model=SONNET,
        idem="s1",
        tokens=64,
    )

    def _no_document_loads(*_args, **_kwargs):
        raise AssertionError("spend_by_model must not fetch ledger documents")

    monkeypatch.setattr(CreditLedgerEntry, "find", _no_document_loads)
    monkeypatch.setattr(CreditLedgerEntry, "find_all", _no_document_loads)

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )

    assert rows == [ModelSpendRow(day="2026-06-01", model=SONNET, credits=8, requests=1, tokens=64)]


def test_pipeline_groups_server_side():
    # Guard the pipeline document itself. The behavioural cases above would still
    # pass if the grouping quietly moved back into Python, so assert the stages.
    query = {"workspace": WS, "applied": True}
    match_stage, group_stage = credits._spend_by_model_pipeline(query)

    # The caller's filter survives, and non-negative deltas are excluded by the
    # query rather than by a post-hoc clamp.
    assert match_stage["$match"]["workspace"] == WS
    assert match_stage["$match"]["applied"] is True
    assert match_stage["$match"]["amount_delta"] == {"$lt": 0}
    assert query == {"workspace": WS, "applied": True}  # caller's dict untouched

    group = group_stage["$group"]
    # The day is resolved by Mongo, in UTC (no ``timezone`` key means UTC).
    assert group["_id"]["day"] == {"$dateToString": {"format": "%Y-%m-%d", "date": "$createdAt"}}
    assert "timezone" not in group["_id"]["day"]["$dateToString"]
    # All three measures accumulate in the ``$group``, not in a Python loop.
    assert group["credits"] == {"$sum": {"$subtract": [0, "$amount_delta"]}}
    assert group["requests"] == {"$sum": 1}
    assert set(group["tokens"]) == {"$sum"}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, 0),  # explicit null on the ref
        (0, 0),
        (-50, 0),  # negative volume is nonsense; reads 0
        (7, 7),
    ],
)
async def test_token_values_that_do_not_count(mongo_db, stored, expected):
    await _seed(
        when=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        amount_delta=-3,
        model=SONNET,
        idem=f"tok_{stored}",
        tokens=stored,
    )

    rows = await credits.spend_by_model(
        WS,
        since=datetime(2026, 6, 1, tzinfo=UTC),
        until=datetime(2026, 6, 2, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert rows[0].tokens == expected
    assert rows[0].credits == 3  # the credits figure is never affected


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
