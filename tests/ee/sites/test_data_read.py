# tests/ee/sites/test_data_read.py — DS-3: control-plane read of a dynamic site's
# Cloudflare D1 data. Created 2026-06-20 (feat/sites-d1-read).
#
# Covers the two operator data-view reads (service + the cloudflare_client D1
# query method) with fakes so no live D1 / network is touched:
#   * list_site_data_tables — lists a dynamic site's tables from the pocket spec's
#     ``objects``; in LOCAL mode it returns available=False /
#     reason="live_on_cloudflare_only" but STILL lists the schema (degrades
#     cleanly, no error).
#   * read_site_data_table — with an injected D1 CF client, returns the table's
#     rows from a mocked D1 response; an UNKNOWN table is rejected (404) before any
#     query (the SQL-safety gate); a NON-dynamic pocket raises not_dynamic;
#     tenant-scoping holds (the foreign-workspace D1 id differs); LOCAL mode (no
#     injected CF) returns the unavailable_local shape with columns still listed.
#   * cloudflare_client.query_d1 — parses rows out of the D1 query envelope
#     (result[0].results), sends a parameterized {sql, params} body, and fails
#     closed on a non-2xx.
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient  # noqa: E402

_PATCH_GET = "pocketpaw_ee.cloud.pockets.service.get"

# A dynamic-site pocket wire dict: pattern="dynamic" + a spec carrying top-level
# ``objects`` (the declared D1 tables) the data-view reads.
_DYNAMIC_WIRE = {
    "name": "Guestbook",
    "pattern": "dynamic",
    "rippleSpec": {
        "type": "container",
        "objects": [
            {
                "name": "entry",
                "fields": {"id": "text", "name": "text", "message": "text"},
                "primaryKey": "id",
            },
            {
                "name": "rsvp",
                "fields": {"id": "text", "guests": "integer"},
                "primaryKey": "id",
            },
        ],
        "sources": [{"name": "entries", "kind": "data", "object": "entry"}],
    },
}


class _FakeD1CF:
    """A Cloudflare client double that records the D1 query it was asked to run and
    returns canned rows — so the service path is exercised without a live D1."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else [{"id": "1", "name": "Ada", "message": "hi"}]
        self.calls: list[dict] = []

    async def query_d1(self, *, database_id: str, sql: str, params=None) -> list[dict]:
        self.calls.append({"database_id": database_id, "sql": sql, "params": params})
        return self.rows


# --------------------------------------------------------------------------- #
# list_site_data_tables
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_tables_lists_objects_from_spec(monkeypatch):
    """A dynamic site lists its tables (name, fields, primary key) from the spec's
    ``objects`` — schema is always available even with no live D1."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    with patch(_PATCH_GET, new=AsyncMock(return_value=_DYNAMIC_WIRE)) as mock_get:
        res = await sites_service.list_site_data_tables(
            workspace_id="ws1", user_id="u1", pocket_id="pk1"
        )
    mock_get.assert_awaited_once_with("pk1", "u1")
    assert res.pocket_id == "pk1"
    names = [t.name for t in res.tables]
    assert names == ["entry", "rsvp"]
    entry = next(t for t in res.tables if t.name == "entry")
    assert entry.fields == {"id": "text", "name": "text", "message": "text"}
    assert entry.primary_key == "id"


@pytest.mark.asyncio
async def test_list_tables_local_mode_unavailable_but_schema_listed(monkeypatch):
    """In LOCAL mode (no CF creds) the data is not reachable, but the read DEGRADES
    cleanly — available=False, reason='live_on_cloudflare_only', and the tables are
    STILL listed from the spec (no error)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    with patch(_PATCH_GET, new=AsyncMock(return_value=_DYNAMIC_WIRE)):
        res = await sites_service.list_site_data_tables(
            workspace_id="ws1", user_id="u1", pocket_id="pk1"
        )
    assert res.available is False
    assert res.reason == "live_on_cloudflare_only"
    assert [t.name for t in res.tables] == ["entry", "rsvp"]


@pytest.mark.asyncio
async def test_list_tables_non_dynamic_pocket_rejected(monkeypatch):
    """A NON-dynamic pocket (a static landing) has no data store → not_dynamic."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    landing = {"name": "Brochure", "pattern": "landing", "rippleSpec": {"type": "container"}}
    with patch(_PATCH_GET, new=AsyncMock(return_value=landing)):
        with pytest.raises(ValidationError) as exc:
            await sites_service.list_site_data_tables(
                workspace_id="ws1", user_id="u1", pocket_id="pk1"
            )
    assert exc.value.code == "sites.not_dynamic"


@pytest.mark.asyncio
async def test_list_tables_available_when_cf_configured(monkeypatch):
    """With CF creds configured (not local mode) the table list reports
    available=True (the rows behind the tables can be read)."""
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_1")
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    with patch(_PATCH_GET, new=AsyncMock(return_value=_DYNAMIC_WIRE)):
        res = await sites_service.list_site_data_tables(
            workspace_id="ws1", user_id="u1", pocket_id="pk1"
        )
    assert res.available is True
    assert res.reason == ""


