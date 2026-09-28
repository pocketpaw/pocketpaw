"""Backfill the concierge marker so CR-12 does not switch off every live bar.

Created 2026-09-28 (feat/concierge-manual-create, CR-12).

WHY THIS HAS TO SHIP IN THE SAME RELEASE AS THE MODEL. CR-12 makes a concierge
something an owner creates: ``Site.concierge_created_at`` is the marker, and
``site_keys.concierge_available`` now requires it. It also flips
``Site.concierge_enabled`` to default False. Rows written before CR-12 have no
marker, and many never stored ``concierge_enabled`` at all (it arrived in D1 with
a True default and no migration), so they read the NEW default. Without this
backfill, the moment the model deploys every bar answering visitors goes dark.

WHAT IT WRITES, per Site row that has no ``concierge_created_at`` field yet:

  * the site's bar is bound to a LIVE agent → it is an existing classic concierge:
    ``concierge_created_at = now``, ``concierge_enabled`` written explicitly as the
    value it effectively had (True unless the owner stored False), and
    ``concierge_runtime = "legacy"`` unless the row already names one. An owner who
    had switched a bound bar off keeps it off: the backfill exists so nothing a
    visitor sees changes, in either direction;
  * anything else → it has no concierge: ``concierge_created_at = null`` and
    ``concierge_enabled`` = the value it effectively had (the stored one, or the
    old True default), written explicitly so nothing hinges on a default again.

IDEMPOTENT, and one-shot per row. Rows are selected on the marker FIELD being
absent, and every row this touches gets the field (as a date or as null), as does
every row the CR-12 model inserts. So a second run matches nothing, and a later
agent bind on a site with no concierge is never mistaken for an old one — which
would be exactly the automatic creation CR-12 forbids. Each write repeats the
``$exists: false`` guard, so an owner's create racing this run wins.

AN EMPTY DATABASE PASSES. A first deploy has no ``sites`` collection; that is zero
rows to classify, not an error.

WHERE THE BARS ARE. Widgets live in the paw-bar SQLite store on the backend's data
volume (``stores.get_paw_bar_store``), not in Mongo. If that file does not exist
while there ARE rows to classify, the run refuses to classify them and writes
nothing: an unreachable store reads as "no bar is bound anywhere", and writing
that would permanently mark live concierges as none. Unwritten rows read the new
defaults (off, none) until a run that can see the store marks them.

HOW IT RUNS. At boot, from ``shared.db.init_cloud_db`` (``migrate_on_boot``), so
it is in force the moment the new model is, whatever the deploy config says. And
as a deploy step, the way ``credits.migrate_micro_credits`` runs, chained before
``pocketpaw serve`` in deploy/coolify/docker-compose.yaml:

    python -m pocketpaw_ee.sites.migrate_concierge_marker --dry-run
    python -m pocketpaw_ee.sites.migrate_concierge_marker

It lives in the package because the deployed image carries no ``scripts/``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UNMARKED: dict[str, Any] = {"concierge_created_at": {"$exists": False}}


@dataclass
class BackfillStats:
    """What one run did. ``skipped`` rows errored and are left for the next run."""

    examined: int = 0
    marked: int = 0
    unmarked: int = 0
    skipped: int = 0
    refused: bool = False


async def _has_live_agent(site: dict[str, Any], store: Any) -> bool:
    """Is this site's bar bound to an agent that still exists?

    Resolved exactly the way the owner surfaces resolve a site's bar
    (``agent_provisioning.site_widget``: workspace AND pocket, never an empty
    pocket). A dangling ``agent_id`` is not a live concierge: chat on it 409s
    today, so it is classified as none rather than resurrected.
    """
    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.cloud.agents import service as agents_service

    pocket_id = str(site.get("pocket_id") or "")
    workspace_id = str(site.get("workspace") or "")
    if not pocket_id or not workspace_id:
        return False
    widgets = await store.list_widgets(pocket_id=pocket_id, workspace_id=workspace_id, limit=1)
    agent_id = str(getattr(widgets[0], "agent_id", "") or "") if widgets else ""
    if not agent_id:
        return False
    try:
        await agents_service.get(agent_id)
    except NotFound:
        return False
    return True


def _store_db_path(store: Any) -> Path | None:
    raw = getattr(store, "_db_path", None)
    return Path(raw) if raw else None


async def backfill_concierge_marker(*, store: Any, dry_run: bool = False) -> BackfillStats:
    """Classify every unmarked Site row as an existing concierge or none.

    ``store`` is the paw-bar store to read bars from; production passes
    ``get_paw_bar_store()``. Requires Beanie to be initialised (Site, Agent).
    """
    from pocketpaw_ee.cloud.models.site import Site

    stats = BackfillStats()
    coll = Site.get_pymongo_collection()
    pending = await coll.count_documents(_UNMARKED)
    if pending == 0:
        return stats

    db_path = _store_db_path(store)
    if db_path is not None and not db_path.exists():
        logger.error(
            "concierge backfill: %d site(s) need classifying but the paw-bar store %s "
            "does not exist, so no bar can be seen. Writing nothing; those sites read "
            "as having no concierge until a run that can see the store.",
            pending,
            db_path,
        )
        stats.refused = True
        return stats

    now = datetime.now(UTC)
    rows = await coll.find(
        _UNMARKED,
        projection={
            "_id": 1,
            "workspace": 1,
            "pocket_id": 1,
            "concierge_enabled": 1,
            "concierge_runtime": 1,
        },
    ).to_list(None)
    for site in rows:
        stats.examined += 1
        try:
            live = await _has_live_agent(site, store)
        except Exception:  # noqa: BLE001 — one unreadable row must not stop the rest
            logger.warning(
                "concierge backfill: could not classify site %s; leaving it for the next run",
                site.get("_id"),
                exc_info=True,
            )
            stats.skipped += 1
            continue

        if live:
            update: dict[str, Any] = {
                "concierge_created_at": now,
                "concierge_enabled": bool(site.get("concierge_enabled", True)),
            }
            if site.get("concierge_runtime") not in ("legacy", "v2"):
                update["concierge_runtime"] = "legacy"
            stats.marked += 1
        else:
            update = {
                "concierge_created_at": None,
                # The old model default was True; a row that never stored the
                # switch was on, and it stays exactly what it was.
                "concierge_enabled": bool(site.get("concierge_enabled", True)),
            }
            stats.unmarked += 1
        if not dry_run:
            await coll.update_one({"_id": site["_id"], **_UNMARKED}, {"$set": update})
    return stats


async def migrate_on_boot() -> None:
    """Run the backfill at cloud startup. Best-effort: logs, never blocks boot.

    A failure here leaves unmarked rows reading the new defaults (off, none) for
    one boot. That is a real outage for those bars, so it logs at ERROR, but it
    is not a reason to hold chat, billing and every other surface down with it.
    """
    try:
        from pocketpaw_ee.api import get_paw_bar_store

        stats = await backfill_concierge_marker(store=get_paw_bar_store())
    except Exception:  # noqa: BLE001 — a migration hiccup must never block boot
        logger.error(
            "concierge backfill failed at boot; unmarked sites read as having no concierge. "
            "Run: python -m pocketpaw_ee.sites.migrate_concierge_marker",
            exc_info=True,
        )
        return
    if stats.examined:
        logger.info(
            "concierge backfill: %d site(s) examined, %d kept as live concierges, "
            "%d marked as none, %d skipped",
            stats.examined,
            stats.marked,
            stats.unmarked,
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

    from pocketpaw_ee.api import get_paw_bar_store
    from pocketpaw_ee.cloud.models.agent import Agent
    from pocketpaw_ee.cloud.models.site import Site

    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        await init_beanie(database=client[db_name], document_models=[Site, Agent])
        stats = await backfill_concierge_marker(store=get_paw_bar_store(), dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 — any driver error is the same answer here
        logger.error("concierge backfill failed: %s", exc)
        return 2
    finally:
        client.close()

    # Refused and skipped rows exit 0 on purpose. This step is chained before
    # ``pocketpaw serve``, so a non-zero exit holds the whole app down, and the
    # boot hook retries every start anyway. Both are logged at ERROR / WARNING.
    if stats.refused:
        return 0
    logger.info(
        "concierge backfill %s: %d examined, %d live concierge(s), %d none, %d skipped",
        "dry run" if args.dry_run else "complete",
        stats.examined,
        stats.marked,
        stats.unmarked,
        stats.skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
