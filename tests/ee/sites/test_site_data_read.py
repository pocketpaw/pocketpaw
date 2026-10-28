# tests/ee/sites/test_site_data_read.py — the read half of the Sites Data dashboard:
# ``sites.site_data.d1`` (introspection, kind inference, query builder) and
# ``sites.data_service`` / ``sites.data_router`` (inventory, table schema, rows page).
#
# D1 is a real in-memory SQLite (``d1_sqlite_fake.SqliteD1``), so quoting, PRAGMA,
# ``instr`` and the COUNT subquery run for real. The pocket read and the Site doc read
# are stubbed per test; no Mongo is needed.
#
# Covers TDD §11 "PR 1 (reads)": key choice (single pk, rowid, composite, WITHOUT
# ROWID), generated columns, FK references, the §5.2 kind table, hidden tables,
# unknown table → 404 before row SQL, identifier injection, search (instr, masked
# columns skipped, q cap), filters per op and kind, the filter and bound-parameter
# caps, the count cap, the dynamic lane (quoted identifier regression, spec-missing
# table), local mode / not published on every read route, the data token, foreign
# workspace → 404, member reads, and the route order next to the legacy data routes.
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError  # noqa: E402
from pocketpaw_ee.sites import data_service  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.site_data import budget, d1  # noqa: E402

from tests.ee.sites.d1_sqlite_fake import SqliteD1  # noqa: E402

_PATCH_GET = "pocketpaw_ee.cloud.pockets.service.get"
_PATCH_CONNECTORS = "pocketpaw_ee.integrations.bindings.list_for_pocket"
_DB = "11111111-2222-3333-4444-555555555555"

_PROJECT_WIRE = {
    "name": "Shop",
    "workspace": "ws1",
    "engine": "project",
    "siteMeta": {"project": {"template": "astro", "recipes": ["d1-drizzle", "better-auth"]}},
    "source": {"package.json": "{}"},
}

