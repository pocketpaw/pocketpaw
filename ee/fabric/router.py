# ee/fabric/router.py — FastAPI router for the Fabric ontology API.
# Created: 2026-03-28 — CRUD endpoints for object types, objects, links, queries, stats.
# Updated: 2026-04-19 (Cluster C / PR3) — Added GET /fabric/objects and
#   GET /fabric/links list endpoints so the Objects/Links sub-tabs in
#   PocketDataPanel render real data instead of the Brew & Co. mock.
# Updated: 2026-05-07 (fix/rbac-guards-fabric-instinct-agent-knowledge) — all
#   endpoints now require a valid license + workspace membership. Read endpoints
#   (GET + POST /query) require ``fabric.read`` (MEMBER). Mutation endpoints
#   (POST /types, /objects, /links) require ``fabric.write`` (MEMBER). Previously
#   the router had zero auth — any unauthenticated caller could read or modify the
#   ontology store.
# Updated: 2026-05-07 (feat/rbac-plan-feature-gate) — added router-level
#   ``require_plan_feature("fabric")`` so the entire Fabric API is gated to
#   business-tier (or higher) plans. Closes the plan-tier bypass where a
#   team-plan member who passed the workspace RBAC check still hit Fabric for
#   free.

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ee.cloud._core.deps import require_plan_feature
from ee.cloud.license import require_license
from ee.cloud.shared.deps import require_action_any_workspace
from ee.fabric.models import (
    FabricLink,
    FabricObject,
    FabricQuery,
    FabricQueryResult,
    ObjectType,
    PropertyDef,
)
from ee.fabric.store import FabricStore

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Fabric"],
    dependencies=[Depends(require_license), Depends(require_plan_feature("fabric"))],
)

_DB_PATH = Path.home() / ".pocketpaw" / "fabric.db"


def _store() -> FabricStore:
    return FabricStore(_DB_PATH)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class DefineTypeRequest(BaseModel):
    name: str
    properties: list[PropertyDef]
    description: str = ""
    icon: str = "box"
    color: str = "#0A84FF"


class CreateObjectRequest(BaseModel):
    type_id: str
    properties: dict[str, Any] = {}
    source_connector: str | None = None
    source_id: str | None = None


class LinkRequest(BaseModel):
    from_id: str
    to_id: str
    link_type: str
    properties: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/fabric/types",
    response_model=list[ObjectType],
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def list_types():
    return await _store().list_types()


@router.post(
    "/fabric/types",
    response_model=ObjectType,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def define_type(req: DefineTypeRequest):
    return await _store().define_type(
        name=req.name,
        properties=req.properties,
        description=req.description,
        icon=req.icon,
        color=req.color,
    )


class ObjectsListResponse(BaseModel):
    objects: list[FabricObject]
    total: int


class LinksListResponse(BaseModel):
    links: list[FabricLink]
    total: int


@router.get(
    "/fabric/objects",
    response_model=ObjectsListResponse,
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def list_objects(
    type_id: str | None = Query(None, description="Filter by object type id"),
    type_name: str | None = Query(None, description="Filter by object type name (case-insensitive)"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ObjectsListResponse:
    """List objects with optional type filter.

    Wraps ``FabricStore.query()`` so we inherit its parameter binding. The
    ``type_id`` / ``type_name`` filters go through ``FabricQuery``, which
    concatenates only whitelisted column names — user input flows exclusively
    through bound parameters.
    """
    q = FabricQuery(type_id=type_id, type_name=type_name, limit=limit, offset=offset)
    result = await _store().query(q)
    return ObjectsListResponse(objects=result.objects, total=result.total)


@router.get(
    "/fabric/links",
    response_model=LinksListResponse,
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def list_links(
    from_id: str | None = Query(None, description="Filter by source object id"),
    to_id: str | None = Query(None, description="Filter by destination object id"),
    link_type: str | None = Query(None, description="Filter by link type"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> LinksListResponse:
    """List links between objects with optional endpoint + type filters."""
    links, total = await _store().list_links(
        from_id=from_id,
        to_id=to_id,
        link_type=link_type,
        limit=limit,
        offset=offset,
    )
    return LinksListResponse(links=links, total=total)


@router.post(
    "/fabric/objects",
    response_model=FabricObject,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def create_object(req: CreateObjectRequest):
    return await _store().create_object(
        type_id=req.type_id,
        properties=req.properties,
        source_connector=req.source_connector,
        source_id=req.source_id,
    )


@router.get(
    "/fabric/objects/{obj_id}",
    response_model=FabricObject,
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def get_object(obj_id: str):
    obj = await _store().get_object(obj_id)
    if not obj:
        raise HTTPException(404, "Object not found")
    return obj


@router.post(
    "/fabric/query",
    response_model=FabricQueryResult,
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def query_fabric(q: FabricQuery):
    return await _store().query(q)


@router.post(
    "/fabric/links",
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def create_link(req: LinkRequest):
    return await _store().link(
        from_id=req.from_id,
        to_id=req.to_id,
        link_type=req.link_type,
        properties=req.properties,
    )


@router.get(
    "/fabric/stats",
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def fabric_stats():
    return await _store().stats()
