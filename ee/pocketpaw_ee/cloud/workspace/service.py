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

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import (
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
)
from pocketpaw_ee.cloud._core.realtime.bus import get_resolver
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    WorkspaceDeleted,
    WorkspaceInviteAccepted,
    WorkspaceInviteCreated,
    WorkspaceInviteRevoked,
    WorkspaceMemberAdded,
    WorkspaceMemberRemoved,
    WorkspaceMemberRole,
    WorkspaceUpdated,
)
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.auth import api_keys as _api_keys_service
from pocketpaw_ee.cloud.auth import sessions as _sessions_service
from pocketpaw_ee.cloud.models.agent import Agent as _AgentDoc
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.invite import Invite as _InviteDoc
from pocketpaw_ee.cloud.models.invite import hash_token
from pocketpaw_ee.cloud.models.notification import NotificationSource
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.user import WorkspaceMembership as _Membership
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.models.workspace import WorkspaceSettings
from pocketpaw_ee.cloud.notifications import service as notifications_service
from pocketpaw_ee.cloud.shared.events import event_bus
from pocketpaw_ee.cloud.uploads.models import FileUpload as _FileUploadDoc
from pocketpaw_ee.cloud.workspace.domain import Invite, Workspace, WorkspaceMember
from pocketpaw_ee.cloud.workspace.dto import (
    BulkInviteRequest,
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.models.user import User


logger = logging.getLogger(__name__)


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


def _invite_to_domain(doc: _InviteDoc, *, plaintext_token: str | None = None) -> Invite:
    return Invite(
        id=str(doc.id),
        workspace_id=doc.workspace,
        email=doc.email,
        role=doc.role,
        invited_by=doc.invited_by,
        token=plaintext_token,  # only populated on create; None on read
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


async def _count_owners(workspace_id: str) -> int:
    """Count members whose role is ``owner`` in this workspace.

    Independent of the ``Workspace.owner`` singular field — once ownership
    transfers via update_member_role, that field can lag behind reality.
    Uses ``$elemMatch`` so a single ``find().count()`` does the work
    (aggregation cursors don't survive mongomock-motor in tests).
    """
    return await _UserDoc.find(
        {
            "workspaces": {
                "$elemMatch": {"workspace": workspace_id, "role": "owner"},
            }
        }
    ).count()


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

    # Seed the default "pocketpaw" agent so the new workspace has a DM target
    # immediately. Idempotent; non-fatal — the boot-time back-fill is the
    # safety net if this raises. Mirrors auth/core.py:seed_workspace().
    from pocketpaw_ee.cloud.agents import service as agents_service

    try:
        await agents_service.seed_default_agent(str(doc.id), ctx.user_id)
    except Exception as exc:
        logger.warning("Failed to seed default agent for workspace %s (non-fatal): %s", doc.id, exc)

    await emit(
        WorkspaceMemberAdded(
            data={"workspace_id": str(doc.id), "user_id": ctx.user_id, "role": "owner"}
        )
    )
    get_resolver().invalidate_workspace(str(doc.id))

    # Wave 2 Task 10: structured audit log. ip + user_agent threading is
    # deferred to Wave 3 (needs RequestContext changes).
    await audit_service.record(
        str(doc.id),
        ctx.user_id,
        "workspace.created",
        target_type="workspace",
        target_id=str(doc.id),
        metadata={"name": doc.name, "slug": doc.slug},
    )

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

    await audit_service.record(
        workspace_id,
        ctx.user_id,
        "workspace.updated",
        target_type="workspace",
        target_id=workspace_id,
        metadata={"patched": patched},
    )

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

    await audit_service.record(
        workspace_id,
        ctx.user_id,
        "workspace.deleted",
        target_type="workspace",
        target_id=workspace_id,
    )


async def get_delete_preview(workspace_id: str) -> dict:
    """Return counts + total bytes for the cascade ``delete()`` would perform.

    Mirrors the resources the workspace ``delete`` path (and its eventual
    cascades) reaches: members, chat groups, agents, file uploads, and
    pending invites. Used by the UI to show a blast-radius summary before
    the type-name-to-confirm step. Owner-only at the route layer.

    Uses ``find().count()`` rather than ``aggregate()`` because aggregation
    cursors don't survive mongomock-motor in tests (see
    reference_mongomock_quirks memory).
    """
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        raise NotFound("workspace", workspace_id)

    member_count = await _count_members(workspace_id)
    room_count = await _GroupDoc.find({"workspace": workspace_id}).count()
    agent_count = await _AgentDoc.find({"workspace": workspace_id}).count()
    file_count = await _FileUploadDoc.find({"workspace": workspace_id, "deleted_at": None}).count()
    invite_count = await _InviteDoc.find(
        {"workspace": workspace_id, "accepted": False, "revoked": False}
    ).count()

    # Python-side reduction over the file rows; aggregate($sum) would be
    # cheaper but mongomock-motor doesn't honour it reliably.
    file_rows = await _FileUploadDoc.find({"workspace": workspace_id, "deleted_at": None}).to_list()
    total_bytes = sum(int(getattr(r, "size", 0) or 0) for r in file_rows)

    return {
        "member_count": member_count,
        "room_count": room_count,
        "agent_count": agent_count,
        "file_count": file_count,
        "invite_count": invite_count,
        "total_bytes": total_bytes,
    }


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
    # Independent last-owner guard: even if target is not doc.owner, if
    # they hold an owner ROLE and they're the only owner, blocking the
    # demotion keeps the workspace governable.
    if role != "owner":
        target_role = await _get_member_role(workspace_id, target_user_id)
        if target_role == "owner":
            owner_count = await _count_owners(workspace_id)
            if owner_count <= 1:
                raise Forbidden(
                    "workspace.last_owner",
                    "Cannot demote the last owner. Promote another member to owner first.",
                )
    user = await _UserDoc.get(PydanticObjectId(target_user_id))
    if user is None:
        raise NotFound("member", target_user_id)
    updated = False
    from_role: str | None = None
    for m in user.workspaces:
        if m.workspace == workspace_id:
            from_role = m.role
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

    await audit_service.record(
        workspace_id,
        actor_user_id,
        "workspace.member_role_changed",
        target_type="user",
        target_id=target_user_id,
        metadata={"from_role": from_role, "to_role": role},
    )


async def _revoke_invites_by_inviter(workspace_id: str, user_id: str) -> int:
    """Revoke pending invites issued by ``user_id`` for ``workspace_id``.

    Sets ``revoked=True`` + ``revoked_reason='inviter_removed'`` on every
    matching row. Returns the count of newly revoked invites.
    """
    rows = await _InviteDoc.find(
        {
            "workspace": workspace_id,
            "invited_by": user_id,
            "accepted": False,
            "revoked": False,
        }
    ).to_list()
    count = 0
    for invite in rows:
        invite.revoked = True
        invite.revoked_reason = "inviter_removed"
        await invite.save()
        count += 1
    return count


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

    # Role-based last-owner guard (independent of doc.owner field).
    target_role = await _get_member_role(workspace_id, target_user_id)
    if target_role == "owner":
        owner_count = await _count_owners(workspace_id)
        if owner_count <= 1:
            raise Forbidden(
                "workspace.last_owner",
                "Cannot remove the last owner. Promote another member to owner first.",
            )

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

    # Why: cascade revocations are best-effort — one failed step (e.g. Redis
    # blip on session revocation) shouldn't undo the membership flip or block
    # the other cascades. Each step logs + continues; the audit row captures
    # whatever counts we did manage.
    api_keys_revoked = 0
    try:
        api_keys_revoked = await _api_keys_service.revoke_keys_for_user_in_workspace(
            target_user_id, workspace_id
        )
    except Exception:
        logger.warning(
            "remove_member: api-key cascade failed for user=%s ws=%s",
            target_user_id,
            workspace_id,
            exc_info=True,
        )

    sessions_revoked = 0
    try:
        sessions_revoked = await _sessions_service.revoke_all_sessions_for_user(target_user_id)
    except Exception:
        logger.warning(
            "remove_member: session cascade failed for user=%s",
            target_user_id,
            exc_info=True,
        )

    invites_revoked = 0
    try:
        invites_revoked = await _revoke_invites_by_inviter(workspace_id, target_user_id)
    except Exception:
        logger.warning(
            "remove_member: invite cascade failed for user=%s ws=%s",
            target_user_id,
            workspace_id,
            exc_info=True,
        )

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

    try:
        resolver = get_resolver()
        resolver.invalidate_workspace(workspace_id)
        # Drop the user's peer cache so they stop receiving presence pings from
        # this workspace's members on their next event tick.
        invalidate_peers = getattr(resolver, "invalidate_user_peers", None)
        if callable(invalidate_peers):
            invalidate_peers(target_user_id)
    except Exception:
        logger.warning("remove_member: realtime invalidation failed", exc_info=True)

    await audit_service.record(
        workspace_id,
        actor_user_id,
        "workspace.member_removed",
        target_type="user",
        target_id=target_user_id,
        metadata={
            "cascade": {
                "api_keys_revoked": api_keys_revoked,
                "sessions_revoked": sessions_revoked,
                "invites_revoked": invites_revoked,
            },
        },
    )


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


async def list_invites(workspace_id: str) -> list[Invite]:
    docs = await _InviteDoc.find(
        {"workspace": workspace_id, "accepted": False, "revoked": False}
    ).to_list()
    return [_invite_to_domain(d) for d in docs if not d.expired]


async def _mint_invite_for_email(
    ctx: RequestContext,
    workspace_id: str,
    email: str,
    role: str,
    group_id: str | None,
) -> Invite:
    """Pre-clean expired/stale rows, reject duplicate pending, insert the
    hashed invite. Shared between ``create_invite`` (single) and
    ``bulk_create_invites`` (batch) so token hashing + pre-cleanup stay
    in lockstep. Does NOT emit or notify — the caller handles side-effects.
    """
    # Mongo TTL is the long-term GC; this pre-cleanup makes the
    # collision check below honest about what's "still pending."
    await _InviteDoc.find(
        {
            "workspace": workspace_id,
            "email": email,
            "group": group_id,
            "$or": [
                {"revoked": True},
                {"accepted": True},
                {"expires_at": {"$lt": datetime.now(UTC)}},
            ],
        }
    ).delete()

    existing = await _InviteDoc.find_one(
        {
            "workspace": workspace_id,
            "email": email,
            "accepted": False,
            "revoked": False,
            "group": group_id,
        }
    )
    if existing is not None and not existing.expired:
        msg = f"A pending invite already exists for {email}" + (
            " in this group" if group_id else ""
        )
        raise ConflictError("invite.already_pending", msg)

    plaintext = secrets.token_urlsafe(32)
    invite_doc = _InviteDoc(
        workspace=workspace_id,
        email=email,
        role=role,
        invited_by=ctx.user_id,
        token=None,  # plaintext never persisted for new invites
        token_hash=hash_token(plaintext),
        group=group_id,
    )
    await invite_doc.insert()
    return _invite_to_domain(invite_doc, plaintext_token=plaintext)


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

    invite = await _mint_invite_for_email(ctx, workspace_id, body.email, body.role, body.group_id)

    invited_user_id = await _find_user_id_by_email(body.email)

    event_data: dict = {
        "workspace_id": workspace_id,
        "invite_id": invite.id,
        "email": body.email,
    }
    if invited_user_id:
        event_data["user_id"] = invited_user_id

    await emit(WorkspaceInviteCreated(data=event_data))

    await audit_service.record(
        workspace_id,
        ctx.user_id,
        "workspace.invite_created",
        target_type="invite",
        target_id=invite.id,
        metadata={"invite_id": invite.id, "email": body.email, "role": body.role},
    )

    if invited_user_id:
        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=invited_user_id,
            kind="invite",
            title=f"You were invited to join {doc.name}",
            body="",
            source=NotificationSource(
                type="invite",
                id=invite.id,  # the invite document id, not the token
                room_id=invite.group_id,
            ),
        )

    return invite


async def bulk_create_invites(
    ctx: RequestContext,
    workspace_id: str,
    body: BulkInviteRequest,
) -> dict:
    """Create many invites in one call.

    Seat-limit is checked ONCE against the full batch size before any row
    is inserted, so callers don't get partial writes when the batch can't
    possibly fit. Per-email failures (already a member, already a pending
    invite) are returned in the ``skipped`` list — they don't abort the
    batch. Emits one WorkspaceInviteCreated event per created row, mirroring
    the single-invite path so downstream subscribers (search index, audit
    log, notifications) see no special bulk shape.
    """
    doc = await _fetch_workspace(workspace_id)
    if doc is None:
        raise NotFound("workspace", workspace_id)

    current_count = await _count_members(workspace_id)
    if current_count + len(body.emails) > doc.seats:
        raise SeatLimitError(doc.seats)

    created: list[Invite] = []
    skipped: list[dict] = []

    for email in body.emails:
        existing_user_id = await _find_user_id_by_email(email)
        if existing_user_id is not None:
            existing_role = await _get_member_role(workspace_id, existing_user_id)
            if existing_role is not None:
                skipped.append({"email": email, "reason": "already_member"})
                continue

        try:
            invite = await _mint_invite_for_email(
                ctx, workspace_id, email, body.role, body.group_id
            )
        except ConflictError as exc:
            if exc.code == "invite.already_pending":
                skipped.append({"email": email, "reason": "already_pending"})
                continue
            raise

        event_data: dict = {
            "workspace_id": workspace_id,
            "invite_id": invite.id,
            "email": email,
        }
        if existing_user_id:
            event_data["user_id"] = existing_user_id
        await emit(WorkspaceInviteCreated(data=event_data))

        await audit_service.record(
            workspace_id,
            ctx.user_id,
            "workspace.invite_created",
            target_type="invite",
            target_id=invite.id,
            metadata={"invite_id": invite.id, "email": email, "role": body.role},
        )

        if existing_user_id:
            await notifications_service.create(
                workspace_id=workspace_id,
                recipient=existing_user_id,
                kind="invite",
                title=f"You were invited to join {doc.name}",
                body="",
                source=NotificationSource(
                    type="invite",
                    id=invite.id,
                    room_id=invite.group_id,
                ),
            )

        created.append(invite)

    return {"created": created, "skipped": skipped}


async def validate_invite(token: str) -> tuple[Invite, str]:
    """Return ``(invite, workspace_name)``. Raises NotFound if unknown."""
    th = hash_token(token)
    invite_doc = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
    if invite_doc is None:
        # Legacy: an invite created before hashing rollout. One-time
        # backfill so the row stops being plaintext-readable.
        invite_doc = await _InviteDoc.find_one(_InviteDoc.token == token)
        if invite_doc is None:
            raise NotFound("invite")
        invite_doc.token_hash = th
        invite_doc.token = None
        await invite_doc.save()
    invite = _invite_to_domain(invite_doc)
    ws_doc = await _fetch_workspace(invite.workspace_id)
    ws_name = ws_doc.name if ws_doc is not None else ""
    return invite, ws_name


async def preview_invite(token: str, viewer_user_id: str | None) -> dict:
    """Typed preview for the accept UI — never raises, returns a state dict."""
    th = hash_token(token)
    invite_doc = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
    if invite_doc is None:
        invite_doc = await _InviteDoc.find_one(_InviteDoc.token == token)
    if invite_doc is None:
        return {"state": "not_found"}

    if invite_doc.accepted:
        return {"state": "already_accepted", "email": invite_doc.email}
    if invite_doc.revoked:
        return {"state": "revoked", "email": invite_doc.email}
    if invite_doc.expired:
        return {"state": "expired", "email": invite_doc.email}

    ws_doc = await _fetch_workspace(invite_doc.workspace)
    ws_name = ws_doc.name if ws_doc is not None else ""

    viewer_email: str | None = None
    state = "ready_new"
    if viewer_user_id:
        try:
            viewer = await _UserDoc.get(PydanticObjectId(viewer_user_id))
        except Exception:
            viewer = None
        if viewer is not None:
            viewer_email = viewer.email
            if (viewer.email or "").lower() == invite_doc.email.lower():
                state = "ready_existing"
            else:
                state = "ready_wrong_user"

    return {
        "state": state,
        "email": invite_doc.email,
        "role": invite_doc.role,
        "workspace_name": ws_name,
        "group": invite_doc.group,
        "group_name": None,
        "viewer_email": viewer_email,
    }


async def accept_invite(ctx: RequestContext, token: str) -> None:
    th = hash_token(token)

    # Identity check: the logged-in user's email must match the invitee's.
    # Comparison is case-insensitive (emails are case-insensitive at the
    # mailbox level for all practical providers). The preview read does
    # NOT mutate the invite, so a mismatch leaves the token usable by the
    # rightful invitee.
    viewer = await _UserDoc.get(PydanticObjectId(ctx.user_id))
    if viewer is None:
        raise NotFound("user", ctx.user_id)
    preview = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
    if preview is None:
        preview = await _InviteDoc.find_one(_InviteDoc.token == token)
    if preview is None:
        raise NotFound("invite")
    if preview.email.lower() != (viewer.email or "").lower():
        raise Forbidden(
            "invite.email_mismatch",
            "This invite was sent to a different email address. "
            "Sign in with the invited account to accept.",
        )

    # Atomic claim: set accepted=True only if it's currently False.
    # Returns the original (BEFORE) doc on success, None on lose.
    collection = _InviteDoc.get_pymongo_collection()
    claimed = await collection.find_one_and_update(
        {"token_hash": th, "accepted": False, "revoked": False},
        {"$set": {"accepted": True, "accepted_at": datetime.now(UTC)}},
        return_document=False,
    )

    if claimed is None:
        # Disambiguate: missing, revoked, expired, or already accepted?
        existing = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
        if existing is None:
            existing = await _InviteDoc.find_one(_InviteDoc.token == token)
        if existing is None:
            raise NotFound("invite")
        if existing.accepted:
            raise ConflictError("invite.already_accepted", "This invite has already been accepted")
        if existing.revoked:
            raise Forbidden("invite.revoked", "This invite has been revoked")
        if existing.expired:
            raise Forbidden("invite.expired", "This invite has expired")
        # Legacy plaintext row — backfill the hash field and retry the claim.
        existing.token_hash = th
        existing.token = None
        await existing.save()
        claimed = await collection.find_one_and_update(
            {"_id": existing.id, "accepted": False, "revoked": False},
            {"$set": {"accepted": True, "accepted_at": datetime.now(UTC)}},
            return_document=False,
        )
        if claimed is None:
            raise ConflictError("invite.already_accepted", "This invite has already been accepted")

    # Rebuild the domain object from the BEFORE doc so downstream emit/
    # membership logic has the data it needs.
    invite = _invite_to_domain(_InviteDoc.model_validate(claimed))

    ws_doc = await _fetch_workspace(invite.workspace_id)
    if ws_doc is None:
        # Roll back the claim — the invite must stay usable.
        await collection.update_one(
            {"_id": claimed["_id"]},
            {"$set": {"accepted": False, "accepted_at": None}},
        )
        raise NotFound("workspace", invite.workspace_id)

    already_member = await _get_member_role(invite.workspace_id, ctx.user_id) is not None
    if not already_member:
        member_count = await _count_members(invite.workspace_id)
        if member_count >= ws_doc.seats:
            await collection.update_one(
                {"_id": claimed["_id"]},
                {"$set": {"accepted": False, "accepted_at": None}},
            )
            raise SeatLimitError(ws_doc.seats)
        await _add_member(
            invite.workspace_id,
            ctx.user_id,
            role=invite.role,
            set_active=True,
        )

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

    await audit_service.record(
        wid,
        ctx.user_id,
        "workspace.invite_accepted",
        target_type="invite",
        target_id=invite.id,
        metadata={"invite_id": invite.id, "email": invite.email},
    )


async def revoke_invite(
    workspace_id: str,
    invite_id: str,
    actor_user_id: str | None = None,
) -> None:
    try:
        invite_doc = await _InviteDoc.get(PydanticObjectId(invite_id))
    except Exception:
        invite_doc = None
    if invite_doc is None or invite_doc.workspace != workspace_id:
        raise NotFound("invite", invite_id)
    invite_doc.revoked = True
    await invite_doc.save()
    await emit(WorkspaceInviteRevoked(data={"workspace_id": workspace_id, "invite_id": invite_id}))

    if actor_user_id is not None:
        await audit_service.record(
            workspace_id,
            actor_user_id,
            "workspace.invite_revoked",
            target_type="invite",
            target_id=invite_id,
            metadata={"invite_id": invite_id, "reason": "revoked"},
        )


async def resend_invite(
    ctx: RequestContext,
    workspace_id: str,
    invite_id: str,
) -> dict:
    """Rotate the invite's token and reset the 7-day expiry.

    Mints a fresh plaintext, persists only the new hash, and returns the
    plaintext so the route can ship it to the inviter's clipboard — the
    original plaintext is gone (server only ever stored the hash). The
    invite_id, workspace_id, email, and group are unchanged.
    """
    try:
        invite_doc = await _InviteDoc.get(PydanticObjectId(invite_id))
    except Exception:
        invite_doc = None
    if invite_doc is None or invite_doc.workspace != workspace_id:
        raise NotFound("invite", invite_id)
    if invite_doc.accepted:
        raise ConflictError("invite.already_accepted", "This invite has already been accepted")
    if invite_doc.revoked:
        raise ConflictError("invite.revoked", "This invite has been revoked")

    plaintext = secrets.token_urlsafe(32)
    new_expiry = datetime.now(UTC) + timedelta(days=7)
    invite_doc.token = None
    invite_doc.token_hash = hash_token(plaintext)
    invite_doc.expires_at = new_expiry
    invite_doc.resend_count = (invite_doc.resend_count or 0) + 1
    await invite_doc.save()

    await emit(
        WorkspaceInviteCreated(
            data={
                "workspace_id": workspace_id,
                "invite_id": invite_id,
                "email": invite_doc.email,
                "resend": True,
            }
        )
    )

    await audit_service.record(
        workspace_id,
        ctx.user_id,
        "workspace.invite_resent",
        target_type="invite",
        target_id=invite_id,
        metadata={
            "invite_id": invite_id,
            "email": invite_doc.email,
            "resend_count": invite_doc.resend_count,
        },
    )

    return {
        "invite_id": invite_id,
        "token": plaintext,
        "expires_at": new_expiry.isoformat(),
    }


async def decline_invite(token: str) -> None:
    """Invitee-side decline. No auth — the invitee may not have an account.

    Atomically marks the invite revoked with ``revoked_reason="declined"`` so
    audit can later distinguish an inviter-revoke from an invitee-decline.
    Idempotent: declining an already-declined/revoked invite is a no-op (no
    duplicate event). Refuses to decline an accepted invite (409).
    """
    th = hash_token(token)

    invite_doc = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
    if invite_doc is None:
        invite_doc = await _InviteDoc.find_one(_InviteDoc.token == token)
    if invite_doc is None:
        raise NotFound("invite")

    if invite_doc.accepted:
        raise ConflictError("invite.already_accepted", "This invite has already been accepted")
    if invite_doc.revoked:
        return  # idempotent — already declined or revoked

    collection = _InviteDoc.get_pymongo_collection()
    claimed = await collection.find_one_and_update(
        {"token_hash": th, "accepted": False, "revoked": False},
        {"$set": {"revoked": True, "revoked_reason": "declined"}},
        return_document=False,
    )

    if claimed is None:
        # Race: re-read and disambiguate. Mirror accept_invite's fallback,
        # including the legacy plaintext backfill path.
        existing = await _InviteDoc.find_one(_InviteDoc.token_hash == th)
        if existing is None:
            existing = await _InviteDoc.find_one(_InviteDoc.token == token)
        if existing is None:
            raise NotFound("invite")
        if existing.accepted:
            raise ConflictError("invite.already_accepted", "This invite has already been accepted")
        if existing.revoked:
            return  # somebody else just revoked/declined it — idempotent
        existing.token_hash = th
        existing.token = None
        await existing.save()
        claimed = await collection.find_one_and_update(
            {"_id": existing.id, "accepted": False, "revoked": False},
            {"$set": {"revoked": True, "revoked_reason": "declined"}},
            return_document=False,
        )
        if claimed is None:
            return  # raced again; treat as idempotent

    workspace_id = claimed["workspace"]
    invite_id = str(claimed["_id"])
    await emit(
        WorkspaceInviteRevoked(
            data={
                "workspace_id": workspace_id,
                "invite_id": invite_id,
                "reason": "declined",
            }
        )
    )

    # Decline is unauthed (the invitee may not have an account), so we
    # log the invitee's email as the actor identifier.
    await audit_service.record(
        workspace_id,
        f"email:{claimed.get('email', '')}",
        "workspace.invite_declined",
        target_type="invite",
        target_id=invite_id,
        metadata={"invite_id": invite_id, "email": claimed.get("email", "")},
    )


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


async def get_workspace_plan(workspace_id: str) -> str | None:
    """Return the plan tier string for a workspace, or None if missing.

    Used by the plan-feature gate dependency. Returns None when the
    workspace genuinely doesn't exist (invalid id, never created, or
    soft-deleted) so the caller can map that to a 404.

    Re-raises any DB-level exception rather than swallowing it. The
    previous implementation silently degraded to the most restrictive
    plan on transient Mongo errors, which 403'd paying customers during
    DB hiccups. Let the framework surface a 5xx instead.
    """
    try:
        oid = PydanticObjectId(workspace_id)
    except Exception:
        return None
    doc = await _WorkspaceDoc.get(oid)
    if doc is None or doc.deleted_at is not None:
        return None
    return doc.plan


__all__ = [
    "accept_invite",
    "bulk_create_invites",
    "create",
    "create_invite",
    "decline_invite",
    "delete",
    "get",
    "get_delete_preview",
    "get_workspace_plan",
    "legacy_ctx",
    "list_admin_ids",
    "list_for_user",
    "list_invites",
    "list_member_ids",
    "list_members",
    "list_peer_ids",
    "preview_invite",
    "remove_member",
    "resend_invite",
    "revoke_invite",
    "seed_default_workspace",
    "update",
    "update_member_role",
    "validate_invite",
]
