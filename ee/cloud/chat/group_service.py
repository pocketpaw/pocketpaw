"""Chat domain — group business logic (CRUD, membership, agents, DMs).

Sole owner of writes to the ``Group`` Beanie document. Module-level
``async def`` API. The doc → domain mapping helpers (formerly in
``repositories.py``) live alongside the public API as private helpers.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Literal

from beanie import PydanticObjectId

from ee.cloud.chat.domain import Group as _GroupDomain
from ee.cloud.chat.domain import GroupAgent as _GroupAgentDomain
from ee.cloud.chat.schemas import (
    AddGroupAgentRequest,
    CreateGroupRequest,
    UpdateGroupAgentRequest,
    UpdateGroupRequest,
)
from ee.cloud.models.group import Group as _GroupDoc
from ee.cloud.models.group import GroupAgent as _GroupAgentDoc
from ee.cloud.models.group import MemberRole
from ee.cloud.realtime.bus import get_resolver
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import (
    GroupAgentAdded,
    GroupAgentRemoved,
    GroupAgentUpdated,
    GroupCreated,
    GroupJoined,
    GroupMemberAdded,
    GroupMemberRemoved,
    GroupMemberRole,
    GroupUpdated,
)
from ee.cloud.shared.errors import Forbidden, NotFound, ValidationError
from ee.cloud.shared.time import iso_utc
from pocketpaw.ee.guards.actions import GroupRole
from pocketpaw.ee.guards.audit import log_denial

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Doc → domain mapping (formerly in repositories.py)
# ---------------------------------------------------------------------------


def _group_agent_to_domain(ga: _GroupAgentDoc) -> _GroupAgentDomain:
    return _GroupAgentDomain(agent_id=ga.agent, role=ga.role, respond_mode=ga.respond_mode)


def _group_doc_to_domain(doc: _GroupDoc) -> _GroupDomain:
    return _GroupDomain(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        slug=doc.slug,
        description=doc.description,
        icon=doc.icon,
        color=doc.color,
        type=doc.type,
        members=tuple(doc.members),
        member_roles=tuple(doc.member_roles.items()),
        agents=tuple(_group_agent_to_domain(a) for a in doc.agents),
        pinned_messages=tuple(doc.pinned_messages),
        owner=doc.owner,
        archived=doc.archived,
        last_message_at=doc.last_message_at,
        message_count=doc.message_count,
        created_at=getattr(doc, "createdAt", None),  # type: ignore[arg-type]
        updated_at=getattr(doc, "updatedAt", None),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Beanie helpers (used by message_service for membership / can-post checks)
# ---------------------------------------------------------------------------


async def _get_group_or_404(group_id: str) -> _GroupDoc:
    """Load a group Beanie doc by ID or raise NotFound."""
    group = await _GroupDoc.get(PydanticObjectId(group_id))
    if not group:
        raise NotFound("group", group_id)
    return group


async def _get_group_domain_or_none(group_id: str) -> _GroupDomain | None:
    try:
        doc = await _GroupDoc.get(PydanticObjectId(group_id))
    except Exception:
        return None
    return _group_doc_to_domain(doc) if doc else None


async def _get_group_domain_or_404(group_id: str) -> _GroupDomain:
    group = await _get_group_domain_or_none(group_id)
    if group is None:
        raise NotFound("group", group_id)
    return group


# ---------------------------------------------------------------------------
# Slug + auth helpers
# ---------------------------------------------------------------------------


def _generate_slug(name: str) -> str:
    """Lowercase, replace spaces/underscores with hyphens, strip non-alnum."""
    slug = name.lower().strip()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def _require_group_member(group: _GroupDoc, user_id: str) -> None:
    """Raise Forbidden if user is not a member of the (Beanie) group."""
    if user_id not in group.members:
        log_denial(
            actor=user_id,
            action="group.view",
            code="group.not_member",
            resource_id=str(group.id),
        )
        raise Forbidden("group.not_member", "You are not a member of this group")


def _require_group_admin(group: _GroupDoc, user_id: str) -> None:
    """Raise Forbidden if user is not a group admin or owner.

    Admin tier is derived from ``group.member_roles[user_id] == "admin"``.
    The group owner is always an implicit admin.
    """
    if group.owner == user_id:
        return
    if group.member_roles.get(user_id) == "admin":
        return
    log_denial(
        actor=user_id,
        action="group.admin",
        code="group.not_admin",
        resource_id=str(group.id),
    )
    raise Forbidden("group.not_admin", "Only group admins can perform this action")


def _role_for(group: _GroupDoc, user_id: str) -> Literal["owner", "admin", "edit", "view", "none"]:
    """Return the role of a user in a group.

    - "owner" if user_id == group.owner
    - member_roles[user_id] if present ("admin" | "edit" | "view")
    - "edit" if user is a member without an explicit role entry (back-compat default)
    - "none" if user is not a member
    """
    if group.owner == user_id:
        return "owner"
    if user_id not in group.members:
        return "none"
    explicit = group.member_roles.get(user_id)
    if explicit in ("admin", "edit", "view"):
        return explicit  # type: ignore[return-value]
    return "edit"


def resolve_group_role(group: _GroupDoc, user_id: str) -> GroupRole:
    """Structured role resolution for the canonical guards matrix.

    Raises Forbidden ``group.not_member`` if the user has no membership.
    """
    raw = _role_for(group, user_id)
    if raw == "none":
        raise Forbidden("group.not_member", "You are not a member of this group")
    return GroupRole.from_str("edit" if raw == "edit" else raw)


def _require_can_post(group: _GroupDoc, user_id: str) -> None:
    """Raise Forbidden if the user's role in the group cannot post / mutate."""
    role = _role_for(group, user_id)
    if role == "view":
        log_denial(
            actor=user_id,
            action="group.post",
            code="group.view_only",
            resource_id=str(group.id),
        )
        raise Forbidden("group.view_only", "You have read-only access in this group")
    if role == "none":
        log_denial(
            actor=user_id,
            action="group.post",
            code="group.not_member",
            resource_id=str(group.id),
        )
        raise Forbidden("group.not_member", "You are not a member of this group")


