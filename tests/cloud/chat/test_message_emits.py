"""Tests that message_service emits realtime events via the bus.

Each public message_service mutation must fire the appropriate Event
class through ``emit()``. Tests run against a real Beanie in-memory
database (``mongo_db`` fixture) and assert on ``recording_bus.events``;
no fake repositories or seam-patching needed.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.chat.schemas import (
    EditMessageRequest,
    SendMessageRequest,
)
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.message import Message as _MessageDoc
from pocketpaw_ee.cloud.realtime.events import (
    MessageDeleted,
    MessageEdited,
    MessageNew,
    MessageReaction,
    MessageSent,
    UnreadUpdate,
)


async def _make_group(
    *,
    workspace: str = "w1",
    owner: str = "u1",
    members: list[str] | None = None,
    type: str = "private",
) -> _GroupDoc:
    """Insert a Group doc and return it."""
    if members is None:
        members = [owner]
    doc = _GroupDoc(
        workspace=workspace,
        name="G",
        slug="g",
        type=type,
        members=members,
        owner=owner,
    )
    await doc.insert()
    return doc


async def _make_message(
    *,
    group_id: str,
    sender: str = "u1",
    content: str = "hello",
) -> _MessageDoc:
    doc = _MessageDoc(
        context_type="group",
        group=group_id,
        sender=sender,
        sender_type="user",
        content=content,
    )
    await doc.insert()
    return doc


@pytest.mark.asyncio
async def test_send_message_emits_new_and_sent(mongo_db, recording_bus):
    group = await _make_group(owner="u1", members=["u1"])

    await message_service.send_message(str(group.id), "u1", SendMessageRequest(content="hi"))

    new_evs = [e for e in recording_bus.events if isinstance(e, MessageNew)]
    sent_evs = [e for e in recording_bus.events if isinstance(e, MessageSent)]
    assert len(new_evs) == 1
    assert len(sent_evs) == 1

    # message.new payload must carry sender so AudienceResolver can exclude.
    assert new_evs[0].data.get("sender") == "u1"
    # message.sent payload must carry sender_id so AudienceResolver can address it.
    assert sent_evs[0].data.get("sender_id") == "u1"


@pytest.mark.asyncio
async def test_edit_message_emits_edited(mongo_db, recording_bus):
    group = await _make_group(owner="u1", members=["u1"])
    msg = await _make_message(group_id=str(group.id), sender="u1", content="orig")

    await message_service.edit_message(str(msg.id), "u1", EditMessageRequest(content="new"))

    edits = [e for e in recording_bus.events if isinstance(e, MessageEdited)]
    assert len(edits) == 1
    assert edits[0].data["message_id"] == str(msg.id)
    assert edits[0].data["group_id"] == str(group.id)
    assert edits[0].data["content"] == "new"
    assert "edited_at" in edits[0].data


@pytest.mark.asyncio
async def test_delete_message_emits_deleted(mongo_db, recording_bus):
    group = await _make_group(owner="u1", members=["u1"])
    msg = await _make_message(group_id=str(group.id), sender="u1")

    await message_service.delete_message(str(msg.id), "u1")

    dels = [e for e in recording_bus.events if isinstance(e, MessageDeleted)]
    assert len(dels) == 1
    assert dels[0].data["message_id"] == str(msg.id)
    assert dels[0].data["group_id"] == str(group.id)

    # Verify soft-delete actually persisted.
    refreshed = await _MessageDoc.get(msg.id)
    assert refreshed.deleted is True


@pytest.mark.asyncio
async def test_toggle_reaction_emits_message_reaction(mongo_db, recording_bus):
    group = await _make_group(owner="u1", members=["u1"])
    msg = await _make_message(group_id=str(group.id), sender="u1")

    await message_service.toggle_reaction(str(msg.id), "u1", "\U0001f44d")

    reacts = [e for e in recording_bus.events if isinstance(e, MessageReaction)]
    assert len(reacts) == 1
    assert reacts[0].data["message_id"] == str(msg.id)
    assert reacts[0].data["group_id"] == str(group.id)
    assert reacts[0].data["emoji"] == "\U0001f44d"
    assert reacts[0].data["user_id"] == "u1"


def test_router_no_longer_broadcasts_message_events():
    """Regression guard: the four _ws_message_* handlers must not call manager.broadcast/send."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[3] / "ee/pocketpaw_ee/cloud/chat/router.py").read_text(
        encoding="utf-8"
    )

    start = src.index("async def _ws_message_send")
    end = src.index("async def _ws_typing")
    segment = src[start:end]
    assert "manager.broadcast_to_group" not in segment, (
        "message send/edit/delete/react handler still broadcasts directly — route via emit()"
    )
    assert "manager.send_to_user" not in segment, (
        "message handler still calls send_to_user directly"
    )


