# Tests for file memory fixes: UUID collision, fuzzy search, daily loading,
# dedup, persistent delete, ForgetTool, auto-learn, context limits.
# Created: 2026-02-09

import sqlite3
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.memory.file_store import FileMemoryStore, _make_deterministic_id, _tokenize
from pocketpaw.memory.manager import MemoryManager
from pocketpaw.memory.protocol import MemoryEntry, MemoryType

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


class TestVectorSchemaMigrations:
    """Schema version checks for sqlite vector index."""

    async def test_vector_schema_migrates_user_version_on_init(self, tmp_path):
        db_path = tmp_path / "vector_index.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA user_version = 0")

        FileMemoryStore(base_path=tmp_path, vector_enabled=True, embedding_provider="hash")

        with sqlite3.connect(db_path) as conn:
            version_row = conn.execute("PRAGMA user_version").fetchone()
            assert version_row is not None
            assert int(version_row[0]) == 1

            columns = conn.execute("PRAGMA table_info(memory_vectors)").fetchall()
            column_names = {str(row[1]) for row in columns}
            assert "doc_id" in column_names
            assert "embedding_json" in column_names


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
        from pocketpaw.tools.builtin.memory import ForgetTool

        tool = ForgetTool()
        assert tool.name == "forget"
        assert "query" in tool.parameters["properties"]
        assert "query" in tool.parameters["required"]

    async def test_forget_removes_matching_memory(self, tmp_path):
        """ForgetTool deletes memories matching the query."""
        from pocketpaw.tools.builtin.memory import ForgetTool

        store = FileMemoryStore(base_path=tmp_path)
        manager = MemoryManager(store=store)

        await manager.remember("User's name is Rohit")
        await manager.remember("User prefers dark mode")

        # Verify both exist
        all_lt = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(all_lt) == 2

        tool = ForgetTool()
        with patch("pocketpaw.tools.builtin.memory.get_memory_manager", return_value=manager):
            result = await tool.execute(query="name Rohit")

        assert "Forgot" in result
        assert "1" in result

        remaining = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(remaining) == 1
        assert "dark mode" in remaining[0].content

    def test_forget_in_policy_group(self):
        """'forget' is in the group:memory policy group."""
        from pocketpaw.tools.policy import TOOL_GROUPS

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


# ===========================================================================
# TestFileVectorSemanticSearch
# ===========================================================================


class TestFileVectorSemanticSearch:
    """Phase 1: file backend is markdown + vector retrieval."""

    async def test_semantic_search_returns_relevant_memory(self, tmp_path):
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="API refactor should preserve backward compatibility",
                metadata={"header": "Architecture"},
            )
        )
        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Buy groceries tomorrow morning",
                metadata={"header": "Personal"},
            )
        )

        results = await store.semantic_search("api backward compatibility", user_id="default")
        assert len(results) >= 1
        assert "API refactor" in results[0]["memory"]

    async def test_delete_removes_vector_record(self, tmp_path):
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        entry_id = await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Legacy deployment issue in staging",
                metadata={"header": "Deploy"},
            )
        )

        before = await store.semantic_search("deployment issue", user_id="default")
        assert any(item["id"] == entry_id for item in before)

        assert await store.delete(entry_id) is True

        after = await store.semantic_search("deployment issue", user_id="default")
        assert all(item["id"] != entry_id for item in after)

    async def test_manager_uses_file_semantic_context(self, tmp_path):
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )
        manager = MemoryManager(store=store)

        await manager.remember(
            "Project Phoenix uses React and TypeScript",
            header="Project",
        )

        context = await manager.get_semantic_context("what stack does project phoenix use")
        assert "Relevant Memories" in context
        assert "React" in context


