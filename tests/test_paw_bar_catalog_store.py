# tests/test_paw_bar_catalog_store.py — the concierge catalog as rows in paw_bar.db
# (``pocketpaw.paw_bar.catalog_store`` mixed into ``PawBarStore``).
#
#   * CRUD: upsert keeps positions and appends, replace / delete / reorder, the
#     cap (``CatalogFull``, nothing written), tenancy (another workspace's widget
#     reads empty and writes nothing), and a widget delete removing its rows.
#   * Lookups: ids in the order asked, the page lookup (url-less items name no
#     page; an absolute url on another host does not count), search through FTS5
#     and through the LIKE fallback (the probe monkeypatched off).
#   * Spec back-compat: a spec write with a non-empty catalog replaces the rows and
#     stores the spec without it; an empty or absent catalog leaves the rows; a
#     rollback never restores a revision's catalog.
#   * The ``catalog_to_table_v1`` migration on a DB an older build left behind,
#     opened twice, after the money migration.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pocketpaw.paw_bar import catalog_store
from pocketpaw.paw_bar.catalog_store import CATALOG_MIGRATION, CatalogFull
from pocketpaw.paw_bar.models import PawBarCatalogItem, PawBarSpec, PawBarWidget
from pocketpaw.paw_bar.store import SCHEMA_SQL, PawBarStore

_WS = "ws-1"


def _item(i: int | str, **ov) -> dict:
    return {"id": f"p{i}", "name": f"Product {i}", "price_cents": 100, **ov}


def _spec(catalog: list | None = None) -> PawBarSpec:
    return PawBarSpec(widget_id="w", pocket_id="p", catalog=catalog or [])


async def _widget(store: PawBarStore, catalog: list | None = None, ws: str = _WS) -> PawBarWidget:
    return await store.create_widget(
        PawBarWidget(pocket_id="p", owner="o", workspace_id=ws, spec=_spec(catalog))
    )


@pytest.fixture
def store(tmp_path: Path) -> PawBarStore:
    return PawBarStore(tmp_path / "paw_bar.db")


@pytest.fixture
def no_fts(monkeypatch):
    async def _missing(_db) -> bool:
        return False

    monkeypatch.setattr(catalog_store, "fts5_available", _missing)


async def _ids(store: PawBarStore, widget_id: str) -> list[str]:
    items, _ = await store.list_catalog(widget_id, limit=200)
    return [i.id for i in items]


# --------------------------------------------------------------------------- #
# CRUD, cap, tenancy
# --------------------------------------------------------------------------- #


async def test_upsert_keeps_positions_and_appends_new_items(store):
    w = await _widget(store)
    assert await store.upsert_catalog_items(w.id, [_item(1), _item(2)]) == (2, 2)
    assert await store.upsert_catalog_items(w.id, [_item(3), _item(1, name="Renamed")]) == (2, 3)

    items, total = await store.list_catalog(w.id)
    assert total == 3
    assert [(i.id, i.name, i.position) for i in items] == [
        ("p1", "Renamed", 0),
        ("p2", "Product 2", 1),
        ("p3", "Product 3", 2),
    ]


async def test_rows_are_cleaned_like_spec_items_and_carry_a_source(store):
    w = await _widget(store)
    await store.upsert_catalog_items(
        w.id,
        [
            {
                "id": " shopify:9 ",
                "name": "x" * 500,
                "currency": "jpy",
                "image_url": "javascript:alert(1)",
                "url": "//evil.example/x",
                "in_stock": False,
            },
            {"id": "item-1", "name": "Manual", "source": "csv"},
        ],
    )
    a, b = await store.get_catalog_items(w.id, ["shopify:9", "item-1"])
    assert (a.id, len(a.name), a.currency, a.image_url, a.url) == ("shopify:9", 200, "JPY", "", "")
    assert (a.in_stock, a.source) == (False, "shopify")
    assert b.source == "csv"


async def test_a_duplicate_id_in_one_write_is_refused(store):
    w = await _widget(store)
    with pytest.raises(ValueError):
        await store.upsert_catalog_items(w.id, [_item(1), _item(1)])
    assert await store.catalog_count(w.id) == 0


async def test_the_cap_refuses_the_whole_write(store):
    w = await _widget(store)
    await store.upsert_catalog_items(w.id, [_item(1), _item(2)], max_items=3)
    with pytest.raises(CatalogFull) as exc:
        await store.upsert_catalog_items(w.id, [_item(3), _item(4)], max_items=3)
    assert exc.value.limit == 3
    assert await store.catalog_count(w.id) == 2
    # Updating existing ids never counts against the cap.
    assert await store.upsert_catalog_items(w.id, [_item(1), _item(2)], max_items=2) == (2, 2)
    with pytest.raises(CatalogFull):
        await store.replace_catalog(w.id, [_item(i) for i in range(4)], max_items=3)
    assert await _ids(store, w.id) == ["p1", "p2"]


