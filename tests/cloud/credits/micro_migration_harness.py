# tests/cloud/credits/micro_migration_harness.py — runs the REAL micro-credit
# migration against a test database.
#
# WHY THIS EXISTS. ``test_micro_credit_migration.py`` used to carry its own copy of
# the conversion pipeline, marked "copied verbatim" from the script. That copy is the
# thing that makes a migration test worthless at the moment it matters: a fix applied
# to one side and not the other leaves the suite green while the operator tool stays
# wrong, and a migration gets exactly one attempt against real money. Loading the
# script itself means the tests can only ever be testing what an operator will run.
#
# The script is a standalone tool with a ``__main__`` and an argparse front door, so
# it is not importable by name from anywhere on the path. It is loaded from its file
# instead. Its argparse lives inside ``main()``, which nothing here calls, so nothing
# of the CLI enters the suite.
#
# Created 2026-09-04 (fix/wallet-migration-guard): new module, replacing the copied
# pipeline in test_micro_credit_migration.py.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "migrations" / "2026_09_04_micro_credits.py"
)


def _load() -> ModuleType:
    """Import the migration script from its path, once per session."""
    name = "_micro_credits_migration_under_test"
    if name in sys.modules:
        return sys.modules[name]

    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover — path is a constant
        raise RuntimeError(f"cannot load the migration script at {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migration = _load()


async def run_migration(db) -> dict[str, int]:
    """Convert every wallet document in ``db``. Returns per-old-field match counts.

    Drives the script's own ``_scale_and_rename`` — the same call ``main()`` makes,
    minus the Mongo client construction and the logging.
    """
    counts: dict[str, int] = {}
    for collection, old, new in migration._RENAMES:
        counts[old] = await migration._scale_and_rename(db, collection, old, new, dry_run=False)
    return counts


async def drop_obsolete_fields(db) -> dict[str, int]:
    """The script's second half: fields the finer unit made unnecessary."""
    counts: dict[str, int] = {}
    for collection, fields in migration._DROPS:
        counts[collection] = await migration._drop_fields(db, collection, fields, dry_run=False)
    return counts


async def verify(db) -> Any:
    """The script's own post-run check: no document still carries an old field."""
    return await migration._verify(db)
