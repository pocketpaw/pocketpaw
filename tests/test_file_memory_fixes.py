# Tests for file memory fixes: UUID collision, fuzzy search, daily loading,
# dedup, persistent delete, ForgetTool, auto-learn, context limits.
# Created: 2026-02-09

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketclaw.memory.file_store import FileMemoryStore, _make_deterministic_id, _tokenize
from pocketclaw.memory.manager import MemoryManager
from pocketclaw.memory.protocol import MemoryEntry, MemoryType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path):
    """Create a FileMemoryStore with a temp directory."""
    return FileMemoryStore(base_path=tmp_path)


@pytest.fixture
def tmp_manager(tmp_store):
    """Create a MemoryManager wrapping a temp FileMemoryStore."""
    return MemoryManager(store=tmp_store)


# ===========================================================================
# TestUUIDCollision
# ===========================================================================


class TestUUIDCollision:
    """Step 1: Entries with the same header but different body get unique IDs."""

    async def test_multiple_memories_survive(self, tmp_store):
        """Two entries with header 'Memory' but different content get different IDs."""
        e1 = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="User's name is Rohit",
            metadata={"header": "Memory"},
        )
        e2 = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="User prefers dark mode",
            metadata={"header": "Memory"},
        )
        id1 = await tmp_store.save(e1)
        id2 = await tmp_store.save(e2)

        assert id1 != id2
        assert len(tmp_store._index) >= 2

        # Both retrievable
        assert (await tmp_store.get(id1)) is not None
        assert (await tmp_store.get(id2)) is not None

    async def test_survive_restart(self, tmp_path):
        """Memories survive creating a new FileMemoryStore (simulating restart)."""
        store1 = FileMemoryStore(base_path=tmp_path)
        await store1.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Fact A",
                metadata={"header": "Memory"},
            )
        )
        await store1.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Fact B",
                metadata={"header": "Memory"},
            )
        )
        await store1.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Fact C",
                metadata={"header": "Memory"},
            )
        )

        # Simulate restart
        store2 = FileMemoryStore(base_path=tmp_path)
        lt = await store2.get_by_type(MemoryType.LONG_TERM)
        assert len(lt) == 3
        contents = {e.content for e in lt}
        assert contents == {"Fact A", "Fact B", "Fact C"}

    async def test_custom_headers_work(self, tmp_store):
        """Entries with different headers also get unique IDs."""
        id1 = await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Same body",
                metadata={"header": "Header A"},
            )
        )
        id2 = await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Same body",
                metadata={"header": "Header B"},
            )
        )
        assert id1 != id2

    async def test_deterministic_id_includes_body(self, tmp_path):
        """_make_deterministic_id produces different IDs for different bodies."""
        p = tmp_path / "test.md"
        id1 = _make_deterministic_id(p, "Memory", "Fact one")
        id2 = _make_deterministic_id(p, "Memory", "Fact two")
        assert id1 != id2

    async def test_deterministic_id_same_content(self, tmp_path):
        """Same path/header/body always yields the same ID."""
        p = tmp_path / "test.md"
        id1 = _make_deterministic_id(p, "Memory", "Fact one")
        id2 = _make_deterministic_id(p, "Memory", "Fact one")
        assert id1 == id2


# ===========================================================================
# TestFuzzySearch
# ===========================================================================


class TestFuzzySearch:
    """Step 3: Word-overlap search replaces broken substring match."""

    async def test_word_overlap_matches(self, tmp_store):
        """'name' matches 'User's name is Rohit'."""
        await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="User's name is Rohit",
                metadata={"header": "Memory"},
            )
        )
        results = await tmp_store.search("name")
        assert len(results) == 1
        assert "Rohit" in results[0].content

    async def test_multi_word_query(self, tmp_store):
        """Multi-word query scores by overlap ratio."""
        await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="User's name is Rohit",
                metadata={"header": "Memory"},
            )
        )
        await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Rohit prefers dark mode",
                metadata={"header": "Memory"},
            )
        )

        # "Rohit name" has 2 query words; first entry matches both, second matches 1
        results = await tmp_store.search("Rohit name")
        assert len(results) == 2
        assert "name is Rohit" in results[0].content  # Higher score (2/2)

    async def test_no_false_matches(self, tmp_store):
        """Query with no overlapping words returns nothing."""
        await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="User prefers dark mode",
                metadata={"header": "Memory"},
            )
        )
        results = await tmp_store.search("banana")
        assert len(results) == 0

    async def test_stop_words_excluded(self):
        """Tokenizer strips stop words."""
        tokens = _tokenize("the user is a developer")
        assert "the" not in tokens
        assert "is" not in tokens
        assert "a" not in tokens
        assert "user" in tokens
        assert "developer" in tokens

    async def test_search_includes_header(self, tmp_store):
        """Search also matches against the header text."""
        await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Likes coffee",
                metadata={"header": "Preferences"},
            )
        )
        results = await tmp_store.search("preferences")
        assert len(results) == 1

    async def test_ranking_order(self, tmp_store):
        """Results are sorted by descending overlap score."""
        await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Python developer",
                metadata={"header": "Memory"},
            )
        )
        await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Python backend developer at Google",
                metadata={"header": "Memory"},
            )
        )

        results = await tmp_store.search("Python backend developer Google")
        assert len(results) == 2
        # Second entry matches more words → should be first
        assert "Google" in results[0].content


