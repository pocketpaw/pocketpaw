# tests/test_fabric_statements.py
# Created: 2026-07-10 (FST-1 — Fabric source-truth schema).
# Updated: 2026-07-10 (FST-1 schema-freeze ruling — workspace_id tenancy) —
#   both new tables carry the W4a workspace_id: added tests that the same
#   source identity in two workspaces is TWO rows (workspace is part of the
#   dedup identity), that get_statements applies the standard W4a read scope
#   (own rows + legacy NULL), and that a DB from the early FST-1 build
#   (tables without workspace_id, old idx_sources_identity index) is healed
#   by the ALTER migration (column added, index swapped for
#   idx_sources_identity_ws).
#
# Proves the FST-1 foundation the rest of the source-truth chain stacks on:
#   * statement round-trip — append_statement / get_statements preserve every
#     field (JSON value, writer_class, bitemporal datetimes, rank, pinned),
#   * statements are append-only — the store exposes NO update/delete verbs,
#   * source dedup — upsert_source returns the SAME row for the same identity
#     tuple (kind + connector/run_id/document_uri/actor_id/session_id +
#     workspace_id), including identities with absent (None) fields, and a
#     DIFFERENT row for a different identity or a different workspace,
#   * get_statements property filter narrows to one property; workspace_id
#     scopes reads per W4a (own rows + legacy NULL),
#   * migration idempotency — booting the store on a PRE-FST fabric.db (built
#     from the old schema, no statements/sources tables) creates the new
#     tables without touching existing data, and a second boot / second store
#     is a no-op (no error, no duplicate tables/indexes),
#   * the fabric_source_truth_mode flag exists, defaults to "off", rejects
#     junk, and accepts the three documented positions.

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from pocketpaw.fabric.store import FabricStore

# The pre-FST-1 schema (fabric.db as it existed before this slice): the three
# original tables + their indexes, WITHOUT fabric_statements / fabric_sources.
# Inlined (rather than importing SCHEMA_SQL) so the fixture keeps simulating an
# OLD database even as SCHEMA_SQL evolves.
_PRE_FST_SCHEMA = """
CREATE TABLE IF NOT EXISTS fabric_object_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT 'box',
    color TEXT DEFAULT '#0A84FF',
    properties_schema TEXT DEFAULT '[]',
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS fabric_objects (
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
CREATE TABLE IF NOT EXISTS fabric_links (
    id TEXT PRIMARY KEY,
    from_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    to_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    link_type TEXT NOT NULL,
    properties TEXT DEFAULT '{}',
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_objects_type ON fabric_objects(type_id);
CREATE INDEX IF NOT EXISTS idx_objects_source
    ON fabric_objects(source_connector, source_id);
"""


async def _seeded_store(tmp_path: Path) -> tuple[FabricStore, str]:
    """A store with one type + one object; returns (store, object_id)."""
    store = FabricStore(tmp_path / "fabric.db")
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(obj_type.id, {"name": "Acme", "arr": 120})
    return store, obj.id


# ---------------------------------------------------------------------------
# Statement round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_statement_round_trip(tmp_path: Path) -> None:
    store, obj_id = await _seeded_store(tmp_path)
    source = await store.upsert_source("connector_run", connector="gcalendar", run_id="run-1")
    observed = datetime(2026, 7, 1, 12, 0, 0)
    valid_to = datetime(2026, 12, 31, 23, 59, 59)

    written = await store.append_statement(
        obj_id,
        "arr",
        {"amount": 120, "currency": "USD"},
        source.id,
        "connector",
        observed_at=observed,
        valid_to=valid_to,
        rank="preferred",
        rank_reason="most recent sync",
        pinned=True,
    )

    stmts = await store.get_statements(obj_id)
    assert len(stmts) == 1
    got = stmts[0]
    assert got.id == written.id
    assert got.object_id == obj_id
    assert got.property == "arr"
    assert got.value == {"amount": 120, "currency": "USD"}  # JSON round-trips
    assert got.source_ref_id == source.id
    assert got.writer_class == "connector"
    assert got.observed_at == observed
    assert got.valid_from == observed  # defaults to observed_at
    assert got.valid_to == valid_to
    assert got.recorded_at == written.recorded_at
    assert got.rank == "preferred"
    assert got.rank_reason == "most recent sync"
    assert got.pinned is True


