# tests/ee/sites/test_data_read_project.py — the owner's data viewer on a ``project``
# site (engine ``project`` + the d1-drizzle recipe), and the credential masking both
# data-viewer lanes share.
#
# A fake D1 client stands in for Cloudflare: it holds tables as {name: (columns,
# rows)}, answers the sqlite_master listing and PRAGMA table_info, and records every
# statement so a test can assert what never reached SQL. The Site doc read is stubbed
# per test (``_canonical_site_doc``), so no Mongo is needed.
#
# Covers: internal-table filtering, PRAGMA columns and primary key, the row cap and
# offset, the whitelist gate (an unknown or internal table is a 404 with no SELECT),
# masking (better-auth tables, the dynamic lane's ``users``, ordinary tables left
# alone), routing (project vs dynamic), the no-database / not-published / local-mode
# answers, and access denial propagating from the pockets read.
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError  # noqa: E402
from pocketpaw_ee.sites import project_data  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402

_PATCH_GET = "pocketpaw_ee.cloud.pockets.service.get"
_DB = "11111111-2222-3333-4444-555555555555"

_PROJECT_WIRE = {
    "name": "Shop",
    "engine": "project",
    "siteMeta": {"project": {"template": "astro", "recipes": ["d1-drizzle", "better-auth"]}},
    "source": {"package.json": "{}"},
}
_PROJECT_NO_DB = {
    "name": "Brochure",
    "engine": "project",
    "siteMeta": {"project": {"template": "static", "recipes": []}},
    "source": {"index.html": "<h1>hi</h1>"},
}

_TABLES = {
    "orders": (
        [("id", "INTEGER", 1), ("email", "TEXT", 0), ("total", "integer", 0)],
        [{"id": 1, "email": "a@x.io", "total": 900}, {"id": 2, "email": "b@x.io", "total": 50}],
    ),
    "account": (
        [("id", "text", 1), ("password", "text", 0), ("access_token", "text", 0)],
        [{"id": "a1", "password": "scrypt$abc", "access_token": None}],
    ),
    "verification": (
        [("id", "text", 1), ("identifier", "text", 0), ("value", "text", 0)],
        [{"id": "v1", "identifier": "a@x.io", "value": "otp-123"}],
    ),
    "settings": (
        [("key", "text", 1), ("value", "text", 0)],
        [{"key": "theme", "value": "dark"}],
    ),
    "sqlite_sequence": ([("name", "", 0)], []),
    "_cf_KV": ([("key", "text", 1)], []),
    "d1_migrations": ([("id", "integer", 1)], []),
    "__drizzle_migrations": ([("id", "integer", 1)], []),
    "_paw_migrations": ([("name", "text", 1)], []),
}


class _FakeD1:
    def __init__(self, tables=None) -> None:
        self.tables = _TABLES if tables is None else tables
        self.sql: list[tuple[str, list]] = []

    def _run(self, sql: str, params: list) -> list[dict]:
        self.sql.append((sql, list(params or [])))
        if sql == project_data.LIST_TABLES_SQL:
            return [{"name": n} for n in sorted(self.tables)]
        if sql.startswith("PRAGMA table_info("):
            name = sql[len('PRAGMA table_info("') : -2].replace('""', '"')
            cols = self.tables[name][0]
            return [
                {"cid": i, "name": c, "type": t, "notnull": 0, "dflt_value": None, "pk": pk}
                for i, (c, t, pk) in enumerate(cols)
            ]
        if sql.startswith('SELECT * FROM "'):
            name = sql[len('SELECT * FROM "') :].split('"', 1)[0]
            rows = self.tables[name][1]
            limit = params[0]
            offset = params[1] if len(params) > 1 else 0
            return rows[offset : offset + limit]
        raise AssertionError(f"unexpected SQL: {sql}")

    async def query_d1(self, *, database_id: str, sql: str, params=None) -> list[dict]:
        assert database_id == _DB
        return self._run(sql, params or [])

    async def query_d1_batch(self, *, database_id: str, statements) -> list[list[dict]]:
        assert database_id == _DB
        return [self._run(sql, params) for sql, params in statements]

    def selects(self) -> list[str]:
        return [s for s, _ in self.sql if s.startswith("SELECT * FROM")]


@pytest.fixture
def site_doc(monkeypatch):
    """Stub the canonical Site read; set ``.d1_database_id`` per test."""
    doc = SimpleNamespace(d1_database_id=_DB)

    async def _doc(workspace_id, pocket_id):
        return doc

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _doc)
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_1")
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.delenv("PAW_SITES_DATA_ROW_LIMIT", raising=False)
    return doc


async def _list(cf, wire=_PROJECT_WIRE):
    with patch(_PATCH_GET, new=AsyncMock(return_value=wire)):
        return await sites_service.list_site_data_tables(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", _cloudflare=cf
        )


async def _read(cf, table, wire=_PROJECT_WIRE, **kw):
    with patch(_PATCH_GET, new=AsyncMock(return_value=wire)):
        return await sites_service.read_site_data_table(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table=table, _cloudflare=cf, **kw
        )


