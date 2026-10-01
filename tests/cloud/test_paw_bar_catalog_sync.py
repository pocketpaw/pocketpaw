# tests/cloud/test_paw_bar_catalog_sync.py — the background catalog sync that keeps
# a concierge's catalog in step with its site's products
# (``pocketpaw_ee.paw_bar.catalog_sync``).
#
# The importer is stubbed (its reading is covered against a mocked origin in
# tests/ee/sites/test_catalog_import.py) and the store is a real PawBarStore, so
# what is under test is the glue: finding the site's widget, deciding whether an
# import was complete enough to mark missing products sold out, skipping
# products with no currency, recording the outcome on the Site's own catalog_*
# fields, and never raising. The row rules themselves (owner edits win,
# tombstones, the cap) are in tests/test_paw_bar_catalog_store.py.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest_asyncio
from pocketpaw_ee.paw_bar import catalog_import, catalog_sync
from pocketpaw_ee.paw_bar.catalog_import import CatalogImportPreview, ImportedProduct

from pocketpaw.paw_bar.models import PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import PawBarStore
from tests.cloud.test_paw_bar_concierge_v2 import _site


def _product(i: int, **ov: Any) -> ImportedProduct:
    d: dict[str, Any] = {
        "id": f"web:{i}",
        "name": f"Mug {i}",
        "price_cents": 1200,
        "currency": "EUR",
        "url": f"/products/mug-{i}",
    }
    d.update(ov)
    return ImportedProduct(**d)


def _preview(*items: ImportedProduct, **ov: Any) -> CatalogImportPreview:
    d: dict[str, Any] = {
        "status": "ok",
        "source": "jsonld",
        "host": "brewco.com",
        "items": list(items),
        "total_found": len(items),
    }
    d.update(ov)
    return CatalogImportPreview(**d)


def _stub_import(monkeypatch, result: Any) -> list[Any]:
    seen: list[Any] = []

    async def _run(site, **_kw):
        seen.append(site)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(catalog_import, "preview_catalog_import", _run)
    return seen


@pytest_asyncio.fixture
async def store(tmp_path, mongo_db):
    s = PawBarStore(tmp_path / "catalog_sync.db")
    with patch("pocketpaw_ee.paw_bar.router._store", return_value=s):
        yield s


async def _wired(store: PawBarStore, **site_ov: Any):
    site = await _site(**site_ov)
    widget = await store.create_widget(
        PawBarWidget(
            pocket_id=site.pocket_id,
            owner="user:maya",
            workspace_id=site.workspace,
            spec=PawBarSpec(widget_id="w", pocket_id=site.pocket_id),
        )
    )
    return site, widget


async def _rows(store: PawBarStore, widget_id: str) -> dict:
    items, _ = await store.list_catalog(widget_id, limit=200)
    return {i.id: i for i in items}


async def test_a_sync_puts_the_sites_products_in_the_catalog_and_records_it(store, monkeypatch):
    site, widget = await _wired(store)
    _stub_import(monkeypatch, _preview(_product(1), _product(2)))

    summary = await catalog_sync.sync_site_catalog(site)

    rows = await _rows(store, widget.id)
    assert list(rows) == ["web:1", "web:2"]
    assert rows["web:1"].origin == "site"
    assert (summary.status, summary.added) == ("ok", 2)
    assert site.catalog_synced_at is not None
    assert site.catalog_sync_status == "ok"
    assert site.catalog_sync_counts["added"] == 2


async def test_a_failing_importer_leaves_the_catalog_and_the_knowledge_state_alone(
    store, monkeypatch
):
    site, widget = await _wired(store)
    await store.upsert_catalog_items(widget.id, [{"id": "p1", "name": "Mine", "price_cents": 5}])
    await site.set({"kb_sync_error": "crawl_partial"})
    _stub_import(monkeypatch, _preview(status="failed", reason="timeout"))

    summary = await catalog_sync.safe_sync_site_catalog(site)

    assert summary.status == "timeout"
    assert list(await _rows(store, widget.id)) == ["p1"]
    assert site.kb_sync_error == "crawl_partial"
    assert site.catalog_sync_status == "timeout"


async def test_an_importer_that_raises_is_swallowed(store, monkeypatch):
    site, widget = await _wired(store)
    _stub_import(monkeypatch, RuntimeError("boom"))

    summary = await catalog_sync.safe_sync_site_catalog(site)

    assert summary.status == "sync_failed"
    assert await _rows(store, widget.id) == {}


async def test_a_site_without_a_widget_is_not_imported(store, monkeypatch):
    site = await _site(pocket_id="pocket-without-widget")
    seen = _stub_import(monkeypatch, _preview(_product(1)))

    assert await catalog_sync.sync_site_catalog(site) is None
    assert seen == []
    assert site.catalog_synced_at is None


async def test_an_empty_import_changes_nothing(store, monkeypatch):
    site, widget = await _wired(store)
    _stub_import(monkeypatch, _preview(_product(1)))
    await catalog_sync.sync_site_catalog(site)
    _stub_import(monkeypatch, _preview(status="empty"))

    await catalog_sync.sync_site_catalog(site)

    assert (await _rows(store, widget.id))["web:1"].in_stock is None


async def test_only_a_complete_import_marks_missing_products_sold_out(store, monkeypatch):
    site, widget = await _wired(store)
    _stub_import(monkeypatch, _preview(_product(1), _product(2)))
    await catalog_sync.sync_site_catalog(site)

    # partial (a page failed), truncated at the cap, robots-skipped pages: not complete
    for incomplete in (
        _preview(_product(2), status="partial"),
        _preview(_product(2), total_found=5),
        _preview(_product(2), warnings=["skipped_by_robots:1"]),
    ):
        _stub_import(monkeypatch, incomplete)
        await catalog_sync.sync_site_catalog(site)
        assert (await _rows(store, widget.id))["web:1"].in_stock is None

    _stub_import(monkeypatch, _preview(_product(2)))
    await catalog_sync.sync_site_catalog(site)
    assert (await _rows(store, widget.id))["web:1"].in_stock is False


async def test_a_product_with_no_currency_is_left_for_the_owner(store, monkeypatch):
    site, widget = await _wired(store)
    _stub_import(monkeypatch, _preview(_product(1), _product(2, currency="")))

    summary = await catalog_sync.sync_site_catalog(site)

    assert list(await _rows(store, widget.id)) == ["web:1"]
    assert summary.skipped_no_currency == 1


async def test_the_detached_sync_runs_once_per_site_at_a_time(monkeypatch):
    ran: list[Any] = []
    gate = asyncio.Event()

    async def _sync(site):
        await gate.wait()
        ran.append(site)

    monkeypatch.setattr(catalog_sync, "safe_sync_site_catalog", _sync)
    site = SimpleNamespace(id="site-1")

    catalog_sync._detach_sync(site)
    catalog_sync._detach_sync(site)  # a second publish while the first runs
    gate.set()
    await asyncio.gather(*list(catalog_sync._TASKS))

    assert ran == [site]
    catalog_sync._detach_sync(site)  # and the next one runs again
    await asyncio.gather(*list(catalog_sync._TASKS))
    assert ran == [site, site]


def test_scheduling_never_raises(monkeypatch):
    def _boom(_site):
        raise RuntimeError("no loop")

    monkeypatch.setattr(catalog_sync, "_scheduler", _boom)
    catalog_sync.schedule_site_catalog_sync(SimpleNamespace(id="s"))
