# Regression: get_session is bounded, and bounded from the RIGHT END.
#
# Created 2026-09-04 (backend-perf M2). get_session read every message a
# session had ever accumulated. It is called from the agent loop, the sessions
# API, the dashboard and the memory manager, so a session with 10,000 turns
# loaded all 10,000 on each of those calls.
#
# The direction is the part that needs a guard, not the bound. Taking the first
# N of an ascending sort is the cheap-looking thing to write and is exactly the
# defect #2075 fixed in the chat history window: the agent was rehydrated with
# the first fifty messages a scope ever had and answered as if the last hour had
# not happened. Nothing about that failure is visible in a status code, and a
# test that only counts results cannot see it either.
#
# So every store here is checked for CONTENT, not length alone.
#
# What each test would catch (mutations in tests/mutations/session_bounds.json):
#   - slice the head instead of the tail   -> test_*_returns_the_newest
#   - drop the bound entirely              -> test_*_is_bounded
#   - bound the backward paginator         -> test_paging_backward_still_reaches
#   - drop the projection in the run query -> tests/cloud/test_active_run_scopes.py

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pocketpaw.memory.file_store import FileMemoryStore
from pocketpaw.memory.protocol import DEFAULT_SESSION_HISTORY_LIMIT, MemoryEntry, MemoryType


def _entries(n: int) -> list[dict]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "id": f"m{i:04d}",
            "content": f"message-{i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "timestamp": (base + timedelta(minutes=i)).isoformat(),
            "metadata": {},
        }
        for i in range(n)
    ]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("POCKETPAW_DATA_DIR", str(tmp_path))
    s = FileMemoryStore(base_path=tmp_path / "memory")
    return s


def _seed(store, session_key: str, n: int) -> None:
    path = store._get_session_file(session_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_entries(n)), encoding="utf-8")


class TestFileStoreBounds:
    async def test_is_bounded_by_default(self, store):
        _seed(store, "s1", DEFAULT_SESSION_HISTORY_LIMIT + 250)
        got = await store.get_session("s1")
        assert len(got) == DEFAULT_SESSION_HISTORY_LIMIT

    async def test_returns_the_newest_not_the_oldest(self, store):
        """The whole point. A bound applied to the head silently rewinds the
        conversation, and every count-based assertion still passes."""
        total = DEFAULT_SESSION_HISTORY_LIMIT + 250
        _seed(store, "s1", total)
        got = await store.get_session("s1")

        assert got[-1].content == f"message-{total - 1}", (
            f"newest message is {got[-1].content!r}, so the bound took the "
            "OLDEST rows — the #2075 defect in a new place"
        )
        assert got[0].content == f"message-{total - DEFAULT_SESSION_HISTORY_LIMIT}"

    async def test_order_stays_ascending(self, store):
        """Callers slice with entries[-limit:] and render top to bottom, so a
        reversed list would silently invert every transcript."""
        _seed(store, "s1", 20)
        got = await store.get_session("s1")
        assert [e.created_at for e in got] == sorted(e.created_at for e in got)

    async def test_an_explicit_limit_is_honoured(self, store):
        _seed(store, "s1", 100)
        got = await store.get_session("s1", limit=10)
        assert len(got) == 10
        assert got[-1].content == "message-99"

    async def test_limit_none_restores_the_unbounded_read(self, store):
        total = DEFAULT_SESSION_HISTORY_LIMIT + 250
        _seed(store, "s1", total)
        got = await store.get_session("s1", limit=None)
        assert len(got) == total

    async def test_a_short_session_is_returned_whole(self, store):
        _seed(store, "s1", 7)
        assert len(await store.get_session("s1")) == 7

    async def test_a_missing_session_is_still_empty(self, store):
        assert await store.get_session("nope") == []


class _RecordingStore:
    """Captures the limit each caller passes, without touching a backend."""

    def __init__(self, count: int = 1000) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        self._all = [
            MemoryEntry(
                id=f"m{i:04d}",
                type=MemoryType.SESSION,
                content=f"message-{i}",
                role="user",
                session_key="s1",
                created_at=base + timedelta(minutes=i),
                updated_at=base + timedelta(minutes=i),
            )
            for i in range(count)
        ]
        self.limits: list[int | None] = []

    async def get_session(
        self, session_key: str, limit: int | None = DEFAULT_SESSION_HISTORY_LIMIT
    ) -> list[MemoryEntry]:
        self.limits.append(limit)
        return self._all if limit is None else self._all[-limit:]


class TestManagerCallers:
    """Two callers must stay unbounded, and it is not a matter of taste."""

    def _manager(self, store):
        from pocketpaw.memory.manager import MemoryManager

        m = MemoryManager.__new__(MemoryManager)
        m._store = store
        return m

    async def test_paging_backward_still_reaches_the_oldest_message(self):
        """get_session_history_page filters by a `before` cursor in Python. A
        bounded read makes everything older than the bound unreachable, so the
        transcript appears to end and the client shows an empty page."""
        store = _RecordingStore(count=1000)
        manager = self._manager(store)

        oldest = store._all[0]
        cursor = f"{store._all[600].created_at.isoformat()}|{store._all[600].id}"
        page = await manager.get_session_history_page("s1", limit=50, before=cursor)

        assert store.limits == [None], f"the paginator asked for limit={store.limits}"
        assert page["messages"], "paging backward returned nothing"
        assert page["has_more"] is True
        assert any(m["_id"] <= oldest.id for m in page["messages"]) or page["messages"]

    async def test_compaction_still_sees_the_whole_session(self):
        """Summarization input must not change silently; it has its own
        char budget, so the OUTPUT was already bounded."""
        store = _RecordingStore(count=1000)
        manager = self._manager(store)

        await manager.get_compacted_history("s1")

        assert store.limits == [None]

    async def test_the_display_path_takes_the_default_bound(self):
        store = _RecordingStore(count=1000)
        manager = self._manager(store)

        history = await manager.get_session_history("s1", limit=50)

        assert store.limits == [DEFAULT_SESSION_HISTORY_LIMIT], (
            "the display path reads unbounded; it is the hot one"
        )
        assert history[-1]["content"] == "message-999", "display path lost the newest message"
