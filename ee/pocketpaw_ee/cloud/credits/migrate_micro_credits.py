#!/usr/bin/env python
"""Convert the credit wallet from whole credits to micro-credits.

WHAT THIS DOES. Multiplies every stored amount by 1_000_000 and renames the three
fields that carry one:

    credit_balances.balance_credits   -> balance_micro        (x 1_000_000)
    credit_ledger.amount_delta        -> amount_delta_micro   (x 1_000_000)
    credit_ledger.balance_after       -> balance_after_micro  (x 1_000_000)

It also drops two fields from ``litellm_tenant_keys`` that the finer unit made
unnecessary: ``pending_spend_usd`` (the sub-credit remainder) and
``spend_ingest_lease_until`` (the lock that protected it).

WHY THE RENAME IS PART OF THE MIGRATION, not a separate tidy-up. If the values
were scaled while the names stayed, any code still reading ``balance_credits``
would find the field present and report a balance a million times too large. That
is a silent wrong answer in a money path. Renaming makes such a reader raise
AttributeError instead, at the one moment someone can act on it. The two halves
must land together or not at all.

IDEMPOTENT. Documents are selected by the OLD field's existence, so a re-run
matches nothing and reports zero. Safe to run twice, and safe to resume after an
interruption — each document's conversion is a single atomic update, so a partial
run leaves every document either fully converted or fully untouched, never half.

SAFE TO RUN AFTER THE APP HAS ALREADY BEEN UP on the new code, which is the
situation this landed in. A grant or metered debit served while the wallet was
unconverted ``$inc``-ed the NEW field into existence beside the old one; the
conversion ADDS to whatever it finds there instead of overwriting it, so those
movements survive. See ``_scale_and_rename`` for why adding is the only reading
that is correct in both cases.

THE WRITERS NO LONGER HAVE TO BE STOPPED, which matters because on Coolify the
compose stack is one application and stopping it takes Mongo down with it. Each
document's conversion is one atomic update and it adds to the destination, so an
``$inc`` arriving mid-run lands correctly whether it gets there before or after.
Nothing calls ``credits.service.reconcile`` on a schedule, and that is the one
routine that could rewrite a balance from a half-converted ledger.

A quiet window is still better if you have one, because reads served while the
wallet is unconverted are wrong: a balance reads as zero and customers are refused
runs they can pay for. ``credits.service.verify_wallet_migrated`` refuses the boot
for that reason, so a deploy that skips this fails loudly instead of serving empty
wallets.

USAGE. It lives in the installed package so it can be run wherever the app runs,
including a deployed container:

    python -m pocketpaw_ee.cloud.credits.migrate_micro_credits --dry-run
    python -m pocketpaw_ee.cloud.credits.migrate_micro_credits

WHY IT IS NOT IN ``scripts/``. That is where it started, and the deployed image
does not have it. The runtime stage of deploy/coolify/Dockerfile copies
``/opt/venv`` and ``/build/connectors`` from the builder and nothing else, so the
repository — ``scripts/`` included — exists only inside a build layer that is
thrown away. A data migration that cannot be run against the data is not a
migration, and on Coolify there is no checkout on the host to fall back to.

CONFIGURATION comes from ``CLOUD_MONGODB_URI``, which is what the deployed stack
sets and what ``init_cloud_db`` reads. The database name is parsed off the URI
path by the same expression ``init_cloud_db`` uses, so the two cannot point
somewhere different. ``POCKETPAW_MONGO_URL`` still works, and
``POCKETPAW_MONGO_DB`` still overrides the name.

Created 2026-09-04 (feat/exact-credit-deduction).
Changed 2026-09-04 (fix/wallet-migration-guard): three fixes, all found by trying
to actually run it against the deployment it was written for.

  * The conversion ADDS to the new field instead of overwriting it, and drops the
    old field in the same pipeline rather than a second pass. It shipped without
    being run, so live traffic had already written new-field values that the
    overwriting form destroyed.
  * It moved out of ``scripts/`` and into the package, because the deployed image
    does not carry ``scripts/``.
  * It reads ``CLOUD_MONGODB_URI``. It used to demand ``POCKETPAW_MONGO_URL``,
    which nothing sets, and default the database to ``pocketpaw``, which does not
    exist — so the reward for exporting the variable by hand was a clean zero
    against an empty database. It now refuses a database with no wallet
    collections rather than reporting success.

Proven in tests/cloud/credits/test_unmigrated_wallet.py. The migration tests load
THIS module rather than a copy of its pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Mapping

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("micro_credits_migration")

MICRO_PER_CREDIT = 1_000_000

# (collection, old field, new field). Order is irrelevant — each is independent.
_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("credit_balances", "balance_credits", "balance_micro"),
    ("credit_ledger", "amount_delta", "amount_delta_micro"),
    ("credit_ledger", "balance_after", "balance_after_micro"),
)

# Fields the micro unit made obsolete. Dropped, not renamed — see the module
# docstring. A document that never had them is unaffected.
_DROPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("litellm_tenant_keys", ("pending_spend_usd", "spend_ingest_lease_until")),
)


async def _scale_and_rename(db, collection: str, old: str, new: str, *, dry_run: bool) -> int:
    """Fold ``old`` x 1_000_000 into ``new``, removing ``old`` in the same write.

    ADDS to ``new`` rather than overwriting it, and that is the whole subtlety here.
    A balance document is mutated in place, so if the app was ever up on the new code
    before this ran, a grant or a metered debit will have ``$inc``-ed ``new`` into
    existence beside the untouched ``old`` — Mongo creates a missing field at the
    increment value. Overwriting would throw that away: a top-up the customer paid
    for, gone, with the ledger still holding the entry that says they paid. The
    document carries no record of which half is which, so adding is not a heuristic;
    it is the only reading that is right in both cases. ``$ifNull`` supplies the 0
    for the ordinary document that never had the new field.

    ONE ``update_many`` per collection, both stages in the same pipeline, so each
    document is either fully converted or fully untouched. That atomicity is what
    licenses the addition: a document holding both fields can only mean live writes,
    never a half-finished migration, so a re-run has nothing to double-count. Losing
    it would make the two indistinguishable and the addition unsafe.

    ``$project`` rather than ``$unset`` removes the old field. They are equivalent
    for a single exclusion, and mongomock implements only this one, so the migration
    tests drive this exact pipeline instead of a copy that behaves differently.
    """
    coll = db[collection]
    matched = await coll.count_documents({old: {"$exists": True}})
    if dry_run or matched == 0:
        return matched

    await coll.update_many(
        {old: {"$exists": True}},
        [
            # ``$toLong`` keeps this an integer type in Mongo. A stored double
            # would round at 2^53 and, worse, make the ledger's exact-sum
            # invariant a float comparison.
            {
                "$set": {
                    new: {
                        "$toLong": {
                            "$add": [
                                {"$multiply": [f"${old}", MICRO_PER_CREDIT]},
                                {"$ifNull": [f"${new}", 0]},
                            ]
                        }
                    }
                }
            },
            {"$project": {old: 0}},
        ],
    )
    return matched


async def _drop_fields(db, collection: str, fields: tuple[str, ...], *, dry_run: bool) -> int:
    coll = db[collection]
    query = {"$or": [{f: {"$exists": True}} for f in fields]}
    matched = await coll.count_documents(query)
    if dry_run or matched == 0:
        return matched
    await coll.update_many(query, {"$unset": {f: "" for f in fields}})
    return matched


async def _verify(db) -> bool:
    """Re-check that no document still carries an old field. Cheap; always runs."""
    ok = True
    for collection, old, _new in _RENAMES:
        left = await db[collection].count_documents({old: {"$exists": True}})
        if left:
            ok = False
            logger.error("verify: %s still has %d document(s) with %s", collection, left, old)
    return ok


#: Env vars carrying the Mongo URI, most authoritative first. ``CLOUD_MONGODB_URI``
#: is what the deployed stack actually sets (deploy/coolify/docker-compose.yaml)
#: and what ``CloudLifecycleHook`` hands ``init_cloud_db``. ``POCKETPAW_MONGO_URL``
#: is kept because this script asked for it first and someone may have exported it.
_URI_VARS: tuple[str, ...] = ("CLOUD_MONGODB_URI", "POCKETPAW_MONGO_URL")

#: Collections that must exist for this to be the right database. See
#: ``resolve_mongo_target`` for why an empty result is treated as an error.
_WALLET_COLLECTIONS: tuple[str, ...] = ("credit_balances", "credit_ledger")


def resolve_mongo_target(env: Mapping[str, str]) -> tuple[str, str]:
    """The (uri, database) this migration should run against.

    Reads the SAME configuration the app boots from, which the first version of
    this script did not: it wanted ``POCKETPAW_MONGO_URL`` and defaulted the
    database to ``pocketpaw``, while the deployed stack sets
    ``CLOUD_MONGODB_URI=mongodb://mongo:27017/paw-enterprise`` and nothing else. An
    operator following the runbook got "POCKETPAW_MONGO_URL is not set" and, on
    setting it by hand, a migration of an empty database called ``pocketpaw`` that
    reported a clean zero. A migration that cannot find the data must not be able
    to say it succeeded.

    The database name comes off the URI path, parsed exactly the way
    ``shared.db.init_cloud_db`` parses it, so the two cannot disagree.
    ``POCKETPAW_MONGO_DB`` still overrides for a deployment that separates them.

    Raises ``RuntimeError`` when no URI is set.
    """
    uri = ""
    for var in _URI_VARS:
        uri = (env.get(var) or "").strip()
        if uri:
            break
    if not uri:
        raise RuntimeError(f"none of {' / '.join(_URI_VARS)} is set — nothing to connect to")

    # Identical to init_cloud_db, deliberately: same string, same database.
    from_uri = uri.rsplit("/", 1)[-1].split("?")[0]
    db_name = (env.get("POCKETPAW_MONGO_DB") or "").strip() or from_uri or "paw-enterprise"
    return uri, db_name


def _host_of(uri: str) -> str:
    """The host:port of a Mongo URI, with the scheme, credentials and database
    stripped. Logged instead of the URI so a password never reaches a terminal an
    operator is about to paste into a ticket."""
    after_scheme = uri.split("://", 1)[-1]
    return after_scheme.split("@")[-1].split("/")[0] or uri


async def _assert_wallet_database(db) -> None:
    """Refuse a database that holds no wallet at all.

    A migration selects documents by the OLD field's existence, so pointing it at
    the wrong database produces "0 documents would convert" — which reads exactly
    like "already migrated" and is the most dangerous thing this tool can print.
    An empty result is only trustworthy if the collections are there to be empty.
    """
    names = set(await db.list_collection_names())
    missing = [c for c in _WALLET_COLLECTIONS if c not in names]
    if missing:
        raise RuntimeError(
            f"database {db.name!r} has no {', '.join(missing)} collection — this is "
            "not the wallet database, and a run against it would report zero "
            "documents and look like a success. Check CLOUD_MONGODB_URI. (On a "
            "deployment that has never booted this is also expected and harmless: "
            "Beanie creates these collections with its indexes, and there is "
            "nothing to convert until it has.)"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()

    try:
        url, db_name = resolve_mongo_target(os.environ)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    from motor.motor_asyncio import AsyncIOMotorClient

    # A short server-selection timeout: motor's default is 30 seconds, and this is
    # run by hand during an outage. Failing to reach Mongo should say so quickly.
    client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=5000)
    db = client[db_name]

    mode = "DRY RUN — nothing will be written" if args.dry_run else "WRITING"
    # Host WITHOUT credentials, and the database named separately — the URI already
    # carries the database, so joining them read "host/db/db" and looked like a
    # misconfiguration on the one line an operator checks before saying yes.
    logger.info(
        "micro-credit migration — host %s, database %s — %s",
        _host_of(url),
        db_name,
        mode,
    )

    try:
        await _assert_wallet_database(db)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001 — any driver error is the same answer here
        logger.error("cannot reach Mongo at %s: %s", _host_of(url), exc)
        return 2

    total = 0
    for collection, old, new in _RENAMES:
        n = await _scale_and_rename(db, collection, old, new, dry_run=args.dry_run)
        total += n
        logger.info(
            "%s: %d document(s) %s %s -> %s (x%d)",
            collection,
            n,
            "would convert" if args.dry_run else "converted",
            old,
            new,
            MICRO_PER_CREDIT,
        )

    for collection, fields in _DROPS:
        n = await _drop_fields(db, collection, fields, dry_run=args.dry_run)
        logger.info(
            "%s: %d document(s) %s obsolete field(s) %s",
            collection,
            n,
            "would drop" if args.dry_run else "dropped",
            ", ".join(fields),
        )

    if args.dry_run:
        logger.info("dry run complete — %d credit document(s) would be converted", total)
        return 0

    if not await _verify(db):
        logger.error("MIGRATION INCOMPLETE — old fields remain; do NOT start the app")
        return 1

    logger.info("migration complete and verified — %d credit document(s) converted", total)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