async def test_the_cap_defaults_to_config(store, monkeypatch):
    monkeypatch.setattr(catalog_store, "catalog_max_items", lambda: 1)
    w = await _widget(store)
    with pytest.raises(CatalogFull):
        await store.upsert_catalog_items(w.id, [_item(1), _item(2)])


async def test_delete_reorder_and_replace(store):
    w = await _widget(store)
    await store.upsert_catalog_items(w.id, [_item(i) for i in range(1, 6)])

    assert await store.delete_catalog_items(w.id, ["p2", "nope"]) == (1, 4)
    assert await store.reorder_catalog(w.id, ["p5", "p3", "ghost"]) == 4
    assert await _ids(store, w.id) == ["p5", "p3", "p1", "p4"]

    assert await store.replace_catalog(w.id, [_item(9), _item(8)]) == 2
    assert await _ids(store, w.id) == ["p9", "p8"]


async def test_another_workspace_reads_nothing_and_writes_nothing(store):
    w = await _widget(store, [_item(1)])
    assert await store.catalog_count(w.id, workspace_id="ws-other") == 0
    assert await store.list_catalog(w.id, workspace_id="ws-other") == ([], 0)
    assert await store.get_catalog_items(w.id, ["p1"], workspace_id="ws-other") == []
    assert await store.upsert_catalog_items(w.id, [_item(2)], workspace_id="ws-other") is None
    assert await store.delete_catalog_items(w.id, ["p1"], workspace_id="ws-other") is None
    assert await store.reorder_catalog(w.id, ["p1"], workspace_id="ws-other") is None
    assert await store.replace_catalog(w.id, [], workspace_id="ws-other") is None
    assert await _ids(store, w.id) == ["p1"]
    # A write for a widget that does not exist writes nothing either.
    assert await store.upsert_catalog_items("missing", [_item(1)]) is None


async def test_deleting_a_widget_deletes_its_rows(store, tmp_path):
    w = await _widget(store, [_item(1), _item(2)])
    keep = await _widget(store, [_item(3)])
    assert await store.delete_widget(w.id, workspace_id=_WS)
    with sqlite3.connect(tmp_path / "paw_bar.db") as db:
        rows = db.execute("SELECT widget_id, item_id FROM paw_bar_catalog_items").fetchall()
    assert rows == [(keep.id, "p3")]


# --------------------------------------------------------------------------- #
# Lookups and search
# --------------------------------------------------------------------------- #


async def test_get_catalog_items_keeps_the_order_asked_and_drops_unknowns(store):
    w = await _widget(store, [_item(1), _item(2), _item(3)])
    found = await store.get_catalog_items(w.id, ["p3", "nope", "p1", "p3"])
    assert [i.id for i in found] == ["p3", "p1"]


async def test_the_page_lookup(store):
    w = await _widget(
        store,
        [
            _item("none"),  # no url: names no page, not the homepage
            _item("home", url="/"),
            _item("other-host", url="https://elsewhere.com/shop/latte"),
            _item("latte", url="/shop/latte/"),
            _item("abs", url="https://brewco.com/shop/espresso.html"),
        ],
    )
    hit = await store.catalog_item_for_page(w.id, "shop/latte", host="brewco.com")
    assert hit is not None and hit.id == "platte"
    abs_hit = await store.catalog_item_for_page(w.id, "shop/espresso", host="brewco.com")
    assert abs_hit is not None and abs_hit.id == "pabs"
    home = await store.catalog_item_for_page(w.id, "", host="brewco.com")
    assert home is not None and home.id == "phome"
    assert await store.catalog_item_for_page(w.id, "menu", host="brewco.com") is None


