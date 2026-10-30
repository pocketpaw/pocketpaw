"""Move stored Paw Bar appearances off the old defaults so the bar follows the site.

The bar now wears the host website's own look (accent, font, corners) unless the
owner overrides a facet, and ``ConciergeAppearance`` says "follow the site" with
``accent = ""``, ``font = "site"`` and ``radius = None``. Before that, the model's
defaults were ``#3b6fe0`` / ``"system"`` / ``20``, and every appearance an owner
ever saved stored them as literal values. Read under the new semantics they are
overrides, so those bars would keep the old blue instead of the site's colour.

WHAT IT WRITES, per Site row with no ``concierge_appearance_version``:

  * ``concierge_appearance.accent`` "#3b6fe0" (any case) -> ""
  * ``concierge_appearance.font`` "system" -> "site"
  * ``concierge_appearance.radius`` 20 -> null
  * ``concierge_appearance_version = 2`` on every row, changed or not.

Any other value is kept: the owner picked it. A row with no stored appearance
reads the new defaults already and only gets the version stamp.

IDEMPOTENT AND ONE-SHOT PER ROW. Rows are selected on the version field being
absent and each write repeats that guard, and every row the current model inserts
carries the field. So a second run matches nothing, and an owner who deliberately
picks #3b6fe0 after this ran keeps it.

DEPLOY ORDER. Only after the paw-bar app that detects the site theme is live: a
followed accent with no site theme falls to the bar's default, not the old blue.

HOW IT RUNS. At boot, from ``shared.db.init_cloud_db`` (``migrate_on_boot``),
beside ``sites.migrate_concierge_marker``. And by hand:

    python -m pocketpaw_ee.sites.migrate_appearance_follow_site --dry-run
    python -m pocketpaw_ee.sites.migrate_appearance_follow_site

It lives in the package because the deployed image carries no ``scripts/``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

APPEARANCE_VERSION = 2
_UNMIGRATED: dict[str, Any] = {"concierge_appearance_version": {"$exists": False}}

# The old model defaults, and what each now means "follow the site" as.
_OLD_ACCENT = "#3b6fe0"
_OLD_FONT = "system"
_OLD_RADIUS = 20


@dataclass
class MigrationStats:
    """What one run did. ``skipped`` rows errored and are left for the next run."""

    examined: int = 0
    changed: int = 0
    skipped: int = 0


def follow_site_update(look: Any) -> dict[str, Any]:
    """The ``$set`` that moves one stored appearance off the old defaults."""
    update: dict[str, Any] = {}
    if not isinstance(look, dict):
        return update
    accent = look.get("accent")
    if isinstance(accent, str) and accent.strip().lower() == _OLD_ACCENT:
        update["concierge_appearance.accent"] = ""
    if look.get("font") == _OLD_FONT:
        update["concierge_appearance.font"] = "site"
    if look.get("radius") == _OLD_RADIUS:
        update["concierge_appearance.radius"] = None
    return update


async def migrate_appearance_follow_site(*, dry_run: bool = False) -> MigrationStats:
    """Rewrite old-default appearance values on every unmigrated Site row.

    Requires Beanie to be initialised (Site).
    """
    from pocketpaw_ee.cloud.models.site import Site

    stats = MigrationStats()
    coll = Site.get_pymongo_collection()
    rows = await coll.find(_UNMIGRATED, projection={"_id": 1, "concierge_appearance": 1}).to_list(
        None
    )
    for row in rows:
        stats.examined += 1
        update = follow_site_update(row.get("concierge_appearance"))
        if update:
            stats.changed += 1
        if dry_run:
            continue
        update["concierge_appearance_version"] = APPEARANCE_VERSION
        try:
            await coll.update_one({"_id": row["_id"], **_UNMIGRATED}, {"$set": update})
        except Exception:  # noqa: BLE001 — one bad row must not stop the rest
            logger.warning(
                "appearance migration: could not update site %s; leaving it for the next run",
                row.get("_id"),
                exc_info=True,
            )
            stats.skipped += 1
    return stats


async def migrate_on_boot() -> None:
    """Run the migration at cloud startup. Best-effort: logs, never blocks boot.

    A failure leaves unmigrated rows on their stored literals (the old blue, the
    system font, 20px corners) for one boot: a look regression, not an outage.
    """
    try:
        stats = await migrate_appearance_follow_site()
    except Exception:  # noqa: BLE001 — a migration hiccup must never block boot
        logger.error(
            "appearance migration failed at boot; those bars keep the old default look. "
            "Run: python -m pocketpaw_ee.sites.migrate_appearance_follow_site",
            exc_info=True,
        )
        return
    if stats.examined:
        logger.info(
            "appearance migration: %d site(s) examined, %d moved to follow the site, %d skipped",
            stats.examined,
            stats.changed,
            stats.skipped,
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from pocketpaw_ee.cloud.credits.migrate_micro_credits import resolve_mongo_target

    try:
        uri, db_name = resolve_mongo_target(os.environ)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    from beanie import init_beanie
    from pymongo import AsyncMongoClient

    from pocketpaw_ee.cloud.models.site import Site

    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        await init_beanie(database=client[db_name], document_models=[Site])
        stats = await migrate_appearance_follow_site(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — any driver error is the same answer here
        logger.error("appearance migration failed: %s", exc)
        return 2
    finally:
        client.close()

    logger.info(
        "appearance migration %s: %d examined, %d moved to follow the site, %d skipped",
        "dry run" if args.dry_run else "complete",
        stats.examined,
        stats.changed,
        stats.skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
