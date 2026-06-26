# tests/test_workspace_store_migration.py
# Created: 2026-06-26 (ISO-4) — Tests for the one-time shared-store split migration.
#
# Verifies migrate_shared_stores_to_workspaces():
#   - seeds a SHARED fabric.db + instinct.db with rows across two workspaces
#     (ws-a, ws-b) plus NULL-workspace rows;
#   - runs the migration and asserts per-workspace files exist on disk;
#   - row counts match (sum across workspaces == shared total; no rows lost);
#   - NULL-workspace rows land in system0 (the default system_workspace);
#   - a query inside ws-a's fabric store returns only ws-a objects;
#   - InstinctStore(<per-ws-file>).verify_audit_chain() is intact=True AND hashed>0
#     for each workspace (re-chain worked);
#   - shared source files were NOT destroyed (renamed to .migrated);
#   - re-running the migration is a strict no-op (skipped=True; counts show 0 new rows);
#   - SOURCE-CHAIN GATE (captain hard requirement): a clean source records an
#     intact verdict in the marker; a TAMPERED source ABORTS with the source
#     left untouched (no laundering); force=True overrides + records the override.
#
# Mirrors the style of tests/test_instinct_workspace_isolation.py.

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import pocketpaw.stores as stores
from pocketpaw.fabric.store import FabricStore
from pocketpaw.instinct.models import ActionTrigger
from pocketpaw.instinct.store import InstinctStore
from pocketpaw.migrations.split_workspace_stores import (
    SourceChainTamperedError,
    migrate_shared_stores_to_workspaces,
)

