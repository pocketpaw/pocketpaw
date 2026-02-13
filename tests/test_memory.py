# Tests for Memory System
# Created: 2026-02-02


import pytest
import tempfile
from pathlib import Path

from pocketclaw.memory.protocol import MemoryType, MemoryEntry
from pocketclaw.memory.file_store import FileMemoryStore
from pocketclaw.memory.manager import MemoryManager


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
