"""Persistent workspace-to-sandbox mapping store, and the workspace VM lifecycle.

TWO layers of mapping:

1. **Workspace-level VM** (PRIMARY, DB-backed).
   Maps ``workspace_id`` to a single Daytona sandbox shared by ALL projects
   in that workspace, stored in MongoDB (the ``workspace_vms`` collection,
   ``WorkspaceVm`` doc) so reads are tenant-scoped and survive across
   processes/replicas. Projects live as subdirectories under a configurable
   workspace root (default ``/workspace``). The accessors are ``async``.

2. **Per-project sandbox** (LEGACY, deprecated, migrate on touch).
   Maps ``projects/{workspace_id}/{user_id}/{project_name}/`` keys to Daytona
   sandbox IDs in a local JSON file.

``workspace_vm_lifecycle`` is the one source of a workspace VM's idle lifecycle
(stop after idle, archive soon after the stop, never delete); provisioning and
``context._ensure_lifecycle`` both apply it, so an existing VM converges on its
next resolve. The archive interval reads ``PAW_DAYTONA_WORKSPACE_VM_AUTO_ARCHIVE_MINUTES``.

store.py owns the ``workspace_vms`` collection: the ``WorkspaceVm`` doc class is
imported ONLY inside this module (ee/cloud Rule 2 spirit: store.py is a
persistence helper, not a full 4-file entity).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache + disk paths (LEGACY per-project map only)
# ---------------------------------------------------------------------------

_workspace_map: dict[str, dict] = {}
_MAP_PATH = Path.home() / ".pocketpaw" / "daytona_workspace_map.json"

# Legacy JSON path for the workspace-level VM map. No longer read/written by the
# accessors (they are DB-backed now); retained only so the one-time
# ``migrate_workspace_vm_map_to_db`` boot task in ``ee.cloud.shared.db`` can
# import + import the captain's existing on-disk entries into Mongo.
WS_VM_MAP_PATH = Path.home() / ".pocketpaw" / "daytona_workspace_vm_map.json"

# Default VM configuration
# TODO(bug): root_dir should be /home/daytona. Changing it alters legacy VM layout.
# ``auto_stop_interval`` is SECONDS here (the API's documented unit); it is converted
# to Daytona's MINUTES by :func:`workspace_vm_lifecycle`, never passed through raw.
_DEFAULT_VM_CONFIG: dict = {
    "cpu": 2,
    "memory": 4,  # GB
    "disk": 10,  # GB
    "root_dir": "/workspace",
    "auto_stop_interval": 1800,  # seconds — 30 min idle
}

# Workspace VM lifecycle, in Daytona's MINUTES. The VM is persistent: it holds the
# workspace's project files and ``resolve_daytona_context`` restarts it when it is
# stopped or archived. So an idle VM STOPS (frees its cpu/memory from the org's
# running total, which site builds also draw from), is ARCHIVED shortly after (frees
# its disk: our VMs are Daytona CONTAINER sandboxes, and a stopped container keeps its
# full disk reservation on the org quota until archived; files survive in object
# storage, the next start restores them and takes longer), and is NEVER auto-deleted.
WORKSPACE_VM_DEFAULT_AUTO_STOP_MINUTES = 30
#: Ceiling on a configured auto-stop: a VM idling longer than a day holds 4 GiB of
#: the org's memory limit for nobody.
WORKSPACE_VM_MAX_AUTO_STOP_MINUTES = 24 * 60
WORKSPACE_VM_MIN_AUTO_STOP_MINUTES = 5
#: Minutes a STOPPED VM keeps its 10 GiB disk on the org quota before Daytona archives
#: it. 15 lets a user back from a short break resume a stopped VM (fast) rather than
#: restore an archived one (slow), and the VM has already sat idle for its auto-stop
#: window before this clock starts. The env override is clamped to 1..60: 1 is
#: Daytona's smallest interval (0 means its MAXIMUM, not "now") and the ops setting for
#: a disk crunch; past an hour a stopped VM is just holding disk.
WORKSPACE_VM_DEFAULT_AUTO_ARCHIVE_MINUTES = 15
WORKSPACE_VM_MIN_AUTO_ARCHIVE_MINUTES = 1
WORKSPACE_VM_MAX_AUTO_ARCHIVE_MINUTES = 60
WORKSPACE_VM_AUTO_ARCHIVE_ENV = "PAW_DAYTONA_WORKSPACE_VM_AUTO_ARCHIVE_MINUTES"
WORKSPACE_VM_AUTO_DELETE_MINUTES = -1  # never


def workspace_vm_archive_minutes() -> int:
    """Auto-archive interval for workspace VMs: the env override clamped to 1..60, or
    the default when unset or not an integer. Read per call (no code deploy to change)."""
    raw = os.environ.get(WORKSPACE_VM_AUTO_ARCHIVE_ENV, "").strip()
    if not raw:
        return WORKSPACE_VM_DEFAULT_AUTO_ARCHIVE_MINUTES
    try:
        minutes = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a number of minutes; using %d",
            WORKSPACE_VM_AUTO_ARCHIVE_ENV,
            raw,
            WORKSPACE_VM_DEFAULT_AUTO_ARCHIVE_MINUTES,
        )
        return WORKSPACE_VM_DEFAULT_AUTO_ARCHIVE_MINUTES
    clamped = max(
        WORKSPACE_VM_MIN_AUTO_ARCHIVE_MINUTES, min(minutes, WORKSPACE_VM_MAX_AUTO_ARCHIVE_MINUTES)
    )
    if clamped != minutes:
        logger.warning(
            "%s=%d is outside %d..%d minutes; using %d",
            WORKSPACE_VM_AUTO_ARCHIVE_ENV,
            minutes,
            WORKSPACE_VM_MIN_AUTO_ARCHIVE_MINUTES,
            WORKSPACE_VM_MAX_AUTO_ARCHIVE_MINUTES,
            clamped,
        )
    return clamped


def workspace_vm_lifecycle(config: dict | None) -> dict[str, int]:
    """``create_sandbox`` lifecycle kwargs (MINUTES) for a workspace VM.

    ``config["auto_stop_interval"]`` is read as SECONDS (the unit the config API has
    always documented) and clamped to [5 min, 1 day]. Before this, the raw number
    reached Daytona as minutes, so the 3600 default kept an idle VM up for 60 hours."""
    raw = (config or {}).get("auto_stop_interval")
    if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
        minutes = -(-int(raw) // 60)  # ceil
    else:
        minutes = WORKSPACE_VM_DEFAULT_AUTO_STOP_MINUTES
    minutes = max(
        WORKSPACE_VM_MIN_AUTO_STOP_MINUTES, min(minutes, WORKSPACE_VM_MAX_AUTO_STOP_MINUTES)
    )
    return {
        "auto_stop_interval": minutes,
        "auto_archive_interval": workspace_vm_archive_minutes(),
        "auto_delete_interval": WORKSPACE_VM_AUTO_DELETE_MINUTES,
    }


# ---------------------------------------------------------------------------
# Per-project accessors (LEGACY — DEPRECATED, still JSON-file-backed)
#
# These map ``projects/{workspace_id}/{user_id}/{project_name}/`` keys to
# sandbox IDs in ``daytona_workspace_map.json``. Superseded by the DB-backed
# workspace-level VM map below; kept only for backward compat. Migrate to the
# workspace-level VM (or a DB collection of their own) on next touch.
# ---------------------------------------------------------------------------


def load_workspace_map() -> dict[str, dict]:
    """Load the per-project workspace mapping from disk, caching in memory."""
    if _workspace_map:
        return _workspace_map
    try:
        if _MAP_PATH.exists():
            with open(_MAP_PATH) as f:
                data = json.load(f)
                _workspace_map.update(data)
    except Exception as exc:
        logger.warning("Failed to load Daytona workspace map: %s", exc)
    return _workspace_map


def save_workspace_map() -> None:
    """Persist the per-project workspace mapping to disk."""
    try:
        _MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MAP_PATH, "w") as f:
            json.dump(_workspace_map, f, indent=2)
    except Exception as exc:
        logger.warning("Failed to save Daytona workspace map: %s", exc)


def get_sandbox_id(project_key: str) -> str | None:
    """Get the sandbox ID for a project key, or None.  LEGACY."""
    mapping = load_workspace_map()
    entry = mapping.get(project_key)
    return entry.get("sandbox_id") if entry else None


def set_sandbox_id(project_key: str, sandbox_id: str, sandbox_name: str) -> None:
    """Record the sandbox ID for a project key.  LEGACY."""
    mapping = load_workspace_map()
    existing = mapping.get(project_key, {})
    existing.update({"sandbox_id": sandbox_id, "sandbox_name": sandbox_name})
    mapping[project_key] = existing
    save_workspace_map()


def remove_sandbox_id(project_key: str) -> None:
    """Remove the sandbox mapping for a project key.  LEGACY."""
    mapping = load_workspace_map()
    mapping.pop(project_key, None)
    save_workspace_map()


def update_sync_timestamp(project_key: str) -> None:
    """Update the last_synced_at field for a project key.  LEGACY."""
    from datetime import UTC, datetime

    mapping = load_workspace_map()
    entry = mapping.get(project_key)
    if entry:
        entry["last_synced_at"] = datetime.now(UTC).isoformat()
        save_workspace_map()


def list_project_keys_for_workspace(workspace_id: str) -> list[str]:
    """Return all project keys for a given workspace that have sandboxes.  LEGACY."""
    mapping = load_workspace_map()
    prefix = f"projects/{workspace_id}/"
    return [k for k in mapping if k.startswith(prefix) and mapping[k].get("sandbox_id")]


def list_all_mappings() -> dict[str, dict]:
    """Return the full per-project workspace map (read-only snapshot).  LEGACY."""
    return dict(load_workspace_map())


# ---------------------------------------------------------------------------
# Workspace-level VM accessors (PRIMARY — DB-backed, ``workspace_vms``)
#
# Each accessor is ``async`` and tenant-scoped by ``workspace``. store.py is the
# sole importer of the ``WorkspaceVm`` doc class (imported locally inside each
# function so a plain ``import ee.cloud.daytona.store`` never drags Beanie in).
# ---------------------------------------------------------------------------


async def get_workspace_vm_sandbox_id(workspace_id: str) -> str | None:
    """Get the sandbox ID for a workspace, or None."""
    from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

    doc = await WorkspaceVm.find_one(WorkspaceVm.workspace == workspace_id)
    return doc.sandbox_id if doc else None


async def set_workspace_vm(
    workspace_id: str,
    sandbox_id: str,
    sandbox_name: str,
    config: dict | None = None,
) -> None:
    """Upsert the workspace VM sandbox mapping with optional config override."""
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

    doc = await WorkspaceVm.find_one(WorkspaceVm.workspace == workspace_id)
    if doc is None:
        # Insert. Merge config over defaults when given, else seed defaults.
        merged = {**_DEFAULT_VM_CONFIG, **config} if config else dict(_DEFAULT_VM_CONFIG)
        doc = WorkspaceVm(
            workspace=workspace_id,
            sandbox_id=sandbox_id,
            sandbox_name=sandbox_name,
            config=merged,
        )
        await doc.insert()
        return

    # Update in place.
    doc.sandbox_id = sandbox_id
    doc.sandbox_name = sandbox_name
    if config:
        doc.config = {**_DEFAULT_VM_CONFIG, **config}
    elif not doc.config:
        doc.config = dict(_DEFAULT_VM_CONFIG)
    doc.updated_at = datetime.now(UTC)
    await doc.save()


async def remove_workspace_vm(workspace_id: str) -> None:
    """Remove the workspace VM mapping (deletes the row)."""
    from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

    doc = await WorkspaceVm.find_one(WorkspaceVm.workspace == workspace_id)
    if doc is not None:
        await doc.delete()


async def get_workspace_vm_config(workspace_id: str) -> dict:
    """Return the VM config for a workspace, merged over defaults."""
    from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

    doc = await WorkspaceVm.find_one(WorkspaceVm.workspace == workspace_id)
    stored = doc.config if doc else {}
    return {**_DEFAULT_VM_CONFIG, **(stored or {})}


async def update_workspace_vm_config(workspace_id: str, config_updates: dict) -> None:
    """Merge *config_updates* into the workspace VM config and persist.

    Creates the row if the workspace has no VM record yet (config-only row —
    ``sandbox_id``/``sandbox_name`` are empty until a VM is provisioned).
    """
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

    doc = await WorkspaceVm.find_one(WorkspaceVm.workspace == workspace_id)
    if doc is None:
        merged = {**_DEFAULT_VM_CONFIG, **config_updates}
        doc = WorkspaceVm(
            workspace=workspace_id,
            sandbox_id="",
            sandbox_name="",
            config=merged,
        )
        await doc.insert()
        return

    current_config = {**_DEFAULT_VM_CONFIG, **(doc.config or {})}
    current_config.update(config_updates)
    doc.config = current_config
    doc.updated_at = datetime.now(UTC)
    await doc.save()


async def list_all_workspace_vms() -> dict[str, dict]:
    """Return the full workspace VM map (``workspace_id`` -> entry dict).

    Global read across tenants — the map is a system-level registry consumed by
    ops/admin surfaces, not a per-tenant read path.
    """
    from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

    # global-read: system-level VM registry (ops/admin), not a tenant read path.
    docs = await WorkspaceVm.find_all().to_list()
    return {
        doc.workspace: {
            "sandbox_id": doc.sandbox_id,
            "sandbox_name": doc.sandbox_name,
            "config": dict(doc.config or {}),
        }
        for doc in docs
    }


__all__ = [
    # Legacy per-project
    "load_workspace_map",
    "save_workspace_map",
    "get_sandbox_id",
    "set_sandbox_id",
    "remove_sandbox_id",
    "update_sync_timestamp",
    "list_project_keys_for_workspace",
    "list_all_mappings",
    # Workspace-level VM (NEW)
    "get_workspace_vm_sandbox_id",
    "set_workspace_vm",
    "remove_workspace_vm",
    "get_workspace_vm_config",
    "update_workspace_vm_config",
    "list_all_workspace_vms",
    "_DEFAULT_VM_CONFIG",
]
