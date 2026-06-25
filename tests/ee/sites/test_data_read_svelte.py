# tests/ee/sites/test_data_read_svelte.py — DSV-2b: engine-appropriate objects
# read for a DYNAMIC SVELTE site. Created 2026-06-21 (feat/dsv-2b-objects-read).
#
# DS-3 reads a dynamic site's tables from the pocket's ``rippleSpec.objects`` — the
# RIPPLE-track storage shape. A dynamic SVELTE pocket stores its bindings
# (``objects``/``sources``/``actions``/``auth``) as sibling keys on its ``source``
# content envelope instead (mirroring the publish switch ``version_content =
# (source if engine == "svelte" else ripple_spec)``). These tests prove the
# data-read path now selects the engine-appropriate envelope:
#   * a dynamic SVELTE pocket (engine="svelte", objects on ``source``) →
#     list_site_data_tables returns its tables; _is_dynamic true.
#   * a dynamic SVELTE pocket → read_site_data_table reads a table's rows + columns.
#   * a RIPPLE dynamic pocket is UNCHANGED — still reads from ``rippleSpec`` (no
#     regress), even if its ``source`` is empty/absent.
#   * a STATIC svelte pocket (engine="svelte", no bindings on ``source``) → NOT
#     dynamic (not_dynamic), so no tables.
#   * the ``rippleSpec`` of a svelte pocket is IGNORED — bindings there do NOT make
#     a svelte pocket dynamic (the engine selects the envelope, not a union).
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import ValidationError  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402

_PATCH_GET = "pocketpaw_ee.cloud.pockets.service.get"


