# Tests for Memory System
# Created: 2026-02-02


import tempfile
from pathlib import Path

import pytest

from pocketpaw.memory.file_store import FileMemoryStore
from pocketpaw.memory.manager import MemoryManager
from pocketpaw.memory.protocol import MemoryEntry, MemoryType


@pytest.fixture
def temp_memory_path():
    """Create a temporary directory for memory tests."""
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    # Windows: SQLite WAL files may still be locked; best-effort cleanup
    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)


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
        content = memory_store.long_term_file.read_text(encoding="utf-8")
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


class TestGraphExtraction:
    """Tests for knowledge graph extraction with conservative regex patterns."""

    @pytest.fixture
    def vector_memory_store(self, temp_memory_path):
        """Create a FileMemoryStore with vector/graph features enabled."""
        return FileMemoryStore(
            base_path=temp_memory_path,
            vector_enabled=True,  # Enables graph extraction
        )

    @pytest.fixture
    def plain_memory_store(self, temp_memory_path):
        """Create a FileMemoryStore with vector/graph features disabled."""
        return FileMemoryStore(
            base_path=temp_memory_path,
            vector_enabled=False,  # Disables graph extraction
        )

    def test_entity_blacklist_filtering(self, vector_memory_store):
        """Test that blacklisted generic words are rejected as entities."""
        # These should be filtered out
        blacklisted = [
            "something",
            "anything",
            "question",
            "thing",
            "it",
            "this",
            "that",
            "they",
            "meeting",
            "call",
            "plan",
        ]
        for word in blacklisted:
            assert not vector_memory_store._is_valid_entity_candidate(word)

    def test_valid_entity_candidates(self, vector_memory_store):
        """Test that valid entities pass validation."""
        valid = [
            "Project Phoenix",
            "PostgreSQL",
            "FastAPI",
            "OpenAI",
            "MyProject",
        ]
        for word in valid:
            assert vector_memory_store._is_valid_entity_candidate(word)

    def test_entity_length_validation(self, vector_memory_store):
        """Test that overly long entities are rejected."""
        # Too long (sentence fragment)
        too_long = "This is a very long sentence that should not be an entity"
        assert not vector_memory_store._is_valid_entity_candidate(too_long)

        # Just right
        ok_length = "Project Phoenix"
        assert vector_memory_store._is_valid_entity_candidate(ok_length)

    def test_self_loop_prevention(self, vector_memory_store):
        """Test that self-referential relationships are rejected."""
        assert not vector_memory_store._is_valid_relationship_candidate(
            "Project", "uses", "Project"
        )
        assert not vector_memory_store._is_valid_relationship_candidate(
            "OpenAI",
            "uses",
            "openai",  # Case insensitive
        )

    def test_valid_relationship_candidates(self, vector_memory_store):
        """Test that valid relationships pass validation."""
        assert vector_memory_store._is_valid_relationship_candidate(
            "Project Phoenix", "uses", "PostgreSQL"
        )
        assert vector_memory_store._is_valid_relationship_candidate("MyApp", "depends_on", "Redis")

    def test_extract_graph_signals_tech_terms(self, vector_memory_store):
        """Test extraction of technology terms."""
        content = "The project uses Python and PostgreSQL for the backend"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        # Should extract canonical tech names
        assert "Python" in entities
        assert "PostgreSQL" in entities

    def test_extract_graph_signals_title_case(self, vector_memory_store):
        """Test extraction of title-case entities."""
        content = "Project Phoenix uses FastAPI for the API layer"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        # Should extract title-case entities
        assert "Project Phoenix" in entities
        assert "FastAPI" in entities

    def test_extract_graph_signals_uses_pattern(self, vector_memory_store):
        """Test 'uses' relationship pattern extraction."""
        content = "Project Phoenix uses PostgreSQL for data storage"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        # Should find the relationship
        assert any(
            src == "Project Phoenix" and rel == "uses" and tgt == "PostgreSQL for data"
            for src, rel, tgt in relationships
        )

    def test_extract_graph_signals_depends_on_pattern(self, vector_memory_store):
        """Test 'depends_on' relationship pattern extraction."""
        content = "MyService depends on Redis for caching"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "MyService" and rel == "depends_on" and tgt == "Redis for caching"
            for src, rel, tgt in relationships
        )

    def test_entity_canonicalization(self, vector_memory_store):
        """Canonicalization strips determiners and normalizes known tech names."""
        assert vector_memory_store._canonicalize_entity_name("the openai") == "OpenAI"
        assert vector_memory_store._canonicalize_entity_name("an postgresql") == "PostgreSQL"
        assert (
            vector_memory_store._canonicalize_entity_name("  Project   Phoenix  ")
            == "Project Phoenix"
        )

    def test_relation_normalization_schema(self, vector_memory_store):
        """Relation normalization maps aliases into controlled schema."""
        assert vector_memory_store._normalize_relation_type("depends on") == "depends_on"
        assert vector_memory_store._normalize_relation_type("invokes") == "calls"
        assert vector_memory_store._normalize_relation_type("inherits_from") == "extends"
        assert vector_memory_store._normalize_relation_type("unknown_rel") == ""

    def test_confidence_threshold_blocks_low_confidence_edges(
        self,
        vector_memory_store,
        monkeypatch,
    ):
        """Low-confidence relationship candidates are dropped."""

        monkeypatch.setattr(
            FileMemoryStore,
            "_score_relationship_candidate",
            staticmethod(lambda src, rel, tgt: 0.50),
        )

        content = "Project Phoenix uses PostgreSQL for data storage"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert "Project Phoenix" in entities
        assert "PostgreSQL" in entities
        assert relationships == []

    def test_stores_only_high_confidence_edges(self, vector_memory_store):
        """Only confidence-qualified edges are emitted from extraction."""
        content = "Project Alpha is built on FastAPI"
        entities, relationships = vector_memory_store._extract_graph_signals(content)
        assert any(rel == "built_on" for _, rel, _ in relationships)

    def test_extract_graph_signals_built_on_pattern(self, vector_memory_store):
        """Test 'built_on' relationship pattern extraction."""
        content = "Project Alpha is built on FastAPI"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "Project Alpha" and rel == "built_on" and tgt == "FastAPI"
            for src, rel, tgt in relationships
        )

    def test_extract_graph_signals_is_a_pattern(self, vector_memory_store):
        """Test 'is_a' relationship pattern extraction."""
        content = "PostgreSQL is a type of database"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "PostgreSQL" and rel == "is_a" and tgt == "database"
            for src, rel, tgt in relationships
        )

    def test_extract_graph_signals_part_of_pattern(self, vector_memory_store):
        """Test 'part_of' relationship pattern extraction."""
        content = "AuthService is part of Project Phoenix"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "AuthService" and rel == "part_of" and tgt == "Project Phoenix"
            for src, rel, tgt in relationships
        )

    def test_extract_graph_signals_implements_pattern(self, vector_memory_store):
        """Test 'implements' relationship pattern extraction."""
        content = "MyClient implements the API interface"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "MyClient" and rel == "implements" and tgt == "API interface"
            for src, rel, tgt in relationships
        )

    def test_extract_graph_signals_extends_pattern(self, vector_memory_store):
        """Test 'extends' relationship pattern extraction."""
        content = "MyBackend extends BaseService"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "MyBackend" and rel == "extends" and tgt == "BaseService"
            for src, rel, tgt in relationships
        )

    def test_extract_graph_signals_inherits_from_pattern(self, vector_memory_store):
        """Test 'inherits_from' relationship pattern extraction."""
        content = "MyBackend inherits from BaseService"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "MyBackend" and rel == "extends" and tgt == "BaseService"
            for src, rel, tgt in relationships
        )

    def test_extract_graph_signals_calls_pattern(self, vector_memory_store):
        """Test 'calls' relationship pattern extraction."""
        content = "Frontend calls the Backend API"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "Frontend" and rel == "calls" and tgt == "Backend API"
            for src, rel, tgt in relationships
        )

    def test_extract_graph_signals_invokes_pattern(self, vector_memory_store):
        """Test 'invokes' relationship pattern extraction."""
        content = "Frontend invokes the Backend API"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        assert any(
            src == "Frontend" and rel == "calls" and tgt == "Backend API"
            for src, rel, tgt in relationships
        )

    def test_no_false_positive_has_pattern(self, vector_memory_store):
        """Test that 'has' pattern does NOT create false positives."""
        content = "The user has a question about billing"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        # Should NOT create a "user has question" relationship
        assert not any(
            rel == "has" or "user" in src.lower() and "question" in tgt.lower()
            for src, rel, tgt in relationships
        )

    def test_no_false_positive_generic_verbs(self, vector_memory_store):
        """Test that generic conversational text doesn't create relationships."""
        content = "I have a meeting tomorrow and need to check something"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        # Should not extract relationships from generic text
        # (no capitalized tech entities, blacklisted words filtered)
        assert len(relationships) == 0

    def test_graph_disabled_when_vector_disabled(self, plain_memory_store):
        """Test that graph extraction is disabled when vector_enabled=False."""
        assert not plain_memory_store._graph_enabled

    def test_graph_enabled_when_vector_enabled(self, vector_memory_store):
        """Test that graph extraction is enabled when vector_enabled=True."""
        assert vector_memory_store._graph_enabled

    def test_extraction_bounds(self, vector_memory_store):
        """Test that extraction is bounded to prevent runaway processing."""
        # Create content with many potential entities
        content = " ".join([f"Project{i} uses Tool{i}" for i in range(50)])
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        # Should be bounded
        assert len(entities) <= 12
        assert len(relationships) <= 24

    def test_no_arbitrary_fallback_edges(self, vector_memory_store):
        """Test that arbitrary 'related_to' fallback edges are NOT created."""
        content = "Project Alpha and Project Beta are both important"
        entities, relationships = vector_memory_store._extract_graph_signals(content)

        # Should NOT create arbitrary fallback edges
        assert not any(rel == "related_to" for src, rel, tgt in relationships)


async def test_get_by_type_delegates_to_store(tmp_path):
    """Test that get_by_type public method delegates to the underlying store."""
    from unittest.mock import AsyncMock

    from pocketpaw.memory.manager import MemoryManager
    from pocketpaw.memory.protocol import MemoryType

    manager = MemoryManager(base_path=tmp_path)
    manager._store.get_by_type = AsyncMock(return_value=[])

    await manager.get_by_type(MemoryType.LONG_TERM, limit=10)

    manager._store.get_by_type.assert_awaited_once_with(
        MemoryType.LONG_TERM, limit=10, user_id=None
    )


async def test_get_by_type_forwards_user_id(tmp_path):
    """Test that get_by_type forwards user_id to the store for scoped retrieval."""
    from unittest.mock import AsyncMock

    from pocketpaw.memory.manager import MemoryManager
    from pocketpaw.memory.protocol import MemoryType

    manager = MemoryManager(base_path=tmp_path)
    manager._store.get_by_type = AsyncMock(return_value=[])

    await manager.get_by_type(MemoryType.LONG_TERM, limit=5, user_id="user123")

    manager._store.get_by_type.assert_awaited_once_with(
        MemoryType.LONG_TERM, limit=5, user_id="user123"
    )
