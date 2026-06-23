# tests/cloud/credits/test_ledger.py — proves the BC-1 credit ledger contract:
# the atomic, idempotent grant / debit / balance / history / reconcile API and
# its two money-handling invariants:
#   (a) a duplicate idempotency_key never double-applies;
#   (b) a rejected over-debit leaves NO ledger entry and NO balance change.
#
# Uses the shared ``mongo_db`` fixture (mongomock-motor + Beanie init over
# ALL_DOCUMENTS) from tests/cloud/conftest.py — the same DB-fixture pattern the
# chat-runs / home-pocket service tests use. The autouse ``recording_bus``
# fixture installs a RecordingBus so the service's ``emit(CreditMovement(...))``
# calls never raise.
#
# Created 2026-06-23 (integration/billing-credits, BC-1): new test module.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud._core.errors import InsufficientCredits, ValidationError
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.credit import CreditBalance, CreditLedgerEntry

WS = "ws_credit_test"


# ---------------------------------------------------------------------------
# Criterion 1 — grant raises the balance; balance() reflects it.
# ---------------------------------------------------------------------------


async def test_grant_raises_balance(mongo_db):
    new_balance = await credits.grant(WS, 500, cause="top_up", idempotency_key="g1")
    assert new_balance == 500
    assert await credits.balance(WS) == 500

    # A second, distinct grant accumulates.
    await credits.grant(WS, 250, cause="promo", idempotency_key="g2")
    assert await credits.balance(WS) == 750


# ---------------------------------------------------------------------------
# Criterion 2 — debit lowers the balance.
# ---------------------------------------------------------------------------


async def test_debit_lowers_balance(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="g1")
    new_balance = await credits.debit(WS, 300, cause="compute_spend", idempotency_key="d1")
    assert new_balance == 700
    assert await credits.balance(WS) == 700

    # The spend is recorded as a signed-negative ledger movement.
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == WS).to_list()
    spend = [e for e in entries if e.idempotency_key == "d1"][0]
    assert spend.amount_delta == -300
    assert spend.balance_after == 700
    assert spend.kind == "spend"


# ---------------------------------------------------------------------------
# Criterion 3 — over-debit raises InsufficientCredits AND leaves ZERO side
# effects (no balance change, no stray applied ledger entry).
# ---------------------------------------------------------------------------


async def test_over_debit_raises_and_has_no_side_effects(mongo_db):
    await credits.grant(WS, 100, cause="top_up", idempotency_key="g1")

    with pytest.raises(InsufficientCredits) as exc:
        await credits.debit(WS, 250, cause="compute_spend", idempotency_key="d_over")
    assert exc.value.status_code == 402
    assert exc.value.code == "credits.insufficient"

    # Balance unchanged.
    assert await credits.balance(WS) == 100

    # No ledger entry survives for the rejected debit (the insert was rolled
    # back), and the genesis grant is the only movement.
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == WS).to_list()
    assert all(e.idempotency_key != "d_over" for e in entries)
    assert len(entries) == 1

    # The freed key can be reused after a top-up — a retry re-evaluates cleanly.
    await credits.grant(WS, 500, cause="top_up", idempotency_key="g2")
    assert await credits.debit(WS, 250, cause="compute_spend", idempotency_key="d_over") == 350


# ---------------------------------------------------------------------------
# Criterion 4 — a duplicate idempotency_key is a no-op (balance unchanged, no
# second movement applied) for BOTH grant and debit.
# ---------------------------------------------------------------------------


async def test_duplicate_idempotency_key_is_noop_for_grant(mongo_db):
    first = await credits.grant(WS, 400, cause="top_up", idempotency_key="dup")
    second = await credits.grant(WS, 400, cause="top_up", idempotency_key="dup")
    assert first == 400
    assert second == 400  # returns current balance, does NOT re-apply
    assert await credits.balance(WS) == 400

    # Exactly one ledger row for the key.
    rows = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "dup",
    ).to_list()
    assert len(rows) == 1


async def test_duplicate_idempotency_key_is_noop_for_debit(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="g1")
    first = await credits.debit(WS, 200, cause="compute_spend", idempotency_key="dup_d")
    second = await credits.debit(WS, 200, cause="compute_spend", idempotency_key="dup_d")
    assert first == 800
    assert second == 800  # no double-spend
    assert await credits.balance(WS) == 800

    rows = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "dup_d",
    ).to_list()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Criterion 5 — two concurrent debits each for the full balance: exactly ONE
# succeeds (no double-spend); the other raises InsufficientCredits; final
# balance == 0.
# ---------------------------------------------------------------------------


async def test_concurrent_full_debits_exactly_one_succeeds(mongo_db):
    await credits.grant(WS, 500, cause="top_up", idempotency_key="g1")

    results = await asyncio.gather(
        credits.debit(WS, 500, cause="compute_spend", idempotency_key="c1"),
        credits.debit(WS, 500, cause="compute_spend", idempotency_key="c2"),
        return_exceptions=True,
    )

    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, InsufficientCredits)]
    assert len(successes) == 1, f"expected exactly one success, got {results!r}"
    assert len(failures) == 1, f"expected exactly one InsufficientCredits, got {results!r}"
    assert successes[0] == 0
    assert await credits.balance(WS) == 0

    # The loser left no applied ledger entry — only the grant + one spend remain.
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == WS).to_list()
    spends = [e for e in entries if e.kind == "spend"]
    assert len(spends) == 1


