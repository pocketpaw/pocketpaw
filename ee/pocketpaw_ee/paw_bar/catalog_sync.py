# ee/pocketpaw_ee/paw_bar/catalog_sync.py — keep a concierge's catalog in step with
# the products its site publishes, so the concierge can show them as cards.
#
# ``sites.kb_ingest`` schedules this after a knowledge sync that reached the site
# (hosted lanes once the pocket is read, the connected lane once the crawl reached
# its verified origin). It finds the site's widget the way the owner's catalog
# routes do (the first widget on ``Site.pocket_id`` in the site's workspace; no
# widget, nothing happens), runs the owner's own importer
# (``catalog_import.preview_catalog_import``: same hosts, robots, SSRF-safe fetch
# and wall clock) and hands the products to ``PawBarStore.sync_site_catalog``,
# whose rows rules are: new id added as a "site" row up to the cap, a "site" row
# updated, an "owner" row or an owner-deleted id never touched, nothing deleted.
#
# COMPLETE IMPORTS ONLY mark missing products sold out: status ``ok``, not cut at
# the cap (``total_found`` beyond the items), no product page skipped by robots,
# and no product dropped here for having no currency. A product with no currency
# is left out (its price units are unknown); the owner can import it by hand,
# where the dashboard asks for the currency.
#
# Fire-and-forget and fail-soft: ``schedule_site_catalog_sync`` never raises or
# waits, one sync per site runs at a time in this process, and the outcome is
# written only to the Site's own ``catalog_*`` fields, never the knowledge ones.

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.paw_bar import catalog_import

logger = logging.getLogger(__name__)


@dataclass
class CatalogSyncSummary:
    """How one catalog sync went. ``status`` is the importer's ``ok`` / ``partial``
    / ``empty``, its failure reason, or ``sync_failed`` when the sync crashed."""

    status: str
    added: int = 0
    updated: int = 0
    sold_out: int = 0
    owner_kept: int = 0
    deleted_skipped: int = 0
    capped: int = 0
    skipped_no_currency: int = 0

    def counts(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items() if k != "status"}


def _store() -> Any:
    """The paw-bar store, looked up per call so a patched ``router._store`` holds."""
    from pocketpaw_ee.paw_bar import router

    return router._store()


async def _widget_for(site: Any) -> Any:
    pocket_id = getattr(site, "pocket_id", "") or ""
    if not pocket_id:  # never an unfiltered lookup (see router._resolve_site_and_widget)
        return None
    workspace_id = str(getattr(site, "workspace", "") or "") or None
    widgets = await _store().list_widgets(pocket_id=pocket_id, workspace_id=workspace_id, limit=1)
    return widgets[0] if widgets else None


def _complete(preview: Any, skipped_no_currency: int) -> bool:
    """Whether the import is a trustworthy answer to "what does the site sell"."""
    return (
        preview.status == "ok"
        and preview.total_found <= len(preview.items)
        and not skipped_no_currency
        and not any(w.startswith("skipped_by_robots") for w in preview.warnings)
    )


async def _record(site: Any, summary: CatalogSyncSummary) -> None:
    """Write the outcome to the Site's catalog_* fields via a targeted ``$set``
    (this runs detached, on a Site snapshot). Never raises."""
    try:
        await site.set(
            {
                "catalog_synced_at": datetime.now(UTC),
                "catalog_sync_status": summary.status,
                "catalog_sync_counts": summary.counts(),
            }
        )
    except Exception:  # noqa: BLE001 — bookkeeping must not fail the sync
        logger.warning(
            "paw_bar: could not record the catalog sync on site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


async def sync_site_catalog(site: Any) -> CatalogSyncSummary | None:
    """Import the site's products into its concierge's catalog. None when the
    site has no concierge widget (nothing read, nothing recorded)."""
    widget = await _widget_for(site)
    if widget is None:
        return None
    preview = await catalog_import.preview_catalog_import(site)
    if preview.status == "failed":
        summary = CatalogSyncSummary(status=preview.reason or "failed")
    else:
        items = [item for item in preview.items if item.currency]
        summary = CatalogSyncSummary(
            status=preview.status, skipped_no_currency=len(preview.items) - len(items)
        )
        if items:
            counts = await _store().sync_site_catalog(
                widget.id,
                items,
                complete=_complete(preview, summary.skipped_no_currency),
                workspace_id=getattr(widget, "workspace_id", "") or None,
            )
            if counts is None:  # the widget went away mid-sync
                return None
            for key, value in asdict(counts).items():
                if hasattr(summary, key):
                    setattr(summary, key, value)
    await _record(site, summary)
    logger.info(
        "paw_bar: catalog sync for site %s: %s %s",
        getattr(site, "id", "?"),
        summary.status,
        summary.counts(),
    )
    return summary


async def safe_sync_site_catalog(site: Any) -> CatalogSyncSummary | None:
    """``sync_site_catalog`` that never raises; a crash is recorded as ``sync_failed``."""
    try:
        return await sync_site_catalog(site)
    except Exception:  # noqa: BLE001 — a catalog sync is never a gate on anything
        logger.warning(
            "paw_bar: catalog sync failed for site %s", getattr(site, "id", "?"), exc_info=True
        )
        summary = CatalogSyncSummary(status="sync_failed")
        await _record(site, summary)
        return summary


# asyncio keeps only a weak ref to a bare task; hold each until it finishes.
_TASKS: set[asyncio.Task[Any]] = set()
# Sites with a sync running in this process: a second schedule meanwhile is dropped.
_IN_FLIGHT: set[str] = set()


def _detach_sync(site: Any) -> None:
    """Run ``safe_sync_site_catalog`` as a task on the running loop."""
    key = str(getattr(site, "id", "") or id(site))
    if key in _IN_FLIGHT:
        logger.info("paw_bar: catalog sync for site %s already running; skipped", key)
        return
    loop = asyncio.get_running_loop()

    async def _run() -> None:
        try:
            await safe_sync_site_catalog(site)
        finally:
            _IN_FLIGHT.discard(key)

    _IN_FLIGHT.add(key)
    task = loop.create_task(_run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


# The detach seam: tests swap it for a recorder (tests/conftest.py).
_scheduler = _detach_sync


def schedule_site_catalog_sync(site: Any) -> None:
    """Fire a background catalog sync for a site. Never blocks, never raises."""
    try:
        _scheduler(site)
    except Exception:  # noqa: BLE001 — no loop, or a broken scheduler: skip it
        logger.warning(
            "paw_bar: could not schedule a catalog sync for site %s",
            getattr(site, "id", "?"),
            exc_info=True,
        )


__all__ = [
    "CatalogSyncSummary",
    "safe_sync_site_catalog",
    "schedule_site_catalog_sync",
    "sync_site_catalog",
]
