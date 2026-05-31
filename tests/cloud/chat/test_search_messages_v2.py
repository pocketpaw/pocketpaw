"""End-to-end test for ``message_service.search_messages``.

Inserts real ``Message`` Beanie docs into an in-memory mongomock-motor
database (via the ``mongo_db`` fixture) and asserts on the wire shape
produced by ``_message_response`` / ``message_to_wire_dict``.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.message import Attachment as _AttachmentDoc
from pocketpaw_ee.cloud.models.message import Mention as _MentionDoc
from pocketpaw_ee.cloud.models.message import Message as _MessageDoc
from pocketpaw_ee.cloud.models.message import Reaction as _ReactionDoc


async def _make_group(*, type: str = "public", owner: str = "u1") -> _GroupDoc:
    doc = _GroupDoc(
        workspace="w1",
        name="G",
        slug="g",
        type=type,
        members=[owner],
        owner=owner,
    )
    await doc.insert()
    return doc


async def _make_message(*, group_id: str, content: str, sender: str = "u1") -> _MessageDoc:
    doc = _MessageDoc(
        context_type="group",
        group=group_id,
        sender=sender,
        sender_type="user",
        content=content,
        mentions=[_MentionDoc(type="user", id="u2", display_name="Bob")],
        attachments=[
            _AttachmentDoc(type="file", url="https://x/y.pdf", name="y.pdf", meta={"size": 1024})
        ],
        reactions=[_ReactionDoc(emoji="👍", users=["u1", "u2"])],
    )
    await doc.insert()
    return doc


@pytest.mark.asyncio
async def test_search_messages_finds_matching_content(mongo_db):
    group = await _make_group()
    await _make_message(group_id=str(group.id), content="hello world")
    await _make_message(group_id=str(group.id), content="goodbye")
    await _make_message(group_id=str(group.id), content="world peace")

    results = await message_service.search_messages(str(group.id), "u1", "world")

    assert len(results) == 2
    contents = sorted(r["content"] for r in results)
    assert contents == ["hello world", "world peace"]


@pytest.mark.asyncio
async def test_search_messages_wire_shape_matches_legacy(mongo_db):
    """Wire keys + value types match what ``_message_response`` produced."""
    group = await _make_group()
    await _make_message(group_id=str(group.id), content="alpha")

    out = await message_service.search_messages(str(group.id), "u1", "alpha")

    item = out[0]
    assert set(item.keys()) == {
        "_id",
        "group",
        "sender",
        "senderType",
        "senderName",
        "agent",
        "content",
        "mentions",
        "replyTo",
        "replyPreview",
        "threadCount",
        "threadId",
        "isThreadParent",
        "attachments",
        "reactions",
        "edited",
        "editedAt",
        "deleted",
        "createdAt",
    }
    assert item["senderType"] == "user"
    assert item["mentions"] == [{"type": "user", "id": "u2", "display_name": "Bob"}]
    assert item["attachments"][0]["meta"] == {"size": 1024}
    assert item["reactions"][0]["users"] == ["u1", "u2"]


@pytest.mark.asyncio
async def test_search_messages_private_group_requires_membership(mongo_db):
    """Auth check still fires before the search runs."""
    from pocketpaw_ee.cloud.shared.errors import Forbidden

    group = await _make_group(type="private", owner="u_other")
    await _make_message(group_id=str(group.id), content="anything", sender="u_other")

    with pytest.raises(Forbidden):
        await message_service.search_messages(str(group.id), "u1", "anything")