def _require_domain_group_member(group: _GroupDomain, user_id: str) -> None:
    """Raise Forbidden if user is not a member of the (domain) group."""
    if user_id not in group.members:
        log_denial(
            actor=user_id,
            action="group.view",
            code="group.not_member",
            resource_id=group.id,
        )
        raise Forbidden("group.not_member", "You are not a member of this group")


def _require_domain_group_admin(group: _GroupDomain, user_id: str) -> None:
    """Raise Forbidden if user is not a group admin or owner.

    Operates on the domain ``Group`` value object. ``member_roles`` is
    a tuple of (user_id, role) pairs on the domain entity.
    """
    if group.owner == user_id:
        return
    if dict(group.member_roles).get(user_id) == "admin":
        return
    log_denial(
        actor=user_id,
        action="group.admin",
        code="group.not_admin",
        resource_id=group.id,
    )
    raise Forbidden("group.not_admin", "Only group admins can perform this action")


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


async def _group_response(group: _GroupDoc) -> dict:
    """Convert a Group document to a frontend-compatible dict.

    Populates member IDs -> {_id, name, email} and agent IDs ->
    {_id, agent, name, role, respond_mode}.
    Uses batch queries to avoid N+1 per-member / per-agent lookups.
    """
    from ee.cloud.models.agent import Agent as AgentModel
    from ee.cloud.models.user import User

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
                "avatar": agent_doc.avatar if agent_doc else "",
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
        "memberRoles": dict(group.member_roles),
        "agents": populated_agents,
        "pinnedMessages": group.pinned_messages,
        "archived": group.archived,
        "lastMessageAt": iso_utc(group.last_message_at),
        "messageCount": group.message_count,
        "createdAt": iso_utc(group.createdAt),
    }


