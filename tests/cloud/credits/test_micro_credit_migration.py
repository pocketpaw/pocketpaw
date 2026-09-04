# tests/cloud/credits/test_micro_credit_migration.py — proves the wallet survives
# the move from whole credits to micro-credits.
#
# THE MIGRATION. ``pocketpaw_ee.cloud.credits.migrate_micro_credits`` multiplies every
# stored amount by 1_000_000 and renames the three fields that carry one, so the
# ledger can express what a single API call costs. A credit is a cent; the proxy
# prices one call; a $0.0015 call is 0.375 of a credit and had no honest integer
# representation until the unit got finer.
#
# WHAT THESE TESTS ARE FOR. A migration that runs once against real money gets no
# second attempt, and its failure modes are quiet ones:
#
#   * a document converted twice (a re-run, or a resumed interrupted run) would be
#     a million times too large and nothing would flag it;
#   * a value stored as a double instead of a long would break the ledger's
#     exact-sum invariant at the point where floats stop being exact;
#   * a negative balance — legitimate, from metered overage — must scale like any
#     other, not get clamped;
#   * and the ledger invariant ``balance == sum(amount_delta)`` has to still hold
#     afterwards, because that is the only thing that says the wallet is intact.
#
# These drive the REAL migration module, via ``micro_migration_harness``. They used
# to run a copy of its pipeline, pasted in because the migration was a loose script
# under ``scripts/`` with no importable name. That copy was the failure this module
# exists to prevent: on 2026-09-04 the conversion turned out to destroy live writes,
# and a fix applied to the script alone would have left all seven tests here green
# against code no operator runs.
#
# Created 2026-09-04 (feat/exact-credit-deduction): new test module.
# Changed 2026-09-04 (fix/wallet-migration-guard): the copied pipeline is gone and
# the migration itself is under test. See tests/cloud/credits/test_unmigrated_wallet.py
# for the case that copy was hiding.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.credits.domain import credits_to_micro
from pocketpaw_ee.cloud.models.credit import CreditBalance, CreditLedgerEntry

from tests.cloud.credits.micro_migration_harness import run_migration as _run_migration

WS = "ws_micro_migration"
_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


async def _seed_old_shape(db, *, balance: int, movements: list[tuple[str, int]]) -> None:
    """Write documents in the PRE-migration shape, bypassing the models.

    The Beanie classes now describe the new field names, so the old shape has to be
    written through the raw collection — which is also exactly how it exists in a
    deployed database on the morning of the migration.
    """
    await db["credit_balances"].insert_one(
        {"workspace": WS, "balance_credits": balance, "createdAt": _NOW, "updatedAt": _NOW}
    )
    running = 0
    for idem, delta in movements:
        running += delta
        await db["credit_ledger"].insert_one(
            {
                "workspace": WS,
                "kind": "grant" if delta > 0 else "spend",
                "amount_delta": delta,
                "balance_after": running,
                "applied": True,
                "conditional": False,
                "cause": "top_up" if delta > 0 else "compute_spend",
                "ref": {},
                "idempotency_key": idem,
                "createdAt": _NOW,
                "updatedAt": _NOW,
            }
        )


async def test_a_migrated_wallet_reads_back_the_same_balance(mongo_db):
    """The headline guarantee. A customer holding 700 credits before the migration
    holds 700 credits after it — the number they see must not move."""
    await _seed_old_shape(mongo_db, balance=700, movements=[("g1", 1000), ("d1", -300)])

    await _run_migration(mongo_db)

    assert await credits.balance(WS) == 700
    assert await credits.balance_micro(WS) == credits_to_micro(700)


async def test_the_ledger_invariant_survives(mongo_db):
    """``balance == sum(amount_delta)`` is what says the wallet is intact, and
    ``reconcile`` is what checks it. If the migration scaled the balance and the
    entries by different factors — or missed one collection — this is where it
    shows, and it would otherwise show as a silent repair on the next reconcile."""
    await _seed_old_shape(
        mongo_db, balance=660, movements=[("g1", 1000), ("d1", -300), ("d2", -40)]
    )

    await _run_migration(mongo_db)

    assert await credits.reconcile(WS) == 660
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == WS).to_list()
    assert sum(e.amount_delta_micro for e in entries) == credits_to_micro(660)