@pytest.mark.asyncio
async def test_send_message_fans_out_everyone_mention_to_all_members(
    mongo_db, recording_bus, monkeypatch
):
    """@everyone creates one notification per non-sender member and bumps their
    mention counter."""
    group = await _make_group(owner="sender", members=["sender", "u2", "u3"])

    created_notifs: list[dict] = []
    bumped: list[tuple[str, str]] = []

    async def fake_notif(**kwargs):
        created_notifs.append(kwargs)

    async def fake_bump(user_id, group_id):
        bumped.append((user_id, group_id))

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.message_service.notifications_service.create", fake_notif
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.message_service.unread_service.bump_mention", fake_bump
    )

    body = SendMessageRequest(
        content="hello team",
        mentions=[{"type": "everyone", "id": "", "display_name": "@everyone"}],
    )
    await message_service.send_message(str(group.id), "sender", body)

    recipients = {n["recipient"] for n in created_notifs}
    assert recipients == {"u2", "u3"}
    assert set(bumped) == {("u2", str(group.id)), ("u3", str(group.id))}


@pytest.mark.asyncio
async def test_send_message_user_and_broadcast_mention_dedupes(
    mongo_db, recording_bus, monkeypatch
):
    """If a message has both @user(u2) and @everyone, u2 only gets one notification."""
    group = await _make_group(owner="sender", members=["sender", "u2", "u3"])

    created_notifs: list[dict] = []

    async def fake_notif(**kwargs):
        created_notifs.append(kwargs)

    async def fake_bump(user_id, group_id):
        pass

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.message_service.notifications_service.create", fake_notif
    )
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.message_service.unread_service.bump_mention", fake_bump
    )

    body = SendMessageRequest(
        content="hi u2",
        mentions=[
            {"type": "user", "id": "u2", "display_name": "@u2"},
            {"type": "everyone", "id": "", "display_name": "@everyone"},
        ],
    )
    await message_service.send_message(str(group.id), "sender", body)

    recipients = sorted(n["recipient"] for n in created_notifs)
    assert recipients == ["u2", "u3"]  # no duplicate u2


@pytest.mark.asyncio
async def test_send_message_emits_unread_update_for_non_senders(mongo_db, recording_bus):
    """Every non-sender member should receive an unread.update event."""
    group = await _make_group(owner="sender", members=["sender", "u2", "u3"])

    await message_service.send_message(str(group.id), "sender", SendMessageRequest(content="hi"))

    updates = [e for e in recording_bus.events if isinstance(e, UnreadUpdate)]
    recipients = {e.data["user_id"] for e in updates}
    assert recipients == {"u2", "u3"}
    for e in updates:
        assert e.data["group_id"] == str(group.id)
        assert e.data["delta"] == 1


@pytest.mark.asyncio
async def test_send_reply_does_not_bump_thread_count(mongo_db, recording_bus):
    """Inline-quoted replies replaced threads: the parent message is fetched
    for preview only — never edited as a side effect of the reply send."""
    group = await _make_group(owner="sender", members=["sender", "u2"])
    parent = await _make_message(group_id=str(group.id), sender="u_other", content="parent")

    await message_service.send_message(
        str(group.id),
        "sender",
        SendMessageRequest(content="a reply", reply_to=str(parent.id)),
    )

    refreshed_parent = await _MessageDoc.get(parent.id)
    assert refreshed_parent.thread_count == 0
    assert refreshed_parent.edited is False
    assert refreshed_parent.deleted is False


@pytest.mark.asyncio
async def test_send_reply_emits_message_new_not_thread_reply(mongo_db, recording_bus):
    """Inline replies fan out via MessageNew; no ThreadReply event fires."""
    from pocketpaw_ee.cloud.realtime.events import ThreadReply

    group = await _make_group(owner="sender", members=["sender"])
    parent = await _make_message(group_id=str(group.id), sender="u_other")

    await message_service.send_message(
        str(group.id),
        "sender",
        SendMessageRequest(content="a reply", reply_to=str(parent.id)),
    )

    threads = [e for e in recording_bus.events if isinstance(e, ThreadReply)]
    news = [e for e in recording_bus.events if isinstance(e, MessageNew)]
    assert len(threads) == 0
    assert len(news) == 1
