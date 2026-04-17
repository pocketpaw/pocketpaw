# Refactored: Split from service.py — contains GroupService class and group-related
# helper functions. N+1 query in _group_response() fixed with batch loading for
# both members (User) and agents (AgentModel).

"""Chat domain — group business logic (CRUD, membership, agents, DMs)."""

from __future__ import annotations

import logging
import re

from beanie import PydanticObjectId

from ee.cloud.chat.schemas import (
    AddGroupAgentRequest,
    CreateGroupRequest,
    UpdateGroupAgentRequest,
    UpdateGroupRequest,
)
from ee.cloud.models.group import Group, GroupAgent
from ee.cloud.shared.errors import Forbidden, NotFound, ValidationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_slug(name: str) -> str:
    """Lowercase, replace spaces/underscores with hyphens, strip non-alnum."""
    slug = name.lower().strip()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


async def _group_response(group: Group) -> dict:
    """Convert a Group document to a frontend-compatible dict.

    Populates member IDs -> {_id, name, email} and agent IDs ->
    {_id, agent, name, role, respond_mode}.
    Uses batch queries to avoid N+1 per-member / per-agent lookups.
    """
    from ee.cloud.models.agent import Agent as AgentModel
    from ee.cloud.models.user import User

    # Batch load members
    member_ids = [PydanticObjectId(uid) for uid in group.members]
    users = await User.find({"_id": {"$in": member_ids}}).to_list() if member_ids else []
    user_map = {str(u.id): u for u in users}

    populated_members = []
    for uid in group.members:
        user = user_map.get(uid)
        if user:
            populated_members.append(
                {
                    "_id": str(user.id),
                    "name": user.full_name or user.email,
                    "email": user.email,
                    "avatar": user.avatar,
                }
            )
        else:
            populated_members.append({"_id": uid, "name": uid, "email": ""})

    # Batch load agents
    agent_ids = [PydanticObjectId(ga.agent) for ga in group.agents]
    agents = await AgentModel.find({"_id": {"$in": agent_ids}}).to_list() if agent_ids else []
    agent_map = {str(a.id): a for a in agents}

    populated_agents = []
    for ga in group.agents:
        agent_doc = agent_map.get(ga.agent)
        populated_agents.append(
            {
                "_id": str(agent_doc.id) if agent_doc else ga.agent,
                "agent": ga.agent,
                "name": agent_doc.name if agent_doc else "Agent",
                "uname": agent_doc.slug if agent_doc else "",
                "role": ga.role,
                "respond_mode": ga.respond_mode,
            }
        )

    return {
        "_id": str(group.id),
        "workspace": group.workspace,
        "name": group.name,
        "slug": group.slug,
        "description": group.description,
        "type": group.type,
        "icon": group.icon,
        "color": group.color,
        "owner": group.owner,
        "members": populated_members,
        "agents": populated_agents,
        "pinnedMessages": group.pinned_messages,
        "archived": group.archived,
        "lastMessageAt": group.last_message_at.isoformat() if group.last_message_at else None,
        "messageCount": group.message_count,
        "createdAt": group.createdAt.isoformat() if group.createdAt else None,
    }


def _require_group_member(group: Group, user_id: str) -> None:
    """Raise Forbidden if user is not a member of the group."""
    if user_id not in group.members:
        raise Forbidden("group.not_member", "You are not a member of this group")


def _require_group_admin(group: Group, user_id: str) -> None:
    """Raise Forbidden if user is not the group owner (admin).

    Groups don't have per-member roles -- the owner is the sole admin.
    """
    if group.owner != user_id:
        raise Forbidden("group.not_admin", "Only the group owner can perform this action")


async def _get_group_or_404(group_id: str) -> Group:
    """Load a group by ID or raise NotFound."""
    group = await Group.get(PydanticObjectId(group_id))
    if not group:
        raise NotFound("group", group_id)
    return group


# ---------------------------------------------------------------------------
# GroupService
# ---------------------------------------------------------------------------


