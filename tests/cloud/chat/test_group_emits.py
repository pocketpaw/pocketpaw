"""Tests that group_service emits realtime events via the bus.

Each public group_service mutation must fire the appropriate Event class
through ``emit()`` and, when membership/admin shifts, invalidate the
AudienceResolver cache. Tests run against a real Beanie in-memory
database (``mongo_db`` fixture) and assert on ``recording_bus.events``.
``_populate_lookups_for_domain_groups`` is monkey-patched out because it
hits User/Agent collections we don't seed; ``get_resolver`` is patched so
we can assert the cache-invalidate calls directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud.chat import group_service
from pocketpaw_ee.cloud.chat.schemas import (
    AddGroupAgentRequest,
    CreateGroupRequest,
    UpdateGroupAgentRequest,
    UpdateGroupRequest,
)
from pocketpaw_ee.cloud.models.agent import Agent as _AgentDoc
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.group import GroupAgent as _GroupAgentDoc
from pocketpaw_ee.cloud.realtime.events import (
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


async def _empty_lookups(_groups):
    """Stub for ``_populate_lookups_for_domain_groups`` — bypasses
    User/Agent Beanie queries that aren't seeded in unit tests."""
    return {}, {}


@pytest.fixture
def patched_lookups(monkeypatch):
    """Replace ``_populate_lookups_for_domain_groups`` with an empty stub."""
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.group_service._populate_lookups_for_domain_groups", _empty_lookups
    )


@pytest.fixture
def resolver_mock(monkeypatch):
    """Replace ``get_resolver()`` so we can assert invalidate_group calls."""
    rmock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.group_service.get_resolver", lambda: rmock)
    return rmock


async def _make_group(
    *,
    workspace: str = "w1",
    owner: str = "u1",
    name: str = "G",
    slug: str = "g",
    type: str = "private",
    members: list[str] | None = None,
    member_roles: dict[str, str] | None = None,
    agents: list[tuple[str, str, str]] | None = None,
) -> _GroupDoc:
    if members is None:
        members = [owner]
    agent_docs = [
        _GroupAgentDoc(agent=aid, role=arole, respond_mode=amode)
        for (aid, arole, amode) in agents or []
    ]
    doc = _GroupDoc(
        workspace=workspace,
        name=name,
        slug=slug,
        type=type,
        members=members,
        member_roles=member_roles or {},
        owner=owner,
        agents=agent_docs,
    )
    await doc.insert()
    return doc


async def _make_agent(
    *,
    workspace: str = "w1",
    owner: str = "u1",
    name: str = "Agent",
    slug: str = "agent",
) -> _AgentDoc:
    doc = _AgentDoc(workspace=workspace, name=name, slug=slug, owner=owner)
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# create_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_group_emits_group_created(mongo_db, recording_bus, patched_lookups):
    await group_service.create_group("w1", "u1", CreateGroupRequest(name="Test", member_ids=["u2"]))

    created = [e for e in recording_bus.events if isinstance(e, GroupCreated)]
    assert len(created) == 1
    assert set(created[0].data.get("member_ids", [])) == {"u1", "u2"}


# ---------------------------------------------------------------------------
# update_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_group_emits_group_updated(mongo_db, recording_bus, patched_lookups):
    group = await _make_group(owner="u1", member_roles={"u1": "admin"})

    await group_service.update_group(str(group.id), "u1", UpdateGroupRequest(name="NewName"))

    events = [e for e in recording_bus.events if isinstance(e, GroupUpdated)]
    assert len(events) == 1
    assert events[0].data["group_id"] == str(group.id)
    assert events[0].data["name"] == "NewName"


# ---------------------------------------------------------------------------
# archive_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_group_emits_group_updated_archived(mongo_db, recording_bus):
    group = await _make_group(owner="u1", member_roles={"u1": "admin"})

    await group_service.archive_group(str(group.id), "u1")

    events = [e for e in recording_bus.events if isinstance(e, GroupUpdated)]
    assert len(events) == 1
    assert events[0].data == {"group_id": str(group.id), "archived": True}


