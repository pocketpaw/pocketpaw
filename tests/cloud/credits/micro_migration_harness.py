# tests/cloud/credits/micro_migration_harness.py — runs the REAL micro-credit
# migration against a test database.
#
# WHY THIS EXISTS. ``test_micro_credit_migration.py`` used to carry its own copy of
# the conversion pipeline, marked "copied verbatim" from the script. That copy is
# the thing that makes a migration test worthless at the moment it matters: a fix
# applied to one side and not the other leaves the suite green while the operator
# tool stays wrong, and a migration gets exactly one attempt against real money.
# Driving the module itself means the tests can only ever be testing what an
# operator will run.
#
# Created 2026-09-04 (fix/wallet-migration-guard): new module, replacing the copied
# pipeline in test_micro_credit_migration.py. It loaded the script from its path by
# importlib until the migration moved into the package, where a plain import works
# — which is itself the point of the move.

from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud.credits import migrate_micro_credits as migration


async def run_migration(db) -> dict[str, int]:
    """Convert every wallet document in ``db``. Returns per-old-field match counts.

    Drives the module's own ``_scale_and_rename`` — the same call ``main()`` makes,
    minus the Mongo client construction and the logging.
    """
    counts: dict[str, int] = {}
    for collection, old, new in migration._RENAMES:
        counts[old] = await migration._scale_and_rename(db, collection, old, new, dry_run=False)
    return counts


async def drop_obsolete_fields(db) -> dict[str, int]:
    """The second half: fields the finer unit made unnecessary."""
    counts: dict[str, int] = {}
    for collection, fields in migration._DROPS:
        counts[collection] = await migration._drop_fields(db, collection, fields, dry_run=False)
    return counts


async def verify(db) -> Any:
    """The post-run check: no document still carries an old field."""
    return await migration._verify(db)
