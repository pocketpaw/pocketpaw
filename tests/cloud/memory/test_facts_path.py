"""MongoMemoryStore — LONG_TERM and DAILY fact path."""

from __future__ import annotations

from pocketpaw.memory.protocol import MemoryEntry, MemoryType


def _long_term(content: str, *, tags=None, user_id=None, **metadata) -> MemoryEntry:
    md = dict(metadata)
    if user_id is not None:
        md["user_id"] = user_id
    return MemoryEntry(
        id="", type=MemoryType.LONG_TERM, content=content, tags=tags or [], metadata=md
    )


def _daily(content: str, *, tags=None, **metadata) -> MemoryEntry:
    return MemoryEntry(
        id="",
        type=MemoryType.DAILY,
        content=content,
        tags=tags or [],
        metadata=dict(metadata),
    )


class TestFactsRoundtrip:
    async def test_save_long_term_returns_objectid_hex(self, store):
        entry_id = await store.save(_long_term("user prefers dark mode"))
        assert isinstance(entry_id, str) and len(entry_id) == 24

    async def test_save_then_get_long_term(self, store):
        entry_id = await store.save(
            _long_term("user prefers dark mode", tags=["ui", "prefs"], user_id="u1")
        )
        got = await store.get(entry_id)
        assert got is not None
        assert got.id == entry_id
        assert got.type == MemoryType.LONG_TERM
        assert got.content == "user prefers dark mode"
        assert set(got.tags) == {"ui", "prefs"}
        assert got.metadata.get("user_id") == "u1"

    async def test_save_daily_and_retrieve(self, store):
        entry_id = await store.save(_daily("met with Alice about Project X"))
        got = await store.get(entry_id)
        assert got is not None
        assert got.type == MemoryType.DAILY
        assert got.content == "met with Alice about Project X"

    async def test_delete_fact_removes_it(self, store):
        entry_id = await store.save(_long_term("forget me"))
        assert await store.delete(entry_id) is True
        assert await store.get(entry_id) is None

    async def test_delete_returns_false_when_missing(self, store):
        assert await store.delete("507f1f77bcf86cd799439099") is False


class TestGetByType:
    async def test_long_term_filtered_by_user_id(self, store):
        await store.save(_long_term("u1 pref", user_id="u1"))
        await store.save(_long_term("u2 pref", user_id="u2"))
        await store.save(_long_term("anon"))  # no user_id

        u1_entries = await store.get_by_type(MemoryType.LONG_TERM, user_id="u1")
        assert len(u1_entries) == 1
        assert u1_entries[0].content == "u1 pref"

    async def test_long_term_unfiltered_returns_all(self, store):
        await store.save(_long_term("a", user_id="u1"))
        await store.save(_long_term("b", user_id="u2"))
        await store.save(_long_term("c"))

        entries = await store.get_by_type(MemoryType.LONG_TERM, limit=100)
        assert len(entries) == 3

    async def test_daily_does_not_leak_into_long_term(self, store):
        await store.save(_long_term("a long-term fact"))
        await store.save(_daily("a daily note"))

        lt = await store.get_by_type(MemoryType.LONG_TERM)
        dl = await store.get_by_type(MemoryType.DAILY)
        lt_contents = [e.content for e in lt]
        dl_contents = [e.content for e in dl]
        assert "a long-term fact" in lt_contents and "a daily note" not in lt_contents
        assert "a daily note" in dl_contents and "a long-term fact" not in dl_contents


class TestSearch:
    async def test_long_term_substring_match(self, store):
        await store.save(_long_term("User prefers DARK mode"))
        await store.save(_long_term("User prefers compact layouts"))
        got = await store.search(query="dark", memory_type=MemoryType.LONG_TERM, limit=5)
        assert len(got) == 1
        assert "DARK" in got[0].content

    async def test_search_by_tags(self, store):
        await store.save(_long_term("prefers dark", tags=["ui", "prefs"]))
        await store.save(_long_term("prefers chess", tags=["games"]))
        got = await store.search(tags=["ui"], memory_type=MemoryType.LONG_TERM, limit=5)
        assert len(got) == 1
        assert "dark" in got[0].content

    async def test_search_untyped_spans_facts_both_types(self, store):
        # No memory_type → fact search spans LONG_TERM + DAILY (not SESSION).
        await store.save(_long_term("long-term needle"))
        await store.save(_daily("daily needle"))
        got = await store.search(query="needle", limit=10)
        contents = [e.content for e in got]
        assert "long-term needle" in contents
        assert "daily needle" in contents

    async def test_regex_metacharacters_are_escaped(self, store):
        # A raw regex-special query should match literally, not as a pattern.
        await store.save(_long_term("user paid $100 for the plan"))
        await store.save(_long_term("also a fact"))
        got = await store.search(query="$100", memory_type=MemoryType.LONG_TERM, limit=5)
        assert len(got) == 1


class TestProtocolConformance:
    def test_satisfies_memory_store_protocol(self):
        """Structural type check — MongoMemoryStore conforms to MemoryStoreProtocol."""
        from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

        from pocketpaw.memory.protocol import MemoryStoreProtocol

        store: MemoryStoreProtocol = MongoMemoryStore()  # static check via assignment
        # Runtime presence check of every required coroutine method.
        for method in (
            "save",
            "get",
            "delete",
            "search",
            "get_by_type",
            "get_session",
            "clear_session",
        ):
            assert callable(getattr(store, method)), f"missing {method}"

    async def test_get_dispatches_to_facts_when_not_in_messages(self, store):
        fact_id = await store.save(_long_term("fact content"))
        # get() doesn't know the type; it must find in memory_facts.
        got = await store.get(fact_id)
        assert got is not None
        assert got.type == MemoryType.LONG_TERM

    async def test_search_across_types_uses_correct_collection(self, store):
        """A SESSION-typed search must not see facts, and vice versa."""
        from pocketpaw.memory.protocol import MemoryEntry as ME

        # Seed session + facts with similar content
        await store.save(
            ME(
                id="",
                type=MemoryType.SESSION,
                content="needle in session",
                role="user",
                session_key="s-match",
            )
        )
        await store.save(_long_term("needle in fact"))

        session_hits = await store.search(query="needle", memory_type=MemoryType.SESSION, limit=10)
        assert [e.type for e in session_hits] == [MemoryType.SESSION]

        fact_hits = await store.search(query="needle", memory_type=MemoryType.LONG_TERM, limit=10)
        assert [e.type for e in fact_hits] == [MemoryType.LONG_TERM]


class TestEdgeCases:
    async def test_invalid_object_id_returns_none_from_get(self, store):
        assert await store.get("not-an-id") is None

    async def test_invalid_object_id_returns_false_from_delete(self, store):
        assert await store.delete("not-an-id") is False

    async def test_user_id_metadata_not_duplicated(self, store):
        """user_id lives on the column; metadata dict shouldn't hold a copy."""
        entry_id = await store.save(_long_term("owned", user_id="u7"))
        # Read the raw document to confirm metadata is clean.
        from beanie import PydanticObjectId
        from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc

        raw = await MemoryFactDoc.get(PydanticObjectId(entry_id))
        assert raw is not None
        assert raw.user_id == "u7"
        assert "user_id" not in raw.metadata

    async def test_search_without_filters_returns_all_facts(self, store):
        await store.save(_long_term("one"))
        await store.save(_daily("two"))
        got = await store.search(limit=10)
        assert len(got) == 2
