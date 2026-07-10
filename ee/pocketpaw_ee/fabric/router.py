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
# Updated: 2026-07-10 (ontology-operator-ux) — makes the ontology operable by a
#   non-engineer. Three additions:
#     1. A new ADMIN-gated schema-authoring surface under ``/fabric/schema`` —
#        ``POST /fabric/schema/types`` (create a typed object type),
#        ``POST /fabric/schema/types/{id}/properties`` (add a property, additive),
#        ``PATCH /fabric/schema/types/{id}`` (version + rename/additive migrate),
#        ``POST /fabric/schema/link-types`` (declare a link type), and
#        ``GET /fabric/schema`` (list object types + their properties + link types
#        for the UI). The write routes are gated on the new ``fabric.admin`` action
#        (ADMIN); the list is ``fabric.read`` so any member can render the browser.
#        The schema WRITES call ``WorkspaceFabricStore.register_*`` directly IN the
#        handler — the injected FabricRegistry Protocol is read-only, so registry
#        authoring must run here, never through it.
#     2. Write-time LINK enforcement in ``create_link``: when the workspace has
#        declared any link types, a new link must name a declared type AND match
#        its declared (from_type -> to_type) endpoints, else 422. No declarations
#        -> no enforcement (backward compatible).
#     3. ``create_object`` now surfaces the OSS store's ``FabricTypeError`` (a
#        declared-property type clash) as a 422 instead of a 500.

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
    FabricTypeError,
    ObjectType,
    PropertyDef,
)
from pocketpaw.fabric.store import FabricStore
from pocketpaw.stores import get_fabric_store
from pocketpaw_ee.cloud._core.deps import current_workspace_id, require_plan_feature
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import require_action_any_workspace
from pocketpaw_ee.fabric.storage import WorkspaceFabricStore, get_registry_store

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


# --- Schema-authoring surface (ontology-operator-ux) ---


class SchemaTypeRequest(BaseModel):
    """Create an object type with typed properties (the operator 'create a type')."""

    name: str
    properties: list[PropertyDef] = []
    description: str = ""
    icon: str = "box"
    color: str = "#0A84FF"


class AddPropertyRequest(BaseModel):
    """Add one property to an existing object type (additive schema change)."""

    property: PropertyDef


class UpdateTypeRequest(BaseModel):
    """Version + non-destructively migrate a type (rename and/or additive).

    ``renames`` maps ``old_property_name -> new_property_name`` (the key is moved
    on every existing object). ``properties`` replaces the declared schema (a
    property dropped from it is deferred — its orphaned key is left on existing
    objects, never scrubbed). All fields optional; an empty body still bumps the
    version.
    """

    properties: list[PropertyDef] | None = None
    renames: dict[str, str] | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None


class LinkTypeRequest(BaseModel):
    """Declare a directed link type between two object types."""

    name: str
    from_type: str
    to_type: str


class LinkTypeDef(BaseModel):
    """A declared link type as rendered in the schema list."""

    name: str
    from_type: str
    to_type: str