@pytest.mark.parametrize("fts", [True, False], ids=["fts5", "like"])
async def test_search_ranks_name_hits_and_admin_filter_needs_every_word(tmp_path, monkeypatch, fts):
    if not fts:

        async def _missing(_db) -> bool:
            return False

        monkeypatch.setattr(catalog_store, "fts5_available", _missing)
    store = PawBarStore(tmp_path / "search.db")
    w = await _widget(
        store,
        [
            _item("tea", name="Green tea", description="Loose leaves for your mug"),
            _item("mug", name="Stoneware mug", description="Holds 350 ml"),
            _item("kettle", name="Kettle", description="Gooseneck pour-over"),
        ],
    )
    assert store._catalog_fts is fts

    hits = await store.search_catalog(w.id, "do you have a mug?", k=5)
    assert [h.id for h in hits] == ["pmug", "ptea"]  # a name hit outranks a description hit
    assert await store.search_catalog(w.id, "the", k=5) == []  # stopwords only

    items, total = await store.list_catalog(w.id, q="mug stoneware")
    assert ([i.id for i in items], total) == (["pmug"], 1)
    items, total = await store.list_catalog(w.id, q="kett")
    assert ([i.id for i in items], total) == (["pkettle"], 1)
    # Search sees updates and deletes.
    await store.upsert_catalog_items(w.id, [_item("kettle", name="Teapot")])
    await store.delete_catalog_items(w.id, ["pmug"])
    assert [h.id for h in await store.search_catalog(w.id, "mug kettle teapot", k=5)] == [
        "pkettle",
        "ptea",
    ]


async def test_a_search_index_created_later_is_rebuilt_from_existing_rows(tmp_path, monkeypatch):
    path = tmp_path / "late.db"

    async def _missing(_db) -> bool:
        return False

    monkeypatch.setattr(catalog_store, "fts5_available", _missing)
    first = PawBarStore(path)
    w = await _widget(first, [_item("mug", name="Stoneware mug")])
    monkeypatch.undo()

    second = PawBarStore(path)
    assert [h.id for h in await second.search_catalog(w.id, "mug")] == ["pmug"]
    assert second._catalog_fts


# --------------------------------------------------------------------------- #
# Spec back-compat
# --------------------------------------------------------------------------- #


async def test_create_widget_moves_a_spec_catalog_into_rows(store):
    w = await _widget(store, [_item(1), _item(2)])
    assert w.spec.catalog == []
    stored = await store.get_widget(w.id)
    assert stored is not None and stored.spec.catalog == []
    assert await _ids(store, w.id) == ["p1", "p2"]


async def test_a_spec_write_with_a_catalog_replaces_it_and_without_one_leaves_it(store):
    w = await _widget(store, [_item(1), _item(2)])

    # No catalog key at all, and an explicit empty one: the rows stay.
    await store.update_spec(w.id, PawBarSpec.model_validate({"widget_id": w.id, "pocket_id": "p"}))
    await store.update_spec(w.id, _spec([]))
    assert await _ids(store, w.id) == ["p1", "p2"]

    # A non-empty catalog (an older editor's save) replaces the rows.
    updated = await store.update_spec(w.id, _spec([_item(7)]))
    assert updated is not None and updated.spec.catalog == []
    assert await _ids(store, w.id) == ["p7"]


async def test_rollback_never_restores_a_revisions_catalog(store, tmp_path):
    w = await _widget(store)
    # A revision archived by an older build still holds a catalog.
    with sqlite3.connect(tmp_path / "paw_bar.db") as db:
        db.execute(
            "INSERT INTO paw_bar_spec_revisions (widget_id, revision, spec) VALUES (?, 1, ?)",
            (w.id, json.dumps({"widget_id": w.id, "pocket_id": "p", "catalog": [_item("old")]})),
        )
    await store.upsert_catalog_items(w.id, [_item("live")])

    restored = await store.rollback_spec(w.id)
    assert restored is not None and restored.spec.catalog == []
    assert await _ids(store, w.id) == ["plive"]


# --------------------------------------------------------------------------- #
# catalog_to_table_v1
# --------------------------------------------------------------------------- #


