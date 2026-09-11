#!/usr/bin/env python
"""Import the legacy Studio generation history into Mongo.

WHAT THIS DOES. Reads ``~/.pocketpaw/studio/generations.jsonl`` — the
deployment-wide append-only file the history used to live in — and upserts each
record into the ``studio_generations`` collection, keyed ``(workspace,
generation_id)``.

WHY IT IS NEEDED AT ALL. The file sits on the ``backend-data`` named volume, so
it survives redeploys and holds real galleries. Shipping the Mongo store without
this would silently empty every workspace's /studio page.

UNTAGGED RECORDS ARE SKIPPED, AND THAT IS THE POINT. A record with no
``_workspace`` cannot be attributed to anyone, and the old reader's
``or _workspace is None`` clause is exactly why: it showed those rows to EVERY
tenant. Importing them would mean choosing an owner, and any choice is wrong —
so they are counted, reported, and left behind. Their disappearance from the
gallery IS the leak closing.

IDEMPOTENT. A record already present under its ``(workspace, generation_id)`` is
counted as ``already`` and not rewritten, so a re-run converges and a resumed run
after an interruption cannot duplicate a tile.

SAFE ON AN EMPTY OR MISSING FILE — it reports zero and exits 0. A fresh install
has no file, and a migration that failed there would deadlock the first deploy.
It also does NOT delete the file: leaving it makes the run repeatable and keeps a
copy while the change is still young.

IT RUNS ITSELF. ``init_cloud_db`` calls ``migrate_on_boot`` on every cloud start,
beside the workspace-VM map import, because the alternative was a one-off command
a human had to remember in an environment with no shell — and forgetting it left
every existing gallery silently EMPTY. The CLI remains for a dry run or a manual
re-run:

    python -m pocketpaw_ee.cloud.studio.migrate_generations_jsonl [--dry-run]

CONFIGURATION comes from ``CLOUD_MONGODB_URI``, the same variable
``init_cloud_db`` reads, so the migration and the app cannot point at different
databases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from pocketpaw.config import get_config_dir
from pocketpaw_ee.cloud.studio import schemas, service

logger = logging.getLogger(__name__)

#: Env vars carrying the Mongo URI, most authoritative first — the same order
#: ``credits.migrate_micro_credits`` uses, for the same reason.
_URI_VARS: tuple[str, ...] = ("CLOUD_MONGODB_URI", "POCKETPAW_MONGO_URL")

#: Set by the CLI before it calls ``init_cloud_db``. Without it ``--dry-run``
#: would be a lie: init_cloud_db now runs ``migrate_on_boot``, so the real import
#: would already have happened before the dry run reported what it 'would' do.
_suppress_boot_import = False


def legacy_history_path() -> Path:
    """Where the JSONL history lived before this migration."""
    return get_config_dir() / "studio" / "generations.jsonl"


@dataclass(frozen=True)
class Result:
    """Counts only — no record content, which may carry a user's prompts."""

    imported: int = 0
    skipped_untagged: int = 0
    skipped_unreadable: int = 0
    already: int = 0


async def migrate_file(path: Path, *, dry_run: bool = False) -> Result:
    """Import one JSONL file. Never raises on a bad line — a corrupt record is
    counted and stepped over, exactly as the old reader did."""
    if not path.exists():
        logger.info("no legacy history at %s — nothing to import", path)
        return Result()

    imported = untagged = unreadable = already = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            unreadable += 1
            continue

        workspace_id = record.pop("_workspace", None)
        if not workspace_id:
            untagged += 1
            continue

        try:
            generation = schemas.Generation.model_validate(record)
        except Exception:  # noqa: BLE001 — a malformed row must not stop the run
            unreadable += 1
            continue

        if await service.get_generation(workspace_id, generation.id) is not None:
            already += 1
            continue

        if not dry_run:
            await service.record_generation(workspace_id, generation)
        imported += 1

    return Result(
        imported=imported,
        skipped_untagged=untagged,
        skipped_unreadable=unreadable,
        already=already,
    )


async def migrate_on_boot() -> None:
    """Import the legacy history at cloud startup. Best-effort, never blocks boot.

    WHY THIS RUNS AUTOMATICALLY. Without it the import is a one-off command a human
    has to remember, in an environment the team's own notes describe as having no
    shell — and the failure mode is silent: every existing /studio gallery renders
    EMPTY, nothing errors, the tiles are simply gone. Same shape and same reasoning
    as ``migrate_workspace_vm_map_to_db`` beside it in ``init_cloud_db``.

    SAFE TO RUN FROM BOTH CONTAINERS. ``backend`` and ``worker`` boot independently
    and share the volume, which would once have been a race: ``migrate_file`` does
    find-then-insert per record with no lock. The unique index on
    ``(workspace, generation_id)`` closes it — a concurrent double-insert raises
    ``DuplicateKeyError`` and ``record_generation`` applies the row instead.

    CHEAP WHEN THERE IS NOTHING TO DO. A fresh install has no file and this returns
    immediately; a converged deployment re-reads the file and writes nothing.
    """
    if _suppress_boot_import:
        return

    path = legacy_history_path()
    if not path.exists():
        return

    try:
        result = await migrate_file(path)
    except Exception:  # noqa: BLE001 — a migration hiccup must never block boot
        logger.warning("studio: legacy history import failed; run it by hand", exc_info=True)
        return

    if result.imported or result.skipped_untagged or result.skipped_unreadable:
        logger.info(
            "studio: legacy history import — %d imported, %d already present, "
            "%d skipped (untagged), %d skipped (unreadable)",
            result.imported,
            result.already,
            result.skipped_untagged,
            result.skipped_unreadable,
        )


def resolve_mongo_uri(env: dict[str, str] | None = None) -> str:
    """The deployed stack sets ``CLOUD_MONGODB_URI``; fall back to the same
    default ``init_cloud_db`` carries so a local run works with no env at all."""
    source = env if env is not None else dict(os.environ)
    for var in _URI_VARS:
        value = (source.get(var) or "").strip()
        if value:
            return value
    return "mongodb://localhost:27017/paw-enterprise"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Import the legacy studio history into Mongo.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be imported without writing anything",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from pocketpaw_ee.cloud.shared.db import init_cloud_db

    global _suppress_boot_import
    _suppress_boot_import = True  # this command IS the import; see the flag's note
    await init_cloud_db(resolve_mongo_uri())

    path = legacy_history_path()
    result = await migrate_file(path, dry_run=args.dry_run)

    logger.info(
        "%s: %d imported, %d already present, %d skipped (untagged), %d skipped (unreadable)",
        "dry run" if args.dry_run else "migration complete",
        result.imported,
        result.already,
        result.skipped_untagged,
        result.skipped_unreadable,
    )
    if result.skipped_untagged:
        logger.info(
            "the %d untagged record(s) carry no workspace and were left behind on purpose — "
            "they were readable by every tenant under the old reader",
            result.skipped_untagged,
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
