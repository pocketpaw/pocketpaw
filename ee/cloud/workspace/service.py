"""Workspace domain — business logic service."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from beanie import PydanticObjectId

from ee.cloud.models.invite import Invite
from ee.cloud.models.user import User, WorkspaceMembership
from ee.cloud.models.workspace import Workspace, WorkspaceSettings
from ee.cloud.shared.errors import ConflictError, Forbidden, NotFound, SeatLimitError
from ee.cloud.shared.events import event_bus
from ee.cloud.shared.permissions import check_workspace_role
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
        "createdAt": ws.createdAt.isoformat() if ws.createdAt else None,
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
        "expiresAt": invite.expires_at.isoformat() if invite.expires_at else None,
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
        """Update workspace fields. Requires admin+ role."""
        membership = _get_membership(user, workspace_id)
        check_workspace_role(membership.role, minimum="admin")

        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)

        if body.name is not None:
            ws.name = body.name
        if body.settings is not None:
            ws.settings = WorkspaceSettings(**body.settings)

        await ws.save()
        count = await _count_members(workspace_id)
        return _workspace_response(ws, member_count=count)

    @staticmethod
    async def delete(workspace_id: str, user: User) -> None:
        """Soft-delete a workspace. Requires owner role."""
        membership = _get_membership(user, workspace_id)
        check_workspace_role(membership.role, minimum="owner")

        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if not ws or ws.deleted_at is not None:
            raise NotFound("workspace", workspace_id)

        ws.deleted_at = datetime.now(UTC)
        await ws.save()

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
                    "joinedAt": m.joined_at.isoformat() if m.joined_at else None,
                }
            )
        return result

    @staticmethod
    async def update_member_role(
        workspace_id: str, target_user_id: str, role: str, user: User
    ) -> None:
        """Update a member's role. Requires admin+. Cannot demote the workspace owner."""
        membership = _get_membership(user, workspace_id)
        check_workspace_role(membership.role, minimum="admin")

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

    @staticmethod
    async def remove_member(workspace_id: str, target_user_id: str, user: User) -> None:
        """Remove a member from a workspace. Requires admin+. Cannot remove owner."""
        membership = _get_membership(user, workspace_id)
        check_workspace_role(membership.role, minimum="admin")

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

    # ------------------------------------------------------------------
    # Invites
    # ------------------------------------------------------------------

    @staticmethod
    async def create_invite(workspace_id: str, user: User, body: CreateInviteRequest) -> dict:
        """Create an invite. Requires admin+. Checks seat limit and existing pending invites."""
        membership = _get_membership(user, workspace_id)
        check_workspace_role(membership.role, minimum="admin")

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

        return _invite_response(invite)

    @staticmethod
    async def validate_invite(token: str) -> dict:
        """Find an invite by token and return its status. No auth required."""
        invite = await Invite.find_one(Invite.token == token)
        if not invite:
            raise NotFound("invite")

        return _invite_response(invite)

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

    @staticmethod
    async def revoke_invite(workspace_id: str, invite_id: str, user: User) -> None:
        """Revoke an invite. Requires admin+."""
        membership = _get_membership(user, workspace_id)
        check_workspace_role(membership.role, minimum="admin")

        invite = await Invite.get(PydanticObjectId(invite_id))
        if not invite or invite.workspace != workspace_id:
            raise NotFound("invite", invite_id)

        invite.revoked = True
        await invite.save()
