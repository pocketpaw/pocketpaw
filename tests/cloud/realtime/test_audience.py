"""Tests for AudienceResolver."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.events import (
    GroupCreated,
    GroupMemberRemoved,
    MessageSent,
    NotificationNew,
    SessionCreated,
    WorkspaceInviteCreated,
    WorkspaceMemberRemoved,
)


@pytest.mark.asyncio
async def test_group_created_audience_is_member_ids_from_payload():
    # group.created uses the payload's member_ids so the *newly created* group is
    # visible to its new members without needing a DB lookup.
    r = AudienceResolver()
    ev = GroupCreated(data={"group_id": "g1", "member_ids": ["u1", "u2", "u3"]})
    assert set(await r.audience(ev)) == {"u1", "u2", "u3"}


@pytest.mark.asyncio
async def test_group_member_removed_includes_removed_user():
    async def members(_gid: str) -> list[str]:
        return ["u1", "u2"]

    r = AudienceResolver(group_members=members)
    ev = GroupMemberRemoved(data={"group_id": "g1", "user_id": "u3"})
    # Removed user must also get the event so their client can close the group.
    assert set(await r.audience(ev)) == {"u1", "u2", "u3"}


@pytest.mark.asyncio
async def test_workspace_member_removed_includes_removed_user():
    async def members(_wid: str) -> list[str]:
        return ["a", "b"]

    r = AudienceResolver(workspace_members=members)
    ev = WorkspaceMemberRemoved(data={"workspace_id": "w1", "user_id": "c"})
    assert set(await r.audience(ev)) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_message_sent_only_to_sender():
    r = AudienceResolver()
    ev = MessageSent(data={"group_id": "g1", "sender_id": "u1"})
    assert await r.audience(ev) == ["u1"]


@pytest.mark.asyncio
async def test_session_created_fanout_to_both_participants():
    r = AudienceResolver()
    ev = SessionCreated(data={"session_id": "s1", "user_id": "u1", "peer_id": "u2"})
    assert set(await r.audience(ev)) == {"u1", "u2"}


@pytest.mark.asyncio
async def test_notification_new_only_to_target_user():
    r = AudienceResolver()
    ev = NotificationNew(data={"id": "n1", "user_id": "u1", "kind": "mention"})
    assert await r.audience(ev) == ["u1"]


@pytest.mark.asyncio
async def test_workspace_invite_created_to_admins_plus_invitee_if_registered():
    async def admins(_wid: str) -> list[str]:
        return ["admin1", "admin2"]

    r = AudienceResolver(workspace_admins=admins)
    # Invitee is a known user
    ev = WorkspaceInviteCreated(
        data={"workspace_id": "w1", "invite_id": "i1", "email": "x@y", "user_id": "u5"}
    )
    assert set(await r.audience(ev)) == {"admin1", "admin2", "u5"}

    # Invitee is not yet a user (no user_id in payload)
    ev2 = WorkspaceInviteCreated(data={"workspace_id": "w1", "invite_id": "i1", "email": "x@y"})
    assert set(await r.audience(ev2)) == {"admin1", "admin2"}


@pytest.mark.asyncio
async def test_cache_hits_within_ttl_then_refetches():
    calls = {"n": 0}

    async def members(_gid: str) -> list[str]:
        calls["n"] += 1
        return ["u1", "u2"]

    r = AudienceResolver(group_members=members, cache_ttl_seconds=60)
    # group.created doesn't hit the cache (uses payload), so use GroupUpdated-like path:
    from pocketpaw_ee.cloud._core.realtime.events import GroupUpdated

    u = GroupUpdated(data={"group_id": "g1"})
    await r.audience(u)
    await r.audience(u)
    assert calls["n"] == 1, "second call within TTL should hit cache"

    # Invalidate → new fetch
    r.invalidate_group("g1")
    await r.audience(u)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_unknown_event_type_returns_empty_list():
    from pocketpaw_ee.cloud._core.realtime.events import Event

    r = AudienceResolver()
    assert await r.audience(Event(type="something.made.up", data={})) == []


@pytest.mark.asyncio
async def test_invalidate_user_peers_clears_peer_cache():
    calls = {"n": 0}

    async def peers(_uid: str) -> list[str]:
        calls["n"] += 1
        return ["p1"]

    r = AudienceResolver(workspace_peers=peers, cache_ttl_seconds=60)
    from pocketpaw_ee.cloud._core.realtime.events import PresenceOnline

    ev = PresenceOnline(data={"user_id": "u1"})
    await r.audience(ev)
    await r.audience(ev)
    assert calls["n"] == 1
    r.invalidate_user_peers("u1")
    await r.audience(ev)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_session_audience_dedupes_self_participants():
    r = AudienceResolver()
    from pocketpaw_ee.cloud._core.realtime.events import SessionUpdated

    ev = SessionUpdated(data={"session_id": "s1", "user_id": "u1", "peer_id": "u1"})
    assert await r.audience(ev) == ["u1"]


@pytest.mark.asyncio
async def test_session_audience_single_user_when_no_peer():
    r = AudienceResolver()
    from pocketpaw_ee.cloud._core.realtime.events import SessionCreated

    ev = SessionCreated(data={"session_id": "s1", "user_id": "u1"})
    assert await r.audience(ev) == ["u1"]


# --- Gap-fill routing (A6) -------------------------------------------------


@pytest.mark.asyncio
async def test_unread_update_routes_to_target_user():
    from pocketpaw_ee.cloud._core.realtime.events import UnreadUpdate

    r = AudienceResolver()
    ev = UnreadUpdate(data={"group_id": "g1", "user_id": "u1", "delta": 1})
    assert await r.audience(ev) == ["u1"]


@pytest.mark.asyncio
async def test_task_events_route_to_workspace_plus_recipients():
    async def ws_members(_wid: str) -> list[str]:
        return ["wm1", "wm2"]

    from pocketpaw_ee.cloud._core.realtime.events import (
        TaskBlocked,
        TaskClaimed,
        TaskProposed,
        TaskResolved,
        TaskUpdated,
    )

    r = AudienceResolver(workspace_members=ws_members)
    for cls in (TaskProposed, TaskUpdated, TaskClaimed, TaskResolved, TaskBlocked):
        ev = cls(
            data={
                "task_id": "t1",
                "workspace_id": "w1",
                "recipient_ids": ["creator", "assignee"],
            }
        )
        aud = await r.audience(ev)
        assert set(aud) == {"wm1", "wm2", "creator", "assignee"}, cls.__name__


@pytest.mark.asyncio
async def test_cycle_events_route_to_workspace_members():
    async def ws_members(_wid: str) -> list[str]:
        return ["a", "b", "c"]

    from pocketpaw_ee.cloud._core.realtime.events import (
        CycleClosed,
        CycleCreated,
        CycleSnapshotted,
        CycleUpdated,
    )

    r = AudienceResolver(workspace_members=ws_members)
    for cls in (CycleCreated, CycleUpdated, CycleClosed, CycleSnapshotted):
        ev = cls(data={"cycle_id": "c1", "workspace_id": "w1"})
        assert set(await r.audience(ev)) == {"a", "b", "c"}, cls.__name__


@pytest.mark.asyncio
async def test_project_events_route_to_workspace_members():
    async def ws_members(_wid: str) -> list[str]:
        return ["a", "b"]

    from pocketpaw_ee.cloud._core.realtime.events import (
        ProjectArchived,
        ProjectCreated,
        ProjectDeleted,
        ProjectUpdated,
    )

    r = AudienceResolver(workspace_members=ws_members)
    for cls in (ProjectCreated, ProjectUpdated, ProjectArchived, ProjectDeleted):
        ev = cls(data={"project_id": "p1", "workspace_id": "w1"})
        assert set(await r.audience(ev)) == {"a", "b"}, cls.__name__


@pytest.mark.asyncio
async def test_plan_events_route_to_workspace_members():
    async def ws_members(_wid: str) -> list[str]:
        return ["a", "b"]

    from pocketpaw_ee.cloud._core.realtime.events import (
        PlanGapResolved,
        PlanGenerated,
    )

    r = AudienceResolver(workspace_members=ws_members)
    for cls in (PlanGenerated, PlanGapResolved):
        ev = cls(data={"plan_session_id": "s1", "workspace_id": "w1"})
        assert set(await r.audience(ev)) == {"a", "b"}, cls.__name__


@pytest.mark.asyncio
async def test_pocket_outcome_routes_to_workspace_members():
    async def ws_members(_wid: str) -> list[str]:
        return ["a", "b"]

    from pocketpaw_ee.cloud._core.realtime.events import PocketOutcomeEvent

    r = AudienceResolver(workspace_members=ws_members)
    ev = PocketOutcomeEvent(
        data={
            "outcome": "renewal_completed",
            "pocket_id": "p1",
            "workspace_id": "w1",
            "action": "renew",
            "actor": "u1",
        }
    )
    assert set(await r.audience(ev)) == {"a", "b"}


@pytest.mark.asyncio
async def test_composio_events_route_to_single_user():
    from pocketpaw_ee.cloud._core.realtime.events import (
        ComposioConnectionMismatch,
        ComposioConnectionVerified,
    )

    r = AudienceResolver()
    for cls in (ComposioConnectionVerified, ComposioConnectionMismatch):
        ev = cls(data={"workspace_id": "w1", "user_id": "u1", "toolkit": "gmail"})
        assert await r.audience(ev) == ["u1"], cls.__name__