class TestFileGraphAndManagement:
    """Phase 2/3: graph indexing, memory edits, and pruning."""

    async def test_graph_db_not_created_when_graph_feature_disabled(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path, vector_enabled=False)

        assert store._graph_enabled is False
        assert not store._graph_db_path.exists()

    async def test_graph_snapshot_disabled_returns_empty_without_db_creation(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path, vector_enabled=False)

        snapshot = await store.get_graph_snapshot(user_id="default", limit=500)

        assert snapshot == {"nodes": [], "edges": []}
        assert not store._graph_db_path.exists()

    async def test_graph_snapshot_limit_500_avoids_sql_variable_limit(self, tmp_path):
        """Graph snapshot with limit=500 should not exceed SQLite bind variable cap."""
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        now = datetime.now(UTC).isoformat()
        entities = [
            (f"entity_{i}", "default", f"entity-{i}", f"Entity {i}", 1, now, now)
            for i in range(500)
        ]
        relationships = [
            (
                f"rel_{i}",
                "default",
                f"entity_{i}",
                f"entity_{i + 1}",
                "related_to",
                1,
                now,
                now,
            )
            for i in range(499)
        ]

        with sqlite3.connect(store._graph_db_path) as conn:
            conn.executemany(
                """INSERT INTO entities
                (entity_id, user_scope, entity_key, display_name,
                mention_count, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                entities,
            )
            conn.executemany(
                """INSERT INTO relationships
                (relationship_id, user_scope, source_entity_id, target_entity_id,
                relation_type, weight, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                relationships,
            )
            conn.commit()

        snapshot = await store.get_graph_snapshot(user_id="default", limit=500)

        assert len(snapshot["nodes"]) == 500
        assert len(snapshot["edges"]) <= 500

    async def test_graph_snapshot_contains_entities_and_edges(self, tmp_path):
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Project Phoenix uses React",
                metadata={"header": "Project"},
            )
        )

        graph = await store.get_graph_snapshot(user_id="default")
        assert len(graph["nodes"]) >= 2
        assert any(edge["relation"] == "uses" for edge in graph["edges"])

    async def test_update_entry_reindexes_content(self, tmp_path):
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        entry_id = await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Service runs on Flask",
                metadata={"header": "Tech"},
            )
        )

        updated = await store.update_entry(entry_id, content="Service runs on FastAPI")
        assert updated is True

        entry = await store.get(entry_id)
        assert entry is not None
        assert "FastAPI" in entry.content

    async def test_prune_memories_deletes_old_daily_entries(self, tmp_path):
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        old_entry = MemoryEntry(
            id="",
            type=MemoryType.DAILY,
            content="Old daily memory",
            created_at=datetime.now(tz=UTC) - timedelta(days=45),
            metadata={"header": "Daily"},
        )
        old_id = await store.save(old_entry)

        result = await store.prune_memories(older_than_days=30)
        assert result["ok"] is True
        assert result["deleted_daily_memories"] >= 1
        assert await store.get(old_id) is None

    async def test_clear_session_handles_non_list_json(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)
        session_key = "discord:test-session"
        session_file = store._get_session_file(session_key)
        session_file.write_text('{"unexpected": "shape"}', encoding="utf-8")

        count = await store.clear_session(session_key)
        assert count == 0
        assert not session_file.exists()

    async def test_cleanup_orphan_records_handles_large_memory_stores(self, tmp_path):
        """Test cleanup_orphan_records with 1000+ memories without SQLite variable limit crash.

        Verifies the fix for SQLite SQLITE_LIMIT_VARIABLE_NUMBER (default 999).
        With >999 valid memory IDs, a direct NOT IN clause would fail with
        OperationalError: too many SQL variables. The fix uses a temporary
        table approach to avoid this limit.
        """
        store = FileMemoryStore(base_path=tmp_path, vector_enabled=True, embedding_provider="hash")

        # Create 1050 entries to comfortably exceed SQLite's default variable limit (999)
        entry_ids = []
        for i in range(1050):
            entry = MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content=f"Memory {i}: Important fact about topic {i}",
                metadata={"header": f"Entry {i}"},
            )
            entry_id = await store.save(entry)
            entry_ids.append(entry_id)

        # Verify all 1050 are indexed
        assert len(store._index) == 1050

        # Create orphan vector records (not in _index)
        with sqlite3.connect(store._vector_db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_vectors
                (doc_id, content, memory_type, user_scope, created_at, metadata_json,
                 embedding_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "orphan_1",
                    "orphaned text",
                    "long_term",
                    "default",
                    datetime.now(tz=UTC).isoformat(),
                    "{}",
                    "[]",
                ),
            )
            conn.execute(
                """
                INSERT INTO memory_vectors
                (doc_id, content, memory_type, user_scope, created_at, metadata_json,
                 embedding_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "orphan_2",
                    "another orphaned text",
                    "long_term",
                    "default",
                    datetime.now(tz=UTC).isoformat(),
                    "{}",
                    "[]",
                ),
            )
            conn.commit()

        # Verify orphans are present before cleanup
        with sqlite3.connect(store._vector_db_path) as conn:
            orphan_count = conn.execute(
                "SELECT COUNT(*) FROM memory_vectors WHERE doc_id LIKE 'orphan_%'"
            ).fetchone()[0]
            total_before = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]

        assert orphan_count == 2
        assert total_before >= 1050 + 2

        # Run cleanup — must not crash with "too many SQL variables" error
        # This is the critical test: with 1050 valid IDs in a direct NOT IN clause,
        # SQLite would hit the variable limit. The temp table approach should work.
        await store._cleanup_orphan_records()

        # Verify orphans are deleted and valid entries remain
        with sqlite3.connect(store._vector_db_path) as conn:
            remaining_orphan_count = conn.execute(
                "SELECT COUNT(*) FROM memory_vectors WHERE doc_id LIKE 'orphan_%'"
            ).fetchone()[0]
            total_after = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]

        assert remaining_orphan_count == 0, "Orphaned records should be deleted"
        # All 1050 valid entries should still have vector records
        assert total_after >= 1050, "Valid entries should be preserved"

    async def test_cleanup_orphan_records_with_graph_entities(self, tmp_path):
        """Test that cleanup_orphan_records also cleans up graph relationships correctly."""
        store = FileMemoryStore(base_path=tmp_path, vector_enabled=True, embedding_provider="hash")

        # Create 100 entries with graph relationships
        entry_ids = []
        for i in range(100):
            entry = MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Project Alpha uses PostgreSQL and FastAPI framework",
                metadata={"header": f"Entry {i}"},
            )
            entry_id = await store.save(entry)
            entry_ids.append(entry_id)

        await store._cleanup_orphan_records()

        # Verify that the cleanup completes without error and maintains valid entities
        stats = await store.get_memory_stats()
        assert stats["total_memories"] >= 100

    async def test_semantic_search_sqlite_caps_candidate_limit(self, tmp_path):
        """Candidate fetch size should be bounded to avoid excessive memory usage."""
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Semantic candidate limit regression test",
                metadata={"header": "Limits"},
            )
        )

        observed_limits: list[int] = []
        real_connect = sqlite3.connect

        class _ConnectionProxy:
            def __init__(self, conn):
                self._conn = conn

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return self._conn.__exit__(exc_type, exc_val, exc_tb)

            def execute(self, sql, params=()):
                if "FROM memory_vectors" in sql and "LIMIT ?" in sql and len(params) >= 2:
                    observed_limits.append(int(params[1]))
                return self._conn.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def _connect_proxy(*args, **kwargs):
            return _ConnectionProxy(real_connect(*args, **kwargs))

        with patch("pocketpaw.memory.file_store.sqlite3.connect", side_effect=_connect_proxy):
            await store._semantic_search_sqlite("semantic query", user_id="default", limit=1)
            await store._semantic_search_sqlite("semantic query", user_id="default", limit=10_000)

        assert observed_limits, "Expected semantic search SQL query to run"
        assert 200 in observed_limits
        assert 2000 in observed_limits


# ===========================================================================
# TestGraphSVGHtmlEscaping
# ===========================================================================


class TestGraphSVGHtmlEscaping:
    """HTML escaping in get_graph_svg to prevent malformed SVG."""

    @staticmethod
    def _skip_if_graph_unavailable(svg: str) -> None:
        if "Graph visualization unavailable" in svg:
            pytest.skip("graph extras unavailable; SVG renderer fallback active")

    async def test_direct_entity_escaping_greater_than(self, tmp_path):
        """Test HTML escaping by directly inserting entities with >."""
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(store._graph_db_path) as conn:
            conn.execute(
                """INSERT INTO entities
                (entity_id, entity_key, display_name, mention_count, user_scope,
                first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "entity_1",
                    "value>100",
                    "Value>100",
                    5,
                    "default",
                    now,
                    now,
                ),
            )
            conn.commit()

        svg = await store.get_graph_svg(user_id="default")
        self._skip_if_graph_unavailable(svg)
        assert "&gt;" in svg
        assert "<svg" in svg
        assert "</svg>" in svg

    async def test_direct_entity_escaping_ampersand(self, tmp_path):
        """Test HTML escaping by directly inserting entities with &."""
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(store._graph_db_path) as conn:
            conn.execute(
                """INSERT INTO entities
                (entity_id, entity_key, display_name, mention_count, user_scope,
                first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "entity_1",
                    "dogs&cats",
                    "Dogs&Cats",
                    5,
                    "default",
                    now,
                    now,
                ),
            )
            conn.commit()

        svg = await store.get_graph_svg(user_id="default")
        self._skip_if_graph_unavailable(svg)
        assert "&amp;" in svg
        assert "<svg" in svg
        assert "</svg>" in svg

    async def test_direct_relation_type_escaping(self, tmp_path):
        """Test HTML escaping for relation types in edge labels."""
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        # Insert entities and a relationship with special chars in type
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(store._graph_db_path) as conn:
            conn.execute(
                """INSERT INTO entities
                (entity_id, entity_key, display_name, mention_count, user_scope,
                first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("entity_1", "python", "Python", 5, "default", now, now),
            )
            conn.execute(
                """INSERT INTO entities
                (entity_id, entity_key, display_name, mention_count, user_scope,
                first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("entity_2", "framework", "Framework", 3, "default", now, now),
            )
            # Relationship with normalized types; test escaping
            conn.execute(
                """INSERT INTO relationships
                (relationship_id, source_entity_id, target_entity_id,
                relation_type, weight, user_scope, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                ("rel_1", "entity_1", "entity_2", "uses", 0, "default", now, now),
            )
            conn.commit()

        svg = await store.get_graph_svg(user_id="default")
        self._skip_if_graph_unavailable(svg)
        # Verify SVG is well-formed and contains the entities
        assert "<svg" in svg
        assert "</svg>" in svg
        assert "Python" in svg or "Framework" in svg or "uses" in svg

    async def test_svg_without_special_chars_unchanged(self, tmp_path):
        """Normal entity names without special chars work as expected."""
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Python is great for backend development",
                metadata={"header": "Language"},
            )
        )

        svg = await store.get_graph_svg(user_id="default")
        self._skip_if_graph_unavailable(svg)
        # Should have normal SVG structure
        assert "<svg" in svg
        assert "</svg>" in svg
        assert "<text" in svg
        # Should have node label text
        assert "Python" in svg

    async def test_svg_malformed_without_escaping(self, tmp_path):
        """Demonstrate that without escaping, SVG would be malformed."""
        # This test verifies the fix prevents SVG malformation
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(store._graph_db_path) as conn:
            conn.execute(
                """INSERT INTO entities
                (entity_id, entity_key, display_name, mention_count, user_scope,
                first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "entity_1",
                    "test<bad>evil",
                    "Test<Bad>Evil",
                    5,
                    "default",
                    now,
                    now,
                ),
            )
            conn.commit()

        svg = await store.get_graph_svg(user_id="default")
        self._skip_if_graph_unavailable(svg)
        # SVG should still be well-formed (properly closed tags)
        assert svg.count("<svg") == 1
        assert svg.count("</svg>") == 1
        # Should have escaped the angle brackets
        assert "&lt;" in svg or "&gt;" in svg

    async def test_empty_graph_returns_valid_svg(self, tmp_path):
        """Empty graph with no entities returns valid SVG."""
        store = FileMemoryStore(base_path=tmp_path)

        svg = await store.get_graph_svg(user_id="default")
        # Should return valid SVG even with no data
        assert "<svg" in svg
        assert "</svg>" in svg
        assert not store._graph_db_path.exists()

    async def test_graph_with_networkx_unavailable_returns_fallback(self, tmp_path):
        """If networkx is unavailable, returns fallback SVG."""
        store = FileMemoryStore(
            base_path=tmp_path,
            vector_enabled=True,
            embedding_provider="hash",
        )

        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="Test entity",
                metadata={"header": "Test"},
            )
        )

        # Mock networkx import failure
        with patch("importlib.import_module", side_effect=ImportError("No networkx")):
            svg = await store.get_graph_svg(user_id="default")

        # Should return fallback SVG
        assert "<svg" in svg
        assert "</svg>" in svg
        assert "Graph visualization unavailable" in svg or "pocketpaw[graph]" in svg
