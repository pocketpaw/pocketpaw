# tests/cloud/credits/test_unmigrated_wallet.py — what the cloud does when it boots
# on a database the micro-credit migration has not touched.
#
# THE INCIDENT THIS COMES FROM. #2064 renamed the three fields that carry money
# (``balance_credits`` -> ``balance_micro``, ``amount_delta`` ->
# ``amount_delta_micro``, ``balance_after`` -> ``balance_after_micro``) and shipped
# a migration to convert them. Nothing in the deploy runs it, and on 2026-09-04 it
# was not run. The container came up on
# the new code over old documents and served real customers that way.
#
# Only one of the four consequences was audible:
#
#   * the ledger read raised a pydantic ValidationError and returned a 500 — the
#     traceback that started the investigation, and the least harmful of the four;
#   * every balance read ZERO, because ``balance_micro`` carries a default of 0 and a
#     document that has never held the field parses cleanly as an empty wallet;
#   * the run-start gate and every strict debit read that zero and refused, so paying
#     customers were locked out with "insufficient credits";
#   * and any grant or metered debit that landed meanwhile ``$inc``-ed the NEW field
#     onto a document that still carried the old one, leaving both — a shape the
#     migration then silently overwrote, destroying the newer of the two.
#
# WHAT IS UNDER TEST. Two things, and they are deliberately in one module because
# neither is sufficient alone. ``verify_wallet_migrated`` is the guard that makes the
# boot fail instead of the wallet: it is what should have stopped the container. The
# migration's handling of a both-fields document is what makes today's database
# recoverable, since the guard arrives after the damage.
#
# The first two tests assert the BROKEN behaviour on purpose. They are the evidence
# for why a boot guard is the right shape of fix rather than a tolerant reader: the
# zero balance is unfixable at the read (nothing in the document says whether 0 means
# empty or unmigrated), so the only honest answer is to refuse to serve at all.
#
# Created 2026-09-04 (fix/wallet-migration-guard): new test module.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.credits import migrate_micro_credits as migration
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.credits.domain import credits_to_micro

from tests.cloud.credits.micro_migration_harness import run_migration

WS = "ws_unmigrated"
_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


async def _seed_pre_rename_balance(db, *, balance_credits: int, **extra) -> None:
    """A ``credit_balances`` row exactly as a deployed database still holds it.

    Written through the raw collection because the Beanie class no longer describes
    this shape — which is the whole problem.
    """
    await db["credit_balances"].insert_one(
        {
            "workspace": WS,
            "balance_credits": balance_credits,
            "createdAt": _NOW,
            "updatedAt": _NOW,
            **extra,
        }
    )


async def _seed_pre_rename_entry(db, *, delta: int, key: str = "e1") -> None:
    await db["credit_ledger"].insert_one(
        {
            "workspace": WS,
            "kind": "grant" if delta > 0 else "spend",
            "amount_delta": delta,
            "balance_after": delta,
            "applied": True,
            "conditional": False,
            "cause": "top_up" if delta > 0 else "compute_spend",
            "ref": {},
            "idempotency_key": key,
            "createdAt": _NOW,
            "updatedAt": _NOW,
        }
    )


# ---------------------------------------------------------------------------
# The symptoms. These assert the broken behaviour, and exist to justify the guard.
# ---------------------------------------------------------------------------


async def test_a_wallet_from_before_the_rename_reads_as_empty(mongo_db):
    """The silent half, and the reason the fix cannot live in the reader.

    A workspace holding 700 credits reads as 0, with no error anywhere. The default
    on ``balance_micro`` is doing exactly what a default is for, and it cannot tell
    an empty wallet from an unconverted one — nothing in the document distinguishes
    them. So no reader can be written that gets this right, and refusing to boot is
    the only answer that does not involve guessing about money.
    """
    await _seed_pre_rename_balance(mongo_db, balance_credits=700)

    assert await credits.balance(WS) == 0

    # And so the run-start gate locks a paying customer out of a run they can afford.
    with pytest.raises(Exception, match="credit"):
        await credits.check_balance(WS)