async def _populate_lookups_for_domain_groups(
    groups: list[_GroupDomain],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """Batch-load user + agent details for the given domain groups.

    Returns ``(users_by_id, agents_by_id)`` ready for
    ``group_to_wire_dict``. Two Mongo queries regardless of group count.
    """
    from ee.cloud.models.agent import Agent as AgentModel
    from ee.cloud.models.user import User

    all_user_ids: set[str] = set()
    all_agent_ids: set[str] = set()
    for g in groups:
        all_user_ids.update(g.members)
        for ga in g.agents:
            all_agent_ids.add(ga.agent_id)

    users_by_id: dict[str, dict[str, str]] = {}
    if all_user_ids:
        user_oids = []
        for uid in all_user_ids:
            try:
                user_oids.append(PydanticObjectId(uid))
            except Exception:
                pass
        user_docs = await User.find({"_id": {"$in": user_oids}}).to_list() if user_oids else []
        for u in user_docs:
            users_by_id[str(u.id)] = {
                "_id": str(u.id),
                "name": u.full_name or u.email,
                "email": u.email,
                "avatar": u.avatar,
            }

    agents_by_id: dict[str, dict[str, str]] = {}
    if all_agent_ids:
        agent_oids = []
        for aid in all_agent_ids:
            try:
                agent_oids.append(PydanticObjectId(aid))
            except Exception:
                pass
        agent_docs = (
            await AgentModel.find({"_id": {"$in": agent_oids}}).to_list() if agent_oids else []
        )
        for a in agent_docs:
            agents_by_id[str(a.id)] = {
                "_id": str(a.id),
                "name": a.name,
                "uname": a.slug,
                "avatar": a.avatar,
            }

    return users_by_id, agents_by_id


# ---------------------------------------------------------------------------
# Internal Beanie ops (formerly MongoGroupRepository methods)
# ---------------------------------------------------------------------------


async def _list_visible_in_workspace(workspace_id: str, user_id: str) -> list[_GroupDomain]:
    """Public/channel groups in the workspace + private/DM groups
    the user is a member of. Excludes archived."""
    docs = await _GroupDoc.find(
        {
            "workspace": workspace_id,
            "archived": False,
            "$or": [
                {"type": {"$in": ["public", "channel"]}},
                {"members": user_id},
            ],
        }
    ).to_list()
    return [_group_doc_to_domain(d) for d in docs]


async def _find_dm_between_users(workspace_id: str, members: list[str]) -> _GroupDomain | None:
    """Find a DM group whose members exactly equal the given set."""
    doc = await _GroupDoc.find_one(
        {
            "workspace": workspace_id,
            "type": "dm",
            "members": {"$all": members, "$size": len(members)},
        }
    )
    return _group_doc_to_domain(doc) if doc else None


async def _find_user_agent_dm(
    workspace_id: str, user_id: str, agent_id: str
) -> _GroupDomain | None:
    """Find a DM with exactly one user member and the given agent."""
    doc = await _GroupDoc.find_one(
        {
            "workspace": workspace_id,
            "type": "dm",
            "members": [user_id],
            "agents.agent": agent_id,
        }
    )
    return _group_doc_to_domain(doc) if doc else None


async def _create_group_doc(
    *,
    workspace_id: str,
    name: str,
    slug: str,
    owner: str,
    type: str,
    members: list[str],
    description: str = "",
    icon: str = "",
    color: str = "",
    agents: list[tuple[str, str, str]] | None = None,
) -> _GroupDomain:
    """Insert a new group and return its domain projection.

    ``agents`` is an optional list of ``(agent_id, role, respond_mode)``
    triples to attach at creation time (used by DM-with-agent).
    """
    agent_docs = (
        [
            _GroupAgentDoc(agent=aid, role=arole, respond_mode=amode)
            for (aid, arole, amode) in agents or []
        ]
        if agents
        else []
    )
    doc = _GroupDoc(
        workspace=workspace_id,
        name=name,
        slug=slug,
        description=description,
        type=type,
        icon=icon,
        color=color,
        members=members,
        owner=owner,
        agents=agent_docs,
    )
    await doc.insert()
    return _group_doc_to_domain(doc)


async def _update_group_fields(
    group_id: str,
    *,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    type: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    archived: bool | None = None,
) -> _GroupDomain:
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    if name is not None:
        doc.name = name
    if slug is not None:
        doc.slug = slug
    if description is not None:
        doc.description = description
    if type is not None:
        doc.type = type
    if icon is not None:
        doc.icon = icon
    if color is not None:
        doc.color = color
    if archived is not None:
        doc.archived = archived
    await doc.save()
    return _group_doc_to_domain(doc)


async def _add_member_doc(group_id: str, user_id: str, *, role: str | None = None) -> _GroupDomain:
    """Add user_id to the group's members list (idempotent).
    ``role`` optionally records the user's role in member_roles."""
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    changed = False
    if user_id not in doc.members:
        doc.members.append(user_id)
        changed = True
    if role is not None and doc.member_roles.get(user_id) != role:
        doc.member_roles[user_id] = role  # type: ignore[assignment]
        changed = True
    if changed:
        await doc.save()
    return _group_doc_to_domain(doc)


async def _add_members_doc(
    group_id: str, member_ids: list[str], *, role: str = "edit"
) -> tuple[_GroupDomain, list[str]]:
    """Batched member-add. Returns ``(group, newly_added_ids)``."""
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)

    newly_added: list[str] = []
    for mid in member_ids:
        if mid not in doc.members:
            doc.members.append(mid)
            newly_added.append(mid)
        if role in ("admin", "view"):
            doc.member_roles[mid] = role  # type: ignore[assignment]
        elif role == "edit" and mid in doc.member_roles:
            doc.member_roles.pop(mid, None)

    if newly_added or role in ("admin", "view"):
        await doc.save()
    return _group_doc_to_domain(doc), newly_added