@pytest.mark.asyncio
async def test_statement_null_value_round_trips(tmp_path: Path) -> None:
    store, obj_id = await _seeded_store(tmp_path)
    source = await store.upsert_source("human_actor", actor_id="user-7")
    await store.append_statement(obj_id, "churn_reason", None, source.id, "human")
    stmts = await store.get_statements(obj_id, property="churn_reason")
    assert len(stmts) == 1
    assert stmts[0].value is None
    assert stmts[0].writer_class == "human"


@pytest.mark.asyncio
async def test_get_statements_property_filter_and_order(tmp_path: Path) -> None:
    store, obj_id = await _seeded_store(tmp_path)
    source = await store.upsert_source("agent_session", session_id="sess-1")
    first = await store.append_statement(obj_id, "arr", 120, source.id, "agent")
    second = await store.append_statement(obj_id, "arr", 150, source.id, "agent")
    await store.append_statement(obj_id, "name", "Acme Corp", source.id, "agent")

    arr_stmts = await store.get_statements(obj_id, property="arr")
    assert [s.value for s in arr_stmts] == [120, 150]  # oldest first (recorded_at)
    assert [s.id for s in arr_stmts] == [first.id, second.id]  # insertion order
    assert len(await store.get_statements(obj_id)) == 3
    assert await store.get_statements(obj_id, property="missing") == []


def test_statements_are_append_only() -> None:
    """The store exposes NO update/delete verbs for statements in FST-1."""
    verbs = [n for n in dir(FabricStore) if "statement" in n.lower()]
    assert "append_statement" in verbs
    assert "get_statements" in verbs
    assert not any("update" in v or "delete" in v or "remove" in v for v in verbs)


# ---------------------------------------------------------------------------
# Source dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_source_dedups_on_identity(tmp_path: Path) -> None:
    store, _ = await _seeded_store(tmp_path)
    a = await store.upsert_source("connector_run", connector="stripe", run_id="run-9")
    b = await store.upsert_source("connector_run", connector="stripe", run_id="run-9")
    assert a.id == b.id  # same identity -> same row

    c = await store.upsert_source("connector_run", connector="stripe", run_id="run-10")
    assert c.id != a.id  # different identity -> new row

    # Absent (None) identity fields dedup too.
    d = await store.upsert_source("document", document_uri="s3://bucket/report.pdf")
    e = await store.upsert_source("document", document_uri="s3://bucket/report.pdf")
    assert d.id == e.id

    # Same field values under a DIFFERENT kind is a different identity.
    f = await store.upsert_source("human_actor", actor_id="u-1")
    g = await store.upsert_source("agent_session", actor_id="u-1")
    assert f.id != g.id


@pytest.mark.asyncio
async def test_upsert_source_returns_existing_row_unmutated(tmp_path: Path) -> None:
    store, _ = await _seeded_store(tmp_path)
    first_seen = datetime(2026, 7, 1, 8, 0, 0)
    a = await store.upsert_source(
        "connector_run", connector="gmail", run_id="r1", retrieved_at=first_seen
    )
    # retrieved_at is provenance metadata, NOT identity: a later call with a
    # different retrieved_at still resolves to the original row, unchanged.
    b = await store.upsert_source(
        "connector_run",
        connector="gmail",
        run_id="r1",
        retrieved_at=datetime(2026, 7, 2, 8, 0, 0),
    )
    assert b.id == a.id
    assert b.retrieved_at == first_seen


# ---------------------------------------------------------------------------
# Workspace tenancy (schema-freeze ruling)
# ---------------------------------------------------------------------------

WS_A = "ws-alpha"
WS_B = "ws-bravo"