# --------------------------------------------------------------------------- #
# read_site_data_table
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_read_table_returns_rows_for_dynamic_site(beanie_test_db, monkeypatch):
    """A dynamic site's table read returns the D1 rows (via the injected CF
    client), with the declared columns and a bounded, PARAMETERIZED query."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    cf = _FakeD1CF(rows=[{"id": "1", "name": "Ada"}, {"id": "2", "name": "Grace"}])
    with patch(_PATCH_GET, new=AsyncMock(return_value=_DYNAMIC_WIRE)):
        res = await sites_service.read_site_data_table(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="entry", _cloudflare=cf
        )
    assert res.available is True
    assert res.table == "entry"
    assert res.columns == ["id", "name", "message"]
    assert res.rows == [{"id": "1", "name": "Ada"}, {"id": "2", "name": "Grace"}]
    # The query was bounded + parameterized (the LIMIT value rides params, the
    # table is the whitelisted identifier — never a free-text interpolation).
    assert len(cf.calls) == 1
    call = cf.calls[0]
    assert "FROM entry" in call["sql"]
    assert "LIMIT ?" in call["sql"]
    assert isinstance(call["params"], list) and len(call["params"]) == 1


@pytest.mark.asyncio
async def test_read_table_unknown_table_rejected_before_query(beanie_test_db, monkeypatch):
    """An UNKNOWN table is rejected (404) BEFORE any D1 query runs — the SQL-safety
    gate: the identifier must be one of the spec's declared objects."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    cf = _FakeD1CF()
    with patch(_PATCH_GET, new=AsyncMock(return_value=_DYNAMIC_WIRE)):
        with pytest.raises(NotFound) as exc:
            await sites_service.read_site_data_table(
                workspace_id="ws1",
                user_id="u1",
                pocket_id="pk1",
                table="users; DROP TABLE entry",
                _cloudflare=cf,
            )
    assert exc.value.status_code == 404
    # No query was ever sent for the unknown / injection-shaped table.
    assert cf.calls == []


@pytest.mark.asyncio
async def test_read_table_non_dynamic_pocket_rejected(monkeypatch):
    """A NON-dynamic pocket → not_dynamic (no data store to read)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    landing = {"name": "Brochure", "pattern": "landing", "rippleSpec": {"type": "container"}}
    with patch(_PATCH_GET, new=AsyncMock(return_value=landing)):
        with pytest.raises(ValidationError) as exc:
            await sites_service.read_site_data_table(
                workspace_id="ws1", user_id="u1", pocket_id="pk1", table="entry"
            )
    assert exc.value.code == "sites.not_dynamic"


@pytest.mark.asyncio
async def test_read_table_local_mode_unavailable_shape(monkeypatch):
    """LOCAL mode (no injected CF, no creds) returns the clean unavailable shape —
    available=False, reason='live_on_cloudflare_only', no rows — but the table's
    declared columns are STILL listed from the spec (no error)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    with patch(_PATCH_GET, new=AsyncMock(return_value=_DYNAMIC_WIRE)):
        res = await sites_service.read_site_data_table(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="entry"
        )
    assert res.available is False
    assert res.reason == "live_on_cloudflare_only"
    assert res.rows == []
    assert res.columns == ["id", "name", "message"]


@pytest.mark.asyncio
async def test_read_table_tenant_scoped_d1_id(beanie_test_db, monkeypatch):
    """Tenant-scoping: the derived D1 database id is a function of (workspace,
    pocket), so two workspaces reading the SAME pocket id target DIFFERENT
    databases — a foreign workspace cannot read another tenant's data."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    cf_a, cf_b = _FakeD1CF(), _FakeD1CF()
    with patch(_PATCH_GET, new=AsyncMock(return_value=_DYNAMIC_WIRE)):
        await sites_service.read_site_data_table(
            workspace_id="ws_A", user_id="u1", pocket_id="pk1", table="entry", _cloudflare=cf_a
        )
        await sites_service.read_site_data_table(
            workspace_id="ws_B", user_id="u1", pocket_id="pk1", table="entry", _cloudflare=cf_b
        )
    assert cf_a.calls[0]["database_id"] != cf_b.calls[0]["database_id"]


# --------------------------------------------------------------------------- #
# cloudflare_client.query_d1
# --------------------------------------------------------------------------- #


def _d1_client(handler) -> CloudflareClient:
    transport = httpx.MockTransport(handler)
    return CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="zone_1",
        dispatch_namespace="paw-sites",
        _transport=transport,
    )


@pytest.mark.asyncio
async def test_query_d1_parses_rows_and_sends_parameterized_body():
    """query_d1 POSTs a {sql, params} body to the D1 query endpoint and returns the
    rows out of the D1 envelope's result[0].results."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": [
                    {
                        "success": True,
                        "meta": {"rows_read": 2},
                        "results": [{"id": "1", "name": "Ada"}, {"id": "2", "name": "Grace"}],
                    }
                ],
            },
        )

    client = _d1_client(handler)
    rows = await client.query_d1(
        database_id="db_1", sql="SELECT * FROM entry LIMIT ?", params=[200]
    )
    assert rows == [{"id": "1", "name": "Ada"}, {"id": "2", "name": "Grace"}]
    assert "d1/database/db_1/query" in seen["url"]
    assert seen["method"] == "POST"
    assert seen["body"] == {"sql": "SELECT * FROM entry LIMIT ?", "params": [200]}


@pytest.mark.asyncio
async def test_query_d1_empty_results_returns_empty_list():
    """A table with no rows returns an empty list (not an error)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"success": True, "result": [{"success": True, "results": []}]}
        )

    client = _d1_client(handler)
    rows = await client.query_d1(
        database_id="db_1", sql="SELECT * FROM entry LIMIT ?", params=[200]
    )
    assert rows == []


@pytest.mark.asyncio
async def test_query_d1_non_2xx_fails_closed():
    """A non-2xx D1 response fails closed (raises) — never a silent empty read."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"message": "denied"}]})

    client = _d1_client(handler)
    with pytest.raises(ValidationError):
        await client.query_d1(database_id="db_1", sql="SELECT 1", params=[])
