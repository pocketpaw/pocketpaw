"""Mongo query-shape guards for cloud chat.

Covers the indexes the thread / unread / membership queries rely on, the
unread count (old semantics, batched ReadState read, capped at 100), the
room-list lookups (one projected user query for all groups), the bounded
reply list, regex escaping on mention suggest, and the concurrent reads in
mention suggest and ``get_messages``. mongomock can't ``explain()``, so index
coverage is asserted on the declared index list.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud.chat import group_service, message_service, unread_service
from pocketpaw_ee.cloud.models.agent import Agent as _AgentDoc
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.group import GroupAgent as _GroupAgentDoc
from pocketpaw_ee.cloud.models.message import Message as _MessageDoc
from pocketpaw_ee.cloud.models.read_state import ReadState as _ReadStateDoc
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.models.user import WorkspaceMembership

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("mongo_db")]


def _index_keys(model) -> list[list[tuple[str, int]]]:
    out = []
    for idx in model.Settings.indexes:
        keys = idx.document["key"].items() if hasattr(idx, "document") else idx
        out.append([(k, v) for k, v in keys])
    return out


async def _group(members: list[str], *, ws: str = "w1", **kw) -> _GroupDoc:
    g = _GroupDoc(workspace=ws, name=kw.pop("name", "G"), owner=members[0], members=members, **kw)
    await g.insert()
    return g


async def _msg(group: _GroupDoc, sender: str, content: str = "hi", **kw) -> _MessageDoc:
    m = _MessageDoc(
        context_type="group",
        group=str(group.id),
        sender=sender,
        sender_type="user",
        content=content,
        **kw,
    )
    await m.insert()
    return m


async def _user(email: str, full_name: str = "", ws: str = "w1") -> _UserDoc:
    u = _UserDoc(
        email=email,
        hashed_password="secret-hash",
        full_name=full_name,
        avatar=f"https://a/{email}",
        workspaces=[WorkspaceMembership(workspace=ws, role="member")],
    )
    await u.insert()
    return u


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


async def test_indexes_cover_thread_reply_unread_and_membership_queries() -> None:
    msg = _index_keys(_MessageDoc)
    assert [("thread_id", 1), ("createdAt", 1)] in msg
    assert [("reply_to", 1), ("createdAt", 1)] in msg
    assert [("group", 1), ("_id", 1)] in msg
    assert [("workspace", 1), ("members", 1), ("archived", 1)] in _index_keys(_GroupDoc)


# ---------------------------------------------------------------------------
# Unreads
# ---------------------------------------------------------------------------


async def test_unread_counts_keep_old_semantics() -> None:
    """Unread = non-deleted group rows with _id after the last-read message:
    the reader's own messages and thread replies count, deleted rows don't."""
    g = await _group(["u1", "u2"])
    await _msg(g, "u2")
    last = await _msg(g, "u1")
    await unread_service.mark_read("u1", str(g.id), str(last.id))
    parent = await _msg(g, "u2", "unread from peer")
    await _msg(g, "u1", "own message, still counted")
    await _msg(g, "u2", "deleted", deleted=True)
    await _msg(g, "u2", "thread reply", thread_id=str(parent.id))

    never_read = await _group(["u1", "u2"], message_count=7)
    mention_only = await _group(["u1", "u2"], message_count=4)
    await unread_service.bump_mention("u1", str(mention_only.id))
    await _group(["u2"])  # not a member: absent

    rows = {r["group_id"]: r for r in await unread_service.list_unreads("u1", "w1")}

    assert rows == {
        str(g.id): {"group_id": str(g.id), "unread": 3, "mention_unread": 0},
        str(never_read.id): {"group_id": str(never_read.id), "unread": 7, "mention_unread": 0},
        str(mention_only.id): {
            "group_id": str(mention_only.id),
            "unread": 4,
            "mention_unread": 1,
        },
    }
    # The push path's single-group count agrees and stays uncapped.
    assert await unread_service.unread_count("u1", str(g.id)) == 3


async def test_unread_counts_are_capped() -> None:
    g = await _group(["u1", "u2"])
    last = await _msg(g, "u2")
    await unread_service.mark_read("u1", str(g.id), str(last.id))
    await _MessageDoc.insert_many(
        [
            _MessageDoc(context_type="group", group=str(g.id), sender="u2", sender_type="user")
            for _ in range(unread_service.UNREAD_CAP + 5)
        ]
    )
    fresh = await _group(["u1"], message_count=5000)

    rows = {r["group_id"]: r["unread"] for r in await unread_service.list_unreads("u1", "w1")}

    assert rows == {str(g.id): unread_service.UNREAD_CAP, str(fresh.id): unread_service.UNREAD_CAP}
    assert unread_service.UNREAD_CAP == 100  # client renders > 99 as "99+"
    assert await unread_service.unread_count("u1", str(g.id)) == unread_service.UNREAD_CAP + 5


async def test_read_states_are_read_in_one_query() -> None:
    groups = [await _group(["u1"]) for _ in range(4)]
    for g in groups:
        await unread_service.mark_read("u1", str(g.id), str((await _msg(g, "u1")).id))

    calls = []
    real_find = _ReadStateDoc.find

    def counting_find(*args, **kwargs):
        calls.append(args)
        return real_find(*args, **kwargs)

    with (
        patch.object(_ReadStateDoc, "find", side_effect=counting_find),
        patch.object(_ReadStateDoc, "find_one", side_effect=AssertionError("per-group read")),
    ):
        rows = await unread_service.list_unreads("u1", "w1")

    assert [r["unread"] for r in rows] == [0, 0, 0, 0]
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Room list
# ---------------------------------------------------------------------------


async def test_room_list_payload_and_one_projected_user_query() -> None:
    alice = await _user("alice@x.io", "Alice Smith")
    bob = await _user("bob@x.io")
    agent = _AgentDoc(
        workspace="w1", name="Helper", slug="helper", avatar="av", owner=str(alice.id)
    )
    await agent.insert()
    for i in range(3):
        await _group(
            [str(alice.id), str(bob.id)],
            name=f"room{i}",
            agents=[_GroupAgentDoc(agent=str(agent.id))],
        )

    finds: list[tuple] = []
    real_coll = _UserDoc.get_pymongo_collection()

    class _Spy:
        def __getattr__(self, name):
            return getattr(real_coll, name)

        def find(self, *args, **kwargs):
            finds.append(args)
            return real_coll.find(*args, **kwargs)

    with patch.object(_UserDoc, "get_pymongo_collection", return_value=_Spy()):
        rooms = await group_service.list_groups("w1", str(alice.id))

    assert len(rooms) == 3
    assert len(finds) == 1, "user lookups must be one query across every room"
    projection = finds[0][1]
    assert "hashed_password" not in projection and set(projection) >= {"full_name", "email"}
    for room in rooms:
        assert room["members"] == [
            {
                "_id": str(alice.id),
                "name": "Alice Smith",
                "email": "alice@x.io",
                "avatar": "https://a/alice@x.io",
            },
            {
                "_id": str(bob.id),
                "name": "bob@x.io",
                "email": "bob@x.io",
                "avatar": "https://a/bob@x.io",
            },
        ]
        assert room["agents"][0]["name"] == "Helper"
        assert room["agents"][0]["uname"] == "helper"
        assert room["agents"][0]["avatar"] == "av"


# ---------------------------------------------------------------------------
# Reply list
# ---------------------------------------------------------------------------


async def test_reply_list_is_bounded_with_an_after_cursor() -> None:
    g = await _group(["u1"])
    parent = await _msg(g, "u1", "parent")
    for i in range(5):
        await _msg(g, "u1", f"r{i}", reply_to=str(parent.id))

    first = await message_service.get_thread(str(parent.id), "u1", limit=2)
    assert [r["content"] for r in first] == ["r0", "r1"]
    cursor = f"{first[-1]['createdAt']}|{first[-1]['_id']}"
    rest = await message_service.get_thread(str(parent.id), "u1", after=cursor, limit=10)
    assert [r["content"] for r in rest] == ["r2", "r3", "r4"]
    # No arguments: still the full list for a small thread, capped by default.
    assert len(await message_service.get_thread(str(parent.id), "u1")) == 5
    assert message_service.THREAD_REPLY_LIMIT == 200


# ---------------------------------------------------------------------------
# Mention suggest
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q", [".*", "(a+)+$", "[", "a|b", "\\"])
async def test_mention_inputs_are_literal(q: str) -> None:
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.auth import service as auth_service

    await _user("plain@x.io", "Plain User")
    await _AgentDoc(workspace="w1", name="Plain", slug="plain", owner="u1").insert()
    await _group(["u1"], name="plain", slug="plain", type="channel")

    assert await auth_service.suggest_workspace_members("w1", q) == []
    assert await agents_service.suggest_for_mentions("w1", q) == []
    assert await group_service.suggest_channels("w1", q) == []


async def test_mention_suggest_matches_normal_input() -> None:
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.auth import service as auth_service

    alice = await _user("alice@x.io", "Alice Smith")
    await _user("dot.star@x.io", "a.*b")
    await _user("other@elsewhere.io", "Alice Other", ws="w2")
    await _AgentDoc(workspace="w1", name="Research Bot", slug="research", owner="u1").insert()
    await _group(["u1"], name="Design Crit", slug="design-crit", type="channel")

    names = lambda rows: sorted(r["display_name"] for r in rows)  # noqa: E731
    assert names(await auth_service.suggest_workspace_members("w1", "ali")) == ["Alice Smith"]
    # Substring semantics are kept: a last name or an email domain still match.
    assert names(await auth_service.suggest_workspace_members("w1", "SMITH")) == ["Alice Smith"]
    assert names(await auth_service.suggest_workspace_members("w1", ".*")) == ["a.*b"]
    assert (await auth_service.suggest_workspace_members("w1", "alice"))[0]["id"] == str(alice.id)
    assert names(await agents_service.suggest_for_mentions("w1", "bot")) == ["Research Bot"]
    assert names(await group_service.suggest_channels("w1", "crit")) == ["Design Crit"]


async def _all_started(n: int):
    """Return (enter, event): each call to ``enter`` blocks until n have entered."""
    entered = []
    ready = asyncio.Event()

    async def enter(value):
        entered.append(1)
        if len(entered) == n:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=2)
        return value

    return enter


async def test_mention_suggest_runs_the_three_lookups_concurrently() -> None:
    from pocketpaw_ee.cloud.chat.router import suggest_mentions

    enter = await _all_started(3)

    async def users(*_a, **_k):
        return await enter([{"type": "user", "id": "u", "display_name": "U"}])

    async def agents(*_a, **_k):
        return await enter([{"type": "agent", "id": "a", "display_name": "A"}])

    async def channels(*_a, **_k):
        return await enter([{"type": "channel_ref", "id": "c", "display_name": "C"}])

    with (
        patch("pocketpaw_ee.cloud.auth.service.suggest_workspace_members", users),
        patch("pocketpaw_ee.cloud.agents.service.suggest_for_mentions", agents),
        patch("pocketpaw_ee.cloud.chat.group_service.suggest_channels", channels),
    ):
        out = await suggest_mentions(
            q="", types="user,agent,channel", workspace_id="w1", user_id="u1"
        )

    assert [r["type"] for r in out] == [
        "user",
        "agent",
        "channel_ref",
        "here",
        "channel",
        "everyone",
    ]


async def test_get_messages_reads_history_and_active_run_concurrently() -> None:
    g = await _group(["u1"])
    enter = await _all_started(2)

    async def history(*_a, **_k):
        return await enter([])

    async def active(*_a, **_k):
        return await enter(None)

    with (
        patch.object(message_service, "_list_for_group_paged", history),
        patch("pocketpaw_ee.cloud.chat.runs.service.find_active_run_for_scope", active),
    ):
        page = await message_service.get_messages(str(g.id), "u1")

    assert page == {"items": [], "nextCursor": None, "hasMore": False, "active_run": None}
