# tests/test_money_migrations.py — the one-shot ISO 4217 minor-unit data migration
# (money_minor_units_v1) in paw_bar.db and agent_ledger.db, plus the cart
# currency guard and the ledger's case-insensitive currency grouping.
#
# Each migration test builds a populated DB the way an older build left it
# (amounts as major × 100 for every currency), opens the store twice, and checks
# the non-2-decimal rows converted exactly once while USD stayed untouched. The
# catalog then leaves the spec for the catalog table (catalog_to_table_v1, after
# the money migration), so converted catalog prices are read from the store.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pocketpaw.agent_ledger.models import LedgerRow
from pocketpaw.agent_ledger.store import SCHEMA_SQL as LEDGER_SCHEMA
from pocketpaw.agent_ledger.store import AgentLedgerStore
from pocketpaw.paw_bar.models import PawBarCartItem
from pocketpaw.paw_bar.store import SCHEMA_SQL as PAW_BAR_SCHEMA
from pocketpaw.paw_bar.store import CartCurrencyMismatch, PawBarStore

_CATALOG = [
    {"id": "mug", "name": "Mug", "price_cents": 1250, "currency": "USD"},
    {"id": "tea", "name": "Tea", "price_cents": 150000, "currency": "JPY"},
    {"id": "dates", "name": "Dates", "price_cents": 125, "currency": "kwd"},
]


def _spec(catalog: list[dict]) -> str:
    return json.dumps({"widget_id": "w1", "pocket_id": "p1", "blocks": [], "catalog": catalog})


def _legacy_paw_bar_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(PAW_BAR_SCHEMA)
    db.execute(
        "INSERT INTO paw_bar_widgets (id, pocket_id, owner, spec, access_token)"
        " VALUES ('w1', 'p1', 'o1', ?, 't')",
        (_spec(_CATALOG),),
    )
    db.execute(
        "INSERT INTO paw_bar_widgets (id, pocket_id, owner, spec, access_token)"
        " VALUES ('w2', 'p2', 'o1', 'not json', 't')"
    )
    db.execute(
        "INSERT INTO paw_bar_spec_revisions (widget_id, revision, spec) VALUES ('w1', 1, ?)",
        (_spec(_CATALOG[1:2]),),
    )
    db.execute(
        "INSERT INTO paw_bar_carts (widget_id, customer_ref, items, currency, updated_at)"
        " VALUES ('w1', 'c1', ?, 'JPY', '2026-09-01T00:00:00')",
        (json.dumps([{"id": "tea", "name": "Tea", "price_cents": 150000, "qty": 2}]),),
    )
    db.execute(
        "INSERT INTO paw_bar_carts (widget_id, customer_ref, items, currency, updated_at)"
        " VALUES ('w1', 'c2', ?, 'USD', '2026-09-01T00:00:00')",
        (json.dumps([{"id": "mug", "name": "Mug", "price_cents": 1250, "qty": 1}]),),
    )
    db.commit()
    db.close()


def _prices(spec_json: str) -> dict[str, int]:
    return {c["id"]: c["price_cents"] for c in json.loads(spec_json)["catalog"]}


async def test_paw_bar_migration_converts_once_and_leaves_usd(tmp_path: Path) -> None:
    path = tmp_path / "paw_bar.db"
    _legacy_paw_bar_db(path)

    for _ in range(2):  # a second process start must be a no-op
        store = PawBarStore(path)
        widget = await store.get_widget("w1")
        assert widget is not None

    assert widget.spec.catalog == []  # moved to the catalog table, already converted
    by_id = {c.id: c for c in await store.get_catalog_items("w1", ["mug", "tea", "dates"])}
    assert by_id["mug"].price_cents == 1250  # USD untouched
    assert by_id["tea"].price_cents == 1500  # ¥1,500
    assert by_id["dates"].price_cents == 1250  # 1.250 KWD
    assert by_id["dates"].currency == "KWD"

    db = sqlite3.connect(path)
    # Revision 1 is the one the older build archived; the catalog migration
    # archives the pre-migration spec after it.
    [(rev,)] = db.execute(
        "SELECT spec FROM paw_bar_spec_revisions WHERE widget_id = 'w1' AND revision = 1"
    ).fetchall()
    assert _prices(rev) == {"tea": 1500}
    [(bad,)] = db.execute("SELECT spec FROM paw_bar_widgets WHERE id = 'w2'").fetchall()
    assert bad == "not json"  # unparseable rows are left exactly as found
    markers = db.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
    db.close()
    assert markers == [("catalog_to_table_v1",), ("money_minor_units_v1",)]

    jpy_cart = await store.get_cart("w1", "c1")
    usd_cart = await store.get_cart("w1", "c2")
    assert jpy_cart is not None and usd_cart is not None
    assert jpy_cart.items[0].price_cents == 1500
    assert jpy_cart.total_cents == 3000
    assert usd_cart.items[0].price_cents == 1250


async def test_paw_bar_fresh_db_records_the_marker(tmp_path: Path) -> None:
    store = PawBarStore(tmp_path / "fresh.db")
    assert await store.get_widget("nope") is None
    db = sqlite3.connect(tmp_path / "fresh.db")
    assert db.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall() == [
        ("catalog_to_table_v1",),
        ("money_minor_units_v1",),
    ]
    db.close()