# ===========================================================================
# TestDailyFileIndexing
# ===========================================================================


class TestDailyFileIndexing:
    """Step 2: Past daily files are loaded, not just today's."""

    async def test_past_daily_files_loaded(self, tmp_path):
        """Memories in yesterday's daily file are available after restart."""
        yesterday = date.today() - timedelta(days=1)
        daily_file = tmp_path / f"{yesterday.isoformat()}.md"
        daily_file.write_text("## 10:30\n\nHad meeting with Alice\n")

        store = FileMemoryStore(base_path=tmp_path)
        daily_entries = await store.get_by_type(MemoryType.DAILY)
        assert len(daily_entries) == 1
        assert "Alice" in daily_entries[0].content

    async def test_multiple_daily_files(self, tmp_path):
        """Multiple past daily files are all loaded."""
        for i in range(3):
            d = date.today() - timedelta(days=i)
            f = tmp_path / f"{d.isoformat()}.md"
            f.write_text(f"## Note\n\nDay {i} note\n")

        # Create sessions dir (needed by constructor)
        (tmp_path / "sessions").mkdir(exist_ok=True)
        store = FileMemoryStore(base_path=tmp_path)
        daily_entries = await store.get_by_type(MemoryType.DAILY)
        assert len(daily_entries) == 3


# ===========================================================================
# TestDeduplication
# ===========================================================================


class TestDeduplication:
    """Step 1 dedup: saving the same fact twice doesn't create a duplicate."""

    async def test_same_fact_not_duplicated(self, tmp_store):
        """Saving identical content returns the same ID and doesn't grow index."""
        id1 = await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="User's name is Rohit",
                metadata={"header": "Memory"},
            )
        )
        count_after_first = len(tmp_store._index)

        id2 = await tmp_store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="User's name is Rohit",
                metadata={"header": "Memory"},
            )
        )

        assert id1 == id2
        assert len(tmp_store._index) == count_after_first

    async def test_dedup_across_restart(self, tmp_path):
        """Dedup works after reloading from disk."""
        store1 = FileMemoryStore(base_path=tmp_path)
        await store1.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Fact X",
                metadata={"header": "Memory"},
            )
        )

        store2 = FileMemoryStore(base_path=tmp_path)
        count_before = len(store2._index)

        await store2.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Fact X",
                metadata={"header": "Memory"},
            )
        )
        assert len(store2._index) == count_before


# ===========================================================================
# TestPersistentDelete
# ===========================================================================


class TestPersistentDelete:
    """Step 4: Deletions persist across restarts."""

    async def test_delete_persists_across_restart(self, tmp_path):
        """A deleted entry does not reappear after reload."""
        store1 = FileMemoryStore(base_path=tmp_path)
        id1 = await store1.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Keep this",
                metadata={"header": "Memory"},
            )
        )
        id2 = await store1.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Delete this",
                metadata={"header": "Memory"},
            )
        )

        deleted = await store1.delete(id2)
        assert deleted is True

        # Restart
        store2 = FileMemoryStore(base_path=tmp_path)
        assert await store2.get(id1) is not None
        assert await store2.get(id2) is None

        lt = await store2.get_by_type(MemoryType.LONG_TERM)
        assert len(lt) == 1
        assert lt[0].content == "Keep this"

    async def test_delete_only_removes_target(self, tmp_path):
        """Deleting one entry doesn't affect others in the same file."""
        store = FileMemoryStore(base_path=tmp_path)
        ids = []
        for i in range(5):
            eid = await store.save(
                MemoryEntry(
                    id="",
                    type=MemoryType.LONG_TERM,
                    content=f"Fact {i}",
                    metadata={"header": "Memory"},
                )
            )
            ids.append(eid)

        await store.delete(ids[2])

        remaining = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(remaining) == 4
        contents = {e.content for e in remaining}
        assert "Fact 2" not in contents
        for i in [0, 1, 3, 4]:
            assert f"Fact {i}" in contents

    async def test_delete_last_entry_removes_file(self, tmp_path):
        """Deleting the only entry in a file removes the file."""
        store = FileMemoryStore(base_path=tmp_path)
        eid = await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Only fact",
                metadata={"header": "Memory"},
            )
        )

        assert store.long_term_file.exists()
        await store.delete(eid)
        assert not store.long_term_file.exists()


# ===========================================================================
# TestForgetTool
# ===========================================================================


