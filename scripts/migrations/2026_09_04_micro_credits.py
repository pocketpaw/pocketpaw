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

ORDER MATTERS. Run this while the API and worker are STOPPED. Not because a
concurrent write corrupts the arithmetic any more — it does not — but because
every read served in between is wrong: an unconverted balance reads as zero, so
customers are told they have no credits and are refused runs they can pay for.
``credits.service.verify_wallet_migrated`` now refuses the boot for exactly that
reason, so a deploy that skips this script fails loudly instead of serving empty
wallets.

USAGE:
    python scripts/migrations/2026_09_04_micro_credits.py --dry-run
    python scripts/migrations/2026_09_04_micro_credits.py

Reads the same ``POCKETPAW_MONGO_URL`` / ``POCKETPAW_MONGO_DB`` the app uses.

Created 2026-09-04 (feat/exact-credit-deduction).
Changed 2026-09-04 (fix/wallet-migration-guard): the conversion adds to the new
field instead of overwriting it, and drops the old field in the same pipeline
rather than a second pass. It was deployed without being run, so live traffic had
already written new-field values that the overwriting form destroyed — proven in
tests/cloud/credits/test_unmigrated_wallet.py. The tests now load THIS file rather
than a copy of its pipeline.
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
