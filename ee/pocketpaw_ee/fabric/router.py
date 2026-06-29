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
# Updated: 2026-06-10 (W4a — workspace-scope fabric) — closes a cross-tenant
#   read leak. The store is GLOBAL (one shared ``~/.pocketpaw/fabric.db``), and
#   ``require_action_any_workspace`` only proves the caller holds the action in
#   SOME workspace — it does NOT bind the request to one. So on a shared
#   deployment (micro tier / an agency running multiple client tenants) a
#   workspace-A member could read and link workspace-B's objects. Every read
#   and write endpoint now takes the caller's active workspace via
#   ``current_workspace_id`` and threads it into the store as a PLAIN str: reads
#   (``list_objects`` / ``list_links`` / ``query_fabric`` / ``get_object``) are
#   scoped to that tenant; writes (``create_object`` / ``create_link``) stamp
#   it. Legacy NULL-workspace rows stay visible to all tenants (see the store
#   header).
# Updated: 2026-06-11 (fix/fabric-stats-workspace-scope) — scoped the LAST two
#   reads, ``list_types`` and ``fabric_stats``. W4a left them global on the
#   assumption that type definitions and bare counts are not tenant data; a
#   live shared box disproved it — one tenant's chat listed another context's
#   experimental type names through the unscoped stats path. Type NAMES are
#   tenant metadata. Both endpoints now thread ``current_workspace_id`` into
#   the store's scoped ``list_types()`` / ``stats()`` (own rows + legacy NULL
#   rows, matching every other scoped read; type list = defined types with at
#   least one visible object row — definitions stay global in the schema).
# Updated: 2026-06-19 (SZD-2 — workspace-scope object TYPES) — the
#   ``POST /fabric/types`` (define_type) endpoint now stamps the caller's
#   ``current_workspace_id`` onto the new type, so the discovered-type catalog
#   is private per tenant: a type defined by workspace A is invisible/unusable
#   from workspace B (``get_type_by_name`` / ``list_types`` / ``stats`` are
#   scoped on the type's own ``workspace_id`` now). This supersedes the W4a /
#   fix/fabric-stats note above that called definitions "global in the schema"
#   — they carry their own workspace column as of SZD-2.
# Updated: 2026-06-26 (ISO-1 — physical per-workspace isolation) — the store is
#   no longer a single shared ``~/.pocketpaw/fabric.db``. ``_store`` now takes
#   the caller's ``workspace_id`` and routes through
#   ``pocketpaw.stores.get_fabric_store(workspace_id=...)``, so every read/write
#   in this router hits that tenant's OWN
#   ``~/.pocketpaw/workspaces/<id>/fabric.db`` file. The per-endpoint W4a
#   ``workspace_id`` filter args are UNCHANGED — physical file isolation is
#   additive defense-in-depth, kept alongside the in-row WHERE-filter, not a
#   replacement for it.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from pocketpaw.fabric.models import (
    FabricLink,
    FabricObject,
    FabricQuery,
    FabricQueryResult,
    ObjectType,
    PropertyDef,
)
from pocketpaw.fabric.store import FabricStore
from pocketpaw.stores import get_fabric_store
from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_plan_feature
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import require_action_any_workspace

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Fabric"],
    dependencies=[Depends(require_license), Depends(require_plan_feature("fabric"))],
)


def _store(workspace_id: str) -> FabricStore:
    """Return the FabricStore for ``workspace_id`` (ISO-1 — physical isolation).

    Routes through the workspace-keyed factory so every Fabric read/write in
    this router hits the caller's OWN ``~/.pocketpaw/workspaces/<id>/fabric.db``
    file, not the shared one. ``workspace_id`` is the caller's active workspace,
    already resolved by ``current_workspace_id`` on each endpoint. The W4a
    in-row ``workspace_id`` WHERE-filter the endpoints already pass STAYS — the
    physical file split is additive defense-in-depth on top of it.
    """
    return get_fabric_store(workspace_id=workspace_id)


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
async def list_types(workspace_id: str = Depends(current_workspace_id)):
    """List object types visible to the caller's workspace.

    Scoped (fix/fabric-stats-workspace-scope): only types with at least one
    object row visible to the workspace — type names are tenant metadata on a
    shared deployment.
    """
    return await _store(workspace_id).list_types(workspace_id=workspace_id)


