"""Workspace domain — business logic service.

Sole owner of writes to the ``Workspace`` and ``Invite`` Beanie documents.
Module-level ``async def`` API. Membership operations touch the User
document (members are stored as embedded ``WorkspaceMembership`` rows on
User), so workspace-scoped User queries live here too.

Public API:
- ``create(ctx, body)``, ``get(ctx, workspace_id)``, ``update(ctx, ...)``,
  ``delete(ctx, ...)``, ``list_for_user(ctx)``
- ``list_members(ctx, workspace_id)``, ``update_member_role(...)``,
  ``remove_member(...)``
- ``list_invites(workspace_id)``, ``create_invite(...)``,
  ``validate_invite(token)``, ``accept_invite(...)``,
  ``revoke_invite(...)``
- ``list_member_ids(workspace_id)``, ``list_admin_ids(workspace_id)``,
  ``list_peer_ids(user_id)`` — used as function refs by the realtime
  audience resolver
- ``get_workspace_plan(workspace_id)`` — lightweight plan-tier lookup for
  the plan-feature gate dependency; returns "team" on any failure so the
  dep fails open on plan rather than crashing with a 500.

Changes: added get_workspace_plan helper for plan-feature gate dep.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import (
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
)
from ee.cloud._core.realtime.bus import get_resolver
from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import (
    WorkspaceDeleted,
    WorkspaceInviteAccepted,
    WorkspaceInviteCreated,
    WorkspaceInviteRevoked,
    WorkspaceMemberAdded,
    WorkspaceMemberRemoved,
    WorkspaceMemberRole,
    WorkspaceUpdated,
)
from ee.cloud.models.invite import Invite as _InviteDoc
from ee.cloud.models.notification import NotificationSource
from ee.cloud.models.user import User as _UserDoc
from ee.cloud.models.user import WorkspaceMembership as _Membership
from ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from ee.cloud.models.workspace import WorkspaceSettings
from ee.cloud.notifications import service as notifications_service
from ee.cloud.shared.events import event_bus
from ee.cloud.workspace.domain import Invite, Workspace, WorkspaceMember
from ee.cloud.workspace.dto import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)

if TYPE_CHECKING:
    from ee.cloud.models.user import User


# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------


def _workspace_to_domain(doc: _WorkspaceDoc, *, member_count: int = 0) -> Workspace:
    return Workspace(
        id=str(doc.id),
        name=doc.name,
        slug=doc.slug,
        owner=doc.owner,
        plan=doc.plan,
        seats=doc.seats,
        created_at=getattr(doc, "createdAt", None),  # type: ignore[arg-type]
        member_count=member_count,
        deleted_at=doc.deleted_at,
    )


def _invite_to_domain(doc: _InviteDoc) -> Invite:
    return Invite(
        id=str(doc.id),
        workspace_id=doc.workspace,
        email=doc.email,
        role=doc.role,
        invited_by=doc.invited_by,
        token=doc.token,
        group_id=doc.group,
        accepted=doc.accepted,
        revoked=doc.revoked,
        expired=doc.expired,
        expires_at=doc.expires_at,
    )


def legacy_ctx(user: User) -> RequestContext:
    """Build a RequestContext from a Beanie User doc — bridge for routers
    or tests that haven't migrated to ``Depends(request_context)``."""
    return RequestContext(
        user_id=str(user.id),
        workspace_id=user.active_workspace,
        request_id="legacy",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _count_members(workspace_id: str) -> int:
    return await _UserDoc.find({"workspaces.workspace": workspace_id}).count()


async def _count_members_bulk(workspace_ids: list[str]) -> dict[str, int]:
    """Aggregation: ``{workspace_id: member_count}`` in one round-trip."""
    if not workspace_ids:
        return {}
    pipeline: list[dict] = [
        {"$match": {"workspaces.workspace": {"$in": workspace_ids}}},
        {"$unwind": "$workspaces"},
        {"$match": {"workspaces.workspace": {"$in": workspace_ids}}},
        {"$group": {"_id": "$workspaces.workspace", "count": {"$sum": 1}}},
    ]
    results = await _UserDoc.aggregate(pipeline).to_list()
    return {row["_id"]: row["count"] for row in results}


async def _fetch_workspace(workspace_id: str) -> _WorkspaceDoc | None:
    """Fetch a workspace doc, treating soft-deleted as missing."""
    try:
        doc = await _WorkspaceDoc.get(PydanticObjectId(workspace_id))
    except Exception:
        return None
    if doc is None or doc.deleted_at is not None:
        return None
    return doc


async def _get_member_role(workspace_id: str, user_id: str) -> str | None:
    try:
        user = await _UserDoc.get(PydanticObjectId(user_id))
    except Exception:
        return None
    if user is None:
        return None
    for m in user.workspaces:
        if m.workspace == workspace_id:
            return m.role
    return None


async def _add_member(
    workspace_id: str,
    user_id: str,
    *,
    role: str,
    set_active: bool = False,
) -> None:
    user = await _UserDoc.get(PydanticObjectId(user_id))
    if user is None:
        raise NotFound("user", user_id)
    if any(m.workspace == workspace_id for m in user.workspaces):
        return  # idempotent
    user.workspaces.append(
        _Membership(
            workspace=workspace_id,
            role=role,
            joined_at=datetime.now(UTC),
        )
    )
    if set_active:
        user.active_workspace = workspace_id
    await user.save()


async def _find_user_id_by_email(email: str) -> str | None:
    user = await _UserDoc.find_one(_UserDoc.email == email)
    return str(user.id) if user else None


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


async def create(ctx: RequestContext, body: CreateWorkspaceRequest) -> Workspace:
    existing = await _WorkspaceDoc.find_one(
        _WorkspaceDoc.slug == body.slug,
        _WorkspaceDoc.deleted_at == None,  # noqa: E711
    )
    if existing is not None:
        raise ConflictError("workspace.slug_taken", f"Slug '{body.slug}' is already in use")

    doc = _WorkspaceDoc(name=body.name, slug=body.slug, owner=ctx.user_id)
    await doc.insert()

    await _add_member(str(doc.id), ctx.user_id, role="owner", set_active=True)

    await emit(
        WorkspaceMemberAdded(
            data={"workspace_id": str(doc.id), "user_id": ctx.user_id, "role": "owner"}
        )
    )
    get_resolver().invalidate_workspace(str(doc.id))

    return _workspace_to_domain(doc, member_count=1)


async def get(ctx: RequestContext, workspace_id: str) -> Workspace:
    role = await _get_member_role(workspace_id, ctx.user_id)
    if role is None:
        raise NotFound("workspace", workspace_id)
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        raise NotFound("workspace", workspace_id)
    count = await _count_members(workspace_id)
    return _workspace_to_domain(doc, member_count=count)


async def update(
    ctx: RequestContext,
    workspace_id: str,
    body: UpdateWorkspaceRequest,
) -> Workspace:
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        raise NotFound("workspace", workspace_id)
    if body.name is not None:
        doc.name = body.name
    if body.settings is not None:
        doc.settings = WorkspaceSettings(**body.settings)
    await doc.save()

    patched = body.model_dump(exclude_unset=True)
    await emit(WorkspaceUpdated(data={"workspace_id": workspace_id, **patched}))

    count = await _count_members(workspace_id)
    return _workspace_to_domain(doc, member_count=count)


async def delete(ctx: RequestContext, workspace_id: str) -> None:
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        raise NotFound("workspace", workspace_id)

    doc.deleted_at = datetime.now(UTC)
    await doc.save()

    # Cascade: strip workspace from every member's User.workspaces
    members = await _UserDoc.find({"workspaces.workspace": workspace_id}).to_list()
    for member in members:
        before = len(member.workspaces)
        member.workspaces = [m for m in member.workspaces if m.workspace != workspace_id]
        if len(member.workspaces) != before:
            if member.active_workspace == workspace_id:
                member.active_workspace = (
                    member.workspaces[0].workspace if member.workspaces else None
                )
            await member.save()

    await emit(WorkspaceDeleted(data={"workspace_id": workspace_id}))
    get_resolver().invalidate_workspace(workspace_id)


async def list_for_user(ctx: RequestContext) -> list[Workspace]:
    """List the user's non-deleted workspaces with member counts.

    Member counts loaded via a single aggregation rather than N count()
    round-trips (legacy O(N) was visible in user-with-many-workspaces).
    """
    try:
        user = await _UserDoc.get(PydanticObjectId(ctx.user_id))
    except Exception:
        return []
    if user is None or not user.workspaces:
        return []
    ws_ids = [m.workspace for m in user.workspaces]
    docs = await _WorkspaceDoc.find(
        {
            "_id": {"$in": [PydanticObjectId(wid) for wid in ws_ids]},
            "deleted_at": None,
        }
    ).to_list()

    counts = await _count_members_bulk(ws_ids)
    return [_workspace_to_domain(doc, member_count=counts.get(str(doc.id), 0)) for doc in docs]


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


async def list_members(ctx: RequestContext, workspace_id: str) -> list[WorkspaceMember]:
    role = await _get_member_role(workspace_id, ctx.user_id)
    if role is None:
        raise NotFound("workspace", workspace_id)

    members = await _UserDoc.find({"workspaces.workspace": workspace_id}).to_list()
    out: list[WorkspaceMember] = []
    for member in members:
        membership = next((m for m in member.workspaces if m.workspace == workspace_id), None)
        if membership is None:
            continue
        out.append(
            WorkspaceMember(
                user_id=str(member.id),
                email=member.email,
                name=member.full_name,
                avatar=member.avatar,
                role=membership.role,
                joined_at=membership.joined_at,
            )
        )
    return out


async def update_member_role(
    workspace_id: str,
    target_user_id: str,
    role: str,
    actor_user_id: str,
) -> None:
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        raise NotFound("workspace", workspace_id)
    if doc.owner == target_user_id and role != "owner":
        raise Forbidden(
            "workspace.cannot_demote_owner",
            "Cannot demote the workspace owner",
        )
    user = await _UserDoc.get(PydanticObjectId(target_user_id))
    if user is None:
        raise NotFound("member", target_user_id)
    updated = False
    for m in user.workspaces:
        if m.workspace == workspace_id:
            m.role = role
            updated = True
            break
    if not updated:
        raise NotFound("member", target_user_id)
    await user.save()

    await emit(
        WorkspaceMemberRole(
            data={
                "workspace_id": workspace_id,
                "user_id": target_user_id,
                "role": role,
            }
        )
    )
    get_resolver().invalidate_workspace(workspace_id)


async def remove_member(
    workspace_id: str,
    target_user_id: str,
    actor_user_id: str,
) -> None:
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        raise NotFound("workspace", workspace_id)
    if doc.owner == target_user_id:
        raise Forbidden("workspace.cannot_remove_owner", "Cannot remove the workspace owner")

    user = await _UserDoc.get(PydanticObjectId(target_user_id))
    if user is None:
        raise NotFound("member", target_user_id)
    before = len(user.workspaces)
    user.workspaces = [m for m in user.workspaces if m.workspace != workspace_id]
    if len(user.workspaces) == before:
        raise NotFound("member", target_user_id)
    if user.active_workspace == workspace_id:
        user.active_workspace = None
    await user.save()

    await event_bus.emit(
        "member.removed",
        {
            "workspace_id": workspace_id,
            "user_id": target_user_id,
            "removed_by": actor_user_id,
        },
    )
    await emit(
        WorkspaceMemberRemoved(data={"workspace_id": workspace_id, "user_id": target_user_id})
    )
    get_resolver().invalidate_workspace(workspace_id)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


async def list_invites(workspace_id: str) -> list[Invite]:
    docs = await _InviteDoc.find(
        {"workspace": workspace_id, "accepted": False, "revoked": False}
    ).to_list()
    return [_invite_to_domain(d) for d in docs if not d.expired]


async def create_invite(
    ctx: RequestContext,
    workspace_id: str,
    body: CreateInviteRequest,
) -> Invite:
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        raise NotFound("workspace", workspace_id)

    member_count = await _count_members(workspace_id)
    if member_count >= doc.seats:
        raise SeatLimitError(doc.seats)

    existing = await _InviteDoc.find_one(
        {
            "workspace": workspace_id,
            "email": body.email,
            "accepted": False,
            "revoked": False,
            "group": body.group_id,
        }
    )
    if existing is not None and not existing.expired:
        msg = f"A pending invite already exists for {body.email}" + (
            " in this group" if body.group_id else ""
        )
        raise ConflictError("invite.already_pending", msg)

    invite_doc = _InviteDoc(
        workspace=workspace_id,
        email=body.email,
        role=body.role,
        invited_by=ctx.user_id,
        token=secrets.token_urlsafe(32),
        group=body.group_id,
    )
    await invite_doc.insert()
    invite = _invite_to_domain(invite_doc)

    invited_user_id = await _find_user_id_by_email(body.email)

    event_data: dict = {
        "workspace_id": workspace_id,
        "invite_id": invite.id,
        "email": body.email,
    }
    if invited_user_id:
        event_data["user_id"] = invited_user_id

    await emit(WorkspaceInviteCreated(data=event_data))

    if invited_user_id:
        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=invited_user_id,
            kind="invite",
            title=f"You were invited to join {doc.name}",
            body="",
            source=NotificationSource(
                type="invite",
                id=invite.token,
                room_id=invite.group_id,
            ),
        )

    return invite


