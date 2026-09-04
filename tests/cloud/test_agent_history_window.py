"""Regression: rehydrated history must be the NEWEST turns, not the oldest.

``load_history_for_scope`` sorted ascending and applied ``limit``, which
returns the first N messages ever written to a scope. Past turn N the agent
replayed the same opening exchange forever and stopped seeing anything recent
— it appears to the user as an agent whose memory froze partway through the
conversation, while the transcript on screen keeps growing.

The correct form already exists a few files away, at
``ee/pocketpaw_ee/cloud/memory/mongo_store.py:372-374``: sort descending,
limit, then reverse so the caller still gets oldest-first ordering. The
returned window's ORDER was never the bug; which messages land in it was.

WHY THIS FILE HAS ITS OWN STUB
------------------------------
``tests/cloud/test_agent_router_history_rehydrate.py`` already covers this
function, and it could not have caught this. Its ``_StubFindChain.sort()``
records ``sorted = True`` and discards the argument, and its ``to_list()``
returns the seeded list untouched, so the stub yields identical output for an
ascending and a descending query. A fixture that cannot tell the two apart
cannot fail on the difference between them.

The stub below actually applies the sort key, the direction and the limit, so
these assertions test the query rather than the fixture's assumptions.
"""

from __future__ import annotations

import pytest

from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    load_history_for_scope,
)


class _Msg:
    """Only the attributes the loader actually reads, plus a sort key."""

    def __init__(self, *, created_at: int, role: str, content: str):
        self.createdAt = created_at
        self.role = role
        self.sender_type = "user" if role == "user" else "agent"
        self.content = content


class _FindChain:
    """Beanie's find(...).sort(...).limit(...).to_list(), honoured for real.

    Unlike the stub in test_agent_router_history_rehydrate.py, this one obeys
    the sort direction and the limit. That is the entire point: the bug lives
    in the interaction between those two, so a stub that ignores either cannot
    observe it.
    """

    def __init__(self, captured: dict, docs: list[_Msg]):
        self._captured = captured
        self._docs = list(docs)

    def sort(self, key):
        self._captured["sort"] = key
        descending = isinstance(key, str) and key.startswith("-")
        field = key.lstrip("-") if isinstance(key, str) else "createdAt"
        self._docs.sort(key=lambda d: getattr(d, field), reverse=descending)
        return self

    def limit(self, n):
        self._captured["limit"] = n
        self._docs = self._docs[:n]
        return self

    async def to_list(self):
        return list(self._docs)


class _MessageModel:
    _captured: dict = {}
    _docs: list[_Msg] = []

    @classmethod
    def configure(cls, docs: list[_Msg]) -> dict:
        cls._captured = {}
        cls._docs = docs
        return cls._captured

    @classmethod
    def find(cls, query):
        cls._captured["query"] = query
        return _FindChain(cls._captured, cls._docs)


def _ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )


def _conversation(turns: int) -> list[_Msg]:
    """``turns`` user/assistant pairs, numbered in write order."""
    docs: list[_Msg] = []
    for i in range(turns):
        docs.append(_Msg(created_at=2 * i, role="user", content=f"user {i}"))
        docs.append(_Msg(created_at=2 * i + 1, role="assistant", content=f"reply {i}"))
    return docs


@pytest.mark.asyncio
async def test_history_window_is_the_newest_turns(monkeypatch):
    """A long conversation must rehydrate its END, not its beginning."""
    captured = _MessageModel.configure(_conversation(turns=60))  # 120 messages
    import pocketpaw_ee.cloud.models.message as message_mod

    monkeypatch.setattr(message_mod, "Message", _MessageModel)

    result = await load_history_for_scope(_ctx(), limit=50)

    assert len(result) == 50
    # 120 messages, newest 50 => contents "user 35" .. "reply 59".
    assert result[0] == {"role": "user", "content": "user 35"}
    assert result[-1] == {"role": "assistant", "content": "reply 59"}


@pytest.mark.asyncio
async def test_history_window_stays_oldest_first(monkeypatch):
    """The window is the newest N, but the caller still gets them in order.

    The agent backends replay this list as a transcript, so reversed ordering
    would be its own bug. Pinned separately from the window itself so a
    regression names which half broke.
    """
    _MessageModel.configure(_conversation(turns=60))
    import pocketpaw_ee.cloud.models.message as message_mod

    monkeypatch.setattr(message_mod, "Message", _MessageModel)

    result = await load_history_for_scope(_ctx(), limit=50)

    numbers = [int(entry["content"].split()[-1]) for entry in result]
    assert numbers == sorted(numbers), "history must read oldest-first"


@pytest.mark.asyncio
async def test_short_conversation_is_returned_whole(monkeypatch):
    """Under the limit, nothing is dropped and order is unchanged."""
    _MessageModel.configure(_conversation(turns=3))  # 6 messages
    import pocketpaw_ee.cloud.models.message as message_mod

    monkeypatch.setattr(message_mod, "Message", _MessageModel)

    result = await load_history_for_scope(_ctx(), limit=50)

    assert result == [
        {"role": "user", "content": "user 0"},
        {"role": "assistant", "content": "reply 0"},
        {"role": "user", "content": "user 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "user 2"},
        {"role": "assistant", "content": "reply 2"},
    ]


@pytest.mark.asyncio
async def test_query_asks_mongo_for_the_newest_rows(monkeypatch):
    """The narrowing must happen in the query, not after the fetch.

    Sorting descending in Mongo and limiting there is what lets the compound
    (workspace_id, session_key, createdAt) index serve the window. Pulling
    everything and slicing in Python would satisfy the assertions above while
    still reading the whole thread off the wire on every turn.
    """
    captured = _MessageModel.configure(_conversation(turns=60))
    import pocketpaw_ee.cloud.models.message as message_mod

    monkeypatch.setattr(message_mod, "Message", _MessageModel)

    await load_history_for_scope(_ctx(), limit=50)

    assert captured["sort"] == "-createdAt", (
        "history must be fetched newest-first so limit() takes the recent end"
    )
    assert captured["limit"] == 50