class GroupService:
    """Stateless service for group/channel business logic."""

    @staticmethod
    async def create_group(workspace_id: str, user_id: str, body: CreateGroupRequest) -> dict:
        """Create a group and add the creator as a member.

        For DMs: validates exactly 2 member_ids, auto-names as "DM".
        """
        if body.type == "dm":
            if len(body.member_ids) != 1:
                raise ValidationError(
                    "group.dm_requires_one_target",
                    "DM groups require exactly one target member_id (the other party)",
                )
            members = sorted({user_id, body.member_ids[0]})
            name = "DM"
        else:
            members = list({user_id, *body.member_ids})
            name = body.name

        slug = _generate_slug(name)

        group = Group(
            workspace=workspace_id,
            name=name,
            slug=slug,
            description=body.description,
            type=body.type,
            icon=body.icon,
            color=body.color,
            members=members,
            owner=user_id,
        )
        await group.insert()
        return await _group_response(group)

    @staticmethod
    async def list_groups(workspace_id: str, user_id: str) -> list[dict]:
        """List groups visible to the user.

        Returns public groups in the workspace plus private/dm groups
        where the user is a member.
        """
        groups = await Group.find(
            {
                "workspace": workspace_id,
                "archived": False,
                "$or": [
                    {"type": "public"},
                    {"members": user_id},
                ],
            }
        ).to_list()
        return [await _group_response(g) for g in groups]

    @staticmethod
    async def get_group(group_id: str, user_id: str) -> dict:
        """Get a single group. Private/DM groups require membership."""
        group = await _get_group_or_404(group_id)

        if group.type in ("private", "dm"):
            _require_group_member(group, user_id)

        return await _group_response(group)

    @staticmethod
    async def update_group(group_id: str, user_id: str, body: UpdateGroupRequest) -> dict:
        """Update group fields. Owner only. Cannot update DMs."""
        group = await _get_group_or_404(group_id)

        if group.type == "dm":
            raise Forbidden("group.cannot_update_dm", "DM groups cannot be updated")
        _require_group_admin(group, user_id)

        if body.name is not None:
            group.name = body.name
            group.slug = _generate_slug(body.name)
        if body.description is not None:
            group.description = body.description
        if body.icon is not None:
            group.icon = body.icon
        if body.color is not None:
            group.color = body.color

        await group.save()
        return await _group_response(group)

    @staticmethod
    async def archive_group(group_id: str, user_id: str) -> None:
        """Archive a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)
        group.archived = True
        await group.save()

    @staticmethod
    async def join_group(group_id: str, user_id: str) -> None:
        """Join a public group. Adds user to members list."""
        group = await _get_group_or_404(group_id)

        if group.type != "public":
            raise Forbidden("group.not_public", "Only public groups can be joined directly")
        if group.archived:
            raise Forbidden("group.archived", "Cannot join an archived group")

        if user_id not in group.members:
            group.members.append(user_id)
            await group.save()

    @staticmethod
    async def leave_group(group_id: str, user_id: str) -> None:
        """Leave a group. Owner cannot leave (must transfer ownership first)."""
        group = await _get_group_or_404(group_id)
        _require_group_member(group, user_id)

        if group.owner == user_id:
            raise Forbidden(
                "group.owner_cannot_leave",
                "The group owner cannot leave. Transfer ownership first.",
            )

        group.members.remove(user_id)
        await group.save()

    @staticmethod
    async def add_members(group_id: str, user_id: str, member_ids: list[str]) -> None:
        """Add members to a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        if group.archived:
            raise Forbidden("group.archived", "Cannot modify an archived group")

        added = False
        for mid in member_ids:
            if mid not in group.members:
                group.members.append(mid)
                added = True

        if added:
            await group.save()

    @staticmethod
    async def remove_member(group_id: str, user_id: str, target_user_id: str) -> None:
        """Remove a member from a group. Owner only. Cannot remove the owner."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        if target_user_id == group.owner:
            raise Forbidden("group.cannot_remove_owner", "Cannot remove the group owner")

        if target_user_id not in group.members:
            raise NotFound("member", target_user_id)

        group.members.remove(target_user_id)
        await group.save()

    @staticmethod
    async def add_agent(group_id: str, user_id: str, body: AddGroupAgentRequest) -> None:
        """Add an agent to a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        # Check if agent is already in the group
        for existing in group.agents:
            if existing.agent == body.agent_id:
                raise ValidationError(
                    "group.agent_already_added",
                    f"Agent '{body.agent_id}' is already in this group",
                )

        group.agents.append(
            GroupAgent(
                agent=body.agent_id,
                role=body.role,
                respond_mode=body.respond_mode,
            )
        )
        await group.save()

    @staticmethod
    async def update_agent(
        group_id: str, user_id: str, agent_id: str, body: UpdateGroupAgentRequest
    ) -> None:
        """Update an agent's respond_mode in a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        for agent in group.agents:
            if agent.agent == agent_id:
                agent.respond_mode = body.respond_mode
                await group.save()
                return

        raise NotFound("agent", agent_id)

    @staticmethod
    async def remove_agent(group_id: str, user_id: str, agent_id: str) -> None:
        """Remove an agent from a group. Owner only."""
        group = await _get_group_or_404(group_id)
        _require_group_admin(group, user_id)

        original_len = len(group.agents)
        group.agents = [a for a in group.agents if a.agent != agent_id]
        if len(group.agents) == original_len:
            raise NotFound("agent", agent_id)

        await group.save()

    @staticmethod
    async def get_or_create_dm(workspace_id: str, user_id: str, target_user_id: str) -> dict:
        """Find an existing DM between two users, or create one.

        DM groups have type="dm", sorted members, and name="DM".
        """
        members = sorted([user_id, target_user_id])

        existing = await Group.find_one(
            {
                "workspace": workspace_id,
                "type": "dm",
                "members": {"$all": members, "$size": len(members)},
            }
        )
        if existing:
            return await _group_response(existing)

        group = Group(
            workspace=workspace_id,
            name="DM",
            slug=_generate_slug("dm"),
            type="dm",
            members=members,
            owner=user_id,
        )
        await group.insert()
        return await _group_response(group)