# --------------------------------------------------------------------------- list


@pytest.mark.asyncio
async def test_lists_user_tables_with_pragma_columns(site_doc):
    cf = _FakeD1()
    res = await _list(cf)
    assert res.available is True and res.reason == ""
    assert [t.name for t in res.tables] == ["account", "orders", "settings", "verification"]
    orders = next(t for t in res.tables if t.name == "orders")
    assert orders.fields == {"id": "integer", "email": "text", "total": "integer"}
    assert orders.primary_key == "id"
    # Internal tables were never described.
    assert not any("_paw_" in s or "_cf_" in s or "sqlite_" in s for s, _ in cf.sql[1:])


@pytest.mark.parametrize(
    "name",
    ["sqlite_sequence", "_cf_KV", "_CF_METADATA", "d1_migrations", "__drizzle_migrations"]
    + ["_paw_migrations"],
)
def test_internal_tables_are_hidden(name):
    assert project_data.is_user_table(name) is False


@pytest.mark.parametrize("name", ["orders", "cfg", "x_cf_y", "migrations", "user"])
def test_user_tables_are_shown(name):
    assert project_data.is_user_table(name) is True


@pytest.mark.asyncio
async def test_composite_primary_key_reads_empty(site_doc):
    cf = _FakeD1({"pair": ([("a", "text", 1), ("b", "text", 2)], [])})
    res = await _list(cf)
    assert res.tables[0].primary_key == ""


@pytest.mark.asyncio
async def test_empty_database_lists_no_tables(site_doc):
    cf = _FakeD1({"_paw_migrations": ([("name", "text", 1)], [])})
    res = await _list(cf)
    assert res.available is True and res.tables == []


# --------------------------------------------------------------------------- read


@pytest.mark.asyncio
async def test_reads_rows_and_pragma_columns(site_doc):
    cf = _FakeD1()
    res = await _read(cf, "orders")
    assert res.available is True
    assert res.columns == ["id", "email", "total"]
    assert res.rows == _TABLES["orders"][1]
    sql, params = next((s, p) for s, p in cf.sql if s.startswith("SELECT * FROM"))
    assert sql == 'SELECT * FROM "orders" LIMIT ?'
    assert params == [200]


@pytest.mark.asyncio
async def test_limit_is_capped_and_offset_pages(site_doc, monkeypatch):
    monkeypatch.setenv("PAW_SITES_DATA_ROW_LIMIT", "1")
    cf = _FakeD1()
    res = await _read(cf, "orders", limit=500, offset=1)
    assert [r["id"] for r in res.rows] == [2]
    sql, params = next((s, p) for s, p in cf.sql if s.startswith("SELECT * FROM"))
    assert sql.endswith("LIMIT ? OFFSET ?")
    assert params == [1, 1]


@pytest.mark.asyncio
async def test_smaller_limit_is_honoured(site_doc):
    cf = _FakeD1()
    res = await _read(cf, "orders", limit=1)
    assert len(res.rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ['orders"; DROP TABLE orders; --', "_paw_migrations", "nope"])
async def test_unknown_or_internal_table_is_404_before_any_select(site_doc, table):
    cf = _FakeD1()
    with pytest.raises(NotFound):
        await _read(cf, table)
    assert cf.selects() == []
    assert [s for s, _ in cf.sql] == [project_data.LIST_TABLES_SQL]


def test_quote_ident_doubles_quotes():
    assert project_data.quote_ident('a"b') == '"a""b"'


# --------------------------------------------------------------------------- masking


@pytest.mark.asyncio
async def test_better_auth_credentials_are_masked(site_doc):
    cf = _FakeD1()
    account = await _read(cf, "account")
    assert account.rows == [{"id": "a1", "password": project_data.MASK, "access_token": None}]
    verification = await _read(cf, "verification")
    assert verification.rows[0]["value"] == project_data.MASK
    assert verification.rows[0]["identifier"] == "a@x.io"


@pytest.mark.asyncio
async def test_ordinary_tables_are_not_masked(site_doc):
    cf = _FakeD1()
    res = await _read(cf, "settings")
    assert res.rows == [{"key": "theme", "value": "dark"}]


@pytest.mark.parametrize(
    ("table", "columns", "expected"),
    [
        ("session", ["id", "token", "user_id", "ip_address"], {"token"}),
        (
            "account",
            ["refresh_token", "id_token", "password", "scope"],
            {"refresh_token", "id_token", "password"},
        ),
        ("users", ["id", "email", "password_hash"], {"password_hash"}),
        ("user", ["id", "email", "name"], set()),
        ("Accounts", ["api_key", "client_secret"], {"api_key", "client_secret"}),
        ("orders", ["password", "token"], set()),
    ],
)
def test_masked_columns(table, columns, expected):
    assert project_data.masked_columns(table, columns) == expected