async def validate_invite(token: str) -> tuple[Invite, str]:
    """Return ``(invite, workspace_name)``. Raises NotFound if unknown."""
    invite_doc = await _InviteDoc.find_one(_InviteDoc.token == token)
    if invite_doc is None:
        raise NotFound("invite")
    invite = _invite_to_domain(invite_doc)
    ws_doc = await _fetch_workspace(invite.workspace_id)
    ws_name = ws_doc.name if ws_doc is not None else ""
    return invite, ws_name


async def accept_invite(ctx: RequestContext, token: str) -> None:
    invite_doc = await _InviteDoc.find_one(_InviteDoc.token == token)
    if invite_doc is None:
        raise NotFound("invite")
    invite = _invite_to_domain(invite_doc)
    if invite.accepted:
        raise ConflictError("invite.already_accepted", "This invite has already been accepted")
    if invite.revoked:
        raise Forbidden("invite.revoked", "This invite has been revoked")
    if invite.expired:
        raise Forbidden("invite.expired", "This invite has expired")

    ws_doc = await _fetch_workspace(invite.workspace_id)
    if ws_doc is None:
        raise NotFound("workspace", invite.workspace_id)

    already_member = await _get_member_role(invite.workspace_id, ctx.user_id) is not None
    if not already_member:
        member_count = await _count_members(invite.workspace_id)
        if member_count >= ws_doc.seats:
            raise SeatLimitError(ws_doc.seats)
        await _add_member(
            invite.workspace_id,
            ctx.user_id,
            role=invite.role,
            set_active=True,
        )

    invite_doc.accepted = True
    await invite_doc.save()

    await event_bus.emit(
        "invite.accepted",
        {
            "workspace_id": invite.workspace_id,
            "user_id": ctx.user_id,
            "invite_id": invite.id,
            "group_id": invite.group_id,
        },
    )

    wid = invite.workspace_id
    await emit(
        WorkspaceInviteAccepted(
            data={"workspace_id": wid, "invite_id": invite.id, "user_id": ctx.user_id}
        )
    )
    await emit(
        WorkspaceMemberAdded(
            data={"workspace_id": wid, "user_id": ctx.user_id, "role": invite.role}
        )
    )
    get_resolver().invalidate_workspace(wid)


