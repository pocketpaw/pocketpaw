"""Unread service — per-user unread counts across joined groups.

Paired with the ReadState model. Unread for a group is the number of
non-deleted messages with ``_id > last_read_message_id`` (the reader's own
messages and thread replies included); mention_unread is the cached counter
on the ReadState row.

``list_unreads`` reads all of the user's ReadStates in one ``$in`` query,
counts each group through the ``(group, _id)`` index, stops each count at
``UNREAD_CAP``, and runs the per-group counts bounded-concurrent. The client
renders anything over 99 as "99+", so the cap is invisible on screen.
``unread_count`` (the push body, "N new messages") stays exact.

2026-10-01 (feat/meetings-instant, MC-1): meeting rooms (``type="meeting"``) are
hidden, so ``list_unreads`` leaves them out — no sidebar badge for a room the
sidebar never shows.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.realtime.fanout import map_bounded
from pocketpaw_ee.cloud.chat.domain import MEETING_GROUP_TYPE
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.message import Message as _MessageDoc
from pocketpaw_ee.cloud.models.read_state import ReadState as _ReadStateDoc

# Per-group ceiling for the /unreads listing. Must stay above 99 so the
# client's ``> 99 ? "99+"`` badge still fires.
UNREAD_CAP = 100


async def _list_member_groups(user_id: str, workspace_id: str) -> list[_GroupDoc]:
    return await _GroupDoc.find(
        {
            "workspace": workspace_id,
            "archived": False,
            "members": user_id,
            "type": {"$ne": MEETING_GROUP_TYPE},
        }
    ).to_list()


async def _get_read_state(user_id: str, group_id: str) -> _ReadStateDoc | None:
    return await _ReadStateDoc.find_one({"user": user_id, "group": group_id})


async def _read_states_by_group(user_id: str, group_ids: list[str]) -> dict[str, _ReadStateDoc]:
    """All of the user's ReadStates for ``group_ids``, in one query."""
    if not group_ids:
        return {}
    rows = await _ReadStateDoc.find({"user": user_id, "group": {"$in": group_ids}}).to_list()
    return {r.group: r for r in rows}


async def _count_messages_after(
    group_id: str, last_message_id: str, *, cap: int | None = None
) -> int:
    """Count group messages with _id greater than last_message_id.

    ObjectIds sort monotonically by creation time, so $gt on _id works as
    an ordered cursor without a separate timestamp field. ``cap`` stops the
    count after that many rows.

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

    query = _MessageDoc.find(
        {
            "group": group_id,
            "_id": {"$gt": after},
            "deleted": False,
        }
    )
    if cap is not None:
        query = query.limit(cap)
    return await query.count()


async def list_unreads(user_id: str, workspace_id: str) -> list[dict]:
    """For each group the user is a member of, return
    ``{group_id, unread, mention_unread}``, ``unread`` capped at ``UNREAD_CAP``.

    A user with no ReadState row (never acked a read) OR a row whose
    ``last_read_message_id`` is the empty string (row was created by
    a ``bump_mention`` before any ack) both fall through to the
    group's ``message_count`` — treating everything as unread is the
    safe default; a subsequent ``mark_read`` corrects it.
    """
    groups = await _list_member_groups(user_id, workspace_id)
    states = await _read_states_by_group(user_id, [str(g.id) for g in groups])

    async def _row(group: _GroupDoc) -> dict:
        state = states.get(str(group.id))
        if state is None or not state.last_read_message_id:
            unread = min(group.message_count, UNREAD_CAP)
        else:
            unread = await _count_messages_after(
                str(group.id), state.last_read_message_id, cap=UNREAD_CAP
            )
        return {
            "group_id": str(group.id),
            "unread": unread,
            "mention_unread": state.mention_unread if state else 0,
        }

    return await map_bounded(groups, _row)


async def unread_count(user_id: str, group_id: str) -> int:
    """Unread message count for a single ``(user, group)`` pair.

    The per-group half of :func:`list_unreads`, exposed so callers that only
    care about one conversation (e.g. the push new-message notifier) can get a
    count without scanning every group the user belongs to. Same safe default:
    a user with no ReadState row — or a row whose ``last_read_message_id`` is
    the empty string — counts the group's full ``message_count`` (everything
    is unread until a ``mark_read`` corrects it).
    """
    state = await _get_read_state(user_id, group_id)
    if state is not None and state.last_read_message_id:
        return await _count_messages_after(group_id, state.last_read_message_id)

    try:
        group = await _GroupDoc.get(PydanticObjectId(group_id))
    except Exception:
        group = None
    return group.message_count if group else 0


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


__all__ = ["bump_mention", "list_unreads", "mark_read", "unread_count"]