# ---------------------------------------------------------------------------
# join_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_group_emits_member_added_and_invalidates_cache(
    mongo_db, recording_bus, patched_lookups, resolver_mock
):
    group = await _make_group(owner="u1", type="public", members=["u1"])

    await group_service.join_group(str(group.id), "u2")

    events = [e for e in recording_bus.events if isinstance(e, GroupMemberAdded)]
    assert len(events) == 1
    assert events[0].data == {"group_id": str(group.id), "user_id": "u2", "role": "edit"}
    joined = [e for e in recording_bus.events if isinstance(e, GroupJoined)]
    assert len(joined) == 1
    assert joined[0].data["member_ids"] == ["u2"]
    assert joined[0].data["_id"] == str(group.id)
    resolver_mock.invalidate_group.assert_called_once_with(str(group.id))


@pytest.mark.asyncio
async def test_join_group_no_emit_when_already_member(mongo_db, recording_bus, resolver_mock):
    group = await _make_group(owner="u1", type="public", members=["u1", "u2"])

    await group_service.join_group(str(group.id), "u2")

    assert not [e for e in recording_bus.events if isinstance(e, GroupMemberAdded)]
    resolver_mock.invalidate_group.assert_not_called()


# ---------------------------------------------------------------------------
# leave_group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leave_group_emits_member_removed_and_invalidates_cache(
    mongo_db, recording_bus, resolver_mock
):
    group = await _make_group(owner="u1", members=["u1", "u2"])

    await group_service.leave_group(str(group.id), "u2")

    events = [e for e in recording_bus.events if isinstance(e, GroupMemberRemoved)]
    assert len(events) == 1
    assert events[0].data == {"group_id": str(group.id), "user_id": "u2"}
    resolver_mock.invalidate_group.assert_called_once_with(str(group.id))


# ---------------------------------------------------------------------------
# add_members
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_members_emits_one_event_per_newly_added_user(
    mongo_db, recording_bus, patched_lookups, resolver_mock
):
    group = await _make_group(owner="u1", members=["u1"], member_roles={"u1": "admin"})

    added = await group_service.add_members(str(group.id), "u1", ["u2", "u3", "u4"])

    assert added == ["u2", "u3", "u4"]
    events = [e for e in recording_bus.events if isinstance(e, GroupMemberAdded)]
    assert len(events) == 3
    assert {e.data["user_id"] for e in events} == {"u2", "u3", "u4"}
    for ev in events:
        assert ev.data["group_id"] == str(group.id)
        assert ev.data["role"] == "edit"
    joined = [e for e in recording_bus.events if isinstance(e, GroupJoined)]
    assert len(joined) == 1
    assert joined[0].data["member_ids"] == ["u2", "u3", "u4"]
    assert joined[0].data["_id"] == str(group.id)
    resolver_mock.invalidate_group.assert_called_once_with(str(group.id))


