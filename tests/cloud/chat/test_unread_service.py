"""UnreadService — derive per-group unread counts and mention counts for a user."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_list_unreads_zero_when_caught_up():
    """If last_read matches the group's latest message, unread is 0."""
    from pocketpaw_ee.cloud.chat import unread_service

    group = SimpleNamespace(id="g1", workspace="w1", message_count=5)
    state = SimpleNamespace(user="u1", group="g1", last_read_message_id="m5", mention_unread=0)

    with (
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._list_member_groups",
            new=AsyncMock(return_value=[group]),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._get_read_state",
            new=AsyncMock(return_value=state),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._count_messages_after",
            new=AsyncMock(return_value=0),
        ),
    ):
        result = await unread_service.list_unreads("u1", "w1")

    assert result == [{"group_id": "g1", "unread": 0, "mention_unread": 0}]


@pytest.mark.asyncio
async def test_list_unreads_counts_messages_after_last_read():
    from pocketpaw_ee.cloud.chat import unread_service

    group = SimpleNamespace(id="g1", workspace="w1", message_count=10)
    state = SimpleNamespace(user="u1", group="g1", last_read_message_id="m5", mention_unread=2)

    with (
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._list_member_groups",
            new=AsyncMock(return_value=[group]),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._get_read_state",
            new=AsyncMock(return_value=state),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._count_messages_after",
            new=AsyncMock(return_value=5),
        ),
    ):
        result = await unread_service.list_unreads("u1", "w1")

    assert result == [{"group_id": "g1", "unread": 5, "mention_unread": 2}]


@pytest.mark.asyncio
async def test_list_unreads_fresh_user_has_full_count():
    """A user who has never read a group has its whole message_count as unread."""
    from pocketpaw_ee.cloud.chat import unread_service

    group = SimpleNamespace(id="g1", workspace="w1", message_count=10)
    with (
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._list_member_groups",
            new=AsyncMock(return_value=[group]),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._get_read_state",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await unread_service.list_unreads("u1", "w1")

    assert result == [{"group_id": "g1", "unread": 10, "mention_unread": 0}]


@pytest.mark.asyncio
async def test_list_unreads_empty_last_read_falls_through_to_message_count():
    """If a ReadState row exists but last_read_message_id is '' (created by
    bump_mention before any ack), fall through to message_count — never pass
    an empty string to _count_messages_after which would silently return 0
    and under-report unreads to 0 while mention_unread is non-zero."""
    from pocketpaw_ee.cloud.chat import unread_service

    group = SimpleNamespace(id="g1", workspace="w1", message_count=10)
    state = SimpleNamespace(user="u1", group="g1", last_read_message_id="", mention_unread=3)

    with (
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._list_member_groups",
            new=AsyncMock(return_value=[group]),
        ),
        patch(
            "pocketpaw_ee.cloud.chat.unread_service._get_read_state",
            new=AsyncMock(return_value=state),
        ),
    ):
        result = await unread_service.list_unreads("u1", "w1")

    assert result == [{"group_id": "g1", "unread": 10, "mention_unread": 3}]
