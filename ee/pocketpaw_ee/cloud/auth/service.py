"""Auth service — profile, active-workspace, avatar.

Sole owner of writes to the ``User`` Beanie document for the auth domain.
Note: fastapi-users manages registration / password / JWT lifecycle; this
service owns profile fields and active-workspace membership only.

Public API is module-level ``async def`` functions:
- ``get_profile(ctx)``
- ``update_profile(ctx, *, full_name?, avatar?, status?)``
- ``set_active_workspace(ctx, workspace_id)``
- ``set_avatar_path(ctx, avatar_path)``
"""

from __future__ import annotations

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.auth.domain import AuthUser, WorkspaceMembershipRef
from pocketpaw_ee.cloud.models.user import User as _UserDoc

# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------


def _membership_to_domain(m) -> WorkspaceMembershipRef:
    return WorkspaceMembershipRef(workspace=m.workspace, role=m.role, joined_at=m.joined_at)


def _to_domain(doc: _UserDoc) -> AuthUser:
    return AuthUser(
        id=str(doc.id),
        email=doc.email,
        full_name=doc.full_name,
        avatar=doc.avatar,
        status=doc.status,
        active_workspace=doc.active_workspace,
        workspaces=tuple(_membership_to_domain(m) for m in doc.workspaces),
        is_verified=doc.is_verified,
        is_superuser=doc.is_superuser,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_profile(ctx: RequestContext) -> AuthUser:
    doc = await _UserDoc.get(PydanticObjectId(ctx.user_id))
    if doc is None:
        raise NotFound("user", ctx.user_id)
    return _to_domain(doc)


async def update_profile(
    ctx: RequestContext,
    *,
    full_name: str | None = None,
    avatar: str | None = None,
    status: str | None = None,
) -> AuthUser:
    doc = await _UserDoc.get(PydanticObjectId(ctx.user_id))
    if doc is None:
        raise NotFound("user", ctx.user_id)
    if full_name is not None:
        doc.full_name = full_name
    if avatar is not None:
        doc.avatar = avatar
    if status is not None:
        doc.status = status
    await doc.save()
    return _to_domain(doc)


async def set_active_workspace(ctx: RequestContext, workspace_id: str) -> AuthUser:
    if not workspace_id:
        raise ValidationError("workspace_id.required", "workspace_id required")
    doc = await _UserDoc.get(PydanticObjectId(ctx.user_id))
    if doc is None:
        raise NotFound("user", ctx.user_id)
    doc.active_workspace = workspace_id
    await doc.save()
    return _to_domain(doc)


async def set_avatar_path(ctx: RequestContext, avatar_path: str) -> AuthUser:
    """Persist the avatar URL after the router writes the file to disk."""
    return await update_profile(ctx, avatar=avatar_path)


async def suggest_workspace_members(workspace_id: str, q: str, *, limit: int = 8) -> list[dict]:
    """Return up to ``limit`` workspace members matching ``q`` against
    full_name / email. Used by the chat ``/mentions/suggest`` endpoint."""
    query: dict = {"workspaces.workspace": workspace_id}
    if q:
        query["$or"] = [
            {"full_name": {"$regex": q, "$options": "i"}},
            {"email": {"$regex": q, "$options": "i"}},
        ]
    docs = await _UserDoc.find(query).limit(limit).to_list()
    return [
        {
            "type": "user",
            "id": str(u.id),
            "display_name": u.full_name or u.email,
        }
        for u in docs
    ]


__all__ = [
    "get_profile",
    "set_active_workspace",
    "set_avatar_path",
    "suggest_workspace_members",
    "update_profile",
]
