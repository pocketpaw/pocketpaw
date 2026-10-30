"""Tests for the DB-backed workspace→VM store (fix/workspace-vm-map-to-db).

Created 2026-07-15: proves the six workspace-level VM accessors in
``ee.cloud.daytona.store`` read/write the ``workspace_vms`` Mongo collection
(not the legacy local JSON file), that ``set_workspace_vm`` upserts (one row
per workspace), that config merges over defaults, that ``remove`` deletes the
row, that two workspaces are tenant-isolated, and that the one-time
``migrate_workspace_vm_map_to_db`` boot task imports the legacy JSON file into
Mongo, renames it, and is a no-op / non-clobbering on a second run.
"""

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.cloud.daytona import store
from pocketpaw_ee.cloud.daytona.store import (
    _DEFAULT_VM_CONFIG,
    get_workspace_vm_config,
    get_workspace_vm_sandbox_id,
    list_all_workspace_vms,
    remove_workspace_vm,
    set_workspace_vm,
    update_workspace_vm_config,
)
from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm
from pocketpaw_ee.cloud.shared.db import migrate_workspace_vm_map_to_db

pytestmark = pytest.mark.asyncio


async def test_set_then_get_round_trips_through_db(mongo_db):  # noqa: ARG001
    await set_workspace_vm("w1", "sb-123", "paw-ws-w1")

    # Round-trips through the accessor...
    assert await get_workspace_vm_sandbox_id("w1") == "sb-123"
    # ...and the row is really in the DB (not a file).
    doc = await WorkspaceVm.find_one(WorkspaceVm.workspace == "w1")
    assert doc is not None
    assert doc.sandbox_id == "sb-123"
    assert doc.sandbox_name == "paw-ws-w1"


async def test_get_sandbox_id_missing_returns_none(mongo_db):  # noqa: ARG001
    assert await get_workspace_vm_sandbox_id("nope") is None


async def test_set_is_an_upsert_not_a_duplicate(mongo_db):  # noqa: ARG001
    await set_workspace_vm("w1", "sb-1", "name-1")
    await set_workspace_vm("w1", "sb-2", "name-2")

    # Second call updated the same workspace — exactly one row.
    rows = await WorkspaceVm.find(WorkspaceVm.workspace == "w1").to_list()
    assert len(rows) == 1
    assert rows[0].sandbox_id == "sb-2"
    assert rows[0].sandbox_name == "name-2"
    assert await get_workspace_vm_sandbox_id("w1") == "sb-2"


async def test_get_config_merges_over_defaults(mongo_db):  # noqa: ARG001
    # No row yet → pure defaults.
    assert await get_workspace_vm_config("w1") == _DEFAULT_VM_CONFIG

    # A row with a partial config override merges over the defaults.
    await set_workspace_vm("w1", "sb-1", "name-1", config={"cpu": 8})
    cfg = await get_workspace_vm_config("w1")
    assert cfg["cpu"] == 8  # overridden
    assert cfg["memory"] == _DEFAULT_VM_CONFIG["memory"]  # inherited default
    assert cfg["disk"] == _DEFAULT_VM_CONFIG["disk"]


async def test_update_config_persists_partial_update(mongo_db):  # noqa: ARG001
    await set_workspace_vm("w1", "sb-1", "name-1")
    await update_workspace_vm_config("w1", {"memory": 16})

    cfg = await get_workspace_vm_config("w1")
    assert cfg["memory"] == 16
    assert cfg["cpu"] == _DEFAULT_VM_CONFIG["cpu"]
    # The sandbox mapping is untouched by a config update.
    assert await get_workspace_vm_sandbox_id("w1") == "sb-1"


async def test_update_config_creates_row_when_absent(mongo_db):  # noqa: ARG001
    # A config update with no prior VM row seeds a config-only row.
    await update_workspace_vm_config("w1", {"disk": 50})
    cfg = await get_workspace_vm_config("w1")
    assert cfg["disk"] == 50
    assert await get_workspace_vm_sandbox_id("w1") == ""  # empty until provisioned