class TestForgetTool:
    """Step 5: ForgetTool searches and deletes memories."""

    def test_forget_tool_definition(self):
        """ForgetTool has correct name and required params."""
        from pocketclaw.tools.builtin.memory import ForgetTool

        tool = ForgetTool()
        assert tool.name == "forget"
        assert "query" in tool.parameters["properties"]
        assert "query" in tool.parameters["required"]

    async def test_forget_removes_matching_memory(self, tmp_path):
        """ForgetTool deletes memories matching the query."""
        from pocketclaw.tools.builtin.memory import ForgetTool

        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        await manager.remember("User's name is Rohit")
        await manager.remember("User prefers dark mode")

        # Verify both exist
        all_lt = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(all_lt) == 2

        tool = ForgetTool()
        with patch("pocketclaw.tools.builtin.memory.get_memory_manager", return_value=manager):
            result = await tool.execute(query="name Rohit")

        assert "Forgot" in result
        assert "1" in result

        remaining = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(remaining) == 1
        assert "dark mode" in remaining[0].content

    def test_forget_in_policy_group(self):
        """'forget' is in the group:memory policy group."""
        from pocketclaw.tools.policy import TOOL_GROUPS

        assert "forget" in TOOL_GROUPS["group:memory"]


# ===========================================================================
# TestFileAutoLearn
# ===========================================================================


class TestFileAutoLearn:
    """Step 7: LLM-based auto-fact extraction for file backend."""

    async def test_extracts_facts(self, tmp_path):
        """_file_auto_learn calls Haiku and saves extracted facts."""
        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='["User name is Rohit", "Likes Python"]')]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            result = await manager._file_auto_learn(
                [
                    {"role": "user", "content": "My name is Rohit and I like Python"},
                    {"role": "assistant", "content": "Nice to meet you, Rohit!"},
                ]
            )

        assert len(result.get("results", [])) == 2

        lt = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(lt) == 2
        contents = {e.content for e in lt}
        assert "User name is Rohit" in contents
        assert "Likes Python" in contents

    async def test_graceful_without_api_key(self, tmp_path):
        """_file_auto_learn returns empty dict when API is unavailable."""
        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        with patch(
            "anthropic.AsyncAnthropic",
            side_effect=Exception("No API key"),
        ):
            result = await manager._file_auto_learn(
                [
                    {"role": "user", "content": "Hello"},
                ]
            )

        assert result == {}

    async def test_deduplicates_auto_learned_facts(self, tmp_path):
        """Auto-learned facts are deduped by deterministic IDs."""
        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='["User name is Rohit"]')]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            await manager._file_auto_learn(
                [
                    {"role": "user", "content": "My name is Rohit"},
                    {"role": "assistant", "content": "Hi Rohit!"},
                ]
            )
            await manager._file_auto_learn(
                [
                    {"role": "user", "content": "My name is Rohit"},
                    {"role": "assistant", "content": "Hi Rohit!"},
                ]
            )

        lt = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(lt) == 1

    async def test_auto_learn_passes_flag(self, tmp_path):
        """auto_learn() dispatches to _file_auto_learn when file_auto_learn=True."""
        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)
        manager._file_auto_learn = AsyncMock(return_value={"results": []})

        await manager.auto_learn(
            [{"role": "user", "content": "hello"}],
            file_auto_learn=True,
        )
        manager._file_auto_learn.assert_called_once()


# ===========================================================================
# TestContextLimits
# ===========================================================================


class TestContextLimits:
    """Step 6: Increased context injection limits."""

    async def test_more_than_10_memories_in_context(self, tmp_path):
        """With default limits, >10 long-term memories appear in context."""
        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        for i in range(25):
            await manager.remember(f"Fact number {i}")

        context = await manager.get_context_for_agent()
        # Count how many "Fact number" entries appear
        count = context.count("Fact number")
        assert count == 25

    async def test_entry_truncation_at_500(self, tmp_path):
        """Entries are truncated at 500 chars (not 200)."""
        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        long_content = "x" * 600
        await manager.remember(long_content)

        context = await manager.get_context_for_agent()
        # The truncated content should be 500 chars (plus the "- " prefix)
        # It should NOT be truncated at 200
        assert "x" * 500 in context
        assert "x" * 600 not in context

    async def test_custom_limits(self, tmp_path):
        """Custom limits can be passed as parameters."""
        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        for i in range(10):
            await manager.remember(f"Fact {i}")

        context = await manager.get_context_for_agent(long_term_limit=3)
        count = context.count("Fact")
        assert count == 3

    async def test_max_chars_truncation(self, tmp_path):
        """Context is truncated when exceeding max_chars."""
        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        for i in range(100):
            await manager.remember(f"Fact {i}: " + "a" * 100)

        context = await manager.get_context_for_agent(max_chars=500)
        assert len(context) <= 520  # 500 + "...(truncated)" suffix
        assert "(truncated)" in context
