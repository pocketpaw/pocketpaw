# Tests for Memory System
# Created: 2026-02-02


import pytest
import tempfile
import asyncio
from unittest.mock import AsyncMock
from pathlib import Path

from pocketpaw.memory.protocol import MemoryType, MemoryEntry
from pocketpaw.memory.file_store import FileMemoryStore
from pocketpaw.memory.manager import MemoryManager


@pytest.fixture
def temp_memory_path():
    """Create a temporary directory for memory tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def memory_store(temp_memory_path):
    """Create a FileMemoryStore with temp path."""
    return FileMemoryStore(base_path=temp_memory_path)


@pytest.fixture
def memory_manager(temp_memory_path):
    """Create a MemoryManager with temp path."""
    return MemoryManager(base_path=temp_memory_path)


class TestMemoryEntry:
    """Tests for MemoryEntry dataclass."""

    def test_create_entry(self):
        entry = MemoryEntry(
            id="test-id",
            type=MemoryType.LONG_TERM,
            content="Test content",
        )
        assert entry.id == "test-id"
        assert entry.type == MemoryType.LONG_TERM
        assert entry.content == "Test content"
        assert entry.tags == []
        assert entry.metadata == {}

    def test_entry_with_tags(self):
        entry = MemoryEntry(
            id="test-id",
            type=MemoryType.DAILY,
            content="Daily note",
            tags=["work", "important"],
        )
        assert entry.tags == ["work", "important"]

    def test_session_entry(self):
        entry = MemoryEntry(
            id="test-id",
            type=MemoryType.SESSION,
            content="Hello!",
            role="user",
            session_key="websocket:user123",
        )
        assert entry.role == "user"
        assert entry.session_key == "websocket:user123"


class TestFileMemoryStore:
    """Tests for FileMemoryStore."""

    @pytest.mark.asyncio
    async def test_save_and_get_long_term(self, memory_store):
        entry = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="User prefers dark mode",
            tags=["preferences"],
            metadata={"header": "User Preferences"},
        )
        entry_id = await memory_store.save(entry)
        assert entry_id

        # Check file was created
        assert memory_store.long_term_file.exists()
        content = memory_store.long_term_file.read_text()
        assert "User prefers dark mode" in content

    @pytest.mark.asyncio
    async def test_save_session(self, memory_store):
        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="Hello, how are you?",
            role="user",
            session_key="test_session",
        )
        await memory_store.save(entry)

        # Verify session was saved
        history = await memory_store.get_session("test_session")
        assert len(history) == 1
        assert history[0].content == "Hello, how are you?"
        assert history[0].role == "user"

    @pytest.mark.asyncio
    async def test_clear_session(self, memory_store):
        # Add some messages
        for i in range(3):
            entry = MemoryEntry(
                id="",
                type=MemoryType.SESSION,
                content=f"Message {i}",
                role="user",
                session_key="test_session",
            )
            await memory_store.save(entry)

        # Clear session
        count = await memory_store.clear_session("test_session")
        assert count == 3

        # Verify empty
        history = await memory_store.get_session("test_session")
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_search(self, memory_store):
        # Save some memories
        entry1 = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="User likes Python programming",
            metadata={"header": "Preferences"},
        )
        entry2 = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="User prefers dark mode",
            metadata={"header": "UI"},
        )
        await memory_store.save(entry1)
        await memory_store.save(entry2)

        # Search
        results = await memory_store.search(query="Python")
        assert len(results) == 1
        assert "Python" in results[0].content


class TestMemoryManager:
    """Tests for MemoryManager facade."""

    @pytest.mark.asyncio
    async def test_remember(self, memory_manager):
        entry_id = await memory_manager.remember(
            "User prefers dark mode",
            tags=["preferences"],
            header="UI Preferences",
        )
        assert entry_id

    @pytest.mark.asyncio
    async def test_note(self, memory_manager):
        entry_id = await memory_manager.note(
            "Had a meeting about project X",
            tags=["work"],
        )
        assert entry_id

    @pytest.mark.asyncio
    async def test_session_flow(self, memory_manager):
        session_key = "test:session123"

        # Add messages
        await memory_manager.add_to_session(session_key, "user", "Hello!")
        await memory_manager.add_to_session(session_key, "assistant", "Hi there!")
        await memory_manager.add_to_session(session_key, "user", "How are you?")

        # Get history
        history = await memory_manager.get_session_history(session_key)
        assert len(history) == 3
        assert history[0] == {"role": "user", "content": "Hello!"}
        assert history[1] == {"role": "assistant", "content": "Hi there!"}

        # Clear
        count = await memory_manager.clear_session(session_key)
        assert count == 3

    @pytest.mark.asyncio
    async def test_get_context_for_agent(self, memory_manager):
        # Add some memories
        await memory_manager.remember("User prefers dark mode")
        await memory_manager.note("Working on PocketPaw today")

        # Get context
        context = await memory_manager.get_context_for_agent()
        assert "Long-term Memory" in context or "Today's Notes" in context


class TestMemoryIntegration:
    """Integration tests for the memory system."""

    @pytest.mark.asyncio
    async def test_full_workflow(self, temp_memory_path):
        """Test a realistic workflow."""
        manager = MemoryManager(base_path=temp_memory_path)

        # 1. Store user preference
        await manager.remember(
            "User's name is Prakash",
            tags=["user", "identity"],
            header="User Identity",
        )

        # 2. Add daily note
        await manager.note("Started working on memory system")

        # 3. Simulate conversation
        session = "websocket:prakash"
        await manager.add_to_session(session, "user", "What's my name?")
        await manager.add_to_session(session, "assistant", "Your name is Prakash!")

        # 4. Get agent context
        context = await manager.get_context_for_agent()
        assert "Prakash" in context

        # 5. Get session history
        history = await manager.get_session_history(session)
        assert len(history) == 2

        # 6. Verify files exist
        assert (temp_memory_path / "MEMORY.md").exists()
        assert (temp_memory_path / "sessions").is_dir()


class TestFileStoreConcurrency:
    """Concurrent writes to the same session."""

    @pytest.mark.asyncio
    async def test_concurrent_writes_same_session(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)
        session_key = "test:concurrent"

        async def write_message(i):
            entry = MemoryEntry(
                id="",
                type=MemoryType.SESSION,
                content=f"Message {i}",
                role="user",
                session_key=session_key,
            )
            await store.save(entry)

        # Simulate 25 concurrent writes
        await asyncio.gather(*(write_message(i) for i in range(25)))

        history = await store.get_session(session_key)

        assert len(history) == 25

        contents = {e.content for e in history}
        for i in range(25):
            assert f"Message {i}" in contents


class TestFileStoreUnicode:
    """Storing and retrieving unicode content."""

    @pytest.mark.asyncio
    async def test_unicode_storage_and_retrieval(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)

        content = "@Pocket paws 🐾 is the most loyal pup 🐶."

        entry = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content=content,
            metadata={"header": "Agent Personality"},
        )

        await store.save(entry)

        # Ensure file was created
        assert store.long_term_file.exists()

        # Ensure unicode preserved in file
        file_text = store.long_term_file.read_text(encoding="utf-8")
        assert "🐾" in file_text
        assert "🐶" in file_text
        assert "pup" in file_text

        # Ensure retrieval via get_by_type works
        entries = await store.get_by_type(MemoryType.LONG_TERM)
        assert len(entries) == 1
        assert entries[0].content == content

        # Ensure search still works for ASCII tokens
        results = await store.search("loyal")
        assert len(results) == 1
        assert "loyal pup" in results[0].content


class TestFileStoreEmptySession:
    """Handle empty or non-existent sessions."""

    @pytest.mark.asyncio
    async def test_get_nonexistent_session_returns_empty_list(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)

        history = await store.get_session("nonexistent:session")
        assert history == []

    @pytest.mark.asyncio
    async def test_clear_nonexistent_session_returns_zero(self, tmp_path):
        store = FileMemoryStore(base_path=tmp_path)

        count = await store.clear_session("nonexistent:session")
        assert count == 0


class TestSessionCompaction:
    """Session history exceeding compaction threshold."""

    @pytest.mark.asyncio
    async def test_session_exceeds_recent_window(self, tmp_path):
        manager = MemoryManager(base_path=tmp_path)
        session_key = "test:compaction"

        # Add 25 messages (default recent_window = 10)
        for i in range(25):
            await manager.add_to_session(
                session_key,
                role="user",
                content=f"Message number {i}",
            )

        compacted = await manager.get_compacted_history(session_key)

        # Should not return all 25 raw messages
        # Expected: 1 summary block + 10 recent messages
        assert len(compacted) <= 11

        # First message should be the summary block
        assert "[Earlier conversation]" in compacted[0]["content"]

        # Last 10 messages must be preserved verbatim
        for i in range(15, 25):
            assert any(f"Message number {i}" in msg["content"] for msg in compacted)


class TestMemoryManagerAutoLearnTrigger:
    """Auto-learn triggering conditions."""

    @pytest.mark.asyncio
    async def test_auto_learn_default_does_nothing(self, tmp_path):
        manager = MemoryManager(base_path=tmp_path)

        result = await manager.auto_learn([{"role": "user", "content": "My name is Pocket"}])

        assert result == {}

    @pytest.mark.asyncio
    async def test_auto_learn_triggers_when_enabled(self, tmp_path):
        manager = MemoryManager(base_path=tmp_path)

        manager._file_auto_learn = AsyncMock(return_value={"results": []})

        await manager.auto_learn(
            [{"role": "user", "content": "My name is Pocket"}],
            file_auto_learn=True,
        )

        manager._file_auto_learn.assert_called_once()


class TestMemoryManagerSearchNoResults:
    """Memory search returns empty list when nothing matches."""

    @pytest.mark.asyncio
    async def test_search_on_empty_store(self, tmp_path):
        manager = MemoryManager(base_path=tmp_path)

        # No memories added yet
        results = await manager.search("nonexistent")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_no_matching_memory(self, tmp_path):
        manager = MemoryManager(base_path=tmp_path)

        # Add one memory
        await manager.remember("Pocket paws")

        # Search for unrelated word
        results = await manager.search("banana")

        assert results == []


class TestMemoryManagerSessionListing:
    """Session listing and deletion behavior."""

    @pytest.mark.asyncio
    async def test_session_listing_and_deletion(self, tmp_path):
        manager = MemoryManager(base_path=tmp_path)
        session_key = "chat:123"

        # Add messages to create session
        await manager.add_to_session(session_key, "user", "Hello Paws")
        await manager.add_to_session(session_key, "assistant", "Hi there!")

        # List sessions
        sessions = await manager.list_sessions_for_chat(session_key)

        # Verify session appears
        assert len(sessions) == 1
        assert sessions[0]["session_key"] == session_key
        assert sessions[0]["message_count"] == 2
        assert sessions[0]["is_active"] is True

        # Delete session
        deleted = await manager.delete_session(session_key)
        assert deleted is True

        # Listing should now be empty
        sessions_after = await manager.list_sessions_for_chat(session_key)
        assert sessions_after == []