@pytest.mark.asyncio
async def test_same_source_identity_in_two_workspaces_is_two_rows(tmp_path: Path) -> None:
    """workspace_id is PART of the source identity — isolation beats dedup."""
    store, _ = await _seeded_store(tmp_path)
    a = await store.upsert_source(
        "connector_run", connector="stripe", run_id="run-1", workspace_id=WS_A
    )
    b = await store.upsert_source(
        "connector_run", connector="stripe", run_id="run-1", workspace_id=WS_B
    )
    assert a.id != b.id  # two workspaces -> two rows
    assert a.workspace_id == WS_A
    assert b.workspace_id == WS_B

    # Same identity within ONE workspace still dedups.
    a2 = await store.upsert_source(
        "connector_run", connector="stripe", run_id="run-1", workspace_id=WS_A
    )
    assert a2.id == a.id

    # The OSS (None-workspace) identity is its own row, distinct from both.
    none_ws = await store.upsert_source("connector_run", connector="stripe", run_id="run-1")
    assert none_ws.id not in {a.id, b.id}
    assert none_ws.workspace_id is None
    # ... and still dedups against itself.
    assert (await store.upsert_source("connector_run", connector="stripe", run_id="run-1")).id == (
        none_ws.id
    )


@pytest.mark.asyncio
async def test_get_statements_scoped_by_workspace(tmp_path: Path) -> None:
    """W4a read scope on statements: own rows + legacy NULL rows."""
    store, obj_id = await _seeded_store(tmp_path)
    src_a = await store.upsert_source("human_actor", actor_id="u-a", workspace_id=WS_A)
    src_b = await store.upsert_source("human_actor", actor_id="u-b", workspace_id=WS_B)
    src_legacy = await store.upsert_source("human_actor", actor_id="u-legacy")

    stmt_a = await store.append_statement(obj_id, "arr", 100, src_a.id, "human", workspace_id=WS_A)
    stmt_b = await store.append_statement(obj_id, "arr", 200, src_b.id, "human", workspace_id=WS_B)
    stmt_legacy = await store.append_statement(obj_id, "arr", 300, src_legacy.id, "human")

    assert stmt_a.workspace_id == WS_A  # stamped on the returned model too

    # Scoped read: own rows + legacy NULL, never the other tenant's.
    a_ids = {s.id for s in await store.get_statements(obj_id, workspace_id=WS_A)}
    assert a_ids == {stmt_a.id, stmt_legacy.id}
    b_ids = {s.id for s in await store.get_statements(obj_id, workspace_id=WS_B)}
    assert b_ids == {stmt_b.id, stmt_legacy.id}

    # Property filter and workspace scope compose.
    a_arr = await store.get_statements(obj_id, property="arr", workspace_id=WS_A)
    assert {s.value for s in a_arr} == {100, 300}

    # Unscoped read (OSS / single-tenant) sees everything, workspace surfaced.
    all_stmts = await store.get_statements(obj_id)
    assert {s.id for s in all_stmts} == {stmt_a.id, stmt_b.id, stmt_legacy.id}
    assert {s.workspace_id for s in all_stmts} == {WS_A, WS_B, None}


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------


def _table_names(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows]


@pytest.mark.asyncio
async def test_migration_on_existing_pre_fst_db(tmp_path: Path) -> None:
    """Booting on a PRE-FST fabric.db adds the new tables and keeps old data."""
    db_path = tmp_path / "fabric.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_PRE_FST_SCHEMA)
        conn.execute("INSERT INTO fabric_object_types (id, name) VALUES ('ot-old', 'Lease')")
        conn.execute(
            "INSERT INTO fabric_objects (id, type_id, type_name, properties)"
            " VALUES ('obj-old', 'ot-old', 'Lease', '{\"rent\": 900}')"
        )
        conn.commit()

    store = FabricStore(db_path)
    # First boot migrates; the new tables must appear.
    obj = await store.get_object("obj-old")
    assert obj is not None and obj.properties == {"rent": 900}
    tables = _table_names(db_path)
    assert "fabric_statements" in tables
    assert "fabric_sources" in tables

    # And the new tables are immediately usable against the OLD object.
    src = await store.upsert_source("document", document_uri="doc:1")
    await store.append_statement("obj-old", "rent", 900, src.id, "mirror")
    assert (await store.get_statements("obj-old", property="rent"))[0].value == 900