@pytest.mark.asyncio
async def test_dynamic_lane_masks_an_auth_table(monkeypatch):
    """The dynamic viewer reads declared objects; one named ``users`` with a
    ``password_hash`` column gets the same masking."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    async def _no_doc(workspace_id, pocket_id):
        return None

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _no_doc)
    wire = {
        "pattern": "dynamic",
        "rippleSpec": {
            "objects": [{"name": "users", "fields": {"id": "text", "password_hash": "text"}}],
        },
    }
    calls: list = []

    class _CF:
        async def query_d1(self, *, database_id, sql, params=None):
            calls.append((sql, params))
            return [{"id": "u1", "password_hash": "$2b$..."}]

    with patch(_PATCH_GET, new=AsyncMock(return_value=wire)):
        res = await sites_service.read_site_data_table(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="users", _cloudflare=_CF()
        )
    assert res.rows == [{"id": "u1", "password_hash": project_data.MASK}]
    assert calls == [("SELECT * FROM users LIMIT ?", [200])]


@pytest.mark.asyncio
async def test_dynamic_lane_offset(monkeypatch):
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    async def _no_doc(workspace_id, pocket_id):
        return None

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _no_doc)
    wire = {"pattern": "dynamic", "rippleSpec": {"objects": [{"name": "entry", "fields": {}}]}}
    calls: list = []

    class _CF:
        async def query_d1(self, *, database_id, sql, params=None):
            calls.append((sql, params))
            return []

    with patch(_PATCH_GET, new=AsyncMock(return_value=wire)):
        await sites_service.read_site_data_table(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            table="entry",
            limit=10,
            offset=20,
            _cloudflare=_CF(),
        )
    assert calls == [("SELECT * FROM entry LIMIT ? OFFSET ?", [10, 20])]


# --------------------------------------------------------------------------- routing


@pytest.mark.asyncio
async def test_project_without_a_database_is_not_dynamic(site_doc):
    site_doc.d1_database_id = ""
    cf = _FakeD1()
    with pytest.raises(ValidationError) as exc:
        await _list(cf, _PROJECT_NO_DB)
    assert exc.value.code == "sites.not_dynamic"
    with pytest.raises(ValidationError) as exc:
        await _read(cf, "orders", _PROJECT_NO_DB)
    assert exc.value.code == "sites.not_dynamic"
    assert cf.sql == []


@pytest.mark.asyncio
async def test_project_bound_d1_without_recipe_still_reads(site_doc):
    """A D1 the provisioner recorded on the Site is a database, recipe or not."""
    cf = _FakeD1()
    res = await _list(cf, _PROJECT_NO_DB)
    assert res.available is True and res.tables


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["", "derived"])
async def test_project_before_publish_is_not_published(site_doc, stored):
    site_doc.d1_database_id = (
        sites_service._derive_d1_database_id("ws1", "pk1") if stored == "derived" else ""
    )
    cf = _FakeD1()
    listed = await _list(cf)
    assert (listed.available, listed.reason, listed.tables) == (False, "not_published", [])
    rows = await _read(cf, "orders")
    assert (rows.available, rows.reason, rows.columns, rows.rows) == (
        False,
        "not_published",
        [],
        [],
    )
    assert cf.sql == []


@pytest.mark.asyncio
async def test_project_local_mode_is_unavailable(site_doc, monkeypatch):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    listed = await _list(None)
    assert (listed.available, listed.reason, listed.tables) == (
        False,
        "live_on_cloudflare_only",
        [],
    )
    rows = await _read(None, "orders")
    assert (rows.available, rows.reason) == (False, "live_on_cloudflare_only")


@pytest.mark.asyncio
async def test_project_ignores_spec_objects(site_doc):
    """The project lane reads D1, never a stray ``objects`` list on its content."""
    wire = {**_PROJECT_WIRE, "rippleSpec": {"objects": [{"name": "ghost", "fields": {}}]}}
    cf = _FakeD1()
    res = await _list(cf, wire)
    assert "ghost" not in [t.name for t in res.tables]


@pytest.mark.asyncio
async def test_dynamic_site_still_lists_from_spec(site_doc):
    wire = {"pattern": "dynamic", "rippleSpec": {"objects": [{"name": "entry", "fields": {}}]}}
    cf = _FakeD1()
    res = await _list(cf, wire)
    assert [t.name for t in res.tables] == ["entry"]
    assert cf.sql == []


@pytest.mark.asyncio
async def test_access_denied_propagates_before_any_query(site_doc):
    cf = _FakeD1()
    with patch(_PATCH_GET, new=AsyncMock(side_effect=Forbidden("pocket.forbidden", "no"))):
        with pytest.raises(Forbidden):
            await sites_service.list_site_data_tables(
                workspace_id="ws1", user_id="u2", pocket_id="pk1", _cloudflare=cf
            )
        with pytest.raises(Forbidden):
            await sites_service.read_site_data_table(
                workspace_id="ws1", user_id="u2", pocket_id="pk1", table="orders", _cloudflare=cf
            )
    assert cf.sql == []
