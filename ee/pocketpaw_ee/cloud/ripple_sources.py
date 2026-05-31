"""Concrete sources for the ripple $source resolver.

Importing this module registers every source via @register decorators.
``ripple_resolver`` itself stays free of cloud-domain imports — sources
live here, next to the entities they read.

Tenancy rule (from CLAUDE.md ee/cloud rule 7): every Mongo read MUST
scope by ctx.workspace_id.
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