async def _remove_member_doc(group_id: str, user_id: str) -> _GroupDomain:
    """Remove user_id from the group's members list (idempotent)."""
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    changed = False
    if user_id in doc.members:
        doc.members.remove(user_id)
        changed = True
    if user_id in doc.member_roles:
        del doc.member_roles[user_id]
        changed = True
    if changed:
        await doc.save()
    return _group_doc_to_domain(doc)


async def _set_member_role_doc(group_id: str, user_id: str, role: str) -> _GroupDomain:
    """Set member_roles[user_id] = role. ``role == "edit"`` clears the entry."""
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    if role == "edit":
        doc.member_roles.pop(user_id, None)
    else:
        doc.member_roles[user_id] = role  # type: ignore[assignment]
    await doc.save()
    return _group_doc_to_domain(doc)


async def _add_group_agent_doc(
    group_id: str, agent_id: str, *, role: str, respond_mode: str
) -> _GroupDomain:
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    doc.agents.append(_GroupAgentDoc(agent=agent_id, role=role, respond_mode=respond_mode))
    await doc.save()
    return _group_doc_to_domain(doc)


async def _update_group_agent_respond_mode_doc(
    group_id: str, agent_id: str, respond_mode: str
) -> _GroupDomain | None:
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    for agent in doc.agents:
        if agent.agent == agent_id:
            agent.respond_mode = respond_mode  # type: ignore[assignment]
            await doc.save()
            return _group_doc_to_domain(doc)
    return None


async def _remove_group_agent_doc(group_id: str, agent_id: str) -> _GroupDomain | None:
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    before = len(doc.agents)
    doc.agents = [a for a in doc.agents if a.agent != agent_id]
    if len(doc.agents) == before:
        return None
    await doc.save()
    return _group_doc_to_domain(doc)