# A dynamic SVELTE pocket wire dict: engine="svelte" + the dynamic bindings
# (``objects`` = the D1 tables, plus ``sources``) carried as SIBLING KEYS on the
# ``source`` content envelope, ALONGSIDE the hand-written SvelteKit files. This is
# the contract the create-svelte brain + generator must store to.
_SVELTE_DYNAMIC_WIRE = {
    "name": "Svelte Guestbook",
    "pattern": "dynamic",
    "engine": "svelte",
    "source": {
        # The {path: contents} SvelteKit files.
        "src/routes/+page.svelte": "<h1>Guestbook</h1>",
        # The dynamic bindings, as siblings on the same envelope.
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
    """A Cloudflare client double recording the D1 query and returning canned rows
    so the read path is exercised without a live D1 (mirror of DS-3's fake)."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else [{"id": "1", "name": "Ada", "message": "hi"}]
        self.calls: list[dict] = []

    async def query_d1(self, *, database_id: str, sql: str, params=None) -> list[dict]:
        self.calls.append({"database_id": database_id, "sql": sql, "params": params})
        return self.rows


# --------------------------------------------------------------------------- #
# dynamic SVELTE site — objects read from the ``source`` envelope
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_svelte_dynamic_lists_tables_from_source_envelope(monkeypatch):
    """A dynamic SVELTE pocket lists its tables from ``source.objects`` (the
    engine-appropriate envelope), not ``rippleSpec`` — so the Data tab is populated
    for a svelte dynamic site."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    with patch(_PATCH_GET, new=AsyncMock(return_value=_SVELTE_DYNAMIC_WIRE)) as mock_get:
        res = await sites_service.list_site_data_tables(
            workspace_id="ws1", user_id="u1", pocket_id="pk1"
        )
    mock_get.assert_awaited_once_with("pk1", "u1")
    names = [t.name for t in res.tables]
    assert names == ["entry", "rsvp"]
    entry = next(t for t in res.tables if t.name == "entry")
    assert entry.fields == {"id": "text", "name": "text", "message": "text"}
    assert entry.primary_key == "id"


@pytest.mark.asyncio
async def test_svelte_dynamic_is_classified_dynamic(monkeypatch):
    """A dynamic SVELTE pocket classifies as dynamic via the engine-appropriate
    envelope — it does NOT raise not_dynamic."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    with patch(_PATCH_GET, new=AsyncMock(return_value=_SVELTE_DYNAMIC_WIRE)):
        # No raise → dynamic. The call returns a populated table list.
        res = await sites_service.list_site_data_tables(
            workspace_id="ws1", user_id="u1", pocket_id="pk1"
        )
    assert [t.name for t in res.tables] == ["entry", "rsvp"]


@pytest.mark.asyncio
async def test_svelte_dynamic_reads_table_rows(beanie_test_db, monkeypatch):
    """A dynamic SVELTE pocket's table read returns the D1 rows + declared columns,
    proving the per-table read also resolves objects off the ``source`` envelope."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    cf = _FakeD1CF(rows=[{"id": "1", "name": "Ada"}, {"id": "2", "name": "Grace"}])
    with patch(_PATCH_GET, new=AsyncMock(return_value=_SVELTE_DYNAMIC_WIRE)):
        res = await sites_service.read_site_data_table(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="entry", _cloudflare=cf
        )
    assert res.available is True
    assert res.table == "entry"
    assert res.columns == ["id", "name", "message"]
    assert res.rows == [{"id": "1", "name": "Ada"}, {"id": "2", "name": "Grace"}]
    assert len(cf.calls) == 1


@pytest.mark.asyncio
async def test_svelte_dynamic_via_bindings_safety_net(monkeypatch):
    """A svelte pocket NOT stamped pattern="dynamic" but carrying ``sources`` on its
    ``source`` envelope is still dynamic (the _is_dynamic bindings safety-net works
    on the engine-appropriate envelope)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    wire = {
        "name": "Unstamped svelte",
        "engine": "svelte",
        # pattern absent → relies on the bindings safety-net.
        "source": {
            "src/routes/+page.svelte": "<h1>Hi</h1>",
            "objects": [{"name": "lead", "fields": {"id": "text"}, "primaryKey": "id"}],
            "sources": [{"name": "leads", "kind": "data", "object": "lead"}],
        },
    }
    with patch(_PATCH_GET, new=AsyncMock(return_value=wire)):
        res = await sites_service.list_site_data_tables(
            workspace_id="ws1", user_id="u1", pocket_id="pk1"
        )
    assert [t.name for t in res.tables] == ["lead"]


# --------------------------------------------------------------------------- #
# no regress — ripple dynamic still reads from rippleSpec
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ripple_dynamic_still_reads_ripplespec(monkeypatch):
    """A RIPPLE dynamic pocket (the DS-3 shape) is UNCHANGED — its tables still come
    from ``rippleSpec.objects``, even though ``source`` is absent. No regress."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    ripple_wire = {
        "name": "Ripple Guestbook",
        "pattern": "dynamic",
        # engine absent → defaults to "ripple"; bindings live on rippleSpec.
        "rippleSpec": {
            "type": "container",
            "objects": [
                {"name": "entry", "fields": {"id": "text", "name": "text"}, "primaryKey": "id"},
            ],
            "sources": [{"name": "entries", "kind": "data", "object": "entry"}],
        },
    }
    with patch(_PATCH_GET, new=AsyncMock(return_value=ripple_wire)):
        res = await sites_service.list_site_data_tables(
            workspace_id="ws1", user_id="u1", pocket_id="pk1"
        )
    assert [t.name for t in res.tables] == ["entry"]


@pytest.mark.asyncio
async def test_ripple_engine_ignores_source_envelope(monkeypatch):
    """A pocket on the RIPPLE engine reads ONLY ``rippleSpec`` — bindings sitting on
    its ``source`` map are NOT read (the engine selects the envelope, never a
    union)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    wire = {
        "name": "Ripple w/ stray source",
        "engine": "ripple",
        "rippleSpec": {"type": "container"},  # no bindings here → not dynamic
        "source": {
            "objects": [{"name": "ghost", "fields": {"id": "text"}, "primaryKey": "id"}],
            "sources": [{"name": "x", "kind": "data", "object": "ghost"}],
        },
    }
    with patch(_PATCH_GET, new=AsyncMock(return_value=wire)):
        with pytest.raises(ValidationError) as exc:
            await sites_service.list_site_data_tables(
                workspace_id="ws1", user_id="u1", pocket_id="pk1"
            )
    assert exc.value.code == "sites.not_dynamic"


# --------------------------------------------------------------------------- #
# static svelte site — no bindings → not dynamic
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_static_svelte_not_dynamic(monkeypatch):
    """A STATIC svelte pocket (engine="svelte", only files on ``source``, no
    bindings, pattern="landing") has no data store → not_dynamic / no tables."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    wire = {
        "name": "Bakery landing",
        "pattern": "landing",
        "engine": "svelte",
        "source": {
            "src/routes/+page.svelte": "<h1>Fresh bread</h1>",
            "src/routes/+layout.svelte": "<slot />",
        },
    }
    with patch(_PATCH_GET, new=AsyncMock(return_value=wire)):
        with pytest.raises(ValidationError) as exc:
            await sites_service.list_site_data_tables(
                workspace_id="ws1", user_id="u1", pocket_id="pk1"
            )
    assert exc.value.code == "sites.not_dynamic"


@pytest.mark.asyncio
async def test_svelte_rippleSpec_bindings_do_not_make_dynamic(monkeypatch):
    """A svelte pocket whose bindings were (wrongly) put on ``rippleSpec`` instead
    of ``source`` is NOT dynamic for the svelte engine — the engine reads ONLY the
    ``source`` envelope, so stale rippleSpec bindings are ignored (not_dynamic)."""
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    wire = {
        "name": "Svelte w/ rippleSpec bindings",
        "engine": "svelte",
        # bindings on the WRONG envelope for svelte → ignored
        "rippleSpec": {
            "objects": [{"name": "stray", "fields": {"id": "text"}, "primaryKey": "id"}],
            "sources": [{"name": "x", "kind": "data", "object": "stray"}],
        },
        "source": {"src/routes/+page.svelte": "<h1>Hi</h1>"},  # no bindings here
    }
    with patch(_PATCH_GET, new=AsyncMock(return_value=wire)):
        with pytest.raises(ValidationError) as exc:
            await sites_service.list_site_data_tables(
                workspace_id="ws1", user_id="u1", pocket_id="pk1"
            )
    assert exc.value.code == "sites.not_dynamic"