async def test_running_it_twice_changes_nothing(mongo_db):
    """The failure that would be hardest to notice and worst to suffer: a re-run
    scaling an already-migrated wallet by another million. Selecting on the OLD
    field's existence is what prevents it, so a second pass must match zero."""
    await _seed_old_shape(mongo_db, balance=700, movements=[("g1", 1000), ("d1", -300)])

    first = await _run_migration(mongo_db)
    assert first["balance_credits"] == 1

    second = await _run_migration(mongo_db)
    assert second == {"balance_credits": 0, "amount_delta": 0, "balance_after": 0}
    assert await credits.balance(WS) == 700


async def test_a_negative_balance_scales_like_any_other(mongo_db):
    """A metered overage legitimately drives a wallet below zero, and the ledger
    preserves that rather than clamping it. The migration must not quietly repair
    a negative into a zero — it is a debt someone owes."""
    await _seed_old_shape(mongo_db, balance=-200, movements=[("g1", 100), ("d1", -300)])

    await _run_migration(mongo_db)

    assert await credits.balance(WS) == -200
    assert await credits.balance_micro(WS) == credits_to_micro(-200)


async def test_amounts_stay_integers(mongo_db):
    """Stored as a long, not a double. Mongo's ``$multiply`` yields a double for a
    double input, and a float ledger cannot hold an exact-sum invariant — that is
    the whole reason credits are integers. ``$toLong`` in the pipeline is what
    pins this, and nothing else in the system would notice if it were dropped."""
    await _seed_old_shape(mongo_db, balance=700, movements=[("g1", 1000), ("d1", -300)])

    await _run_migration(mongo_db)

    bal = await mongo_db["credit_balances"].find_one({"workspace": WS})
    assert isinstance(bal["balance_micro"], int)
    assert not isinstance(bal["balance_micro"], float)
    async for row in mongo_db["credit_ledger"].find({"workspace": WS}):
        assert isinstance(row["amount_delta_micro"], int)
        assert isinstance(row["balance_after_micro"], int)


async def test_the_old_field_names_are_gone(mongo_db):
    """The rename is the safety net, so it has to actually happen. A document left
    carrying ``balance_credits`` beside ``balance_micro`` would let stale code read
    a plausible-looking number that is a million times wrong, silently."""
    await _seed_old_shape(mongo_db, balance=700, movements=[("g1", 1000), ("d1", -300)])

    await _run_migration(mongo_db)

    assert (
        await mongo_db["credit_balances"].count_documents({"balance_credits": {"$exists": True}})
        == 0
    )
    assert await mongo_db["credit_ledger"].count_documents({"amount_delta": {"$exists": True}}) == 0

    bal_doc = await CreditBalance.find_one(CreditBalance.workspace == WS)
    assert bal_doc is not None
    with pytest.raises(AttributeError):
        _ = bal_doc.balance_credits


async def test_spending_continues_from_the_migrated_balance(mongo_db):
    """End to end. The wallet is migrated, then a real sub-credit proxy charge
    lands on it — the charge the old unit could not express at all. It has to be
    deducted exactly, from the balance the customer already had."""
    await _seed_old_shape(mongo_db, balance=700, movements=[("g1", 1000), ("d1", -300)])
    await _run_migration(mongo_db)

    # A $0.0015 call at the 2.5x markup: 0.375 credits, 375_000 micro.
    await credits.debit(
        WS, amount_micro=375_000, cause="litellm_spend", idempotency_key="litellm:req-1"
    )

    assert await credits.balance_micro(WS) == credits_to_micro(700) - 375_000
    # Still 699 whole credits to the customer, because they have spent under one.
    assert await credits.balance(WS) == 699