_SHOP_DDL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER REFERENCES customers(id),
  note TEXT,
  total REAL DEFAULT 0,
  is_paid INTEGER DEFAULT 0,
  created_at INTEGER DEFAULT (unixepoch()),
  total_x2 REAL GENERATED ALWAYS AS (total * 2) VIRTUAL
);
CREATE TABLE notes (body TEXT, tag TEXT);
CREATE TABLE pair (a TEXT, b TEXT, v TEXT, PRIMARY KEY (a, b));
CREATE TABLE kv_wr (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
CREATE TABLE pair_wr (a TEXT, b TEXT, PRIMARY KEY (a, b)) WITHOUT ROWID;
CREATE TABLE account (id TEXT PRIMARY KEY, user_id TEXT, password TEXT, access_token TEXT);
CREATE TABLE session (id TEXT PRIMARY KEY, token TEXT, user_id TEXT);
CREATE TABLE _paw_migrations (name TEXT PRIMARY KEY);
CREATE TABLE d1_migrations (id INTEGER PRIMARY KEY);
CREATE TABLE __drizzle_migrations (id INTEGER PRIMARY KEY);
INSERT INTO customers (id, name, email) VALUES (1, 'Ada', 'ada@x.io'), (2, 'Bob', 'bob@x.io');
INSERT INTO orders (customer_id, note, total, is_paid, created_at)
  VALUES (1, 'first order', 9.5, 1, 1767225600),
         (2, 'Second ORDER', 50, 0, 1767312000),
         (1, NULL, 12, 0, 1767398400);
INSERT INTO notes (body, tag) VALUES ('hello world', 'a'), ('bye', 'b');
INSERT INTO account VALUES ('a1', 'u1', 'scrypt$abc', 'tok-secret');
INSERT INTO session VALUES ('s1', 'sess-tok', 'u1');
"""


def _site_doc(d1_id: str = _DB) -> SimpleNamespace:
    return SimpleNamespace(
        d1_database_id=d1_id,
        kv_namespaces={"CACHE": "ns-1"},
        r2_buckets={"SITE_FILES": "paw-site-files"},
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    d1.clear_caches()
    budget.reset()
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_1")
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    yield
    d1.clear_caches()
    budget.reset()


@pytest.fixture
def site_doc(monkeypatch):
    doc = _site_doc()

    async def _doc(workspace_id, pocket_id):
        return doc

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _doc)
    return doc


@pytest.fixture
def shop() -> SqliteD1:
    return SqliteD1(_SHOP_DDL, database_id=_DB)


def _patch_pocket(wire=_PROJECT_WIRE):
    return patch(_PATCH_GET, new=AsyncMock(return_value=wire))


async def _schema(cf, table, wire=_PROJECT_WIRE):
    with _patch_pocket(wire):
        return await data_service.get_table_schema(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table=table, _cloudflare=cf
        )


async def _rows(cf, table, wire=_PROJECT_WIRE, **kw):
    with _patch_pocket(wire):
        return await data_service.get_rows(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table=table, _cloudflare=cf, **kw
        )


async def _inventory(cf, wire=_PROJECT_WIRE, **kw):
    with _patch_pocket(wire), patch(_PATCH_CONNECTORS, new=AsyncMock(return_value=[])):
        return await data_service.get_inventory(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", _cloudflare=cf, **kw
        )


def _col(schema, name):
    return next(c for c in schema.columns if c.name == name)


# --------------------------------------------------------------------------- schema


@pytest.mark.asyncio
async def test_single_primary_key_is_the_key(site_doc, shop):
    s = await _schema(shop, "orders")
    assert s.available is True
    assert s.key == "id" and s.editable is True and s.read_only_reason == ""
    pk = _col(s, "id")
    assert pk.primary_key is True and pk.auto is True and pk.writable is False
    assert _col(s, "note").writable is True
    assert s.row_count == 3


@pytest.mark.asyncio
async def test_no_primary_key_falls_back_to_rowid(site_doc, shop):
    s = await _schema(shop, "notes")
    assert s.key == "rowid" and s.editable is True
    assert [c.name for c in s.columns] == ["body", "tag"]  # rowid is never a column
    page = await _rows(shop, "notes")
    assert sorted(r["__key"] for r in page.rows) == [1, 2]


@pytest.mark.asyncio
async def test_composite_primary_key_is_read_only(site_doc, shop):
    s = await _schema(shop, "pair")
    assert s.key is None and s.editable is False and s.read_only_reason == "no_key"
    assert all(c.writable is False for c in s.columns)


@pytest.mark.asyncio
async def test_without_rowid_single_key_is_editable_composite_is_not(site_doc, shop):
    single = await _schema(shop, "kv_wr")
    assert single.key == "k" and single.editable is True
    assert _col(single, "k").auto is False  # a text key is not a rowid alias
    both = await _schema(shop, "pair_wr")
    assert both.key is None and both.read_only_reason == "no_key"
    page = await _rows(shop, "pair_wr")
    assert page.rows == []  # reads still work: __key is NULL, order by the pk columns


@pytest.mark.asyncio
async def test_generated_column_is_auto_and_foreign_key_is_referenced(site_doc, shop):
    s = await _schema(shop, "orders")
    gen = _col(s, "total_x2")
    assert gen.auto is True and gen.writable is False
    assert _col(s, "customer_id").references == "customers.id"
    assert _col(s, "note").references is None


@pytest.mark.asyncio
async def test_login_tables_mask_and_lock(site_doc, shop):
    s = await _schema(shop, "account")
    assert s.editable is False and s.read_only_reason == "login_table"
    assert _col(s, "password").masked is True and _col(s, "access_token").masked is True
    assert _col(s, "user_id").masked is False
    page = await _rows(shop, "account")
    assert page.rows[0]["password"] == "•••" and page.rows[0]["access_token"] == "•••"
    # "__key" matches the credential pattern ("key") but is the row id, not a secret.
    assert page.rows[0]["__key"] == "a1"


@pytest.mark.asyncio
async def test_a_masked_primary_key_never_becomes_the_row_key(site_doc):
    cf = SqliteD1(
        "CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT);"
        "INSERT INTO sessions VALUES ('sess-secret', 'u1');",
        database_id=_DB,
    )
    s = await _schema(cf, "sessions")
    assert s.key is None and s.editable is False
    page = await _rows(cf, "sessions")
    assert page.rows[0]["token"] == "•••"
    assert page.rows[0]["__key"] != "sess-secret"


@pytest.mark.asyncio
async def test_schema_is_cached_until_the_schema_changes(site_doc, shop):
    await _schema(shop, "orders")
    pragmas = len(shop.statements("PRAGMA table_xinfo"))
    await _schema(shop, "notes")
    assert len(shop.statements("PRAGMA table_xinfo")) == pragmas  # cache hit
    shop.execute("ALTER TABLE notes ADD COLUMN extra TEXT")
    s = await _schema(shop, "notes")
    assert len(shop.statements("PRAGMA table_xinfo")) > pragmas  # migration invalidated
    assert "extra" in [c.name for c in s.columns]


# --------------------------------------------------------------------------- kinds


@pytest.mark.parametrize(
    ("name", "sql_type", "default", "samples", "spec", "kind"),
    [
        ("anything", "", None, [], "string", "text"),
        ("anything", "", None, [], "number", "real"),
        ("anything", "", None, [], "int", "integer"),
        ("anything", "", None, [], "bool", "boolean"),
        ("anything", "", None, [], "date", "date"),
        ("anything", "", None, [], "datetime", "datetime"),
        ("anything", "", None, [], "json", "json"),
        ("is_admin", "integer", None, [], None, "boolean"),
        ("has_paid", "integer", None, [], None, "boolean"),
        ("email_enabled", "integer", None, [], None, "boolean"),
        ("active", "integer", None, [], None, "boolean"),
        ("paid", "integer", None, [], None, "boolean"),
        ("flag", "integer", "false", [], None, "boolean"),
        ("flag", "integer", "0", [0, 1, 1], None, "boolean"),
        ("quantity", "integer", "0", [0, 3, 12], None, "integer"),
        ("created_at", "integer", None, [], None, "datetime"),
        ("shipped_on", "integer", None, [], None, "datetime"),
        ("timestamp", "integer", None, [], None, "datetime"),
        ("seen", "integer", "(unixepoch())", [], None, "datetime"),
        ("updated_at", "text", None, [], None, "datetime"),
        ("date", "text", None, [], None, "date"),
        ("made", "text", "CURRENT_TIMESTAMP", [], None, "datetime"),
        ("payload_json", "text", None, [], None, "json"),
        ("metadata", "text", None, [], None, "json"),
        ("data", "text", None, [], None, "json"),
        ("settings", "text", None, [], None, "json"),
        ("extra", "text", None, ['{"a": 1}', "[1, 2]"], None, "json"),
        ("bio", "text", None, ["x" * 121], None, "longtext"),
        ("bio", "text", None, ["short"], None, "text"),
        ("price", "real", None, [], None, "real"),
        ("price", "numeric", None, [], None, "real"),
        ("price", "decimal(10,2)", None, [], None, "real"),
        ("price", "float", None, [], None, "real"),
        ("avatar", "blob", None, [], None, "blob"),
        ("count", "integer", None, [], None, "integer"),
    ],
)
def test_kind_inference(name, sql_type, default, samples, spec, kind):
    assert d1.infer_kind(name, sql_type, default, samples, spec_type=spec) == kind


def test_epoch_unit_from_samples():
    assert d1.time_format("datetime", "integer", [1767225600]) == "epoch_s"
    assert d1.time_format("datetime", "integer", [1767225600000]) == "epoch_ms"
    assert d1.time_format("datetime", "text", ["2026-01-01T00:00:00Z"]) == "iso"
    assert d1.time_format("text", "text", ["x"]) == ""


@pytest.mark.asyncio
async def test_schema_kinds_use_the_first_page_as_samples(site_doc, shop):
    s = await _schema(shop, "orders")
    assert _col(s, "is_paid").kind == "boolean"
    assert _col(s, "created_at").kind == "datetime"
    assert _col(s, "created_at").time_format == "epoch_s"
    assert _col(s, "total").kind == "real"
    assert _col(s, "note").kind == "text"


# --------------------------------------------------------------------------- tables


@pytest.mark.asyncio
async def test_inventory_lists_user_tables_with_counts_and_stores(site_doc, shop):
    inv = await _inventory(shop, can_write=True)
    names = [t.name for t in inv.tables]
    assert names == ["account", "customers", "kv_wr", "notes", "orders", "pair", "pair_wr"] + [
        "session"
    ]
    assert not any(n.startswith(("_paw", "d1_", "__drizzle", "sqlite_")) for n in names)
    orders = next(t for t in inv.tables if t.name == "orders")
    assert orders.row_count == 3 and orders.editable is True
    assert next(t for t in inv.tables if t.name == "pair").editable is False
    assert [(s.binding, s.label) for s in inv.files] == [("SITE_FILES", "Site files")]
    assert [(s.binding, s.label) for s in inv.kv] == [("CACHE", "Cache")]
    assert inv.can_write is True and inv.can_restore is False
    assert inv.available is True and inv.reason == ""


@pytest.mark.asyncio
async def test_inventory_lists_connectors(site_doc, shop):
    views = [SimpleNamespace(provider="airtable", status="connected")]
    with _patch_pocket(), patch(_PATCH_CONNECTORS, new=AsyncMock(return_value=views)):
        inv = await data_service.get_inventory(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", _cloudflare=shop
        )
    assert [(c.provider, c.status, c.dashboard_url) for c in inv.connectors] == [
        ("airtable", "connected", None)
    ]


@pytest.mark.asyncio
async def test_inventory_connectors_forbidden_reads_empty(site_doc, shop):
    from pocketpaw_ee.cloud._core.errors import Forbidden

    boom = AsyncMock(side_effect=Forbidden("pocket.access_denied"))
    with _patch_pocket(), patch(_PATCH_CONNECTORS, new=boom):
        inv = await data_service.get_inventory(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", _cloudflare=shop
        )
    assert inv.connectors == [] and inv.tables


@pytest.mark.parametrize("table", ["nope", "_paw_migrations", "d1_migrations", "sqlite_master"])
@pytest.mark.asyncio
async def test_unknown_or_hidden_table_is_404_before_any_row_sql(site_doc, shop, table):
    with pytest.raises(NotFound):
        await _rows(shop, table)
    with pytest.raises(NotFound):
        await _schema(shop, table)
    assert shop.statements(f'"{table}"') == []


# --------------------------------------------------------------------------- injection

_WEIRD = 'we"ird; --x'
_WEIRD_DDL = (
    'CREATE TABLE "we""ird; --x" (id INTEGER PRIMARY KEY, "a""b" TEXT, "c;--" TEXT);'
    """INSERT INTO "we""ird; --x" VALUES (1, 'q"uote', 'semi');"""
)


@pytest.mark.asyncio
async def test_weird_identifiers_are_quoted(site_doc):
    cf = SqliteD1(_WEIRD_DDL, database_id=_DB)
    s = await _schema(cf, _WEIRD)
    assert [c.name for c in s.columns] == ["id", 'a"b', "c;--"]
    page = await _rows(cf, _WEIRD, q="uote", sort='a"b', dir="asc")
    assert page.rows == [{"id": 1, 'a"b': 'q"uote', "c;--": "semi", "__key": 1}]
    page = await _rows(cf, _WEIRD, f=json.dumps([{"col": "c;--", "op": "eq", "value": "semi"}]))
    assert len(page.rows) == 1


@pytest.mark.parametrize(
    "kw",
    [
        {"sort": "nope"},
        {"sort": "id; DROP TABLE orders"},
        {"f": json.dumps([{"col": "nope", "op": "eq", "value": 1}])},
        {"f": json.dumps([{"col": 'id" OR 1=1 --', "op": "eq", "value": 1}])},
    ],
)
@pytest.mark.asyncio
async def test_sort_or_filter_column_not_in_schema_is_422(site_doc, shop, kw):
    with pytest.raises(ValidationError):
        await _rows(shop, "orders", **kw)
    assert shop.statements('FROM "orders" WHERE') == []
    assert shop.statements("DROP") == []


@pytest.mark.parametrize("kw", [{"dir": "sideways"}, {"limit": 201}, {"limit": 0}])
@pytest.mark.parametrize("extra", [{}, {"offset": 100_001}])
@pytest.mark.asyncio
async def test_paging_and_direction_bounds(site_doc, shop, kw, extra):
    with pytest.raises(ValidationError):
        await _rows(shop, "orders", **{**kw, **extra})


@pytest.mark.asyncio
async def test_offset_cap_alone_is_422(site_doc, shop):
    with pytest.raises(ValidationError):
        await _rows(shop, "orders", offset=100_001)
    page = await _rows(shop, "orders", offset=100_000)
    assert page.rows == [] and page.offset == 100_000


# --------------------------------------------------------------------------- rows


@pytest.mark.asyncio
async def test_default_page_is_key_desc_with_explicit_columns(site_doc, shop):
    page = await _rows(shop, "orders")
    assert [r["id"] for r in page.rows] == [3, 2, 1]
    assert page.columns == [
        "id",
        "customer_id",
        "note",
        "total",
        "is_paid",
        "created_at",
        "total_x2",
    ]
    assert all("__key" in r for r in page.rows)
    assert page.total == 3 and page.limit == 50 and page.offset == 0
    select = shop.statements('AS "__key" FROM "orders"')[0]
    assert "SELECT *" not in select
    # One Cloudflare call for rows + count.
    assert any("COUNT(*)" in s for s in shop.statements("FROM (SELECT 1 FROM"))


@pytest.mark.asyncio
async def test_sort_and_paging(site_doc, shop):
    page = await _rows(shop, "orders", sort="total", dir="asc", limit=2, offset=1)
    assert [r["total"] for r in page.rows] == [12, 50]
    assert page.total == 3


@pytest.mark.asyncio
async def test_search_uses_instr_over_text_columns(site_doc, shop):
    page = await _rows(shop, "orders", q="ORDER")
    assert sorted(r["id"] for r in page.rows) == [1, 2]
    select = shop.statements('AS "__key" FROM "orders"')[-1]
    assert 'instr(lower("note"), lower(?)) > 0' in select
    assert "LIKE" not in select
    assert page.total == 2


@pytest.mark.asyncio
async def test_search_never_touches_masked_columns(site_doc, shop):
    page = await _rows(shop, "account", q="scrypt")
    assert page.rows == []
    assert page.total == 0
    select = shop.statements('AS "__key" FROM "account"')[-1]
    assert "password" not in select.split("WHERE", 1)[1]
    assert "access_token" not in select.split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_masked_column_is_not_filterable_or_sortable(site_doc, shop):
    with pytest.raises(ValidationError):
        await _rows(shop, "account", f=_f(("password", "starts", "s")))
    with pytest.raises(ValidationError):
        await _rows(shop, "account", sort="password")


@pytest.mark.asyncio
async def test_q_over_100_chars_is_422(site_doc, shop):
    with pytest.raises(ValidationError):
        await _rows(shop, "orders", q="x" * 101)
    await _rows(shop, "orders", q="x" * 100)


def _f(*filters) -> str:
    return json.dumps([{"col": c, "op": o, "value": v} for c, o, v in filters])


@pytest.mark.parametrize(
    ("filters", "ids"),
    [
        ([("note", "eq", "first order")], [1]),
        ([("note", "ne", "first order")], [2, 3]),
        ([("note", "contains", "ORDER")], [1, 2]),
        ([("note", "starts", "sec")], [2]),
        ([("note", "empty", None)], [3]),
        ([("note", "not_empty", None)], [1, 2]),
        ([("total", "eq", 50)], [2]),
        ([("total", "gt", 10)], [2, 3]),
        ([("total", "gte", 12)], [2, 3]),
        ([("total", "lt", 12)], [1]),
        ([("total", "lte", "12")], [1, 3]),
        ([("is_paid", "is_true", None)], [1]),
        ([("is_paid", "is_false", None)], [2, 3]),
        ([("created_at", "before", "2026-01-02")], [1]),
        ([("created_at", "after", "2026-01-02T00:00:00Z")], [3]),
        ([("created_at", "on", "2026-01-02")], [2]),
        ([("customer_id", "eq", 1), ("total", "gt", 10)], [3]),
    ],
)
@pytest.mark.asyncio
async def test_filters_per_op_and_kind(site_doc, shop, filters, ids):
    page = await _rows(shop, "orders", f=_f(*filters))
    assert sorted(r["id"] for r in page.rows) == ids
    assert page.total == len(ids)


@pytest.mark.asyncio
async def test_text_datetime_filters(site_doc):
    cf = SqliteD1(
        "CREATE TABLE ev (id INTEGER PRIMARY KEY, updated_at TEXT);"
        "INSERT INTO ev VALUES (1, '2026-01-01 10:00:00'), (2, '2026-01-02T08:30:00Z'),"
        " (3, '2026-01-03T00:00:00.000Z');",
        database_id=_DB,
    )
    on = await _rows(cf, "ev", f=_f(("updated_at", "on", "2026-01-02")))
    assert [r["id"] for r in on.rows] == [2]
    after = await _rows(cf, "ev", f=_f(("updated_at", "after", "2026-01-01T12:00:00Z")))
    assert sorted(r["id"] for r in after.rows) == [2, 3]


@pytest.mark.parametrize(
    "f",
    [
        _f(("note", "gt", 1)),  # numeric op on a text column
        _f(("total", "contains", "5")),  # text op on a number column
        _f(("created_at", "before", "not a date")),
        _f(("total", "eq", "abc")),
        _f(("note", "eq", {"nested": 1})),
        _f(("note", "nope", 1)),
        "not json",
        json.dumps({"col": "note"}),
        json.dumps([["note", "eq", 1]]),
        _f(*[("note", "eq", str(i)) for i in range(11)]),
    ],
)
@pytest.mark.asyncio
async def test_bad_filters_are_422(site_doc, shop, f):
    with pytest.raises(ValidationError):
        await _rows(shop, "orders", f=f)


@pytest.mark.asyncio
async def test_ten_filters_are_fine(site_doc, shop):
    page = await _rows(shop, "orders", f=_f(*[("note", "ne", str(i)) for i in range(10)]))
    assert page.total == 3


@pytest.mark.asyncio
async def test_bound_params_stay_within_100(site_doc):
    cols = ", ".join(f"c{i} TEXT" for i in range(120))
    cf = SqliteD1(
        f"CREATE TABLE wide (id INTEGER PRIMARY KEY, {cols});"
        "INSERT INTO wide (id, c0, c119) VALUES (1, 'needle', 'x');",
        database_id=_DB,
    )
    page = await _rows(cf, "wide", q="needle", f=_f(*[("c1", "empty", None)] * 10))
    assert page.search_truncated is True
    assert [r["id"] for r in page.rows] == [1]
    assert all(len(p) <= 100 for _, p in cf.sql)


@pytest.mark.asyncio
async def test_count_cap_reads_total_none(site_doc):
    cf = SqliteD1("CREATE TABLE big (id INTEGER PRIMARY KEY, v TEXT);", database_id=_DB)
    cf.executemany("INSERT INTO big (v) VALUES (?)", [("x",)] * 10_001)
    page = await _rows(cf, "big", limit=5)
    assert page.total is None and len(page.rows) == 5
    s = await _schema(cf, "big")
    assert s.row_count is None
    cf.execute("DELETE FROM big WHERE id = 1")
    d1.clear_caches()
    assert (await _rows(cf, "big", limit=1)).total == 10_000


# --------------------------------------------------------------------------- dynamic lane

_DYNAMIC_WIRE = {
    "name": "Guestbook",
    "workspace": "ws1",
    "pattern": "dynamic",
    "rippleSpec": {
        "type": "container",
        "objects": [
            {
                "name": "order",
                "fields": {"id": "text", "who": "string", "paid": "bool", "at": "datetime"},
                "primaryKey": "id",
            },
            {"name": "rsvp", "fields": {"id": "text", "guests": "int"}, "primaryKey": "id"},
        ],
    },
}
_DYNAMIC_DDL = (
    'CREATE TABLE "order" (id TEXT PRIMARY KEY, who TEXT, paid INTEGER, at TEXT);'
    """INSERT INTO "order" VALUES ('o1', 'Ada', 1, '2026-01-01T00:00:00Z');"""
    "CREATE TABLE undeclared (id INTEGER PRIMARY KEY);"
)


@pytest.mark.asyncio
async def test_dynamic_lane_reads_with_spec_kinds(site_doc):
    cf = SqliteD1(_DYNAMIC_DDL, database_id=_DB)
    s = await _schema(cf, "order", wire=_DYNAMIC_WIRE)
    assert s.key == "id" and s.editable is True
    assert _col(s, "paid").kind == "boolean" and _col(s, "at").kind == "datetime"
    page = await _rows(cf, "order", wire=_DYNAMIC_WIRE, q="ada")
    assert page.rows[0]["__key"] == "o1"


@pytest.mark.asyncio
async def test_dynamic_lane_lists_spec_tables_only_and_a_missing_one_reads_empty(site_doc):
    cf = SqliteD1(_DYNAMIC_DDL, database_id=_DB)
    inv = await _inventory(cf, wire=_DYNAMIC_WIRE)
    assert [(t.name, t.row_count, t.editable) for t in inv.tables] == [
        ("order", 1, True),
        ("rsvp", 0, False),
    ]
    s = await _schema(cf, "rsvp", wire=_DYNAMIC_WIRE)
    assert s.editable is False and s.read_only_reason == "not_created" and s.row_count == 0
    assert [c.name for c in s.columns] == ["id", "guests"]
    assert _col(s, "guests").kind == "integer"
    before = len(cf.sql)
    page = await _rows(cf, "rsvp", wire=_DYNAMIC_WIRE)
    assert page.rows == [] and page.total == 0
    assert all("rsvp" not in s for s, _ in cf.sql[before:])
    with pytest.raises(NotFound):
        await _rows(cf, "undeclared", wire=_DYNAMIC_WIRE)


@pytest.mark.asyncio
async def test_legacy_dynamic_read_quotes_the_table(site_doc):
    """Regression: read_site_data_table built ``SELECT * FROM order`` unquoted, which
    is a syntax error for a table named like a keyword (and unsafe in principle)."""
    cf = SqliteD1(_DYNAMIC_DDL, database_id=_DB)
    with _patch_pocket(_DYNAMIC_WIRE):
        res = await sites_service.read_site_data_table(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="order", _cloudflare=cf
        )
    assert res.rows == [{"id": "o1", "who": "Ada", "paid": 1, "at": "2026-01-01T00:00:00Z"}]
    assert cf.statements('SELECT * FROM "order" LIMIT ?')


# --------------------------------------------------------------------------- availability


@pytest.mark.asyncio
async def test_local_mode_is_unavailable_on_every_read_route(site_doc, monkeypatch):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    with _patch_pocket(), patch(_PATCH_CONNECTORS, new=AsyncMock(return_value=[])):
        inv = await data_service.get_inventory(workspace_id="ws1", user_id="u1", pocket_id="pk1")
        s = await data_service.get_table_schema(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="orders"
        )
        page = await data_service.get_rows(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="orders"
        )
    for res in (inv, s, page):
        assert res.available is False and res.reason == "live_on_cloudflare_only"
    assert inv.tables == [] and page.rows == []


@pytest.mark.asyncio
async def test_dynamic_local_mode_still_lists_spec_tables(site_doc, monkeypatch):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    with _patch_pocket(_DYNAMIC_WIRE), patch(_PATCH_CONNECTORS, new=AsyncMock(return_value=[])):
        inv = await data_service.get_inventory(workspace_id="ws1", user_id="u1", pocket_id="pk1")
    assert inv.available is False and [t.name for t in inv.tables] == ["order", "rsvp"]
    assert all(t.row_count is None and t.editable is False for t in inv.tables)


@pytest.mark.parametrize("stored", ["", "placeholder"])
@pytest.mark.asyncio
async def test_project_not_published_is_unavailable(monkeypatch, shop, stored):
    d1_id = sites_service._derive_d1_database_id("ws1", "pk1") if stored else ""
    doc = _site_doc(d1_id)

    async def _doc(workspace_id, pocket_id):
        return doc

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _doc)
    inv = await _inventory(shop)
    s = await _schema(shop, "orders")
    page = await _rows(shop, "orders")
    for res in (inv, s, page):
        assert res.available is False and res.reason == "not_published"
    assert shop.calls == 0


@pytest.mark.asyncio
async def test_dynamic_without_a_site_doc_is_not_published(monkeypatch, shop):
    async def _doc(workspace_id, pocket_id):
        return None

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _doc)
    inv = await _inventory(shop, wire=_DYNAMIC_WIRE)
    assert inv.available is False and inv.reason == "not_published"
    assert [t.name for t in inv.tables] == ["order", "rsvp"]


@pytest.mark.asyncio
async def test_foreign_workspace_pocket_is_404(site_doc, shop):
    foreign = {**_PROJECT_WIRE, "workspace": "ws-other"}
    with pytest.raises(NotFound):
        await _rows(shop, "orders", wire=foreign)
    with pytest.raises(NotFound):
        await _inventory(shop, wire=foreign)
    assert shop.calls == 0


@pytest.mark.asyncio
async def test_resolve_target_carries_the_stores(site_doc):
    with _patch_pocket():
        t = await data_service.resolve_target(workspace_id="ws1", user_id="u1", pocket_id="pk1")
    assert t.lane == "project" and t.d1_id == _DB
    assert t.kv == {"CACHE": "ns-1"} and t.r2 == {"SITE_FILES": "paw-site-files"}


# --------------------------------------------------------------------------- data token


def test_data_client_prefers_the_data_token(monkeypatch):
    monkeypatch.setenv("PAW_CF_API_TOKEN", "publish-token")
    monkeypatch.setenv("PAW_CF_DATA_API_TOKEN", "data-token")
    cf = sites_service._data_cf_client()
    assert cf._headers["Authorization"] == "Bearer data-token"
    monkeypatch.delenv("PAW_CF_DATA_API_TOKEN")
    cf = sites_service._data_cf_client()
    assert cf._headers["Authorization"] == "Bearer publish-token"


def test_data_client_unconfigured_is_422(monkeypatch):
    monkeypatch.delenv("PAW_CF_API_TOKEN", raising=False)
    monkeypatch.delenv("PAW_CF_DATA_API_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        sites_service._data_cf_client()


@pytest.mark.asyncio
async def test_reads_spend_the_data_budget(site_doc, shop, monkeypatch):
    monkeypatch.setattr(budget, "WORKSPACE_LIMIT", (1, 60))
    with pytest.raises(budget.DataBusy) as exc:
        await _rows(shop, "orders")  # the listing takes the only token
    assert shop.calls == 1
    assert exc.value.status_code == 429 and exc.value.code == "sites.data_busy"
    assert exc.value.details["retry_after"] >= 1


# --------------------------------------------------------------------------- permissions


def test_data_actions_are_admin_only():
    from pocketpaw_ee.guards.actions import ACTIONS
    from pocketpaw_ee.guards.rbac import WorkspaceRole

    assert ACTIONS["sites.data_write"].minimum == WorkspaceRole.ADMIN
    assert ACTIONS["sites.data_write"].deny_code == "sites.data_write_forbidden"
    assert ACTIONS["sites.data_restore"].minimum == WorkspaceRole.ADMIN
    assert ACTIONS["sites.data_restore"].deny_code == "sites.data_restore_forbidden"


# --------------------------------------------------------------------------- routes


class _Membership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _User:
    def __init__(self, role: str) -> None:
        self.id = "u1"
        self.active_workspace = "ws1"
        self.workspaces = [_Membership("ws1", role)]


def _app(role: str = "member") -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    user = _User(role)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id="ws1",
            request_id="t",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: "ws1"
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest.fixture
def routed(site_doc, monkeypatch):
    """The real routes over a shop with a table literally named ``inventory``."""
    cf = SqliteD1(
        _SHOP_DDL
        + "CREATE TABLE inventory (sku TEXT PRIMARY KEY, qty INTEGER);"
        + "INSERT INTO inventory VALUES ('A-1', 4);",
        database_id=_DB,
    )
    monkeypatch.setattr(sites_service, "_data_cf_client", lambda: cf)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    return cf


async def _get(app: FastAPI, path: str, **params: Any):
    base = "/api/v1/sites/by-pocket/pk1/data"
    with _patch_pocket(), patch(_PATCH_CONNECTORS, new=AsyncMock(return_value=[])):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get(base + path, params=params)


@pytest.mark.asyncio
async def test_route_inventory_shape_for_a_member(routed):
    resp = await _get(_app("member"), "/inventory")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "pocket_id",
        "available",
        "reason",
        "tables",
        "files",
        "kv",
        "connectors",
        "can_write",
        "can_restore",
    }
    assert body["can_write"] is False and body["can_restore"] is False
    assert set(body["tables"][0]) == {"name", "row_count", "editable", "read_only_reason"}
    assert "inventory" in [t["name"] for t in body["tables"]]


@pytest.mark.asyncio
async def test_route_inventory_admin_can_write(routed):
    body = (await _get(_app("admin"), "/inventory")).json()
    assert body["can_write"] is True and body["can_restore"] is True


@pytest.mark.asyncio
async def test_route_table_named_inventory_is_reachable_through_tables(routed):
    schema = await _get(_app(), "/tables/inventory")
    assert schema.status_code == 200, schema.text
    assert schema.json()["key"] == "sku"
    rows = await _get(_app(), "/tables/inventory/rows")
    assert rows.status_code == 200, rows.text
    assert rows.json()["rows"] == [{"sku": "A-1", "qty": 4, "__key": "A-1"}]


@pytest.mark.asyncio
async def test_route_rows_query_params(routed):
    resp = await _get(
        _app(),
        "/tables/orders/rows",
        q="order",
        sort="total",
        dir="desc",
        f=_f(("total", "gt", 1)),
        limit=1,
        offset=0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "pocket_id",
        "table",
        "available",
        "reason",
        "columns",
        "rows",
        "total",
        "limit",
        "offset",
        "search_truncated",
    }
    assert [r["id"] for r in body["rows"]] == [2] and body["total"] == 2


@pytest.mark.asyncio
async def test_route_errors(routed):
    assert (await _get(_app(), "/tables/nope/rows")).status_code == 404
    bad = await _get(_app(), "/tables/orders/rows", sort="nope")
    assert bad.status_code == 422 and bad.json()["error"]["code"].startswith("sites.data_")
    assert (await _get(_app(), "/tables/orders/rows", limit=500)).status_code == 422


@pytest.mark.asyncio
async def test_route_schema_shape(routed):
    body = (await _get(_app(), "/tables/orders")).json()
    assert set(body) == {
        "pocket_id",
        "name",
        "available",
        "reason",
        "columns",
        "key",
        "editable",
        "read_only_reason",
        "row_count",
    }
    assert set(body["columns"][0]) == {
        "name",
        "sql_type",
        "kind",
        "time_format",
        "nullable",
        "has_default",
        "auto",
        "primary_key",
        "masked",
        "writable",
        "references",
    }


@pytest.mark.asyncio
async def test_legacy_data_routes_still_work(routed):
    listing = await _get(_app(), "")
    assert listing.status_code == 200, listing.text
    assert "orders" in [t["name"] for t in listing.json()["tables"]]
    legacy = await _get(_app(), "/orders")
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["table"] == "orders" and len(legacy.json()["rows"]) == 3


def test_data_routes_take_request_context_and_fabric_read():
    from pocketpaw_ee.cloud._core.context import request_context
    from pocketpaw_ee.sites.data_router import router

    for route in router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert request_context in calls, route.path
        names = {getattr(d.call, "__qualname__", "") for d in route.dependant.dependencies}
        assert any("require_action" in n for n in names), route.path
        guards = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        assert "require_action_fabric_read" in guards, route.path


def test_data_routes_come_before_the_legacy_table_route():
    from pocketpaw_ee.sites.router import router

    paths = [getattr(r, "path", "") for r in router.routes]
    legacy = paths.index("/sites/by-pocket/{pocket_id}/data/{table}")
    assert paths.index("/sites/by-pocket/{pocket_id}/data/inventory") < legacy
