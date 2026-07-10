"""Persistent workspace-to-sandbox mapping store.

TWO layers of mapping:

1. **Workspace-level VM** (NEW — primary).
   Maps ``workspace_id`` → a single Daytona sandbox shared by ALL projects
   in that workspace.  Projects live as subdirectories under a configurable
   workspace root (default ``/workspace``).

2. **Per-project sandbox** (LEGACY — kept for backward compat).
   Maps ``projects/{workspace_id}/{user_id}/{project_name}/`` keys to
   Daytona sandbox IDs.  Deprecated in favour of the workspace-level VM.

Both maps are persisted under ``~/.pocketpaw/`` so the mapping survives
process restarts.

Moved from inline helpers in router.py: 2026-07-01
Updated: 2026-07-10 — added workspace-level VM mapping.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache + disk paths
# ---------------------------------------------------------------------------

_workspace_map: dict[str, dict] = {}
_MAP_PATH = Path.home() / ".pocketpaw" / "daytona_workspace_map.json"

# Workspace-level VM map
_WORKSPACE_VM_MAP: dict[str, dict] = {}
_WS_VM_MAP_PATH = Path.home() / ".pocketpaw" / "daytona_workspace_vm_map.json"

# Default VM configuration
_DEFAULT_VM_CONFIG: dict = {
    "cpu": 2,
    "memory": 4,  # GB
    "disk": 10,  # GB
    "root_dir": "/workspace",
    "auto_stop_interval": 3600,
}

# ---------------------------------------------------------------------------
# Per-project accessors (LEGACY — keep for backward compat)
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
# Workspace-level VM accessors (NEW — primary path)
# ---------------------------------------------------------------------------


def _load_ws_vm_map() -> dict[str, dict]:
    """Load the workspace VM map from disk, caching in memory."""
    global _WORKSPACE_VM_MAP
    if _WORKSPACE_VM_MAP:
        return _WORKSPACE_VM_MAP
    try:
        if _WS_VM_MAP_PATH.exists():
            with open(_WS_VM_MAP_PATH) as f:
                _WORKSPACE_VM_MAP = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load workspace VM map: %s", exc)
    return _WORKSPACE_VM_MAP


def _save_ws_vm_map() -> None:
    """Persist the workspace VM map to disk."""
    global _WORKSPACE_VM_MAP
    try:
        _WS_VM_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_WS_VM_MAP_PATH, "w") as f:
            json.dump(_WORKSPACE_VM_MAP, f, indent=2)
    except Exception as exc:
        logger.warning("Failed to save workspace VM map: %s", exc)


def get_workspace_vm_sandbox_id(workspace_id: str) -> str | None:
    """Get the sandbox ID for a workspace, or None."""
    mapping = _load_ws_vm_map()
    entry = mapping.get(workspace_id)
    return entry.get("sandbox_id") if entry else None


def set_workspace_vm(
    workspace_id: str,
    sandbox_id: str,
    sandbox_name: str,
    config: dict | None = None,
) -> None:
    """Record the workspace VM sandbox mapping with optional config override."""
    mapping = _load_ws_vm_map()
    existing = mapping.get(workspace_id, {})
    existing.update(
        {
            "sandbox_id": sandbox_id,
            "sandbox_name": sandbox_name,
        }
    )
    if config:
        existing["config"] = {**_DEFAULT_VM_CONFIG, **config}
    elif "config" not in existing:
        existing["config"] = dict(_DEFAULT_VM_CONFIG)
    mapping[workspace_id] = existing
    _save_ws_vm_map()


def remove_workspace_vm(workspace_id: str) -> None:
    """Remove the workspace VM mapping."""
    mapping = _load_ws_vm_map()
    mapping.pop(workspace_id, None)
    _save_ws_vm_map()


def get_workspace_vm_config(workspace_id: str) -> dict:
    """Return the VM config for a workspace, or defaults if not set."""
    mapping = _load_ws_vm_map()
    entry = mapping.get(workspace_id, {})
    return {**_DEFAULT_VM_CONFIG, **entry.get("config", {})}


def update_workspace_vm_config(workspace_id: str, config_updates: dict) -> None:
    """Merge *config_updates* into the workspace VM config and persist."""
    mapping = _load_ws_vm_map()
    entry = mapping.get(workspace_id, {})
    current_config = {**_DEFAULT_VM_CONFIG, **entry.get("config", {})}
    current_config.update(config_updates)
    entry["config"] = current_config
    mapping[workspace_id] = entry
    _save_ws_vm_map()


def list_all_workspace_vms() -> dict[str, dict]:
    """Return the full workspace VM map (read-only snapshot)."""
    return dict(_load_ws_vm_map())


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
