"""Unread service — per-user unread counts across joined groups.

Paired with the ReadState model. Unread for a group is the number of
messages with _id > last_read_message_id; mention_unread is the cached
counter on the ReadState row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import PydanticObjectId

from ee.cloud.models.group import Group as _GroupDoc
from ee.cloud.models.message import Message as _MessageDoc
from ee.cloud.models.read_state import ReadState as _ReadStateDoc


async def _list_member_groups(user_id: str, workspace_id: str) -> list[_GroupDoc]:
    return await _GroupDoc.find(
        {"workspace": workspace_id, "archived": False, "members": user_id}
    ).to_list()


async def _get_read_state(user_id: str, group_id: str) -> _ReadStateDoc | None:
    return await _ReadStateDoc.find_one({"user": user_id, "group": group_id})


async def _count_messages_after(group_id: str, last_message_id: str) -> int:
    """Count group messages with _id greater than last_message_id.

    ObjectIds sort monotonically by creation time, so $gt on _id works as
    an ordered cursor without a separate timestamp field.

    We filter on ``group`` alone (not ``context_type``) because legacy rows
    written before ``context_type`` existed in the schema have the field
    absent in MongoDB — a strict ``context_type="group"`` equality would
    exclude them and silently under-report unreads. The ``group`` field
    is set on group messages only, so it's a sufficient discriminator.
    """
    try:
        after = PydanticObjectId(last_message_id)
    except Exception:
        return 0

    return await _MessageDoc.find(
        {
            "group": group_id,
            "_id": {"$gt": after},
            "deleted": False,
        }
    ).count()


async def list_unreads(user_id: str, workspace_id: str) -> list[dict]:
    """For each group the user is a member of, return
    ``{group_id, unread, mention_unread}``.

    A user with no ReadState row (never acked a read) OR a row whose
    ``last_read_message_id`` is the empty string (row was created by
    a ``bump_mention`` before any ack) both fall through to the
    group's ``message_count`` — treating everything as unread is the
    safe default; a subsequent ``mark_read`` corrects it.
    """
    groups = await _list_member_groups(user_id, workspace_id)
    out: list[dict] = []
    for group in groups:
        state = await _get_read_state(user_id, str(group.id))
        if state is None or not state.last_read_message_id:
            unread = group.message_count
            mention_unread = state.mention_unread if state else 0
        else:
            unread = await _count_messages_after(str(group.id), state.last_read_message_id)
            mention_unread = state.mention_unread
        out.append({"group_id": str(group.id), "unread": unread, "mention_unread": mention_unread})
    return out


async def mark_read(user_id: str, group_id: str, last_message_id: str) -> None:
    """Upsert read state for (user, group). Resets mention_unread to 0.

    Uses an atomic ``find_one_and_update`` with ``upsert=True`` so that
    two concurrent callers racing on the same (user, group) pair can
    never both decide to insert — MongoDB serializes the upsert.
    """
    now = datetime.now(UTC)
    await _ReadStateDoc.get_pymongo_collection().find_one_and_update(
        {"user": user_id, "group": group_id},
        {
            "$set": {
                "last_read_message_id": last_message_id,
                "mention_unread": 0,
                "last_read_at": now,
            },
            "$setOnInsert": {"user": user_id, "group": group_id},
        },
        upsert=True,
    )


async def bump_mention(user_id: str, group_id: str) -> None:
    """Increment mention_unread for (user, group). Creates the row if
    missing with an empty ``last_read_message_id``.

    Uses an atomic ``$inc`` with ``upsert=True`` so concurrent broadcast
    mentions from two workers for the same recipient cannot produce a
    DuplicateKeyError (the unique index on ``(user, group)`` would
    otherwise reject the second insert).
    """
    now = datetime.now(UTC)
    await _ReadStateDoc.get_pymongo_collection().find_one_and_update(
        {"user": user_id, "group": group_id},
        {
            "$inc": {"mention_unread": 1},
            "$setOnInsert": {
                "user": user_id,
                "group": group_id,
                "last_read_message_id": "",
                "last_read_at": now,
            },
        },
        upsert=True,
    )


__all__ = ["bump_mention", "list_unreads", "mark_read"]