async def revoke_invite(workspace_id: str, invite_id: str) -> None:
    try:
        invite_doc = await _InviteDoc.get(PydanticObjectId(invite_id))
    except Exception:
        invite_doc = None
    if invite_doc is None or invite_doc.workspace != workspace_id:
        raise NotFound("invite", invite_id)
    invite_doc.revoked = True
    await invite_doc.save()
    await emit(WorkspaceInviteRevoked(data={"workspace_id": workspace_id, "invite_id": invite_id}))


# ---------------------------------------------------------------------------
# Realtime audience helpers — used as function refs by realtime/audience.py
# ---------------------------------------------------------------------------


async def list_member_ids(workspace_id: str) -> list[str]:
    users = await _UserDoc.find({"workspaces.workspace": workspace_id}).to_list()
    return [str(u.id) for u in users]


async def list_admin_ids(workspace_id: str) -> list[str]:
    users = await _UserDoc.find(
        {
            "workspaces": {
                "$elemMatch": {
                    "workspace": workspace_id,
                    "role": {"$in": ["owner", "admin"]},
                }
            }
        }
    ).to_list()
    return [str(u.id) for u in users]


async def list_peer_ids(user_id: str) -> list[str]:
    try:
        me_oid = PydanticObjectId(user_id)
    except Exception:
        return []
    me = await _UserDoc.get(me_oid)
    if me is None or not getattr(me, "workspaces", None):
        return []
    ws_ids = [m.workspace for m in me.workspaces]
    peers = await _UserDoc.find(
        {"workspaces.workspace": {"$in": ws_ids}, "_id": {"$ne": me.id}}
    ).to_list()
    return [str(u.id) for u in peers]