@pytest.mark.asyncio
async def test_migration_idempotent_twice_no_dup_tables(tmp_path: Path) -> None:
    """Migrating twice (two stores, plus a forced re-run) errors nowhere and
    duplicates nothing."""
    db_path = tmp_path / "fabric.db"

    store1 = FabricStore(db_path)
    await store1._ensure_schema()
    tables_after_first = _table_names(db_path)

    # A second store on the SAME file re-runs the full migration from scratch.
    store2 = FabricStore(db_path)
    await store2._ensure_schema()
    # Force a third pass on store1 too (aclose resets the initialized latch).
    await store1.aclose()
    await store1._ensure_schema()

    tables_after_third = _table_names(db_path)
    assert tables_after_third == tables_after_first  # no new/dup tables
    assert len(set(tables_after_third)) == len(tables_after_third)
    assert tables_after_third.count("fabric_statements") == 1
    assert tables_after_third.count("fabric_sources") == 1

    # The workspace-aware source-identity unique index survived both re-runs
    # exactly once, and the early-FST-1 (workspace-less) index name is absent.
    with sqlite3.connect(db_path) as conn:
        idx_ws = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
            " AND name='idx_sources_identity_ws'"
        ).fetchone()[0]
        idx_old = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_sources_identity'"
        ).fetchone()[0]
    assert idx_ws == 1
    assert idx_old == 0


@pytest.mark.asyncio
async def test_migration_heals_early_fst1_db_without_workspace(tmp_path: Path) -> None:
    """A DB from the early FST-1 build (statements/sources tables WITHOUT
    workspace_id, plus the workspace-less identity index) is healed on boot:
    the column is ALTERed in and the old index is swapped for
    idx_sources_identity_ws."""
    db_path = tmp_path / "fabric.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_PRE_FST_SCHEMA)
        conn.executescript(
            """
            CREATE TABLE fabric_sources (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                connector TEXT,
                run_id TEXT,
                document_uri TEXT,
                actor_id TEXT,
                session_id TEXT,
                retrieved_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE fabric_statements (
                id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL REFERENCES fabric_objects(id),
                property TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT 'null',
                source_ref_id TEXT NOT NULL REFERENCES fabric_sources(id),
                writer_class TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                rank TEXT NOT NULL DEFAULT 'normal',
                rank_reason TEXT,
                pinned INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX idx_sources_identity ON fabric_sources(
                kind, IFNULL(connector, ''), IFNULL(run_id, ''),
                IFNULL(document_uri, ''), IFNULL(actor_id, ''),
                IFNULL(session_id, '')
            );
            """
        )
        conn.commit()

    store = FabricStore(db_path)
    # Boot heals the schema and the workspace-aware surface works end to end.
    src = await store.upsert_source("document", document_uri="doc:1", workspace_id=WS_A)
    assert src.workspace_id == WS_A
    with sqlite3.connect(db_path) as conn:
        stmt_cols = {r[1] for r in conn.execute("PRAGMA table_info(fabric_statements)")}
        src_cols = {r[1] for r in conn.execute("PRAGMA table_info(fabric_sources)")}
        idx_names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='fabric_sources'"
            )
        }
    assert "workspace_id" in stmt_cols
    assert "workspace_id" in src_cols
    assert "idx_sources_identity_ws" in idx_names
    assert "idx_sources_identity" not in idx_names


# ---------------------------------------------------------------------------
# Mode flag (inert in FST-1)
# ---------------------------------------------------------------------------


def test_mode_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POCKETPAW_FABRIC_SOURCE_TRUTH_MODE", raising=False)
    from pocketpaw.config import Settings

    assert Settings().fabric_source_truth_mode == "off"


def test_mode_flag_env_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    from pocketpaw.config import Settings

    for mode in ("off", "shadow", "enforce"):
        monkeypatch.setenv("POCKETPAW_FABRIC_SOURCE_TRUTH_MODE", mode)
        assert Settings().fabric_source_truth_mode == mode

    monkeypatch.setenv("POCKETPAW_FABRIC_SOURCE_TRUTH_MODE", "sideways")
    with pytest.raises(Exception):
        Settings()