async def test_the_ledger_read_raises_on_a_row_from_before_the_rename(mongo_db):
    """The audible half: the 500 that surfaced the incident.

    ``amount_delta_micro`` is required with no default, so parsing an old row fails
    outright rather than reading wrong. Loud is better than silent here, and this is
    the only one of the four consequences that announced itself.
    """
    await _seed_pre_rename_entry(mongo_db, delta=1000)

    with pytest.raises(Exception) as excinfo:
        await credits.history(WS)
    assert "amount_delta_micro" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The guard: refuse the boot rather than serve a wallet we cannot read.
# ---------------------------------------------------------------------------


async def test_boot_refuses_on_a_balance_row_from_before_the_rename(mongo_db):
    """What should have stopped the container that produced the traceback."""
    await _seed_pre_rename_balance(mongo_db, balance_credits=700)

    with pytest.raises(RuntimeError) as excinfo:
        await credits.verify_wallet_migrated()

    message = str(excinfo.value)
    assert "balance_credits" in message
    # The message has to carry the way out, or the operator is left guessing with
    # the deployment down.
    assert "migrate_micro_credits" in message


async def test_boot_refuses_on_a_ledger_row_from_before_the_rename(mongo_db):
    """The ledger is checked too — it is a whole collection the balance guard
    cannot speak for, and a database can be half-converted."""
    await _seed_pre_rename_entry(mongo_db, delta=1000)

    with pytest.raises(RuntimeError) as excinfo:
        await credits.verify_wallet_migrated()
    assert "amount_delta" in str(excinfo.value)


async def test_boot_proceeds_once_the_migration_has_run(mongo_db):
    """The guard must be silent on a converted database, or it is just an outage."""
    await _seed_pre_rename_balance(mongo_db, balance_credits=700)
    await _seed_pre_rename_entry(mongo_db, delta=700)

    await run_migration(mongo_db)

    await credits.verify_wallet_migrated()  # does not raise
    assert await credits.balance(WS) == 700


async def test_boot_proceeds_on_an_empty_database(mongo_db):
    """A fresh deployment has no wallet documents at all and must not be blocked."""
    await credits.verify_wallet_migrated()  # does not raise


# ---------------------------------------------------------------------------
# Recovering the database the guard arrived too late to protect.
# ---------------------------------------------------------------------------


async def test_the_migration_keeps_credits_the_running_app_already_applied(mongo_db):
    """The money-loss path, and the reason the migration needed changing too.

    While the app ran unmigrated, a grant's ``$inc`` created ``balance_micro`` beside
    the untouched ``balance_credits`` — Mongo creates a missing field at the increment
    value. The document now carries both: 700 credits from before the deploy, and a
    1000-credit top-up bought after it.

    Converting by ``$set``-ing the new field from the old one overwrites that top-up
    and the customer is silently 1000 credits poorer, with the ledger still carrying
    the entry that says they paid. The conversion has to ADD to what is already there.
    """
    await _seed_pre_rename_balance(
        mongo_db, balance_credits=700, balance_micro=credits_to_micro(1000)
    )

    await run_migration(mongo_db)

    assert await credits.balance(WS) == 1700


async def test_a_metered_overdraft_applied_while_unmigrated_survives(mongo_db):
    """Same shape, opposite sign. A metered debit is an unconditional ``$inc`` that
    upserts, so it too lands on the old document and leaves a NEGATIVE new field.
    Adding must carry the sign; clamping or ignoring it would hand back spend the
    customer really used."""
    await _seed_pre_rename_balance(
        mongo_db, balance_credits=700, balance_micro=-credits_to_micro(50)
    )

    await run_migration(mongo_db)

    assert await credits.balance(WS) == 650