@router.post(
    "/fabric/types",
    response_model=ObjectType,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def define_type(
    req: DefineTypeRequest,
    workspace_id: str = Depends(current_workspace_id),
):
    """Define an object type stamped with the caller's active workspace (SZD-2)."""
    return await _store(workspace_id).define_type(
        name=req.name,
        properties=req.properties,
        description=req.description,
        icon=req.icon,
        color=req.color,
        workspace_id=workspace_id,
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
    type_name: str | None = Query(
        None, description="Filter by object type name (case-insensitive)"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    workspace_id: str = Depends(current_workspace_id),
) -> ObjectsListResponse:
    """List objects with optional type filter.

    Wraps ``FabricStore.query()`` so we inherit its parameter binding. The
    ``type_id`` / ``type_name`` filters go through ``FabricQuery``, which
    concatenates only whitelisted column names — user input flows exclusively
    through bound parameters. W4a — results are scoped to the caller's active
    workspace so a tenant never sees another tenant's objects.
    """
    q = FabricQuery(type_id=type_id, type_name=type_name, limit=limit, offset=offset)
    result = await _store(workspace_id).query(q, workspace_id=workspace_id)
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
    workspace_id: str = Depends(current_workspace_id),
) -> LinksListResponse:
    """List links between objects with optional endpoint + type filters.

    W4a — scoped to the caller's active workspace (plus legacy NULL-workspace
    links) so a tenant cannot enumerate another tenant's relationships.
    """
    links, total = await _store(workspace_id).list_links(
        from_id=from_id,
        to_id=to_id,
        link_type=link_type,
        limit=limit,
        offset=offset,
        workspace_id=workspace_id,
    )
    return LinksListResponse(links=links, total=total)


@router.post(
    "/fabric/objects",
    response_model=FabricObject,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def create_object(
    req: CreateObjectRequest,
    workspace_id: str = Depends(current_workspace_id),
):
    """Create an object stamped with the caller's active workspace (W4a)."""
    return await _store(workspace_id).create_object(
        type_id=req.type_id,
        properties=req.properties,
        source_connector=req.source_connector,
        source_id=req.source_id,
        workspace_id=workspace_id,
    )


@router.get(
    "/fabric/objects/{obj_id}",
    response_model=FabricObject,
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def get_object(
    obj_id: str,
    workspace_id: str = Depends(current_workspace_id),
):
    """Fetch one object, scoped to the caller's workspace (W4a).

    A 404 — not a 403 — is returned for another tenant's object so the
    endpoint never leaks the existence of cross-workspace ids.
    """
    obj = await _store(workspace_id).get_object(obj_id, workspace_id=workspace_id)
    if not obj:
        raise HTTPException(404, "Object not found")
    return obj


@router.post(
    "/fabric/query",
    response_model=FabricQueryResult,
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def query_fabric(
    q: FabricQuery,
    workspace_id: str = Depends(current_workspace_id),
):
    """Run an arbitrary FabricQuery, scoped to the caller's workspace (W4a)."""
    return await _store(workspace_id).query(q, workspace_id=workspace_id)


@router.post(
    "/fabric/links",
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.write"))],
)
async def create_link(
    req: LinkRequest,
    workspace_id: str = Depends(current_workspace_id),
):
    """Create a link stamped with the caller's active workspace (W4a)."""
    return await _store(workspace_id).link(
        from_id=req.from_id,
        to_id=req.to_id,
        link_type=req.link_type,
        properties=req.properties,
        workspace_id=workspace_id,
    )


@router.get(
    "/fabric/stats",
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def fabric_stats(workspace_id: str = Depends(current_workspace_id)):
    """Ontology counts scoped to the caller's workspace.

    Scoped (fix/fabric-stats-workspace-scope): counts mirror ``list_objects``
    / ``query`` visibility exactly (own rows plus legacy NULL-workspace rows),
    and ``types`` counts only types with at least one visible object row — type
    names are tenant metadata on a shared deployment, so an unscoped stats here
    leaked another tenant's experimental type names into chat.
    """
    return await _store(workspace_id).stats(workspace_id=workspace_id)
