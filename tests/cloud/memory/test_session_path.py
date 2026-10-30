"""MongoMemoryStore — SESSION path protocol conformance + adapter-specific reads."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from pocketpaw.memory.protocol import MemoryEntry, MemoryType


def _entry(session_key: str, role: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        id="",
        type=MemoryType.SESSION,
        content=content,
        role=role,
        session_key=session_key,
    )


class TestProtocolSessionPath:
    async def test_save_returns_objectid_hex(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        entry_id = await store.save(_entry(key, "user", "hello"))
        assert isinstance(entry_id, str)
        assert len(entry_id) == 24
        int(entry_id, 16)  # valid hex

    async def test_save_then_get_roundtrip(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        entry_id = await store.save(_entry(key, "assistant", "answer"))
        got = await store.get(entry_id)
        assert got is not None
        assert got.id == entry_id
        assert got.type == MemoryType.SESSION
        assert got.role == "assistant"
        assert got.content == "answer"
        assert got.session_key == key

    async def test_save_without_session_key_raises(self, store):
        bad = MemoryEntry(id="", type=MemoryType.SESSION, content="x", role="user")
        with pytest.raises(ValueError, match="session_key"):
            await store.save(bad)

    async def test_save_rejects_invalid_role_via_validator(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        bad = MemoryEntry(
            id="", type=MemoryType.SESSION, content="x", role="attacker", session_key=key
        )
        # Message model_validator rejects roles outside user/assistant/system.
        with pytest.raises(ValueError):
            await store.save(bad)

    async def test_get_missing_returns_none(self, store):
        # Valid ObjectId format but not in DB.
        got = await store.get("507f1f77bcf86cd799439011")
        assert got is None

    async def test_get_invalid_id_returns_none(self, store):
        got = await store.get("not-an-object-id")
        assert got is None

    async def test_delete_removes_entry(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        entry_id = await store.save(_entry(key, "user", "delete-me"))
        assert await store.delete(entry_id) is True
        assert await store.get(entry_id) is None

    async def test_delete_missing_returns_false(self, store):
        assert await store.delete("507f1f77bcf86cd799439011") is False

    async def test_delete_invalid_id_returns_false(self, store):
        assert await store.delete("bogus") is False

    async def test_get_session_returns_messages_ascending(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        for i in range(3):
            await store.save(_entry(key, "user" if i % 2 == 0 else "assistant", f"m{i}"))
            await asyncio.sleep(0.01)  # ensure distinct timestamps
        got = await store.get_session(key)
        assert [e.content for e in got] == ["m0", "m1", "m2"]
        assert all(e.type == MemoryType.SESSION for e in got)
        assert all(e.session_key == key for e in got)

    async def test_get_session_empty_key(self, store):
        got = await store.get_session("no-such-session")
        assert got == []

    async def test_get_session_isolates_by_key(self, store):
        a = f"A-{uuid.uuid4().hex[:8]}"
        b = f"B-{uuid.uuid4().hex[:8]}"
        await store.save(_entry(a, "user", "a-only"))
        await store.save(_entry(b, "user", "b-only"))
        got_a = await store.get_session(a)
        got_b = await store.get_session(b)
        assert [e.content for e in got_a] == ["a-only"]
        assert [e.content for e in got_b] == ["b-only"]

    async def test_clear_session_returns_count(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        for i in range(4):
            await store.save(_entry(key, "user", f"m{i}"))
        count = await store.clear_session(key)
        assert count == 4
        assert await store.get_session(key) == []

    async def test_clear_session_does_not_touch_other_sessions(self, store):
        a = f"A-{uuid.uuid4().hex[:8]}"
        b = f"B-{uuid.uuid4().hex[:8]}"
        await store.save(_entry(a, "user", "a1"))
        await store.save(_entry(a, "user", "a2"))
        await store.save(_entry(b, "user", "b1"))
        cleared = await store.clear_session(a)
        assert cleared == 2
        assert await store.get_session(a) == []
        assert [e.content for e in await store.get_session(b)] == ["b1"]

    async def test_clear_session_missing_returns_zero(self, store):
        assert await store.clear_session("never-existed") == 0


class TestAdapterSpecificReads:
    async def test_get_session_info_returns_none_when_no_session_doc(self, store):
        # We write messages only; the adapter never auto-creates `sessions` rows.
        key = f"sess-{uuid.uuid4().hex[:8]}"
        await store.save(_entry(key, "user", "hi"))
        assert await store.get_session_info(key) is None

    async def test_get_session_info_returns_session_when_api_created_it(self, store):
        from pocketpaw_ee.cloud.models.session import Session

        key = f"sess-{uuid.uuid4().hex[:8]}"
        await Session(
            sessionId=key,
            context_type="pocket",
            workspace="w1",
            owner="u1",
            title="Demo",
        ).insert()

        got = await store.get_session_info(key)
        assert got is not None
        assert got.sessionId == key
        assert got.title == "Demo"
        assert got.context_type == "pocket"

    async def test_get_session_with_messages_returns_both(self, store):
        from pocketpaw_ee.cloud.models.session import Session

        key = f"sess-{uuid.uuid4().hex[:8]}"
        await Session(
            sessionId=key,
            context_type="pocket",
            workspace="w1",
            owner="u1",
            title="Combined",
        ).insert()
        await store.save(_entry(key, "user", "one"))
        await store.save(_entry(key, "assistant", "two"))

        session, messages = await store.get_session_with_messages(key)
        assert session is not None
        assert session.sessionId == key
        assert [m.content for m in messages] == ["one", "two"]

    async def test_get_session_with_messages_limit_returns_recent_ascending(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        for i in range(5):
            await store.save(_entry(key, "user", f"m{i}"))
            await asyncio.sleep(0.01)

        _, messages = await store.get_session_with_messages(key, limit=3)
        # Most recent 3, returned in ascending order
        assert [m.content for m in messages] == ["m2", "m3", "m4"]

    async def test_combined_equals_separate_reads(self, store):
        from pocketpaw_ee.cloud.models.session import Session

        key = f"sess-{uuid.uuid4().hex[:8]}"
        await Session(
            sessionId=key,
            context_type="pocket",
            workspace="w1",
            owner="u1",
        ).insert()
        await store.save(_entry(key, "user", "a"))
        await store.save(_entry(key, "assistant", "b"))

        combined_session, combined_messages = await store.get_session_with_messages(key)
        sep_session = await store.get_session_info(key)
        sep_messages = await store.get_session(key)

        assert combined_session is not None and sep_session is not None
        assert combined_session.sessionId == sep_session.sessionId
        assert [m.content for m in combined_messages] == [m.content for m in sep_messages]


class TestGetByTypeSession:
    async def test_returns_pocket_messages_only(self, store):
        from pocketpaw_ee.cloud.models.message import Message

        # Seed: one pocket, one group. get_by_type(SESSION) should return only pocket.
        await store.save(_entry("sess1", "user", "pocket-row"))
        await Message(
            context_type="group",
            group="g1",
            sender="u1",
            sender_type="user",
            content="group-row",
        ).insert()

        got = await store.get_by_type(MemoryType.SESSION, limit=100)
        contents = [e.content for e in got]
        assert "pocket-row" in contents
        assert "group-row" not in contents


class TestSearchSessionSubstring:
    async def test_case_insensitive_content_match(self, store):
        await store.save(_entry("s1", "user", "Find the NEEDLE here"))
        await store.save(_entry("s1", "user", "unrelated"))
        got = await store.search(query="needle", memory_type=MemoryType.SESSION, limit=10)
        assert len(got) == 1
        assert "NEEDLE" in got[0].content

    async def test_no_query_returns_recent_pocket_messages(self, store):
        for i in range(3):
            await store.save(_entry("s1", "user", f"m{i}"))
            await asyncio.sleep(0.01)
        got = await store.search(memory_type=MemoryType.SESSION, limit=2)
        # DESC by createdAt — most recent first
        assert [e.content for e in got] == ["m2", "m1"]