async def test_the_conversion_leaves_no_document_carrying_both_fields(mongo_db):
    """The old field must be gone in the same operation that writes the new one.

    A document left holding both is the ambiguous state: the adding conversion above
    would run a second time on a re-run and double what it already counted. One
    ``update_many`` per collection makes each document's conversion atomic, so the
    half-done shape cannot exist for a re-run to find.
    """
    await _seed_pre_rename_balance(
        mongo_db, balance_credits=700, balance_micro=credits_to_micro(1000)
    )
    await _seed_pre_rename_entry(mongo_db, delta=700)

    await run_migration(mongo_db)

    balances = mongo_db["credit_balances"]
    ledger = mongo_db["credit_ledger"]
    assert await balances.count_documents({"balance_credits": {"$exists": True}}) == 0
    assert await ledger.count_documents({"amount_delta": {"$exists": True}}) == 0

    # And a re-run finds nothing to do, so the merged balance is not counted twice.
    await run_migration(mongo_db)
    assert await credits.balance(WS) == 1700


# ---------------------------------------------------------------------------
# Pointing the migration at the right database. Getting this wrong is the one
# failure that looks like success.
# ---------------------------------------------------------------------------


def test_the_migration_reads_the_uri_the_deployment_actually_sets():
    """``CLOUD_MONGODB_URI`` is what docker-compose sets and what init_cloud_db
    reads. The first version of this tool demanded POCKETPAW_MONGO_URL, which
    nothing sets, so the operator following the runbook got "not set" and stopped."""
    uri, db_name = migration.resolve_mongo_target(
        {"CLOUD_MONGODB_URI": "mongodb://mongo:27017/paw-enterprise"}
    )

    assert uri == "mongodb://mongo:27017/paw-enterprise"
    assert db_name == "paw-enterprise"


def test_the_database_name_comes_off_the_uri_not_a_default():
    """The old default was ``pocketpaw``. The deployment's database is
    ``paw-enterprise``, so that default pointed at a database that does not exist —
    where every count is legitimately zero and the run reports a clean success."""
    _uri, db_name = migration.resolve_mongo_target(
        {"CLOUD_MONGODB_URI": "mongodb://user:pw@host:27017/paw-enterprise?replicaSet=rs0"}
    )
    assert db_name == "paw-enterprise"


def test_an_explicit_database_name_still_wins():
    _uri, db_name = migration.resolve_mongo_target(
        {"CLOUD_MONGODB_URI": "mongodb://mongo:27017/paw-enterprise", "POCKETPAW_MONGO_DB": "other"}
    )
    assert db_name == "other"


def test_the_legacy_variable_still_works():
    uri, db_name = migration.resolve_mongo_target(
        {"POCKETPAW_MONGO_URL": "mongodb://localhost:27017/pocketpaw"}
    )
    assert uri == "mongodb://localhost:27017/pocketpaw"
    assert db_name == "pocketpaw"


def test_no_uri_at_all_is_an_error_not_a_guess():
    with pytest.raises(RuntimeError, match="CLOUD_MONGODB_URI"):
        migration.resolve_mongo_target({})


async def test_a_database_with_no_wallet_is_refused():
    """The most dangerous thing this tool can print is "0 documents would convert"
    against the wrong database, because it is identical to the output of a database
    that is already converted. Absence of the collections is the tell.

    A bare client, NOT the ``mongo_db`` fixture: that fixture runs ``init_beanie``,
    which creates every collection while building indexes, so it cannot represent a
    database the app has never touched — which is exactly the wrong-name case.
    """
    from mongomock_motor import AsyncMongoMockClient

    db = AsyncMongoMockClient()["pocketpaw"]
    await db["something_else"].insert_one({"x": 1})

    with pytest.raises(RuntimeError, match="credit_balances"):
        await migration._assert_wallet_database(db)


async def test_a_real_wallet_database_passes_the_check(mongo_db):
    await _seed_pre_rename_balance(mongo_db, balance_credits=700)
    await _seed_pre_rename_entry(mongo_db, delta=700)

    await migration._assert_wallet_database(mongo_db)  # does not raise