class SchemaResponse(BaseModel):
    """The current ontology schema: object types (with properties) + link types."""

    object_types: list[ObjectType]
    link_types: list[LinkTypeDef]


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
    """Create an object stamped with the caller's active workspace (W4a).

    Write-time type enforcement (ontology-operator-ux): the OSS store validates
    the property bag against the type's declared schema and raises
    ``FabricTypeError`` on a clash. Surface that as a 422 (a well-formed request
    that fails a field rule) rather than letting it bubble to a 500.
    """
    try:
        return await _store(workspace_id).create_object(
            type_id=req.type_id,
            properties=req.properties,
            source_connector=req.source_connector,
            source_id=req.source_id,
            workspace_id=workspace_id,
        )
    except FabricTypeError as exc:
        raise ValidationError("fabric.property_type_mismatch", str(exc)) from exc


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
    """Create a link stamped with the caller's active workspace (W4a).

    Write-time LINK enforcement (ontology-operator-ux): if the workspace has
    declared any link types, the new link must name a declared type AND match its
    declared ``from_type -> to_type`` endpoints, else 422. A workspace with no
    declared link types is unaffected (backward compatible).
    """
    store = _store(workspace_id)
    await _enforce_link_type(store, workspace_id, req)
    return await store.link(
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


# ---------------------------------------------------------------------------
# Schema-authoring surface (ontology-operator-ux)
#
# ADMIN-gated (``fabric.admin``) writes that let a non-engineer author the
# ontology: create a typed object type, add a property, version + migrate a
# type, and declare a link type. The registry-write methods
# (``register_entity_type`` / ``register_property`` / ``register_link``) live on
# the concrete ``WorkspaceFabricStore`` and are called DIRECTLY here — the
# injected ``FabricRegistry`` Protocol is read-only, so authoring must run in the
# handler, never through it. Object types + their declared properties + versions
# live in the OSS ``FabricStore`` (the source of truth for write-time property
# enforcement and versioning); link types live in the EE registry store. The
# schema list stitches both.
# ---------------------------------------------------------------------------


def _registry() -> WorkspaceFabricStore:
    """The workspace-registry store (single file under the OSS data dir)."""
    return get_registry_store()


async def _enforce_link_type(
    store: FabricStore, workspace_id: str, req: LinkRequest
) -> None:
    """Reject a link that violates the workspace's declared link schema.

    No-op when the workspace has declared no link types (backward compatible).
    Otherwise the link's ``link_type`` must be a declared name AND the from/to
    objects' TYPE NAMES must match one declared ``(from_type -> to_type)`` pair
    for that name. Endpoint types are resolved from the objects themselves
    (workspace-scoped), so a link to a cross-tenant / unknown object cannot
    satisfy a declaration.
    """
    declared = _registry().list_links(workspace_id)
    if not declared:
        return
    names = {d["name"] for d in declared}
    if req.link_type not in names:
        raise ValidationError(
            "fabric.link_type_unregistered",
            f"link type {req.link_type!r} is not defined in this workspace's ontology",
        )
    from_obj = await store.get_object(req.from_id, workspace_id=workspace_id)
    to_obj = await store.get_object(req.to_id, workspace_id=workspace_id)
    from_type = from_obj.type_name if from_obj else None
    to_type = to_obj.type_name if to_obj else None
    if not any(
        d["name"] == req.link_type and d["from_type"] == from_type and d["to_type"] == to_type
        for d in declared
    ):
        expected = [
            f"{d['from_type']} -> {d['to_type']}" for d in declared if d["name"] == req.link_type
        ]
        raise ValidationError(
            "fabric.link_type_mismatch",
            f"link {req.link_type!r} expects {expected} but got "
            f"{from_type!r} -> {to_type!r}",
        )


@router.get(
    "/fabric/schema",
    response_model=SchemaResponse,
    dependencies=[Depends(require_action_any_workspace("fabric.read"))],
)
async def get_schema(workspace_id: str = Depends(current_workspace_id)) -> SchemaResponse:
    """Render the workspace's current ontology schema for the operator UI.

    Object types (with their declared, typed properties and schema version) come
    from the OSS store; link types come from the EE registry. Both are scoped to
    the caller's workspace.
    """
    object_types = await _store(workspace_id).list_types(workspace_id=workspace_id)
    link_types = [LinkTypeDef(**d) for d in _registry().list_links(workspace_id)]
    return SchemaResponse(object_types=object_types, link_types=link_types)


@router.post(
    "/fabric/schema/types",
    response_model=ObjectType,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.admin"))],
)
async def create_schema_type(
    req: SchemaTypeRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> ObjectType:
    """Create a typed object type + register it in the workspace registry (ADMIN)."""
    obj_type = await _store(workspace_id).define_type(
        name=req.name,
        properties=req.properties,
        description=req.description,
        icon=req.icon,
        color=req.color,
        workspace_id=workspace_id,
    )
    reg = _registry()
    reg.register_entity_type(workspace_id, obj_type.name)
    for prop in req.properties:
        reg.register_property(workspace_id, obj_type.name, prop.name, prop.type)
    return obj_type


@router.post(
    "/fabric/schema/types/{type_id}/properties",
    response_model=ObjectType,
    dependencies=[Depends(require_action_any_workspace("fabric.admin"))],
)
async def add_schema_property(
    type_id: str,
    req: AddPropertyRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> ObjectType:
    """Add one property to a type (additive; bumps the type version) (ADMIN)."""
    store = _store(workspace_id)
    existing = await store.get_type(type_id, workspace_id=workspace_id)
    if existing is None:
        raise NotFound("object type", type_id)
    if any(p.name == req.property.name for p in existing.properties):
        raise ValidationError(
            "fabric.property_exists",
            f"property {req.property.name!r} is already declared on {existing.name!r}",
        )
    updated = await store.update_type(
        type_id,
        properties=[*existing.properties, req.property],
        workspace_id=workspace_id,
    )
    reg = _registry()
    reg.register_entity_type(workspace_id, existing.name)
    reg.register_property(workspace_id, existing.name, req.property.name, req.property.type)
    # update_type returned the type; None is impossible here (existing resolved).
    return updated  # type: ignore[return-value]


@router.patch(
    "/fabric/schema/types/{type_id}",
    response_model=ObjectType,
    dependencies=[Depends(require_action_any_workspace("fabric.admin"))],
)
async def update_schema_type(
    type_id: str,
    req: UpdateTypeRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> ObjectType:
    """Version + non-destructively migrate a type (rename / additive) (ADMIN)."""
    updated = await _store(workspace_id).update_type(
        type_id,
        properties=req.properties,
        renames=req.renames,
        description=req.description,
        icon=req.icon,
        color=req.color,
        workspace_id=workspace_id,
    )
    if updated is None:
        raise NotFound("object type", type_id)
    reg = _registry()
    reg.register_entity_type(workspace_id, updated.name)
    for prop in updated.properties:
        reg.register_property(workspace_id, updated.name, prop.name, prop.type)
    return updated


@router.post(
    "/fabric/schema/link-types",
    response_model=LinkTypeDef,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("fabric.admin"))],
)
async def create_link_type(
    req: LinkTypeRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> LinkTypeDef:
    """Declare a directed link type in the workspace registry (ADMIN)."""
    _registry().register_link(workspace_id, req.name, req.from_type, req.to_type)
    return LinkTypeDef(name=req.name, from_type=req.from_type, to_type=req.to_type)