async def test_remove_deletes_the_row(mongo_db):  # noqa: ARG001
    await set_workspace_vm("w1", "sb-1", "name-1")
    assert await get_workspace_vm_sandbox_id("w1") == "sb-1"

    await remove_workspace_vm("w1")
    assert await get_workspace_vm_sandbox_id("w1") is None
    assert await WorkspaceVm.find_one(WorkspaceVm.workspace == "w1") is None


async def test_remove_missing_is_noop(mongo_db):  # noqa: ARG001
    # Deleting a workspace with no VM row must not raise.
    await remove_workspace_vm("ghost")


async def test_two_workspaces_are_tenant_isolated(mongo_db):  # noqa: ARG001
    await set_workspace_vm("w1", "sb-w1", "name-w1", config={"cpu": 4})
    await set_workspace_vm("w2", "sb-w2", "name-w2", config={"cpu": 2})

    assert await get_workspace_vm_sandbox_id("w1") == "sb-w1"
    assert await get_workspace_vm_sandbox_id("w2") == "sb-w2"
    assert (await get_workspace_vm_config("w1"))["cpu"] == 4
    assert (await get_workspace_vm_config("w2"))["cpu"] == 2

    # Removing one leaves the other intact.
    await remove_workspace_vm("w1")
    assert await get_workspace_vm_sandbox_id("w1") is None
    assert await get_workspace_vm_sandbox_id("w2") == "sb-w2"


async def test_list_all_workspace_vms(mongo_db):  # noqa: ARG001
    await set_workspace_vm("w1", "sb-w1", "name-w1")
    await set_workspace_vm("w2", "sb-w2", "name-w2")

    everything = await list_all_workspace_vms()
    assert set(everything.keys()) == {"w1", "w2"}
    assert everything["w1"]["sandbox_id"] == "sb-w1"
    assert everything["w2"]["sandbox_name"] == "name-w2"


# ---------------------------------------------------------------------------
# One-time migration from the legacy JSON file
# ---------------------------------------------------------------------------


async def test_migration_imports_json_and_renames_file(mongo_db, tmp_path, monkeypatch):  # noqa: ARG001
    legacy = tmp_path / "daytona_workspace_vm_map.json"
    legacy.write_text(
        json.dumps(
            {
                "w1": {
                    "sandbox_id": "sb-legacy-1",
                    "sandbox_name": "paw-ws-legacy-1",
                    "config": {"cpu": 8},
                }
            }
        )
    )
    monkeypatch.setattr(store, "WS_VM_MAP_PATH", legacy)

    await migrate_workspace_vm_map_to_db()

    # The row landed in the DB.
    assert await get_workspace_vm_sandbox_id("w1") == "sb-legacy-1"
    cfg = await get_workspace_vm_config("w1")
    assert cfg["cpu"] == 8
    assert cfg["memory"] == _DEFAULT_VM_CONFIG["memory"]

    # The file was renamed so it won't re-import.
    assert not legacy.exists()
    assert (tmp_path / "daytona_workspace_vm_map.json.migrated").exists()


async def test_migration_second_run_is_noop_and_non_clobbering(
    mongo_db,
    tmp_path,
    monkeypatch,  # noqa: ARG001
):
    legacy = tmp_path / "daytona_workspace_vm_map.json"
    legacy.write_text(json.dumps({"w1": {"sandbox_id": "sb-old", "sandbox_name": "old"}}))
    monkeypatch.setattr(store, "WS_VM_MAP_PATH", legacy)

    # First run imports + renames.
    await migrate_workspace_vm_map_to_db()
    assert await get_workspace_vm_sandbox_id("w1") == "sb-old"

    # Meanwhile the DB advances to a newer sandbox.
    await set_workspace_vm("w1", "sb-new", "new")

    # A second run has no file to read (already renamed) → pure no-op; even if
    # the file were re-created, an existing row is never clobbered.
    await migrate_workspace_vm_map_to_db()
    assert await get_workspace_vm_sandbox_id("w1") == "sb-new"

    # Re-create the legacy file to prove non-clobbering explicitly.
    legacy.write_text(json.dumps({"w1": {"sandbox_id": "sb-stale", "sandbox_name": "stale"}}))
    await migrate_workspace_vm_map_to_db()
    assert await get_workspace_vm_sandbox_id("w1") == "sb-new"  # not clobbered