async def test_a_failed_migration_rolls_back_and_writes_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pocketpaw.paw_bar import store as store_mod

    path = tmp_path / "paw_bar.db"
    _legacy_paw_bar_db(path)
    real = store_mod._convert_lines
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # after the first widget row was already updated
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(store_mod, "_convert_lines", flaky)
    await PawBarStore(path).get_widget("w1")  # logged, not raised

    db = sqlite3.connect(path)
    [(spec,)] = db.execute("SELECT spec FROM paw_bar_widgets WHERE id = 'w1'").fetchall()
    assert _prices(spec)["tea"] == 150000  # rolled back, nothing half-written
    assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (0,)
    db.close()

    monkeypatch.setattr(store_mod, "_convert_lines", real)
    store = PawBarStore(path)
    [tea] = await store.get_catalog_items("w1", ["tea"])
    assert tea.price_cents == 1500


async def test_cart_refuses_a_second_currency(tmp_path: Path) -> None:
    store = PawBarStore(tmp_path / "carts.db")
    tea = PawBarCartItem(id="tea", name="Tea", price_cents=1500, currency="jpy")
    cart = await store.upsert_cart_item("w1", "c1", tea)
    assert cart.currency == "JPY"
    mug = PawBarCartItem(id="mug", name="Mug", price_cents=1250, currency="USD")
    with pytest.raises(CartCurrencyMismatch) as err:
        await store.upsert_cart_item("w1", "c1", mug)
    assert (err.value.cart_currency, err.value.item_currency) == ("JPY", "USD")
    assert err.value.code == "cart_currency_mismatch"
    after = await store.get_cart("w1", "c1")
    assert after is not None and [i.id for i in after.items] == ["tea"]
    # Same currency (any case) still merges.
    cart = await store.upsert_cart_item("w1", "c1", tea.model_copy(update={"currency": "JPY"}))
    assert cart.items[0].qty == 2 and cart.total_cents == 3000


async def test_an_empty_cart_takes_the_new_lines_currency(tmp_path: Path) -> None:
    store = PawBarStore(tmp_path / "carts.db")
    await store.get_cart("w1", "c1")  # create the schema
    raw = sqlite3.connect(tmp_path / "carts.db")
    raw.execute(
        "INSERT INTO paw_bar_carts (widget_id, customer_ref, items, currency, updated_at)"
        " VALUES ('w1', 'c1', '[]', 'USD', '2026-09-01T00:00:00')"
    )
    raw.commit()
    raw.close()
    cart = await store.upsert_cart_item(
        "w1", "c1", PawBarCartItem(id="tea", name="Tea", price_cents=1500, currency="JPY")
    )
    assert cart.currency == "JPY"


# --------------------------------------------------------------------------- #
# agent_ledger.db
# --------------------------------------------------------------------------- #


def _legacy_ledger_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(LEDGER_SCHEMA)
    rows = [
        # (ref, value_cents, currency, attrs)
        ("usd", 1250, "USD", {}),
        ("jpy", 300000, "JPY", {}),
        ("kwd", 125, "KWD", {}),
        ("none", None, None, {}),
        ("cart-jpy", None, None, {"paw.cart.value_cents": 150000, "paw.cart.currency": "JPY"}),
        ("cart-usd", None, None, {"paw.cart.value_cents": 1250, "paw.cart.currency": "USD"}),
    ]
    for ref, value, currency, attrs in rows:
        db.execute(
            "INSERT INTO agent_ledger_events (agent_id, workspace_id, surface, kind,"
            " value_cents, currency, ref, actor, attrs, ts)"
            " VALUES ('a1', 'ws', 'paw_bar', 'paw.visitor.action', ?, ?, ?, 'visitor', ?,"
            " '2026-09-01T00:00:00+00:00')",
            (value, currency, ref, json.dumps(attrs)),
        )
    db.commit()
    db.close()


async def test_ledger_migration_converts_once_and_leaves_usd(tmp_path: Path) -> None:
    path = tmp_path / "agent_ledger.db"
    _legacy_ledger_db(path)

    for _ in range(2):
        store = AgentLedgerStore(path)
        totals = await store.value_by_currency(workspace_id="ws")

    assert totals == {"USD": 1250, "JPY": 3000, "KWD": 1250}
    rows = {r.ref: r for r in await store.query(workspace_id="ws")}
    assert rows["cart-jpy"].attrs["paw.cart.value_cents"] == 1500
    assert rows["cart-usd"].attrs["paw.cart.value_cents"] == 1250
    assert rows["none"].value_cents is None

    db = sqlite3.connect(path)
    assert db.execute("SELECT name FROM schema_migrations").fetchall() == [
        ("money_minor_units_v1",)
    ]
    db.close()


async def test_value_by_currency_groups_case_insensitively(tmp_path: Path) -> None:
    store = AgentLedgerStore(tmp_path / "agent_ledger.db")
    for ref, cur in (("a", "usd"), ("b", "USD"), ("c", " Usd ")):
        await store.append(
            LedgerRow(
                workspace_id="ws",
                kind="paw.visitor.action",
                ref=ref,
                value_cents=100,
                currency=cur,
            )
        )
    # Bypass append's normalisation to cover rows written by older builds.
    raw = sqlite3.connect(tmp_path / "agent_ledger.db")
    raw.execute("UPDATE agent_ledger_events SET currency = 'usd' WHERE ref = 'a'")
    raw.commit()
    raw.close()
    assert await store.value_by_currency(workspace_id="ws") == {"USD": 300}