WS_A = "wsa"
WS_B = "wsb"
# "system0" — must start with [A-Za-z0-9] to pass the path-traversal allowlist;
# "__system__" (leading underscore) is rejected by _safe_workspace_dir.
WS_SYS = "system0"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="test", reason="migration test")


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the store factory at tmp_path; reset caches + env between tests."""
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    stores.reset_store_caches()
    token = stores.current_workspace.set(None)
    try:
        yield
    finally:
        try:
            stores.current_workspace.reset(token)
        except ValueError:
            stores.current_workspace.set(None)
        stores.reset_store_caches()


async def _seed_shared_fabric(data_dir: Path) -> dict[str, Any]:
    """Create the shared fabric.db and seed rows across ws-a, ws-b, and NULL workspace.

    Returns the row counts so tests can assert total-preservation.
    """
    shared_path = data_dir / "fabric.db"
    store = FabricStore(shared_path)

    # Define a type (global / NULL workspace)
    obj_type = await store.define_type(
        "TestWidget",
        properties=[],
        workspace_id=None,
    )

    # ws-a objects
    await store.create_object(obj_type.id, {"name": "a1"}, workspace_id=WS_A)
    await store.create_object(obj_type.id, {"name": "a2"}, workspace_id=WS_A)
    # ws-b objects
    await store.create_object(obj_type.id, {"name": "b1"}, workspace_id=WS_B)
    # NULL-workspace object (pre-tenancy)
    await store.create_object(obj_type.id, {"name": "null1"}, workspace_id=None)

    # A link between ws-a objects
    result_a = await store.query(
        __import__("pocketpaw.fabric.models", fromlist=["FabricQuery"]).FabricQuery(
            type_name="TestWidget"
        ),
        workspace_id=WS_A,
    )
    if len(result_a.objects) >= 2:
        await store.link(
            result_a.objects[0].id,
            result_a.objects[1].id,
            "related",
            workspace_id=WS_A,
        )

    return {
        "objects": {"wsa": 2, "wsb": 1, "null": 1},
        "type_id": obj_type.id,
    }


async def _seed_shared_instinct(data_dir: Path) -> dict[str, Any]:
    """Create the shared instinct.db and seed actions + audit rows across ws-a, ws-b, NULL.

    Returns metadata (action ids, audit row expectations).
    """
    shared_path = data_dir / "instinct.db"
    store = InstinctStore(shared_path)
    trigger = _make_trigger()

    action_ids: dict[str, list[str]] = {WS_A: [], WS_B: [], WS_SYS: []}

    # ws-a: 2 actions, approve them (2 audit rows each → 4 hashed rows)
    for i in range(2):
        act = await store.propose(
            pocket_id="pocket-a",
            title=f"A-action-{i}",
            description="",
            recommendation="",
            trigger=trigger,
            workspace_id=WS_A,
        )
        await store.approve(act.id, approver="user")
        action_ids[WS_A].append(act.id)

    # ws-b: 2 actions, approve them
    for i in range(2):
        act = await store.propose(
            pocket_id="pocket-b",
            title=f"B-action-{i}",
            description="",
            recommendation="",
            trigger=trigger,
            workspace_id=WS_B,
        )
        await store.approve(act.id, approver="user")
        action_ids[WS_B].append(act.id)

    # NULL-workspace: 1 action (pre-tenancy)
    null_act = await store.propose(
        pocket_id="pocket-sys",
        title="Sys-action",
        description="",
        recommendation="",
        trigger=trigger,
        workspace_id=None,
    )
    action_ids[WS_SYS].append(null_act.id)

    return {"action_ids": action_ids}


# ---------------------------------------------------------------------------
# Core migration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_creates_per_workspace_files(tmp_path: Path) -> None:
    """After migration, per-workspace db files exist for every workspace found."""
    await _seed_shared_fabric(tmp_path)
    await _seed_shared_instinct(tmp_path)

    result = await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    assert result["skipped"] is False

    # Fabric per-workspace files
    for ws in (WS_A, WS_B, WS_SYS):
        assert (tmp_path / "workspaces" / ws / "fabric.db").exists(), f"expected fabric.db for {ws}"
        assert (tmp_path / "workspaces" / ws / "instinct.db").exists(), (
            f"expected instinct.db for {ws}"
        )


@pytest.mark.asyncio
async def test_migration_preserves_shared_files_as_migrated(tmp_path: Path) -> None:
    """Source files must NOT be destroyed — they are renamed to .migrated."""
    await _seed_shared_fabric(tmp_path)
    await _seed_shared_instinct(tmp_path)

    await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    assert (tmp_path / "fabric.db.migrated").exists(), (
        "shared fabric.db should be renamed not deleted"
    )
    assert (tmp_path / "instinct.db.migrated").exists(), (
        "shared instinct.db should be renamed not deleted"
    )
    assert not (tmp_path / "fabric.db").exists(), "original fabric.db should be gone (renamed)"
    assert not (tmp_path / "instinct.db").exists(), "original instinct.db should be gone (renamed)"


@pytest.mark.asyncio
async def test_fabric_null_rows_land_in_system_workspace(tmp_path: Path) -> None:
    """NULL-workspace fabric objects must end up in the __system__ workspace."""
    await _seed_shared_fabric(tmp_path)
    await migrate_shared_stores_to_workspaces(data_dir=tmp_path, system_workspace=WS_SYS)

    sys_store = FabricStore(tmp_path / "workspaces" / WS_SYS / "fabric.db")

    from pocketpaw.fabric.models import FabricQuery

    # Unscoped query in the __system__ file — should contain the null-workspace object
    result = await sys_store.query(FabricQuery(type_name="TestWidget"))
    names = {obj.properties.get("name") for obj in result.objects}
    assert "null1" in names, f"null-workspace object 'null1' not found in __system__; got {names}"


@pytest.mark.asyncio
async def test_fabric_workspace_isolation_after_migration(tmp_path: Path) -> None:
    """ws-a's per-workspace store must only contain ws-a objects (not ws-b's)."""
    await _seed_shared_fabric(tmp_path)
    await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    from pocketpaw.fabric.models import FabricQuery

    store_a = FabricStore(tmp_path / "workspaces" / WS_A / "fabric.db")
    result = await store_a.query(FabricQuery(type_name="TestWidget"))
    names = {obj.properties.get("name") for obj in result.objects}

    assert "a1" in names, "ws-a object 'a1' missing"
    assert "a2" in names, "ws-a object 'a2' missing"
    assert "b1" not in names, "ws-b object 'b1' must not appear in ws-a's store"
    assert "null1" not in names, "null-ws object must not appear in ws-a's store"


@pytest.mark.asyncio
async def test_fabric_row_count_preserved(tmp_path: Path) -> None:
    """Total objects across all per-workspace files must equal the shared total."""
    import aiosqlite

    await _seed_shared_fabric(tmp_path)
    shared_path = tmp_path / "fabric.db"

    # Count source rows before migration renames it.
    async with aiosqlite.connect(str(shared_path)) as db:
        async with db.execute("SELECT COUNT(*) FROM fabric_objects") as cur:
            row = await cur.fetchone()
            shared_count = row[0] if row else 0

    await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    # Sum across per-workspace files.
    total = 0
    ws_root = tmp_path / "workspaces"
    for ws_dir in ws_root.iterdir():
        ws_db = ws_dir / "fabric.db"
        if ws_db.exists():
            async with aiosqlite.connect(str(ws_db)) as db:
                async with db.execute("SELECT COUNT(*) FROM fabric_objects") as cur:
                    row = await cur.fetchone()
                    total += row[0] if row else 0

    assert total == shared_count, (
        f"Row count mismatch: shared had {shared_count} fabric_objects, "
        f"per-workspace total = {total}"
    )


# ---------------------------------------------------------------------------
# Instinct audit re-chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instinct_audit_chain_intact_per_workspace_after_migration(
    tmp_path: Path,
) -> None:
    """verify_audit_chain() must return intact=True AND hashed>0 for EACH workspace."""
    await _seed_shared_instinct(tmp_path)

    result = await migrate_shared_stores_to_workspaces(data_dir=tmp_path)
    assert result["skipped"] is False

    for ws_id in (WS_A, WS_B):
        ws_file = tmp_path / "workspaces" / ws_id / "instinct.db"
        assert ws_file.exists(), f"instinct.db for {ws_id} should exist"

        ws_store = InstinctStore(ws_file)
        verdict = await ws_store.verify_audit_chain()

        assert verdict["intact"] is True, (
            f"audit chain broken for workspace {ws_id}: {verdict['broken_at']}"
        )
        assert verdict["hashed"] > 0, (
            f"workspace {ws_id} has no hashed audit rows — re-chain did not fire"
        )
        assert verdict["checked"] == verdict["hashed"], (
            f"workspace {ws_id}: not all hashed rows verified (checked={verdict['checked']}, "
            f"hashed={verdict['hashed']})"
        )


@pytest.mark.asyncio
async def test_instinct_actions_isolated_per_workspace_after_migration(
    tmp_path: Path,
) -> None:
    """ws-a's per-workspace store must only contain ws-a actions."""
    await _seed_shared_instinct(tmp_path)
    await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    store_a = InstinctStore(tmp_path / "workspaces" / WS_A / "instinct.db")
    actions_a = await store_a.list_actions()
    titles = {a.title for a in actions_a}

    assert all(t.startswith("A-") for t in titles), f"ws-a store contains non-A actions: {titles}"
    assert not any(t.startswith("B-") for t in titles), "ws-b actions must not appear in ws-a"


