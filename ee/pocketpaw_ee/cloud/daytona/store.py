"""Persistent workspace-to-sandbox mapping store.

Shared by the Daytona router (provision/destroy) and the context resolver
(tool routing). Maps ``projects/{workspace_id}/{user_id}/{project_name}/``
keys to active Daytona sandbox IDs.

The map is persisted to ``~/.pocketpaw/daytona_workspace_map.json`` so the
mapping survives process restarts.

Moved from inline helpers in router.py: 2026-07-01
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache + disk path
# ---------------------------------------------------------------------------

_workspace_map: dict[str, dict] = {}
_MAP_PATH = Path.home() / ".pocketpaw" / "daytona_workspace_map.json"


def load_workspace_map() -> dict[str, dict]:
    """Load the workspace mapping from disk, caching in memory."""
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
    """Persist the workspace mapping to disk."""
    try:
        _MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MAP_PATH, "w") as f:
            json.dump(_workspace_map, f, indent=2)
    except Exception as exc:
        logger.warning("Failed to save Daytona workspace map: %s", exc)


# ---------------------------------------------------------------------------
# Per-project accessors
# ---------------------------------------------------------------------------


def get_sandbox_id(project_key: str) -> str | None:
    """Get the sandbox ID for a project key, or None."""
    mapping = load_workspace_map()
    entry = mapping.get(project_key)
    return entry.get("sandbox_id") if entry else None


def set_sandbox_id(project_key: str, sandbox_id: str, sandbox_name: str) -> None:
    """Record the sandbox ID for a project key."""
    mapping = load_workspace_map()
    # Preserve extra fields (last_synced_at etc.) by merging.
    existing = mapping.get(project_key, {})
    existing.update({"sandbox_id": sandbox_id, "sandbox_name": sandbox_name})
    mapping[project_key] = existing
    save_workspace_map()


def remove_sandbox_id(project_key: str) -> None:
    """Remove the sandbox mapping for a project key."""
    mapping = load_workspace_map()
    mapping.pop(project_key, None)
    save_workspace_map()


def update_sync_timestamp(project_key: str) -> None:
    """Update the last_synced_at field for a project key."""
    from datetime import UTC, datetime

    mapping = load_workspace_map()
    entry = mapping.get(project_key)
    if entry:
        entry["last_synced_at"] = datetime.now(UTC).isoformat()
        save_workspace_map()


# ---------------------------------------------------------------------------
# Workspace-scoped helpers
# ---------------------------------------------------------------------------


def list_project_keys_for_workspace(workspace_id: str) -> list[str]:
    """Return all project keys for a given workspace that have sandboxes."""
    mapping = load_workspace_map()
    prefix = f"projects/{workspace_id}/"
    return [k for k in mapping if k.startswith(prefix) and mapping[k].get("sandbox_id")]


def list_all_mappings() -> dict[str, dict]:
    """Return the full workspace map (read-only snapshot)."""
    return dict(load_workspace_map())


__all__ = [
    "load_workspace_map",
    "save_workspace_map",
    "get_sandbox_id",
    "set_sandbox_id",
    "remove_sandbox_id",
    "update_sync_timestamp",
    "list_project_keys_for_workspace",
    "list_all_mappings",
]