async def _pin_message_doc(group_id: str, message_id: str) -> _GroupDomain:
    """Append a message id to the group's pinned_messages list (idempotent)."""
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    if message_id not in doc.pinned_messages:
        doc.pinned_messages.append(message_id)
        await doc.save()
    return _group_doc_to_domain(doc)


async def _unpin_message_doc(group_id: str, message_id: str) -> _GroupDomain | None:
    doc = await _GroupDoc.get(PydanticObjectId(group_id))
    if doc is None:
        raise NotFound("group", group_id)
    if message_id not in doc.pinned_messages:
        return None
    doc.pinned_messages.remove(message_id)
    await doc.save()
    return _group_doc_to_domain(doc)


async def bump_message_stats(group_id: str, *, last_message_at: datetime) -> None:
    """Atomic ``$set last_message_at, $inc message_count`` for the
    send-message hot path. Avoids the round-trip of load → mutate → save.

    Public so message_service can call it without poking private helpers.
    """
    await _GroupDoc.find_one(_GroupDoc.id == PydanticObjectId(group_id)).update(
        {
            "$set": {"last_message_at": last_message_at},
            "$inc": {"message_count": 1},
        }
    )


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


async def create_group(workspace_id: str, user_id: str, body: CreateGroupRequest) -> dict:
    """Create a group and add the creator as a member.

    For DMs: validates exactly one target member_id, auto-names as "DM".
    """
    from ee.cloud.chat.dto import group_to_wire_dict

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

    group = await _create_group_doc(
        workspace_id=workspace_id,
        name=name,
        slug=_generate_slug(name),
        owner=user_id,
        type=body.type,
        members=members,
        description=body.description,
        icon=body.icon,
        color=body.color,
    )
    users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([group])
    resp = group_to_wire_dict(group, users_by_id=users_by_id, agents_by_id=agents_by_id)
    await emit(GroupCreated(data={**resp, "member_ids": list(group.members)}))
    return resp


async def list_groups(workspace_id: str, user_id: str) -> list[dict]:
    """List groups visible to the user.

    Returns public groups in the workspace plus private/dm groups
    where the user is a member.
    """
    from ee.cloud.chat.dto import group_to_wire_dict

    groups = await _list_visible_in_workspace(workspace_id, user_id)
    if not groups:
        return []

    users_by_id, agents_by_id = await _populate_lookups_for_domain_groups(groups)
    return [
        group_to_wire_dict(g, users_by_id=users_by_id, agents_by_id=agents_by_id) for g in groups
    ]


async def get_group(group_id: str, user_id: str) -> dict:
    """Get a single group. Private/DM groups require membership."""
    from ee.cloud.chat.dto import group_to_wire_dict

    group = await _get_group_domain_or_404(group_id)
    if group.type in ("private", "dm"):
        _require_domain_group_member(group, user_id)
    users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([group])
    return group_to_wire_dict(group, users_by_id=users_by_id, agents_by_id=agents_by_id)


async def update_group(group_id: str, user_id: str, body: UpdateGroupRequest) -> dict:
    """Update group fields. Owner only. Cannot update DMs."""
    from ee.cloud.chat.dto import group_to_wire_dict

    group = await _get_group_domain_or_404(group_id)
    if group.type == "dm":
        raise Forbidden("group.cannot_update_dm", "DM groups cannot be updated")
    _require_domain_group_admin(group, user_id)

    new_slug = _generate_slug(body.name) if body.name is not None else None
    new_type = body.type if (body.type is not None and body.type != group.type) else None
    updated = await _update_group_fields(
        group_id,
        name=body.name,
        slug=new_slug,
        description=body.description,
        type=new_type,
        icon=body.icon,
        color=body.color,
    )
    patched = body.model_dump(exclude_unset=True)
    await emit(GroupUpdated(data={"group_id": group_id, **patched}))
    users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([updated])
    return group_to_wire_dict(updated, users_by_id=users_by_id, agents_by_id=agents_by_id)