@pytest.mark.asyncio
async def test_add_members_skips_duplicates_and_does_not_invalidate_when_none_added(
    mongo_db, recording_bus, resolver_mock
):
    group = await _make_group(owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"})

    added = await group_service.add_members(str(group.id), "u1", ["u2"])

    assert added == []
    assert not [e for e in recording_bus.events if isinstance(e, GroupMemberAdded)]
    resolver_mock.invalidate_group.assert_not_called()


# ---------------------------------------------------------------------------
# remove_member
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_member_emits_member_removed_and_invalidates_cache(
    mongo_db, recording_bus, resolver_mock
):
    group = await _make_group(owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"})

    await group_service.remove_member(str(group.id), "u1", "u2")

    events = [e for e in recording_bus.events if isinstance(e, GroupMemberRemoved)]
    assert len(events) == 1
    assert events[0].data == {"group_id": str(group.id), "user_id": "u2"}
    resolver_mock.invalidate_group.assert_called_once_with(str(group.id))


# ---------------------------------------------------------------------------
# set_member_role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_member_role_emits_member_role_no_invalidation(
    mongo_db, recording_bus, resolver_mock
):
    group = await _make_group(owner="u1", members=["u1", "u2"], member_roles={"u1": "admin"})

    await group_service.set_member_role(str(group.id), "u1", "u2", "admin")

    events = [e for e in recording_bus.events if isinstance(e, GroupMemberRole)]
    assert len(events) == 1
    assert events[0].data == {"group_id": str(group.id), "user_id": "u2", "role": "admin"}
    resolver_mock.invalidate_group.assert_not_called()


# ---------------------------------------------------------------------------
# add_agent / update_agent / remove_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_agent_emits_agent_added(mongo_db, recording_bus):
    agent = await _make_agent(workspace="w1")
    group = await _make_group(workspace="w1", owner="u1", member_roles={"u1": "admin"})

    await group_service.add_agent(
        str(group.id),
        "u1",
        AddGroupAgentRequest(agent_id=str(agent.id), respond_mode="auto"),
    )

    events = [e for e in recording_bus.events if isinstance(e, GroupAgentAdded)]
    assert len(events) == 1
    assert events[0].data == {
        "group_id": str(group.id),
        "agent_id": str(agent.id),
        "respond_mode": "auto",
    }


@pytest.mark.asyncio
async def test_update_agent_emits_agent_updated(mongo_db, recording_bus):
    agent = await _make_agent(workspace="w1")
    group = await _make_group(
        workspace="w1",
        owner="u1",
        member_roles={"u1": "admin"},
        agents=[(str(agent.id), "assistant", "auto")],
    )

    await group_service.update_agent(
        str(group.id), "u1", str(agent.id), UpdateGroupAgentRequest(respond_mode="mention")
    )

    events = [e for e in recording_bus.events if isinstance(e, GroupAgentUpdated)]
    assert len(events) == 1
    assert events[0].data == {
        "group_id": str(group.id),
        "agent_id": str(agent.id),
        "respond_mode": "mention",
    }


@pytest.mark.asyncio
async def test_remove_agent_emits_agent_removed(mongo_db, recording_bus):
    agent = await _make_agent(workspace="w1")
    group = await _make_group(
        workspace="w1",
        owner="u1",
        member_roles={"u1": "admin"},
        agents=[(str(agent.id), "assistant", "auto")],
    )

    await group_service.remove_agent(str(group.id), "u1", str(agent.id))

    events = [e for e in recording_bus.events if isinstance(e, GroupAgentRemoved)]
    assert len(events) == 1
    assert events[0].data == {"group_id": str(group.id), "agent_id": str(agent.id)}


# ---------------------------------------------------------------------------
# Cross-tenant agent-on-group IDOR guard (Wave 2 task 9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_agent_rejects_cross_workspace_agent(mongo_db, recording_bus):
    """A workspace-A admin cannot wire a workspace-B agent into an A-side group.

    The service must raise NotFound (not Forbidden) — 404 keeps the agent's
    existence opaque to a probing attacker.
    """
    from pocketpaw_ee.cloud.shared.errors import NotFound

    foreign_agent = await _make_agent(workspace="w2", owner="u2", slug="foreign")
    group = await _make_group(workspace="w1", owner="u1", member_roles={"u1": "admin"})

    with pytest.raises(NotFound):
        await group_service.add_agent(
            str(group.id),
            "u1",
            AddGroupAgentRequest(agent_id=str(foreign_agent.id), respond_mode="auto"),
        )

    assert not [e for e in recording_bus.events if isinstance(e, GroupAgentAdded)]


@pytest.mark.asyncio
async def test_add_agent_rejects_unknown_agent(mongo_db, recording_bus):
    """A made-up agent_id must surface as NotFound, not Forbidden."""
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.shared.errors import NotFound

    group = await _make_group(workspace="w1", owner="u1", member_roles={"u1": "admin"})

    with pytest.raises(NotFound):
        await group_service.add_agent(
            str(group.id),
            "u1",
            AddGroupAgentRequest(agent_id=str(PydanticObjectId()), respond_mode="auto"),
        )


@pytest.mark.asyncio
async def test_update_agent_rejects_cross_workspace_agent(mongo_db, recording_bus):
    """update_agent must refuse a foreign-workspace agent even if the id
    happens to already be on the group's agents list (corrupt-state guard)."""
    from pocketpaw_ee.cloud.shared.errors import NotFound

    foreign_agent = await _make_agent(workspace="w2", owner="u2", slug="foreign")
    group = await _make_group(
        workspace="w1",
        owner="u1",
        member_roles={"u1": "admin"},
        agents=[(str(foreign_agent.id), "assistant", "auto")],
    )

    with pytest.raises(NotFound):
        await group_service.update_agent(
            str(group.id),
            "u1",
            str(foreign_agent.id),
            UpdateGroupAgentRequest(respond_mode="mention"),
        )

    assert not [e for e in recording_bus.events if isinstance(e, GroupAgentUpdated)]


@pytest.mark.asyncio
async def test_remove_agent_rejects_cross_workspace_agent(mongo_db, recording_bus):
    """remove_agent must refuse a foreign-workspace agent — guards against
    using a leaked agent_id to fingerprint or de-wire foreign agents."""
    from pocketpaw_ee.cloud.shared.errors import NotFound

    foreign_agent = await _make_agent(workspace="w2", owner="u2", slug="foreign")
    group = await _make_group(
        workspace="w1",
        owner="u1",
        member_roles={"u1": "admin"},
        agents=[(str(foreign_agent.id), "assistant", "auto")],
    )

    with pytest.raises(NotFound):
        await group_service.remove_agent(str(group.id), "u1", str(foreign_agent.id))

    assert not [e for e in recording_bus.events if isinstance(e, GroupAgentRemoved)]


# ---------------------------------------------------------------------------
# get_or_create_dm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_dm_emits_when_created(mongo_db, recording_bus, patched_lookups):
    await group_service.get_or_create_dm("w1", "u1", "u2")

    events = [e for e in recording_bus.events if isinstance(e, GroupCreated)]
    assert len(events) == 1
    assert set(events[0].data.get("member_ids", [])) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_get_or_create_dm_no_emit_when_existing(mongo_db, recording_bus, patched_lookups):
    await _make_group(workspace="w1", type="dm", members=["u1", "u2"], slug="dm")

    await group_service.get_or_create_dm("w1", "u1", "u2")

    assert not [e for e in recording_bus.events if isinstance(e, GroupCreated)]


# ---------------------------------------------------------------------------
# get_or_create_agent_dm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_agent_dm_emits_when_created(
    mongo_db, recording_bus, patched_lookups, monkeypatch
):
    fake_agent_doc = SimpleNamespace(
        id="a1",
        workspace="w1",
        owner="u1",
        visibility="workspace",
    )
    fake_agent_cls = MagicMock()
    fake_agent_cls.get = AsyncMock(return_value=fake_agent_doc)
    monkeypatch.setattr("pocketpaw_ee.cloud.models.agent.Agent", fake_agent_cls)
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.group_service.PydanticObjectId", lambda x: x)

    await group_service.get_or_create_agent_dm("w1", "u1", "a1")

    events = [e for e in recording_bus.events if isinstance(e, GroupCreated)]
    assert len(events) == 1
    assert events[0].data.get("member_ids") == ["u1"]