async def seed_default_workspace(admin_id: str, *, name: str, slug: str) -> _WorkspaceDoc | None:
    """Insert a default workspace with enterprise plan + 50 seats and
    register ``admin_id`` as the owner. Skips silently if any workspace
    already exists or if the admin already has a workspace.

    Returns the inserted Beanie doc, or ``None`` if seed was skipped or
    the insert raised. Bootstrap-time analogue of ``create()`` — the
    enterprise plan / 50 seats / explicit ``WorkspaceSettings()`` only
    apply to first-boot seeding.
    """
    import logging

    logger = logging.getLogger(__name__)

    admin = await _UserDoc.get(PydanticObjectId(admin_id))
    if admin is None:
        logger.debug("Admin %s not found — skipping workspace seed", admin_id)
        return None
    if admin.workspaces:
        logger.debug("Admin already has workspace(s) — skipping seed")
        return None
    existing = await _WorkspaceDoc.find_one()
    if existing is not None:
        logger.debug("Workspace already exists — skipping seed")
        return None

    try:
        doc = _WorkspaceDoc(
            name=name,
            slug=slug,
            owner=admin_id,
            plan="enterprise",
            seats=50,
            settings=WorkspaceSettings(),
        )
        await doc.insert()
        await _add_member(str(doc.id), admin_id, role="owner", set_active=True)
        logger.info("Default workspace seeded: %s (slug: %s, id: %s)", name, slug, doc.id)
        return doc
    except Exception:
        logger.warning("Failed to seed default workspace", exc_info=True)
        return None


async def get_workspace_plan(workspace_id: str) -> str:
    """Return the plan tier string for a workspace.

    Used by the plan-feature gate dependency. Returns "team" (the most
    restrictive plan) as a safe fallback when the workspace cannot be
    loaded so the guard fails open on plan rather than raising a 500.
    """
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        return "team"
    return doc.plan


__all__ = [
    "accept_invite",
    "create",
    "create_invite",
    "delete",
    "get",
    "get_workspace_plan",
    "legacy_ctx",
    "list_admin_ids",
    "list_for_user",
    "list_invites",
    "list_member_ids",
    "list_members",
    "list_peer_ids",
    "remove_member",
    "revoke_invite",
    "seed_default_workspace",
    "update",
    "update_member_role",
    "validate_invite",
]
