"""Concrete sources for the ripple $source resolver.

Importing this module registers every source via @register decorators.
``ripple_resolver`` itself stays free of cloud-domain imports — sources
live here, next to the entities they read.

Tenancy rule (from CLAUDE.md ee/cloud rule 7): every Mongo read MUST
scope by ctx.workspace_id.

Updated 2026-06-19 (SZD-1 — fabric.objects ripple source): added two
Fabric-backed sources so a rippleSpec can render discovered Fabric data, not
just pockets/members. ``fabric.objects`` resolves an ``ObjectType`` (by
``type_id`` and/or ``type_name``) into widget rows; ``fabric.query`` is the
filtered variant (same args plus a ``filters`` bag). Both are workspace-scoped
by handing ``ctx.workspace_id`` to ``FabricStore.query`` — the store applies
its own ``workspace_id = ? OR workspace_id IS NULL`` tenant guard (W4a), so a
spec in workspace A can never surface workspace B's objects. The store is the
local SQLite Fabric DB (``~/.pocketpaw/fabric.db`` via ``get_fabric_store``),
NOT Mongo, so the "every Mongo read scopes by workspace" rule is satisfied here
by the SQLite tenant scope instead — same invariant, different store.
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.ripple_resolver import ResolveCtx, register

logger = logging.getLogger(__name__)


@register("workspace.pockets")
async def _workspace_pockets(ctx: ResolveCtx, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Return id+metadata for every pocket in the workspace.
    Visibility filter mirrors pockets.service.list_pockets — owner,
    shared_with, or workspace-visible. The full rippleSpec is excluded
    (would be wasteful and recursive)."""
    if not ctx.workspace_id or not ctx.user_id:
        logger.warning(
            "ripple_resolver: workspace.pockets called with empty ctx (workspace=%r user=%r)",
            ctx.workspace_id,
            ctx.user_id,
        )
        return []
    docs = await _PocketDoc.find(
        {
            "workspace": ctx.workspace_id,
            "$or": [
                {"owner": ctx.user_id},
                {"shared_with": ctx.user_id},
                {"visibility": "workspace"},
            ],
        }
    ).to_list()
    return [
        {
            "id": str(d.id),
            "name": d.name,
            "type": d.type,
            "icon": d.icon,
            "color": d.color,
        }
        for d in docs
    ]


async def _list_workspace_members(workspace_id: str) -> list[dict[str, Any]]:
    """Return enriched member entries for a workspace.

    Indirection so tests can patch a single seam. Joins workspace member
    ids with the User collection to surface name/email/avatar/role —
    widgets like ``people-picker`` call ``.split()`` on a name, so id-only
    entries crash the renderer.

    Members with no matching User row are dropped (rare, but possible
    during async deletion).
    """
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User
    from pocketpaw_ee.cloud.workspace import service as _ws

    member_ids = await _ws.list_member_ids(workspace_id)
    if not member_ids:
        return []

    object_ids: list[PydanticObjectId] = []
    for uid in member_ids:
        try:
            object_ids.append(PydanticObjectId(uid))
        except Exception:
            logger.debug("ripple_resolver: skipping non-ObjectId user_id %r", uid)

    users = await User.find({"_id": {"$in": object_ids}}).to_list()
    by_id = {str(u.id): u for u in users}

    out: list[dict[str, Any]] = []
    for uid in member_ids:
        user = by_id.get(uid)
        if user is None:
            continue
        role = "member"
        for membership in getattr(user, "workspaces", []) or []:
            if getattr(membership, "workspace", None) == workspace_id:
                role = getattr(membership, "role", "member") or "member"
                break
        name = (user.full_name or "").strip() or (user.email or "").split("@")[0]
        out.append(
            {
                "id": uid,
                "name": name,
                "email": user.email,
                "avatar": user.avatar or "",
                "role": role,
            }
        )
    return out


@register("workspace.members")
async def _workspace_members(ctx: ResolveCtx, args: dict[str, Any]) -> list[dict[str, Any]]:
    if not ctx.workspace_id:
        logger.warning("ripple_resolver: workspace.members called with empty workspace_id")
        return []
    return await _list_workspace_members(ctx.workspace_id)


# Hard cap on rows a single Fabric source resolution returns into a spec — a
# ripple widget renders client-side, so an unbounded type with thousands of
# objects would bloat the resolved spec and the wire payload. The caller can
# request fewer via ``limit``; this is the ceiling, mirroring the fabric MCP
# tool's MAX_QUERY_LIMIT spirit.
_FABRIC_SOURCE_MAX_ROWS = 500


