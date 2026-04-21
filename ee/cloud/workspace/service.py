"""Workspace domain — business logic service."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from beanie import PydanticObjectId

from ee.cloud.models.invite import Invite
from ee.cloud.models.notification import NotificationSource
from ee.cloud.models.user import User, WorkspaceMembership
from ee.cloud.models.workspace import Workspace, WorkspaceSettings
from ee.cloud.notifications.service import NotificationService
from ee.cloud.realtime.bus import get_resolver
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    WorkspaceDeleted,
    WorkspaceInviteAccepted,
    WorkspaceInviteCreated,
    WorkspaceInviteRevoked,
    WorkspaceMemberAdded,
    WorkspaceMemberRemoved,
    WorkspaceMemberRole,
    WorkspaceUpdated,
)
from ee.cloud.shared.errors import ConflictError, Forbidden, NotFound, SeatLimitError
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.time import iso_utc
from ee.cloud.workspace.schemas import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)


def _workspace_response(ws: Workspace, member_count: int = 0) -> dict:
    """Build a frontend-compatible dict from a Workspace document."""
    return {
        "_id": str(ws.id),
        "name": ws.name,
        "slug": ws.slug,
        "owner": ws.owner,
        "plan": ws.plan,
        "seats": ws.seats,
        "createdAt": iso_utc(ws.createdAt),
        "memberCount": member_count,
    }


def _invite_response(invite: Invite) -> dict:
    """Build a frontend-compatible dict from an Invite document."""
    return {
        "_id": str(invite.id),
        "email": invite.email,
        "role": invite.role,
        "invitedBy": invite.invited_by,
        "token": invite.token,
        "accepted": invite.accepted,
        "revoked": invite.revoked,
        "expired": invite.expired,
        "expiresAt": iso_utc(invite.expires_at),
    }


def _get_membership(user: User, workspace_id: str) -> WorkspaceMembership:
    """Find user's membership in a workspace or raise NotFound."""
    for m in user.workspaces:
        if m.workspace == workspace_id:
            return m
    raise NotFound("workspace", workspace_id)


async def _count_members(workspace_id: str) -> int:
    """Count users who are members of the given workspace."""
    return await User.find({"workspaces.workspace": workspace_id}).count()


