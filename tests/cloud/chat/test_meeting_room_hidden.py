# tests/cloud/chat/test_meeting_room_hidden.py — meeting rooms stay hidden.
#
# Created 2026-10-01 (feat/meetings-instant, MC-1). A meeting runs in a
# ``type="meeting"`` chat group. These pin that such a room is absent from every
# backend room surface (room list, workspace message search, unread badges,
# workspace room count, mention fan-out to non-members), that it is members-only
# (get_group, get_messages), and that it can't be retyped into a listed room.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat import group_service, message_service, unread_service
from pocketpaw_ee.cloud.chat.schemas import SendMessageRequest, UpdateGroupRequest
from pocketpaw_ee.cloud.models.group import Group
from pocketpaw_ee.cloud.models.message import Message
from pocketpaw_ee.cloud.models.notification import Notification
from pocketpaw_ee.cloud.shared.errors import Forbidden

WS = "w1"
HOST = "u-host"


async def _meeting_room() -> str:
    return await group_service.create_meeting_room(WS, HOST, "Design sync")


async def _private_room() -> str:
    g = Group(workspace=WS, name="team", slug="team", type="private", owner=HOST, members=[HOST])
    await g.insert()
    return str(g.id)


async def _msg(group_id: str, content: str) -> None:
    await Message(
        context_type="group", group=group_id, sender=HOST, sender_type="user", content=content
    ).insert()


async def test_room_list_leaves_out_meeting_rooms(mongo_db) -> None:
    meeting = await _meeting_room()
    private = await _private_room()

    ids = {g["_id"] for g in await group_service.list_groups(WS, HOST)}

    assert private in ids
    assert meeting not in ids


async def test_workspace_message_search_leaves_out_meeting_rooms(mongo_db) -> None:
    meeting = await _meeting_room()
    private = await _private_room()
    await _msg(meeting, "roadmap notes from the meeting")
    await _msg(private, "roadmap notes from the team room")

    hits = await message_service.search_workspace_messages(WS, HOST, "roadmap")

    assert {h["group"] for h in hits} == {private}


async def test_unread_badges_leave_out_meeting_rooms(mongo_db) -> None:
    meeting = await _meeting_room()
    private = await _private_room()
    await Group.find_one(Group.workspace == WS).update({"$set": {"message_count": 3}})

    rows = await unread_service.list_unreads(HOST, WS)

    ids = {r["group_id"] for r in rows}
    assert private in ids
    assert meeting not in ids


async def test_workspace_room_count_leaves_out_meeting_rooms(mongo_db) -> None:
    from pocketpaw_ee.cloud.models.workspace import Workspace
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    ws = Workspace(name="Acme", slug="acme", owner=HOST)
    await ws.insert()
    ws_id = str(ws.id)
    await group_service.create_meeting_room(ws_id, HOST, "Design sync")
    await Group(workspace=ws_id, name="gen", slug="gen", type="public", owner=HOST).insert()

    preview = await workspace_service.get_delete_preview(ws_id)

    assert preview["room_count"] == 1


async def test_mention_of_a_non_member_in_a_meeting_room_notifies_nobody(
    mongo_db, recording_bus, monkeypatch
) -> None:
    async def _no_bump(_u, _g):
        return None

    monkeypatch.setattr(message_service.unread_service, "bump_mention", _no_bump)
    meeting = await _meeting_room()

    body = SendMessageRequest(
        content="@outsider look at this",
        mentions=[{"type": "user", "id": "u-outsider", "display_name": "@outsider"}],
    )
    await message_service.send_message(meeting, HOST, body)

    rows = await Notification.find_all().to_list()
    assert "u-outsider" not in {r.recipient for r in rows}


async def test_meeting_room_is_members_only(mongo_db) -> None:
    meeting = await _meeting_room()

    with pytest.raises(Forbidden):
        await group_service.get_group(meeting, "u-outsider")
    with pytest.raises(Forbidden):
        await message_service.get_messages(meeting, "u-outsider")
    # The host still reads it.
    assert (await group_service.get_group(meeting, HOST))["_id"] == meeting


async def test_meeting_room_cannot_be_retyped_into_a_listed_room(mongo_db) -> None:
    meeting = await _meeting_room()

    with pytest.raises(Forbidden):
        await group_service.update_group(meeting, HOST, UpdateGroupRequest(type="public"))


async def test_meeting_room_is_not_created_with_a_group_event(mongo_db, recording_bus) -> None:
    await _meeting_room()

    assert not [e for e in recording_bus.events if e.type == "group.created"]