@pytest.mark.asyncio
async def test_get_or_create_agent_dm_no_emit_when_existing(
    mongo_db, recording_bus, patched_lookups, monkeypatch
):
    await _make_group(
        workspace="w1",
        type="dm",
        members=["u1"],
        slug="dm",
        agents=[("a1", "assistant", "auto")],
    )

    fake_agent_doc = SimpleNamespace(
        id="a1",
        workspace="w1",
        owner="u1",
        visibility="workspace",
    )
    fake_agent_cls = MagicMock()
    fake_agent_cls.get = AsyncMock(return_value=fake_agent_doc)
    monkeypatch.setattr("pocketpaw_ee.cloud.models.agent.Agent", fake_agent_cls)
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.group_service.PydanticObjectId", lambda x: x)

    await group_service.get_or_create_agent_dm("w1", "u1", "a1")

    assert not [e for e in recording_bus.events if isinstance(e, GroupCreated)]


@pytest.mark.asyncio
async def test_join_group_allows_channel_type(
    mongo_db, recording_bus, patched_lookups, resolver_mock
):
    """Channels should be self-joinable just like public groups."""
    group = await _make_group(owner="u1", type="channel", members=["u1"])

    await group_service.join_group(str(group.id), "u2")

    events = [e for e in recording_bus.events if isinstance(e, GroupMemberAdded)]
    assert len(events) == 1
    refreshed = await _GroupDoc.get(group.id)
    assert "u2" in refreshed.members
    resolver_mock.invalidate_group.assert_called_once_with(str(group.id))


@pytest.mark.asyncio
async def test_join_group_still_rejects_private(mongo_db, recording_bus):
    """Private groups must remain invite-only."""
    from pocketpaw_ee.cloud.shared.errors import Forbidden

    group = await _make_group(owner="u1", type="private", members=["u1"])

    with pytest.raises(Forbidden):
        await group_service.join_group(str(group.id), "u2")