class WorkspaceService:
    """Stateless service encapsulating workspace business logic."""

    # ------------------------------------------------------------------
    # Workspace CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def create(user: User, body: CreateWorkspaceRequest) -> dict:
        """Create a workspace and add the creator as owner."""
        existing = await Workspace.find_one(
            Workspace.slug == body.slug,
            Workspace.deleted_at == None,  # noqa: E711
        )
        if existing:
            raise ConflictError("workspace.slug_taken", f"Slug '{body.slug}' is already in use")

        ws = Workspace(
            name=body.name,
            slug=body.slug,
            owner=str(user.id),
        )
        await ws.insert()

        # Add creator as owner member
        user.workspaces.append(
            WorkspaceMembership(
                workspace=str(ws.id),
                role="owner",
                joined_at=datetime.now(UTC),
            )
        )
        user.active_workspace = str(ws.id)
        await user.save()

        wid = str(ws.id)
        await emit(
            WorkspaceMemberAdded(
                data={"workspace_id": wid, "user_id": str(user.id), "role": "owner"}
            )
        )
        get_resolver().invalidate_workspace(wid)

        return _workspace_response(ws, member_count=1)

    @staticmethod
    async def get(workspace_id: str, user: User) -> dict:
        """Get a workspace by ID. Requires membership."""
        _get_membership(user, workspace_id)

        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)

        count = await _count_members(workspace_id)
        return _workspace_response(ws, member_count=count)

    @staticmethod
    async def update(workspace_id: str, user: User, body: UpdateWorkspaceRequest) -> dict:
        """Update workspace fields. Role check performed at route layer."""
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)

        if body.name is not None:
            ws.name = body.name
        if body.settings is not None:
            ws.settings = WorkspaceSettings(**body.settings)

        await ws.save()
        count = await _count_members(workspace_id)

        patched = body.model_dump(exclude_unset=True)
        await emit(WorkspaceUpdated(data={"workspace_id": workspace_id, **patched}))

        return _workspace_response(ws, member_count=count)

    @staticmethod
    async def delete(workspace_id: str, user: User) -> None:
        """Soft-delete a workspace and cascade membership cleanup.

        Route layer enforces the owner-only role check. This method does the
        additional data-level housekeeping so the workspace disappears from
        every member's UserMenu switcher immediately:

        * Soft-delete the workspace document (``deleted_at``).
        * Strip the workspace out of every user's ``workspaces`` list.
        * Clear ``active_workspace`` on users who had the deleted workspace
          selected; swap it for another membership if one exists, otherwise
          reset to ``None`` so the first-run workspace modal fires again.
        * Emit ``workspace.deleted`` so live clients can react.

        Downstream artefacts (pockets, agents, groups, invites, sessions)
        remain in the database but orphan naturally: their API endpoints
        already scope reads by the caller's active workspace so they
        disappear from the UI as soon as users flip away. A future offline
        sweeper can purge them physically.
        """
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)

        ws.deleted_at = datetime.now(UTC)
        await ws.save()

        # Strip the workspace out of every member's workspaces list so
        # GET /workspaces stops returning it and the switcher no longer
        # renders it as a choice.
        members = await User.find({"workspaces.workspace": workspace_id}).to_list()
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

    @staticmethod
    async def list_for_user(user: User) -> list[dict]:
        """Return all non-deleted workspaces the user belongs to."""
        ws_ids = [m.workspace for m in user.workspaces]
        if not ws_ids:
            return []

        workspaces = await Workspace.find(
            {"_id": {"$in": [PydanticObjectId(wid) for wid in ws_ids]}, "deleted_at": None}
        ).to_list()

        results = []
        for ws in workspaces:
            count = await _count_members(str(ws.id))
            results.append(_workspace_response(ws, member_count=count))
        return results

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    @staticmethod
    async def list_members(workspace_id: str, user: User) -> list[dict]:
        """List all members of a workspace. Requires membership."""
        _get_membership(user, workspace_id)

        members = await User.find({"workspaces.workspace": workspace_id}).to_list()
        result = []
        for member in members:
            m = next(w for w in member.workspaces if w.workspace == workspace_id)
            result.append(
                {
                    "_id": str(member.id),
                    "email": member.email,
                    "name": member.full_name,
                    "avatar": member.avatar,
                    "role": m.role,
                    "joinedAt": iso_utc(m.joined_at),
                }
            )
        return result

    @staticmethod
    async def update_member_role(
        workspace_id: str, target_user_id: str, role: str, user: User
    ) -> None:
        """Update a member's role. Role check at route layer; owner-demotion
        invariant enforced here because it's a data rule, not a role rule."""
        # Load workspace to check owner
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)
        if ws.owner == target_user_id and role != "owner":
            raise Forbidden("workspace.cannot_demote_owner", "Cannot demote the workspace owner")

        target = await User.get(PydanticObjectId(target_user_id))
        if not target:
            raise NotFound("user", target_user_id)

        target_membership = None
        for m in target.workspaces:
            if m.workspace == workspace_id:
                target_membership = m
                break
        if not target_membership:
            raise NotFound("member", target_user_id)

        target_membership.role = role
        await target.save()

        await emit(
            WorkspaceMemberRole(
                data={"workspace_id": workspace_id, "user_id": target_user_id, "role": role}
            )
        )
        get_resolver().invalidate_workspace(workspace_id)

    @staticmethod
    async def remove_member(workspace_id: str, target_user_id: str, user: User) -> None:
        """Remove a member. Role check at route layer; owner-removal invariant
        enforced here because it's a data rule, not a role rule."""
        # Load workspace to check owner
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)
        if ws.owner == target_user_id:
            raise Forbidden("workspace.cannot_remove_owner", "Cannot remove the workspace owner")

        target = await User.get(PydanticObjectId(target_user_id))
        if not target:
            raise NotFound("user", target_user_id)

        original_len = len(target.workspaces)
        target.workspaces = [m for m in target.workspaces if m.workspace != workspace_id]
        if len(target.workspaces) == original_len:
            raise NotFound("member", target_user_id)

        # Clear active workspace if it was the removed one
        if target.active_workspace == workspace_id:
            target.active_workspace = None

        await target.save()

        await event_bus.emit(
            "member.removed",
            {
                "workspace_id": workspace_id,
                "user_id": target_user_id,
                "removed_by": str(user.id),
            },
        )

        await emit(
            WorkspaceMemberRemoved(data={"workspace_id": workspace_id, "user_id": target_user_id})
        )
        get_resolver().invalidate_workspace(workspace_id)

    # ------------------------------------------------------------------
    # Invites
    # ------------------------------------------------------------------

    @staticmethod
    async def list_invites(workspace_id: str) -> list[dict]:
        """List pending (not accepted, not revoked, not expired) invites for
        a workspace. Role check at route layer."""
        invites = await Invite.find(
            {
                "workspace": workspace_id,
                "accepted": False,
                "revoked": False,
            }
        ).to_list()
        return [_invite_response(inv) for inv in invites if not inv.expired]

    @staticmethod
    async def create_invite(workspace_id: str, user: User, body: CreateInviteRequest) -> dict:
        """Create an invite. Role check at route layer; seat-limit + dedup
        enforced here."""
        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)

        # Check seat limit
        member_count = await _count_members(workspace_id)
        if member_count >= ws.seats:
            raise SeatLimitError(ws.seats)

        # Check for existing pending invite to same email + group combination.
        # Different groups can each have their own pending invite for the same email.
        pending_query: dict = {
            "workspace": workspace_id,
            "email": body.email,
            "accepted": False,
            "revoked": False,
        }
        if body.group_id:
            pending_query["group"] = body.group_id
        else:
            # Workspace-level invite (no group) — only one at a time
            pending_query["group"] = None

        existing = await Invite.find_one(pending_query)
        if existing and not existing.expired:
            raise ConflictError(
                "invite.already_pending",
                f"A pending invite already exists for {body.email}"
                + (" in this group" if body.group_id else ""),
            )

        invite = Invite(
            workspace=workspace_id,
            email=body.email,
            role=body.role,
            invited_by=str(user.id),
            token=secrets.token_urlsafe(32),
            group=body.group_id,
        )
        await invite.insert()

        # Resolve invitee-as-existing-user before emitting so the audience
        # resolver can route the event to them (via user_id branch) in addition
        # to workspace admins.
        invited_user = await User.find_one(User.email == body.email)

        event_data: dict = {
            "workspace_id": workspace_id,
            "invite_id": str(invite.id),
            "email": body.email,
        }
        if invited_user:
            event_data["user_id"] = str(invited_user.id)

        # Emit invite.created (token deliberately omitted from payload).
        await emit(WorkspaceInviteCreated(data=event_data))

        # If the invited email matches an existing user, create an in-app
        # notification so their bell icon lights up immediately.
        if invited_user:
            await NotificationService.create(
                workspace_id=workspace_id,
                recipient=str(invited_user.id),
                kind="invite",
                title=f"You were invited to join {ws.name}",
                body="",
                source=NotificationSource(type="invite", id=str(invite.id)),
            )

        return _invite_response(invite)

    @staticmethod
    async def validate_invite(token: str) -> dict:
        """Find an invite by token and return its status. No auth required.

        Enriches the base invite shape with ``valid`` + ``workspace_name`` so
        the /invite/[token] frontend can render the destination without a
        second round-trip (otherwise the subtitle renders a blank name).
        """
        invite = await Invite.find_one(Invite.token == token)
        if not invite:
            raise NotFound("invite")

        ws = await Workspace.get(PydanticObjectId(invite.workspace))
        workspace_name = ws.name if ws and ws.deleted_at is None else ""

        response = _invite_response(invite)
        response["valid"] = not (invite.accepted or invite.revoked or invite.expired)
        response["workspace_name"] = workspace_name
        return response

    @staticmethod
    async def accept_invite(token: str, user: User) -> None:
        """Accept an invite: validate it, check seat limit, add user to workspace."""
        invite = await Invite.find_one(Invite.token == token)
        if not invite:
            raise NotFound("invite")

        if invite.accepted:
            raise ConflictError("invite.already_accepted", "This invite has already been accepted")
        if invite.revoked:
            raise Forbidden("invite.revoked", "This invite has been revoked")
        if invite.expired:
            raise Forbidden("invite.expired", "This invite has expired")

        ws = await Workspace.get(PydanticObjectId(invite.workspace))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", invite.workspace)

        # Add to workspace if not already a member
        already_member = any(m.workspace == invite.workspace for m in user.workspaces)
        if not already_member:
            # Only check seat limit for new members
            member_count = await _count_members(invite.workspace)
            if member_count >= ws.seats:
                raise SeatLimitError(ws.seats)
            user.workspaces.append(
                WorkspaceMembership(
                    workspace=invite.workspace,
                    role=invite.role,
                    joined_at=datetime.now(UTC),
                )
            )
            user.active_workspace = invite.workspace
            await user.save()

        invite.accepted = True
        await invite.save()

        await event_bus.emit(
            "invite.accepted",
            {
                "workspace_id": invite.workspace,
                "user_id": str(user.id),
                "invite_id": str(invite.id),
                "group_id": invite.group,
            },
        )

        wid = invite.workspace
        uid = str(user.id)
        await emit(
            WorkspaceInviteAccepted(
                data={"workspace_id": wid, "invite_id": str(invite.id), "user_id": uid}
            )
        )
        await emit(
            WorkspaceMemberAdded(data={"workspace_id": wid, "user_id": uid, "role": invite.role})
        )
        get_resolver().invalidate_workspace(wid)

    @staticmethod
    async def revoke_invite(workspace_id: str, invite_id: str, user: User) -> None:
        """Revoke an invite. Role check at route layer."""
        invite = await Invite.get(PydanticObjectId(invite_id))
        if not invite or invite.workspace != workspace_id:
            raise NotFound("invite", invite_id)

        invite.revoked = True
        await invite.save()

        await emit(
            WorkspaceInviteRevoked(data={"workspace_id": workspace_id, "invite_id": invite_id})
        )

    # ------------------------------------------------------------------
    # Realtime helpers (audience lookups)
    # ------------------------------------------------------------------

    @staticmethod
    async def list_member_ids(workspace_id: str) -> list[str]:
        """Return user_ids of every workspace member."""
        users = await User.find({"workspaces.workspace": workspace_id}).to_list()
        return [str(u.id) for u in users]

    @staticmethod
    async def list_admin_ids(workspace_id: str) -> list[str]:
        """Return user_ids of owners + admins."""
        users = await User.find(
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

    @staticmethod
    async def list_peer_ids(user_id: str) -> list[str]:
        """Return user_ids that share at least one workspace with the given user.

        Used for presence fan-out. Excludes the user themselves.
        """
        try:
            me_oid = PydanticObjectId(user_id)
        except Exception:
            return []
        me = await User.get(me_oid)
        if not me or not getattr(me, "workspaces", None):
            return []
        workspace_ids = [m.workspace for m in me.workspaces]
        peers = await User.find(
            {"workspaces.workspace": {"$in": workspace_ids}, "_id": {"$ne": me.id}}
        ).to_list()
        return [str(u.id) for u in peers]