async def archive_group(group_id: str, user_id: str) -> None:
    """Archive a group. Owner only."""
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)
    await _update_group_fields(group_id, archived=True)
    await emit(GroupUpdated(data={"group_id": group_id, "archived": True}))


async def join_group(group_id: str, user_id: str) -> None:
    """Join a public group. Adds user to members list."""
    from ee.cloud.chat.dto import group_to_wire_dict

    group = await _get_group_domain_or_404(group_id)
    if group.type not in ("public", "channel"):
        raise Forbidden(
            "group.not_joinable",
            "Only public groups and channels can be joined directly",
        )
    if group.archived:
        raise Forbidden("group.archived", "Cannot join an archived group")

    if user_id in group.members:
        return

    updated = await _add_member_doc(group_id, user_id)
    await emit(GroupMemberAdded(data={"group_id": group_id, "user_id": user_id, "role": "edit"}))
    users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([updated])
    resp = group_to_wire_dict(updated, users_by_id=users_by_id, agents_by_id=agents_by_id)
    await emit(GroupJoined(data={**resp, "member_ids": [user_id]}))
    get_resolver().invalidate_group(group_id)


async def leave_group(group_id: str, user_id: str) -> None:
    """Leave a group. Owner cannot leave (must transfer ownership first)."""
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_member(group, user_id)
    if group.owner == user_id:
        raise Forbidden(
            "group.owner_cannot_leave",
            "The group owner cannot leave. Transfer ownership first.",
        )
    await _remove_member_doc(group_id, user_id)
    await emit(GroupMemberRemoved(data={"group_id": group_id, "user_id": user_id}))
    get_resolver().invalidate_group(group_id)


async def add_members(
    group_id: str,
    user_id: str,
    member_ids: list[str],
    role: MemberRole = "edit",
) -> list[str]:
    """Add members to a group with an initial role. Owner only.

    Returns the list of user IDs that were newly added (skipping duplicates).
    """
    from ee.cloud.chat.dto import group_to_wire_dict

    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)
    if group.archived:
        raise Forbidden("group.archived", "Cannot modify an archived group")

    updated, newly_added = await _add_members_doc(group_id, member_ids, role=role)

    for added_user_id in newly_added:
        await emit(
            GroupMemberAdded(data={"group_id": group_id, "user_id": added_user_id, "role": role})
        )
    if newly_added:
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([updated])
        resp = group_to_wire_dict(updated, users_by_id=users_by_id, agents_by_id=agents_by_id)
        await emit(GroupJoined(data={**resp, "member_ids": newly_added}))
        get_resolver().invalidate_group(group_id)

    return newly_added


async def remove_member(group_id: str, user_id: str, target_user_id: str) -> None:
    """Remove a member from a group. Owner only. Cannot remove the owner."""
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)
    if target_user_id == group.owner:
        raise Forbidden("group.cannot_remove_owner", "Cannot remove the group owner")
    if target_user_id not in group.members:
        raise NotFound("member", target_user_id)
    await _remove_member_doc(group_id, target_user_id)
    await emit(GroupMemberRemoved(data={"group_id": group_id, "user_id": target_user_id}))
    get_resolver().invalidate_group(group_id)


async def set_member_role(
    group_id: str, user_id: str, target_user_id: str, role: MemberRole
) -> MemberRole:
    """Set a member's role to "edit" / "view" / "admin". Owner only."""
    if role not in ("admin", "edit", "view"):
        raise ValidationError(
            "group.invalid_role",
            f"Role must be one of 'admin', 'edit', 'view'; got {role!r}",
        )

    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)
    if target_user_id == group.owner:
        raise Forbidden("group.cannot_change_owner_role", "Cannot change the owner's role")
    if target_user_id not in group.members:
        raise NotFound("member", target_user_id)

    await _set_member_role_doc(group_id, target_user_id, role)
    await emit(
        GroupMemberRole(data={"group_id": group_id, "user_id": target_user_id, "role": role})
    )
    return role