def _legacy_db(path: Path, *, with_money_marker: bool = False) -> None:
    """paw_bar.db as a pre-catalog-table build left it: catalogs in the specs."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_SQL)
    big = [_item(i, url=f"/products/p{i}", in_stock=i % 2 == 0) for i in range(150)]
    rows = [
        ("w1", json.dumps({"widget_id": "w1", "pocket_id": "p1", "catalog": big})),
        (
            "w2",
            json.dumps(
                {
                    "widget_id": "w2",
                    "pocket_id": "p2",
                    "blocks": [{"type": "text", "content": "hi"}],
                    "catalog": [
                        {"id": "tea", "name": "Tea", "price_cents": 150000, "currency": "JPY"},
                        {"id": "tea", "name": "Dupe"},
                        {"id": "bad", "name": "Bad", "price_cents": -5},
                    ],
                }
            ),
        ),
        ("w3", json.dumps({"widget_id": "w3", "pocket_id": "p3"})),
        ("w4", "not json"),
    ]
    for widget_id, spec in rows:
        db.execute(
            "INSERT INTO paw_bar_widgets (id, pocket_id, owner, spec, access_token)"
            " VALUES (?, 'p', 'o', ?, 't')",
            (widget_id, spec),
        )
    if with_money_marker:
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)"
        )
        db.execute("INSERT INTO schema_migrations VALUES ('money_minor_units_v1', 'x')")
    db.commit()
    db.close()


def _raw(path: Path, sql: str, *args) -> list:
    with sqlite3.connect(path) as db:
        return db.execute(sql, args).fetchall()


async def test_the_migration_moves_catalogs_once_after_the_money_migration(tmp_path, caplog):
    path = tmp_path / "paw_bar.db"
    _legacy_db(path)
    caplog.set_level("INFO", logger="pocketpaw.paw_bar.catalog_store")

    for _ in range(2):  # a second process start is a no-op
        store = PawBarStore(path)
        assert await store.catalog_count("w1") == 150

    # The money migration ran first, so the JPY item arrived converted (¥1,500).
    [tea] = await store.get_catalog_items("w2", ["tea"])
    assert (tea.name, tea.price_cents, tea.currency) == ("Tea", 1500, "JPY")
    # The duplicate id and the item the model rejects were skipped.
    assert await store.catalog_count("w2") == 1

    items, _ = await store.list_catalog("w1", limit=200)
    assert [i.position for i in items] == list(range(150))
    assert items[3].in_stock is False and items[4].in_stock is True
    assert await store.catalog_item_for_page("w1", "products/p42") is not None

    for widget_id, spec in _raw(path, "SELECT id, spec FROM paw_bar_widgets WHERE id != 'w4'"):
        assert json.loads(spec).get("catalog", []) == [], widget_id
    w2 = await store.get_widget("w2")
    assert w2 is not None and w2.spec.blocks[0].content == "hi"
    assert _raw(path, "SELECT spec FROM paw_bar_widgets WHERE id = 'w4'") == [("not json",)]
    assert _raw(path, "SELECT name FROM schema_migrations ORDER BY name") == [
        (CATALOG_MIGRATION,),
        ("money_minor_units_v1",),
    ]
    assert _raw(path, "SELECT COUNT(*) FROM paw_bar_catalog_items") == [(151,)]
    assert any("largest spec now" in r.getMessage() for r in caplog.records)


async def test_the_migration_waits_for_the_money_migration(tmp_path, monkeypatch):
    path = tmp_path / "paw_bar.db"
    _legacy_db(path)
    from pocketpaw.paw_bar import store as store_module

    async def _broken(_db) -> None:
        raise RuntimeError("money migration failed")

    monkeypatch.setattr(store_module, "_migrate_money_minor_units", _broken)
    store = PawBarStore(path)
    assert await store.catalog_count("w1") == 0  # catalogs stay in their specs
    assert json.loads(_raw(path, "SELECT spec FROM paw_bar_widgets WHERE id='w1'")[0][0])["catalog"]

    monkeypatch.undo()
    assert await PawBarStore(path).catalog_count("w1") == 150


async def test_a_widget_that_fails_to_move_is_retried_on_the_next_start(tmp_path, monkeypatch):
    path = tmp_path / "paw_bar.db"
    _legacy_db(path, with_money_marker=True)
    real = catalog_store.replace_rows

    async def _flaky(db, widget_id, cleaned, now):
        if widget_id == "w2":
            raise RuntimeError("disk hiccup")
        return await real(db, widget_id, cleaned, now)

    monkeypatch.setattr(catalog_store, "replace_rows", _flaky)
    store = PawBarStore(path)
    assert await store.catalog_count("w1") == 150
    assert await store.catalog_count("w2") == 0
    assert _raw(path, "SELECT name FROM schema_migrations WHERE name = ?", CATALOG_MIGRATION) == []

    monkeypatch.undo()
    store = PawBarStore(path)
    assert await store.catalog_count("w2") == 1
    assert await store.catalog_count("w1") == 150  # not moved twice
    assert _raw(
        path, "SELECT COUNT(*) FROM schema_migrations WHERE name = ?", CATALOG_MIGRATION
    ) == [(1,)]


def test_catalog_rows_are_catalog_items():
    # Card hydration, the cart and the ledger read rows where they read items.
    row = catalog_store._row_to_item(("a", "A", 5, "USD", "", "", "", None, 0, "manual", ""))
    assert isinstance(row, PawBarCatalogItem)