def _fabric_object_to_row(obj: Any) -> dict[str, Any]:
    """Project a FabricObject into a flat widget row.

    The object's JSON ``properties`` bag is spread to the top level so a
    column-oriented widget (table, kanban, list) can bind directly to property
    names, with ``id`` / ``type_id`` / ``type_name`` and provenance kept on
    reserved keys. A property literally named one of the reserved keys would be
    shadowed; that's an acceptable, documented edge — reserved keys win so the
    row always carries a stable id.
    """
    row: dict[str, Any] = dict(obj.properties or {})
    row.update(
        {
            "id": obj.id,
            "type_id": obj.type_id,
            "type_name": obj.type_name,
            "source_connector": obj.source_connector,
            "source_id": obj.source_id,
        }
    )
    return row


async def _resolve_fabric_objects(
    ctx: ResolveCtx, args: dict[str, Any], *, allow_filters: bool
) -> list[dict[str, Any]]:
    """Shared resolver for the ``fabric.objects`` / ``fabric.query`` sources.

    Builds a :class:`FabricQuery` from ``args`` and runs it scoped to
    ``ctx.workspace_id``. Workspace scoping is NOT optional here: a missing
    workspace returns ``[]`` rather than an unscoped (cross-tenant) read, so a
    spec resolved without workspace context can never leak the whole instance's
    objects. ``allow_filters`` gates the property-filter bag — ``fabric.objects``
    is the simple "all rows of a type" source, ``fabric.query`` adds filters.
    """
    if not ctx.workspace_id:
        logger.warning(
            "ripple_resolver: fabric source called with empty workspace_id (pocket=%s)",
            ctx.pocket_id,
        )
        return []

    type_id = args.get("type_id")
    type_name = args.get("type_name")
    if not type_id and not type_name:
        logger.warning(
            "ripple_resolver: fabric source needs a type_id or type_name (workspace=%s pocket=%s)",
            ctx.workspace_id,
            ctx.pocket_id,
        )
        return []
    # Reject non-string identifiers up front — FabricQuery would coerce/raise,
    # but a clean empty result keeps a malformed spec from bricking the canvas.
    if type_id is not None and not isinstance(type_id, str):
        logger.warning("ripple_resolver: fabric source type_id must be a string")
        return []
    if type_name is not None and not isinstance(type_name, str):
        logger.warning("ripple_resolver: fabric source type_name must be a string")
        return []

    raw_limit = args.get("limit", _FABRIC_SOURCE_MAX_ROWS)
    limit = raw_limit if isinstance(raw_limit, int) and raw_limit > 0 else _FABRIC_SOURCE_MAX_ROWS
    limit = min(limit, _FABRIC_SOURCE_MAX_ROWS)
    raw_offset = args.get("offset", 0)
    offset = raw_offset if isinstance(raw_offset, int) and raw_offset >= 0 else 0

    filters = args.get("filters") if allow_filters else None
    if filters is not None and not isinstance(filters, dict):
        logger.warning("ripple_resolver: fabric.query filters must be a JSON object")
        return []

    # Lazy imports keep the resolver free of a hard Fabric dependency at module
    # import time and mirror the fabric MCP tool's availability guard.
    from pocketpaw.fabric.models import FabricQuery
    from pocketpaw.stores import get_fabric_store

    store = get_fabric_store()
    q = FabricQuery(
        type_id=type_id,
        type_name=type_name,
        filters=filters or {},
        limit=limit,
        offset=offset,
    )
    # workspace_id is threaded as a PLAIN str from the server-built ctx (never
    # from the spec), so the store applies its W4a tenant scope: only this
    # workspace's rows (plus legacy NULL-workspace rows) come back.
    result = await store.query(q, workspace_id=ctx.workspace_id)
    return [_fabric_object_to_row(obj) for obj in result.objects]


@register("fabric.objects")
async def _fabric_objects(ctx: ResolveCtx, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve an ObjectType into widget rows, workspace-scoped (SZD-1).

    Args: ``type_id`` and/or ``type_name`` (at least one required), optional
    ``limit`` / ``offset``. Returns one row per object of that type in the
    caller's workspace, the JSON properties spread to the top level (see
    :func:`_fabric_object_to_row`). This is the source the "sovereign zero-setup
    discovery" starter-Pocket binds to so it renders discovered Fabric data.
    """
    return await _resolve_fabric_objects(ctx, args, allow_filters=False)


@register("fabric.query")
async def _fabric_query(ctx: ResolveCtx, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Filtered variant of ``fabric.objects`` (SZD-1).

    Same args as ``fabric.objects`` plus a ``filters`` bag (the exact
    ``FabricQuery.filters`` shape: ``{"status": "active"}`` for equality,
    ``{"rent": {">": 1000}}`` for comparison). Still workspace-scoped — the
    store binds every filter value as a parameter, so there is no injection
    surface from a spec-supplied filter.
    """
    return await _resolve_fabric_objects(ctx, args, allow_filters=True)
