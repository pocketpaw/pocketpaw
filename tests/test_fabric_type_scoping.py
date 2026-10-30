# tests/test_fabric_type_scoping.py
# Created: 2026-06-19 (SZD-2 — workspace-scope object TYPES).
#
# Proves the SZD-2 invariant for the "sovereign zero-setup discovery" feature:
# the Fabric object-TYPE catalog is private per workspace. A type defined in
# workspace A must be invisible AND non-reusable from workspace B —
# get_type_by_name / get_type / list_types / stats all scope on the type's own
# workspace_id (own rows + legacy NULL), and ensure_type / ingest_records thread
# the workspace through so connector ingestion stays inside its tenant.
#
# Also covers:
#   * legacy NULL-workspace types stay globally visible (back-compat),
#   * two workspaces may each define a same-named type (the unique index is now
#     (workspace_id, LOWER(name)), not a global LOWER(name)),
#   * the additive migration runs idempotently on a pre-SZD-2 DB (twice, no
#     error), including dropping the old global unique index, and
#   * the SZD-2 backfill attributes a NULL-workspace type to a tenant only when
#     its objects unambiguously share one workspace.

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pocketpaw.connectors.fabric_ingest import FabricMapping, ingest_records
from pocketpaw.fabric.models import PropertyDef
from pocketpaw.fabric.store import SCHEMA_SQL, FabricStore

WS_A = "ws-alpha"
WS_B = "ws-bravo"


# ---------------------------------------------------------------------------
# Core SZD-2 invariant: A cannot read/reuse B's types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_type_defined_in_a_is_invisible_to_b(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    await store.define_type(name="Customer", properties=[], workspace_id=WS_A)

    # B asks for the same name by its own scope -> not found (cannot read A's).
    assert await store.get_type_by_name("Customer", workspace_id=WS_B) is None
    # A sees its own type.
    a_type = await store.get_type_by_name("Customer", workspace_id=WS_A)
    assert a_type is not None
    assert a_type.workspace_id == WS_A

    # list_types is scoped: A sees Customer, B sees nothing.
    a_names = {t.name for t in await store.list_types(workspace_id=WS_A)}
    b_names = {t.name for t in await store.list_types(workspace_id=WS_B)}
    assert a_names == {"Customer"}
    assert b_names == set()

    # get_type by id is scoped too: B cannot fetch A's type by id.
    assert await store.get_type(a_type.id, workspace_id=WS_B) is None
    assert (await store.get_type(a_type.id, workspace_id=WS_A)).id == a_type.id


@pytest.mark.asyncio
async def test_ensure_type_does_not_reuse_other_workspaces_type(tmp_path: Path) -> None:
    """ensure_type for B must NOT return A's type id — it mints B's own."""
    store = FabricStore(tmp_path / "fabric.db")
    mapping = FabricMapping(
        type_name="CalendarEvent",
        source_id_field="id",
        field_map={"summary": "summary"},
        properties=[PropertyDef(name="summary", type="string")],
    )

    # A ingests one record -> defines A's CalendarEvent type.
    await ingest_records(
        store,
        connector="gcal",
        records=[{"id": "a1", "summary": "A standup"}],
        mapping=mapping,
        workspace_id=WS_A,
    )
    # B ingests -> must define B's OWN CalendarEvent, distinct id.
    await ingest_records(
        store,
        connector="gcal",
        records=[{"id": "b1", "summary": "B standup"}],
        mapping=mapping,
        workspace_id=WS_B,
    )

    a_type = await store.get_type_by_name("CalendarEvent", workspace_id=WS_A)
    b_type = await store.get_type_by_name("CalendarEvent", workspace_id=WS_B)
    assert a_type is not None and b_type is not None
    # The whole point: two distinct, workspace-owned types with the same name.
    assert a_type.id != b_type.id
    assert a_type.workspace_id == WS_A
    assert b_type.workspace_id == WS_B

    # Each workspace lists only its own type.
    assert [t.workspace_id for t in await store.list_types(workspace_id=WS_A)] == [WS_A]
    assert [t.workspace_id for t in await store.list_types(workspace_id=WS_B)] == [WS_B]

    # stats counts only the tenant's own type.
    assert (await store.stats(workspace_id=WS_A))["types"] == 1
    assert (await store.stats(workspace_id=WS_B))["types"] == 1


