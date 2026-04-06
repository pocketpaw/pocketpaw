"""PawKits REST API endpoints.

Provides CRUD operations for kit management and data resolution
for command center panels.

Updated: 2026-03-09 — Added auth scope dependency for access control.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from pocketpaw.api.deps import require_scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Kits"], dependencies=[Depends(require_scope("kits"))])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class InstallKitRequest(BaseModel):
    """Request to install a kit from YAML."""

    yaml: str = Field(..., min_length=1, description="PawKit YAML configuration")
    kit_id: str | None = Field(default=None, description="Optional custom kit ID")


# ---------------------------------------------------------------------------
# Catalog endpoints (must be registered BEFORE /kits/{kit_id} to avoid
# FastAPI treating "catalog" as a kit_id path parameter)
# ---------------------------------------------------------------------------


@router.get("/kits/catalog")
async def list_catalog() -> dict[str, Any]:
    """Return all kits available in the catalog, with installed status."""
    from pocketpaw.kits.catalog import get_all_catalog_kits
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    installed_kits = await store.list_kits()
    installed_ids = {k.id for k in installed_kits}

    catalog = get_all_catalog_kits()
    entries = []
    for entry in catalog:
        entries.append(
            {
                "id": entry.id,
                "name": entry.name,
                "description": entry.description,
                "icon": entry.icon,
                "category": entry.category,
                "author": entry.author,
                "tags": entry.tags,
                "preview": entry.preview,
                "installed": entry.id in installed_ids,
            }
        )

    return {"catalog": entries, "count": len(entries)}


@router.post("/kits/catalog/{kit_id}/install")
async def install_catalog_kit(kit_id: str) -> dict[str, Any]:
    """Install a kit from the catalog by ID. Auto-activates after install."""
    from pocketpaw.kits.catalog import get_builtin_yaml, get_catalog_kit
    from pocketpaw.kits.store import get_kit_store

    entry = get_catalog_kit(kit_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Catalog kit '{kit_id}' not found")

    store = get_kit_store()
    existing = await store.get_kit(kit_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Kit '{kit_id}' is already installed")

    yaml_str = get_builtin_yaml(kit_id)
    if not yaml_str:
        raise HTTPException(status_code=404, detail=f"Built-in YAML for '{kit_id}' not found")

    kit = await store.install_kit(yaml_str, kit_id=kit_id)
    await store.activate_kit(kit_id)

    return {"id": kit.id, "kit": kit.model_dump(), "activated": True}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/kits")
async def list_kits() -> dict[str, Any]:
    """List all installed kits."""
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    kits = await store.list_kits()
    return {
        "kits": [k.model_dump() for k in kits],
        "count": len(kits),
    }


@router.get("/kits/{kit_id}")
async def get_kit(kit_id: str) -> dict[str, Any]:
    """Get an installed kit by ID."""
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    kit = await store.get_kit(kit_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Kit not found")
    return {"kit": kit.model_dump()}


@router.post("/kits/install")
async def install_kit(request: InstallKitRequest) -> dict[str, Any]:
    """Install a kit from a YAML string."""
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    try:
        kit = await store.install_kit(request.yaml, kit_id=request.kit_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid kit configuration: {e}")
    return {"id": kit.id, "kit": kit.model_dump()}


@router.delete("/kits/{kit_id}")
async def remove_kit(kit_id: str) -> dict[str, Any]:
    """Uninstall a kit."""
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    removed = await store.remove_kit(kit_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Kit not found")
    return {"success": True, "message": f"Kit {kit_id} removed"}


@router.post("/kits/{kit_id}/activate")
async def activate_kit(kit_id: str) -> dict[str, Any]:
    """Set a kit as the active command center."""
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    activated = await store.activate_kit(kit_id)
    if not activated:
        raise HTTPException(status_code=404, detail="Kit not found")
    return {"success": True, "message": f"Kit {kit_id} activated"}


@router.get("/kits/{kit_id}/data")
async def get_kit_data(kit_id: str) -> dict[str, Any]:
    """Get all panel data for a kit, resolving api:* sources."""
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    kit = await store.get_kit(kit_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Kit not found")

    data = await _resolve_all_sources(kit_id)
    return {"data": data}


@router.get("/kits/{kit_id}/data/{source:path}")
async def get_kit_data_source(kit_id: str, source: str) -> dict[str, Any]:
    """Get data for a specific panel source."""
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    kit = await store.get_kit(kit_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Kit not found")

    data = await _resolve_source(source)
    return {"source": source, "data": data}


# ---------------------------------------------------------------------------
# Data resolution helpers
# ---------------------------------------------------------------------------

# Maps api:<key> sources to Mission Control store calls.
_API_SOURCE_RESOLVERS: dict[str, str] = {
    "api:stats": "stats",
    "api:tasks": "tasks",
    "api:activities": "activities",
    "api:documents": "documents",
    "api:agents": "agents",
    "api:standup": "standup",
}


async def _resolve_source(source: str) -> Any:
    """Resolve a single data source."""
    if not source.startswith("api:"):
        return None

    try:
        from pocketpaw.mission_control.store import get_mission_control_store

        mc = get_mission_control_store()

        key = source.split(":", 1)[1] if ":" in source else ""

        if key == "stats":
            return await mc.get_stats()

        if key == "tasks":
            tasks = await mc.list_tasks(limit=0)
            grouped: dict[str, list[dict]] = {}
            for t in tasks:
                status_key = t.status.value
                grouped.setdefault(status_key, []).append(t.to_dict())
            return grouped

        if key == "activities":
            activities = await mc.get_activity_feed(limit=50)
            return [a.to_dict() for a in activities]

        if key == "documents":
            docs = await mc.list_documents()
            return [d.to_dict() for d in docs]

        if key == "agents":
            agents = await mc.list_agents()
            return [a.to_dict() for a in agents]

        if key == "standup":
            from pocketpaw.mission_control.manager import get_mission_control_manager

            manager = get_mission_control_manager()
            return await manager.generate_standup()

    except Exception:
        logger.warning("Failed to resolve source %s", source, exc_info=True)

    return None


async def _resolve_all_sources(kit_id: str) -> dict[str, Any]:
    """Resolve all known api:* sources for a kit."""
    result: dict[str, Any] = {}
    for source_key in _API_SOURCE_RESOLVERS:
        result[source_key] = await _resolve_source(source_key)

    # Also include any persisted workflow data
    from pocketpaw.kits.store import get_kit_store

    store = get_kit_store()
    persisted = await store.get_kit_data(kit_id)
    result.update(persisted)

    return result
