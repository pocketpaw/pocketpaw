"""Auth service — profile, active-workspace, avatar, home pocket.

Sole owner of writes to the ``User`` Beanie document for the auth domain.
Note: fastapi-users manages registration / password / JWT lifecycle; this
service owns profile fields and active-workspace membership only.

Public API is module-level ``async def`` functions:
- ``get_profile(ctx)``
- ``update_profile(ctx, *, full_name?, avatar?, status?)``
- ``set_active_workspace(ctx, workspace_id)``
- ``set_avatar_path(ctx, avatar_path)``
- ``get_home_pocket_id(user_id)`` / ``claim_home_pocket_id(user_id, id, *, expected)``

Updated: 2026-05-21 — added the ``home_pocket_id`` get + atomic-claim
pair. The pockets service owns home-pocket provisioning but stores the
resolved id here so it survives across sessions and devices; routing the
read/write through this service keeps ``models.user`` writes inside the
auth entity. ``claim_home_pocket_id`` is a compare-and-swap (not a plain
write) so a first-login provision race resolves to a single home pocket.
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


async def get_home_pocket_id(user_id: str) -> str | None:
    """Return the user's persisted ``home_pocket_id``, or None if unset.

    Takes a plain ``user_id`` (not a ``RequestContext``) so the pockets
    service can resolve the home pocket without minting a context. Raises
    ``NotFound`` when the user record is missing.
    """
    doc = await _UserDoc.get(PydanticObjectId(user_id))
    if doc is None:
        raise NotFound("user", user_id)
    return doc.home_pocket_id


async def claim_home_pocket_id(user_id: str, new_pocket_id: str, *, expected: str | None) -> bool:
    """Atomically set ``home_pocket_id`` to ``new_pocket_id`` — but only if
    it still holds ``expected``. Returns ``True`` when the swap took.

    This is a compare-and-swap, not a plain write. It closes the
    first-login provision race: two concurrent ``ensure_home_pocket`` calls
    for the same new user can both read ``home_pocket_id is None`` and both
    insert a pocket. The CAS lets exactly one of them commit its id — the
    loser sees ``False``, re-reads, and adopts the winner's pocket.

    ``expected`` is the value the caller read before provisioning:
    ``None`` for a genuine first provision (a Mongo ``{field: null}``
    filter matches both an explicit ``null`` and an absent field), or the
    stale id when re-provisioning after the previous home pocket was
    deleted. The single ``find_one_and_update`` mirrors the atomic-claim
    pattern in ``tasks/service.py``.

    Raises ``NotFound`` when the user record is missing entirely.
    """
    collection = _UserDoc.get_pymongo_collection()
    updated = await collection.find_one_and_update(
        {"_id": PydanticObjectId(user_id), "home_pocket_id": expected},
        {"$set": {"home_pocket_id": new_pocket_id}},
        return_document=True,  # mongomock + pymongo accept bool: True == AFTER
    )
    if updated is not None:
        return True
    # The CAS didn't match. Either the user is gone, or another writer
    # already moved home_pocket_id off ``expected`` — disambiguate so a
    # missing user still surfaces as NotFound.
    exists = await collection.find_one({"_id": PydanticObjectId(user_id)}, projection={"_id": 1})
    if exists is None:
        raise NotFound("user", user_id)
    return False


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
    "claim_home_pocket_id",
    "get_home_pocket_id",
    "get_profile",
    "set_active_workspace",
    "set_avatar_path",
    "suggest_workspace_members",
    "update_profile",
]