async def add_agent(group_id: str, user_id: str, body: AddGroupAgentRequest) -> None:
    """Add an agent to a group. Owner only."""
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)

    for existing in group.agents:
        if existing.agent_id == body.agent_id:
            raise ValidationError(
                "group.agent_already_added",
                f"Agent '{body.agent_id}' is already in this group",
            )

    await _add_group_agent_doc(
        group_id, body.agent_id, role=body.role, respond_mode=body.respond_mode
    )
    await emit(
        GroupAgentAdded(
            data={
                "group_id": group_id,
                "agent_id": body.agent_id,
                "respond_mode": body.respond_mode,
            }
        )
    )


async def update_agent(
    group_id: str, user_id: str, agent_id: str, body: UpdateGroupAgentRequest
) -> None:
    """Update an agent's respond_mode in a group. Owner only."""
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)

    result = await _update_group_agent_respond_mode_doc(group_id, agent_id, body.respond_mode)
    if result is None:
        raise NotFound("agent", agent_id)
    await emit(
        GroupAgentUpdated(
            data={
                "group_id": group_id,
                "agent_id": agent_id,
                "respond_mode": body.respond_mode,
            }
        )
    )


async def remove_agent(group_id: str, user_id: str, agent_id: str) -> None:
    """Remove an agent from a group. Owner only."""
    group = await _get_group_domain_or_404(group_id)
    _require_domain_group_admin(group, user_id)

    result = await _remove_group_agent_doc(group_id, agent_id)
    if result is None:
        raise NotFound("agent", agent_id)
    await emit(GroupAgentRemoved(data={"group_id": group_id, "agent_id": agent_id}))


async def get_or_create_dm(workspace_id: str, user_id: str, target_user_id: str) -> dict:
    """Find an existing DM between two users, or create one."""
    from ee.cloud.chat.dto import group_to_wire_dict

    members = sorted([user_id, target_user_id])

    existing = await _find_dm_between_users(workspace_id, members)
    if existing is not None:
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([existing])
        return group_to_wire_dict(existing, users_by_id=users_by_id, agents_by_id=agents_by_id)

    group = await _create_group_doc(
        workspace_id=workspace_id,
        name="DM",
        slug=_generate_slug("dm"),
        owner=user_id,
        type="dm",
        members=members,
    )
    users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([group])
    resp = group_to_wire_dict(group, users_by_id=users_by_id, agents_by_id=agents_by_id)
    await emit(GroupCreated(data={**resp, "member_ids": list(group.members)}))
    return resp


async def get_or_create_agent_dm(workspace_id: str, user_id: str, agent_id: str) -> dict:
    """Find or create a 1:1 DM between the user and an agent."""
    from ee.cloud.chat.dto import group_to_wire_dict
    from ee.cloud.models.agent import Agent as AgentModel

    try:
        agent_oid = PydanticObjectId(agent_id)
    except Exception as exc:  # noqa: BLE001 - surface as NotFound
        raise NotFound("agent", agent_id) from exc

    agent_doc = await AgentModel.get(agent_oid)
    if not agent_doc:
        raise NotFound("agent", agent_id)

    visible = (
        (agent_doc.workspace == workspace_id and agent_doc.owner == user_id)
        or (agent_doc.workspace == workspace_id and agent_doc.visibility == "workspace")
        or agent_doc.visibility == "public"
    )
    if not visible:
        raise NotFound("agent", agent_id)

    existing = await _find_user_agent_dm(workspace_id, user_id, agent_id)
    if existing is not None:
        users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([existing])
        return group_to_wire_dict(existing, users_by_id=users_by_id, agents_by_id=agents_by_id)

    group = await _create_group_doc(
        workspace_id=workspace_id,
        name="DM",
        slug=_generate_slug("dm"),
        owner=user_id,
        type="dm",
        members=[user_id],
        agents=[(agent_id, "assistant", "auto")],
    )
    users_by_id, agents_by_id = await _populate_lookups_for_domain_groups([group])
    resp = group_to_wire_dict(group, users_by_id=users_by_id, agents_by_id=agents_by_id)
    await emit(GroupCreated(data={**resp, "member_ids": list(group.members)}))
    return resp