@pytest.mark.asyncio
async def test_instinct_null_actions_land_in_system_workspace(tmp_path: Path) -> None:
    """NULL-workspace instinct actions must end up in __system__."""
    await _seed_shared_instinct(tmp_path)
    await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    sys_store = InstinctStore(tmp_path / "workspaces" / WS_SYS / "instinct.db")
    actions = await sys_store.list_actions()
    titles = {a.title for a in actions}

    assert "Sys-action" in titles, (
        f"null-ws action 'Sys-action' not found in __system__; got {titles}"
    )


# ---------------------------------------------------------------------------
# Idempotency — second run must be a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_is_idempotent_marker_file(tmp_path: Path) -> None:
    """A second run detects the .workspace_split_done marker and skips entirely."""
    await _seed_shared_fabric(tmp_path)
    await _seed_shared_instinct(tmp_path)

    first = await migrate_shared_stores_to_workspaces(data_dir=tmp_path)
    assert first["skipped"] is False

    second = await migrate_shared_stores_to_workspaces(data_dir=tmp_path)
    assert second["skipped"] is True, "second run should be skipped (marker present)"
    assert second["fabric"] == {}, "second run fabric summary must be empty"
    assert second["instinct"] == {}, "second run instinct summary must be empty"


@pytest.mark.asyncio
async def test_migration_no_double_insert_on_second_run(tmp_path: Path) -> None:
    """Even without the marker, rows are not duplicated on a second run."""

    await _seed_shared_fabric(tmp_path)
    await _seed_shared_instinct(tmp_path)

    await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    # Remove marker to simulate a partial-failure re-run (sources already renamed).
    marker = tmp_path / ".workspace_split_done"
    marker.unlink()

    # Fabric and instinct .migrated still exist; sources are gone.
    # Second run should skip both (no source files, no marker).
    await migrate_shared_stores_to_workspaces(data_dir=tmp_path)
    # skipped=False because the marker was absent, but nothing to copy.
    # Verify the audit chain is still intact for each workspace (no duplicates).
    for ws in (WS_A, WS_B, WS_SYS):
        ws_idb = tmp_path / "workspaces" / ws / "instinct.db"
        if ws_idb.exists() and ws in (WS_A, WS_B):
            ws_store = InstinctStore(ws_idb)
            verdict = await ws_store.verify_audit_chain()
            assert verdict["intact"] is True, (
                f"chain broken after idempotent re-run for {ws}: {verdict['broken_at']}"
            )