@pytest.mark.asyncio
async def test_reingest_same_workspace_reuses_type(tmp_path: Path) -> None:
    """Within ONE workspace, ensure_type still reuses (define-once-by-name)."""
    store = FabricStore(tmp_path / "fabric.db")
    mapping = FabricMapping(type_name="Ticket", source_id_field="tid", field_map={"title": "title"})
    r1 = await ingest_records(
        store,
        connector="hd",
        records=[{"tid": "t1", "title": "x"}],
        mapping=mapping,
        workspace_id=WS_A,
    )
    r2 = await ingest_records(
        store,
        connector="hd",
        records=[{"tid": "t2", "title": "y"}],
        mapping=mapping,
        workspace_id=WS_A,
    )
    assert r1.created == 1 and r2.created == 1
    a_types = [t for t in await store.list_types(workspace_id=WS_A) if t.name == "Ticket"]
    assert len(a_types) == 1  # one type, reused — not duplicated


# ---------------------------------------------------------------------------
# Legacy NULL-workspace types stay globally visible (back-compat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_null_workspace_type_visible_to_all(tmp_path: Path) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    # No workspace_id -> legacy/global type.
    await store.define_type(name="GlobalThing", properties=[])

    # Both tenants see it; the unscoped read sees it; B can reuse it via name.
    assert (await store.get_type_by_name("GlobalThing", workspace_id=WS_A)) is not None
    assert (await store.get_type_by_name("GlobalThing", workspace_id=WS_B)) is not None
    assert (await store.get_type_by_name("GlobalThing")) is not None
    assert {t.name for t in await store.list_types(workspace_id=WS_A)} == {"GlobalThing"}
    assert {t.name for t in await store.list_types()} == {"GlobalThing"}


@pytest.mark.asyncio
async def test_own_type_wins_over_legacy_same_name(tmp_path: Path) -> None:
    """When both a legacy NULL type and the caller's own type share a name, the
    scoped lookup resolves to the caller's OWN type (own-rows-first ordering)."""
    store = FabricStore(tmp_path / "fabric.db")
    await store.define_type(name="Customer", properties=[])  # legacy/global
    own = await store.define_type(name="Customer", properties=[], workspace_id=WS_A)

    resolved = await store.get_type_by_name("Customer", workspace_id=WS_A)
    assert resolved is not None
    assert resolved.id == own.id
    assert resolved.workspace_id == WS_A


@pytest.mark.asyncio
async def test_unscoped_callers_see_everything(tmp_path: Path) -> None:
    """workspace_id=None is fully backward-compatible: sees all defined types."""
    store = FabricStore(tmp_path / "fabric.db")
    await store.define_type(name="A", properties=[], workspace_id=WS_A)
    await store.define_type(name="B", properties=[], workspace_id=WS_B)
    await store.define_type(name="G", properties=[])
    names = {t.name for t in await store.list_types()}
    assert names == {"A", "B", "G"}
    assert (await store.stats())["types"] == 3


# ---------------------------------------------------------------------------
# Migration idempotency on a pre-SZD-2 DB
# ---------------------------------------------------------------------------


# The faithful pre-SZD-2 schema: fabric_object_types had NO workspace_id column,
# fabric_objects / fabric_links already had one (from W4a), and the type-name
# unique index was GLOBAL on LOWER(name). Written out literally rather than
# regex-stripping the live SCHEMA_SQL so the test pins the exact historic shape
# the migration must upgrade from.
_PRE_SZD2_SCHEMA = """
CREATE TABLE fabric_object_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT 'box',
    color TEXT DEFAULT '#0A84FF',
    properties_schema TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE fabric_objects (
    id TEXT PRIMARY KEY,
    type_id TEXT NOT NULL REFERENCES fabric_object_types(id),
    type_name TEXT DEFAULT '',
    properties TEXT NOT NULL DEFAULT '{}',
    source_connector TEXT,
    source_id TEXT,
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE fabric_links (
    id TEXT PRIMARY KEY,
    from_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    to_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    link_type TEXT NOT NULL,
    properties TEXT DEFAULT '{}',
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_object_types_name_unique ON fabric_object_types(LOWER(name));
"""


