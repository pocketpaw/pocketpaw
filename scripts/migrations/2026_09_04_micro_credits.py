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
interruption — a partial run leaves each document either fully converted or fully
untouched, never half.

ORDER MATTERS. Run this while the API and worker are STOPPED. A process on the
old code writing ``balance_credits`` mid-migration creates a document with both
fields, and the new code would then read a zero balance beside a real one. There
is no lock here that can prevent that; stopping the writers is the control.

USAGE:
    python scripts/migrations/2026_09_04_micro_credits.py --dry-run
    python scripts/migrations/2026_09_04_micro_credits.py

Reads the same ``POCKETPAW_MONGO_URL`` / ``POCKETPAW_MONGO_DB`` the app uses.

Created 2026-09-04 (feat/exact-credit-deduction).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

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
    """Multiply ``old`` by a million into ``new``, then remove ``old``.

    One ``update_many`` with an aggregation pipeline, so the read and the write are
    a single server-side operation per document. Doing it as read-then-write from
    here would leave a window where a crash strands a document with neither field.
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
            {"$set": {new: {"$toLong": {"$multiply": [f"${old}", MICRO_PER_CREDIT]}}}},
        ],
    )
    # The unset is a SECOND pass, not a stage in the pipeline above, because
    # pipeline ``$unset`` is unsupported by mongomock and the migration's tests run
    # on it. Splitting costs one extra pass over a small collection and buys the
    # tests. It is also safe to interrupt between the two: a document left with
    # both fields is picked up by a re-run, which re-scales into ``new`` from the
    # untouched ``old`` and gets the same answer.
    await coll.update_many({old: {"$exists": True}}, {"$unset": {old: ""}})
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


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()

    url = os.environ.get("POCKETPAW_MONGO_URL", "").strip()
    if not url:
        logger.error("POCKETPAW_MONGO_URL is not set")
        return 2
    db_name = os.environ.get("POCKETPAW_MONGO_DB", "").strip() or "pocketpaw"

    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(url)
    db = client[db_name]

    mode = "DRY RUN — nothing will be written" if args.dry_run else "WRITING"
    logger.info("micro-credit migration on %s/%s — %s", url.split("@")[-1], db_name, mode)

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