async def list_member_ids(group_id: str) -> list[str]:
    """Return the user_ids that are members of the group. Empty if missing."""
    group = await _get_group_domain_or_none(group_id)
    return list(group.members) if group else []


async def resolve_role_for_id(group_id: str, user_id: str) -> GroupRole:
    """Load a group by id and resolve the caller's ``GroupRole``.

    Used by the ``require_group_action`` FastAPI dependency so the
    Beanie load stays inside the service. Raises ``NotFound`` if the
    group is missing and ``Forbidden`` if the user has no membership
    (mirroring :func:`resolve_group_role`).
    """
    group = await _get_group_or_404(group_id)
    return resolve_group_role(group, user_id)


async def get_for_dispatch(group_id: str) -> _GroupDomain | None:
    """Load the domain group for cross-domain orchestrators (the agent
    bridge fan-out, audience resolvers). Returns ``None`` if missing —
    callers do their own NotFound shaping."""
    return await _get_group_domain_or_none(group_id)


async def seed_default_group(workspace_id: str, owner_id: str) -> _GroupDoc | None:
    """Insert the default ``General`` public channel for a freshly-created
    workspace. Returns the inserted Beanie doc (callers may ignore it).

    Skipped silently if the insert raises so the broader workspace-seed
    flow keeps going — callers log and proceed.
    """
    try:
        doc = _GroupDoc(
            workspace=workspace_id,
            name="General",
            slug="general",
            description="Default channel for team discussion",
            type="public",
            owner=owner_id,
            members=[owner_id],
        )
        await doc.insert()
        logger.info(
            "Default 'General' group seeded in workspace %s (id: %s)",
            workspace_id,
            doc.id,
        )
        return doc
    except Exception:
        logger.warning(
            "Failed to seed default 'General' group for workspace %s",
            workspace_id,
            exc_info=True,
        )
        return None


async def suggest_channels(workspace_id: str, q: str, *, limit: int = 8) -> list[dict]:
    """Return up to ``limit`` channel-type groups in the workspace that
    match the prefix-ish query. Used by the @-mention picker."""
    cquery: dict = {"workspace": workspace_id, "type": "channel", "archived": False}
    if q:
        cquery["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"slug": {"$regex": q, "$options": "i"}},
        ]
    docs = await _GroupDoc.find(cquery).limit(limit).to_list()
    return [
        {
            "type": "channel_ref",
            "id": str(c.id),
            "display_name": c.name or c.slug,
        }
        for c in docs
    ]


# Realtime helper kept for tests that still reference _fetch_group.
async def _fetch_group(group_id: str) -> Any:
    """Wrapped for testability."""
    try:
        oid = PydanticObjectId(group_id)
    except Exception:
        return None
    return await _GroupDoc.get(oid)


__all__ = [
    "_fetch_group",
    "_get_group_or_404",
    "_require_can_post",
    "_require_group_admin",
    "_require_group_member",
    "add_agent",
    "add_members",
    "archive_group",
    "bump_message_stats",
    "create_group",
    "get_group",
    "get_or_create_agent_dm",
    "get_or_create_dm",
    "join_group",
    "leave_group",
    "list_groups",
    "list_member_ids",
    "get_for_dispatch",
    "remove_agent",
    "resolve_role_for_id",
    "remove_member",
    "resolve_group_role",
    "seed_default_group",
    "set_member_role",
    "suggest_channels",
    "update_agent",
    "update_group",
]