def _make_pre_szd2_db(path: Path) -> None:
    """Materialize a DB whose fabric_object_types has NO workspace_id column and
    carries the OLD global unique index on LOWER(name) — the faithful pre-SZD-2
    state a deployment would be upgrading from."""
    # Sanity: confirm SCHEMA_SQL (the current schema) does carry the column we
    # are deliberately omitting here, so this stays a true "before" snapshot.
    types_block = SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS fabric_objects")[0]
    assert "workspace_id" in types_block, "current object_types should have workspace_id"
    conn = sqlite3.connect(path)
    conn.executescript(_PRE_SZD2_SCHEMA)
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent_on_pre_szd2_db(tmp_path: Path) -> None:
    db = tmp_path / "fabric.db"
    _make_pre_szd2_db(db)

    # First open: runs the migration (ALTER + drop old index + new index).
    store1 = FabricStore(db)
    assert await store1.list_types() == []  # opens cleanly, no OperationalError
    # After migration two workspaces can define the same name (old global
    # unique index would have rejected the second).
    await store1.define_type(name="Customer", properties=[], workspace_id=WS_A)
    await store1.define_type(name="Customer", properties=[], workspace_id=WS_B)

    # Second open on the SAME file re-runs _ensure_schema end to end. The ALTER's
    # duplicate-column error and the DROP/CREATE INDEX must all be no-ops.
    store2 = FabricStore(db)
    a = await store2.get_type_by_name("Customer", workspace_id=WS_A)
    b = await store2.get_type_by_name("Customer", workspace_id=WS_B)
    assert a is not None and b is not None and a.id != b.id


@pytest.mark.asyncio
async def test_backfill_attributes_unambiguous_type(tmp_path: Path) -> None:
    """A pre-SZD-2 NULL-workspace type whose objects all live in one workspace is
    attributed to that workspace on migration; an ambiguous one stays NULL."""
    db = tmp_path / "fabric.db"
    _make_pre_szd2_db(db)

    # Seed pre-SZD-2 state directly: types have no workspace_id column yet, but
    # objects (which predate SZD-2 and DO have workspace_id from W4a) carry one.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO fabric_object_types (id, name, properties_schema) VALUES (?, ?, '[]')",
        ("ot-solo", "Solo"),
    )
    conn.execute(
        "INSERT INTO fabric_object_types (id, name, properties_schema) VALUES (?, ?, '[]')",
        ("ot-mixed", "Mixed"),
    )
    # Solo: all objects in WS_A -> backfill attributes Solo to WS_A.
    conn.execute(
        "INSERT INTO fabric_objects (id, type_id, properties, workspace_id)"
        " VALUES ('o1', 'ot-solo', '{}', ?)",
        (WS_A,),
    )
    conn.execute(
        "INSERT INTO fabric_objects (id, type_id, properties, workspace_id)"
        " VALUES ('o2', 'ot-solo', '{}', ?)",
        (WS_A,),
    )
    # Mixed: objects span WS_A and WS_B -> cannot attribute, stays NULL/global.
    conn.execute(
        "INSERT INTO fabric_objects (id, type_id, properties, workspace_id)"
        " VALUES ('o3', 'ot-mixed', '{}', ?)",
        (WS_A,),
    )
    conn.execute(
        "INSERT INTO fabric_objects (id, type_id, properties, workspace_id)"
        " VALUES ('o4', 'ot-mixed', '{}', ?)",
        (WS_B,),
    )
    conn.commit()
    conn.close()

    store = FabricStore(db)
    await store.list_types()  # triggers migration + backfill

    solo = await store.get_type("ot-solo")
    mixed = await store.get_type("ot-mixed")
    assert solo is not None and mixed is not None
    assert solo.workspace_id == WS_A  # unambiguous -> attributed
    assert mixed.workspace_id is None  # ambiguous -> stays global

    # Consequence: WS_A sees Solo (attributed) + Mixed (global); WS_B sees only
    # Mixed (global), NOT Solo.
    a_names = {t.name for t in await store.list_types(workspace_id=WS_A)}
    b_names = {t.name for t in await store.list_types(workspace_id=WS_B)}
    assert a_names == {"Solo", "Mixed"}
    assert b_names == {"Mixed"}