# ---------------------------------------------------------------------------
# Marker written after migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_marker_file_created_after_migration(tmp_path: Path) -> None:
    """A .workspace_split_done marker file must exist after a successful migration."""
    await _seed_shared_fabric(tmp_path)
    await _seed_shared_instinct(tmp_path)

    await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    assert (tmp_path / ".workspace_split_done").exists()


# ---------------------------------------------------------------------------
# Source-chain integrity gate (captain hard requirement): re-chaining is only
# sound over AUTHENTIC rows, so the shared instinct.db chain is verified intact
# BEFORE re-chaining — abort on tamper unless force-overridden.
# ---------------------------------------------------------------------------


def _tamper_shared_instinct(data_dir: Path) -> None:
    """Mutate a hashed row's content in the shared instinct.db, breaking the chain."""
    import sqlite3

    with sqlite3.connect(str(data_dir / "instinct.db")) as conn:
        conn.execute(
            "UPDATE instinct_audit SET description = 'TAMPERED' WHERE rowid = ("
            "SELECT rowid FROM instinct_audit WHERE entry_hash IS NOT NULL "
            "ORDER BY rowid LIMIT 1)"
        )
        conn.commit()


@pytest.mark.asyncio
async def test_clean_source_records_intact_verdict_in_marker(tmp_path: Path) -> None:
    """A clean source migrates AND the marker attests the source chain was intact."""
    import json

    await _seed_shared_instinct(tmp_path)

    result = await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    assert result["skipped"] is False
    assert result["source_chain"] is not None
    assert result["source_chain"]["intact"] is True
    # The marker is JSON recording the source-chain attestation.
    marker_body = json.loads((tmp_path / ".workspace_split_done").read_text())
    assert marker_body["source_chain_verified"]["intact"] is True
    assert marker_body["forced"] is False
    assert "migrated_at" in marker_body


@pytest.mark.asyncio
async def test_tampered_source_aborts_and_leaves_source_intact(tmp_path: Path) -> None:
    """A broken source chain must ABORT the migration, untouched — no laundering."""
    await _seed_shared_instinct(tmp_path)
    _tamper_shared_instinct(tmp_path)

    with pytest.raises(SourceChainTamperedError):
        await migrate_shared_stores_to_workspaces(data_dir=tmp_path)

    # The source was NOT renamed/destroyed, NO per-workspace files were created,
    # and NO marker was written — the abort is clean and reversible.
    assert (tmp_path / "instinct.db").exists()
    assert not (tmp_path / "instinct.db.migrated").exists()
    assert not (tmp_path / "workspaces" / WS_A / "instinct.db").exists()
    assert not (tmp_path / ".workspace_split_done").exists()


@pytest.mark.asyncio
async def test_force_overrides_tamper_gate_and_records_it(tmp_path: Path) -> None:
    """force=True proceeds over a broken source and records the override in the marker."""
    import json

    await _seed_shared_instinct(tmp_path)
    _tamper_shared_instinct(tmp_path)

    result = await migrate_shared_stores_to_workspaces(data_dir=tmp_path, force=True)

    assert result["skipped"] is False
    assert result["source_chain"]["intact"] is False  # we knew it was broken
    # Migration proceeded: per-workspace files exist and the source is renamed.
    assert (tmp_path / "workspaces" / WS_A / "instinct.db").exists()
    assert (tmp_path / "instinct.db.migrated").exists()
    # The override is recorded in the marker (auditable).
    marker_body = json.loads((tmp_path / ".workspace_split_done").read_text())
    assert marker_body["forced"] is True
    assert marker_body["source_chain_verified"]["intact"] is False
    # Re-chained per-workspace files are themselves freshly VALID (the override's
    # documented behavior — valid chains over possibly-tampered rows).
    verdict = await InstinctStore(
        str(tmp_path / "workspaces" / WS_A / "instinct.db")
    ).verify_audit_chain()
    assert verdict["intact"] is True