# ---------------------------------------------------------------------------
# Criterion 6 — reconcile() repairs a simulated drift.
# ---------------------------------------------------------------------------


async def test_reconcile_repairs_drift(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="g1")
    await credits.debit(WS, 300, cause="compute_spend", idempotency_key="d1")
    # Ledger sum: +1000 - 300 = 700.

    # Simulate a crash that left the balance doc stale (e.g. a $inc that landed
    # but a later mutation corrupted the cached balance).
    coll = CreditBalance.get_pymongo_collection()
    await coll.update_one({"workspace": WS}, {"$set": {"balance_credits": 999999}})
    assert await credits.balance(WS) == 999999  # corrupted

    repaired = await credits.reconcile(WS)
    assert repaired == 700
    assert await credits.balance(WS) == 700


async def test_reconcile_recreates_lost_balance_row(mongo_db):
    await credits.grant(WS, 800, cause="top_up", idempotency_key="g1")
    # Simulate the crash window: a committed ledger entry whose balance row was
    # lost entirely.
    coll = CreditBalance.get_pymongo_collection()
    await coll.delete_one({"workspace": WS})
    assert await credits.balance(WS) == 0  # row gone → reads as 0

    repaired = await credits.reconcile(WS)
    assert repaired == 800
    assert await credits.balance(WS) == 800


async def test_reconcile_is_noop_when_in_agreement(mongo_db):
    await credits.grant(WS, 600, cause="top_up", idempotency_key="g1")
    assert await credits.reconcile(WS) == 600
    assert await credits.balance(WS) == 600


# ---------------------------------------------------------------------------
# Extra coverage — tenant isolation, history paging, validation, emit.
# ---------------------------------------------------------------------------


async def test_balance_is_tenant_scoped(mongo_db):
    await credits.grant("ws_a", 100, cause="top_up", idempotency_key="a1")
    await credits.grant("ws_b", 250, cause="top_up", idempotency_key="b1")
    assert await credits.balance("ws_a") == 100
    assert await credits.balance("ws_b") == 250


async def test_history_newest_first_and_pages(mongo_db):
    for i in range(5):
        await credits.grant(WS, 100, cause="top_up", idempotency_key=f"g{i}")

    entries, next_cursor = await credits.history(WS, limit=2)
    assert len(entries) == 2
    assert next_cursor is not None
    # Newest first — last grant (g4) leads.
    assert entries[0].idempotency_key == "g4"
    assert entries[1].idempotency_key == "g3"

    page2, cursor2 = await credits.history(WS, limit=2, cursor=next_cursor)
    assert [e.idempotency_key for e in page2] == ["g2", "g1"]
    assert cursor2 is not None

    page3, cursor3 = await credits.history(WS, limit=2, cursor=cursor2)
    assert [e.idempotency_key for e in page3] == ["g0"]
    assert cursor3 is None  # last page


async def test_history_is_tenant_scoped(mongo_db):
    await credits.grant("ws_a", 100, cause="top_up", idempotency_key="a1")
    await credits.grant("ws_b", 100, cause="top_up", idempotency_key="b1")
    entries, _ = await credits.history("ws_a")
    assert len(entries) == 1
    assert entries[0].workspace_id == "ws_a"


async def test_grant_rejects_non_positive_amount(mongo_db):
    with pytest.raises(ValidationError):
        await credits.grant(WS, 0, cause="top_up", idempotency_key="z")
    with pytest.raises(ValidationError):
        await credits.grant(WS, -5, cause="top_up", idempotency_key="neg")


async def test_debit_rejects_non_positive_amount(mongo_db):
    await credits.grant(WS, 100, cause="top_up", idempotency_key="g1")
    with pytest.raises(ValidationError):
        await credits.debit(WS, 0, cause="compute_spend", idempotency_key="z")


async def test_grant_emits_credit_movement(mongo_db, recording_bus):
    await credits.grant(WS, 500, cause="top_up", idempotency_key="g1")
    movements = [e for e in recording_bus.events if e.type == "credits.movement"]
    assert len(movements) == 1
    data = movements[0].data
    assert data["workspace_id"] == WS
    assert data["kind"] == "grant"
    assert data["amount_delta"] == 500
    assert data["balance_after"] == 500
    assert data["cause"] == "top_up"
    assert data["idempotency_key"] == "g1"


async def test_duplicate_replay_does_not_re_emit(mongo_db, recording_bus):
    await credits.grant(WS, 500, cause="top_up", idempotency_key="g1")
    await credits.grant(WS, 500, cause="top_up", idempotency_key="g1")  # replay
    movements = [e for e in recording_bus.events if e.type == "credits.movement"]
    assert len(movements) == 1  # the no-op replay did not re-emit


async def test_genesis_kind_seeds_wallet(mongo_db):
    bal = await credits.grant(WS, 1000, cause="genesis", idempotency_key="gen", kind="genesis")
    assert bal == 1000
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == WS).to_list()
    assert entries[0].kind == "genesis"
