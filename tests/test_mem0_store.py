# Tests for Mem0 Memory Store Integration
# Created: 2026-02-04
# Updated: 2026-02-07 — Configurable providers, auto-learn, semantic context

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketclaw.memory.file_store import FileMemoryStore
from pocketclaw.memory.manager import MemoryManager, create_memory_store
from pocketclaw.memory.protocol import MemoryEntry, MemoryType

# =========================================================================
# Factory Function Tests (always run — no mem0 needed)
# =========================================================================


class TestCreateMemoryStore:
    """Test the memory store factory function."""

    def test_create_file_store_default(self):
        """Default backend should be file store."""
        store = create_memory_store()
        assert isinstance(store, FileMemoryStore)

    def test_create_file_store_explicit(self):
        """Explicit file backend creates FileMemoryStore."""
        store = create_memory_store(backend="file")
        assert isinstance(store, FileMemoryStore)

    def test_create_unknown_backend_falls_back(self):
        """Unknown backend should fall back to file store."""
        store = create_memory_store(backend="unknown")
        assert isinstance(store, FileMemoryStore)

    def test_factory_passes_mem0_params(self):
        """Factory should accept and pass through mem0 config params."""
        # Even if mem0 is not installed, the factory should accept params
        store = create_memory_store(
            backend="file",
            llm_provider="ollama",
            llm_model="llama3.1",
            embedder_provider="ollama",
            embedder_model="nomic-embed-text",
            vector_store="chroma",
            ollama_base_url="http://localhost:11434",
        )
        assert isinstance(store, FileMemoryStore)


class TestMemoryManagerBackendSelection:
    """Test MemoryManager with different backends."""

    def test_manager_with_file_backend(self):
        """MemoryManager should use file backend by default."""
        manager = MemoryManager(backend="file")
        assert isinstance(manager._store, FileMemoryStore)

    def test_manager_with_custom_store(self):
        """MemoryManager should accept custom store."""
        mock_store = MagicMock()
        manager = MemoryManager(store=mock_store)
        assert manager._store is mock_store

    def test_manager_accepts_mem0_params(self):
        """MemoryManager should accept mem0 config params."""
        manager = MemoryManager(
            backend="file",
            llm_provider="anthropic",
            llm_model="claude-haiku-4-5-20251001",
            embedder_provider="openai",
            embedder_model="text-embedding-3-small",
            vector_store="qdrant",
            ollama_base_url="http://localhost:11434",
        )
        assert isinstance(manager._store, FileMemoryStore)


# =========================================================================
# Config Builder Tests (no mem0 needed)
# =========================================================================


class TestBuildMem0Config:
    """Test _build_mem0_config helper."""

    def test_anthropic_llm_config(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(
            llm_provider="anthropic",
            llm_model="claude-haiku-4-5-20251001",
        )
        assert config["llm"]["provider"] == "anthropic"
        assert config["llm"]["config"]["model"] == "claude-haiku-4-5-20251001"
        assert config["llm"]["config"]["temperature"] == 0

    def test_openai_llm_config(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(llm_provider="openai", llm_model="gpt-4o")
        assert config["llm"]["provider"] == "openai"
        assert config["llm"]["config"]["model"] == "gpt-4o"

    def test_ollama_llm_config(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(
            llm_provider="ollama",
            llm_model="llama3.1",
            ollama_base_url="http://localhost:11434",
        )
        assert config["llm"]["provider"] == "ollama"
        assert config["llm"]["config"]["model"] == "llama3.1"
        assert config["llm"]["config"]["ollama_base_url"] == "http://localhost:11434"

    def test_openai_embedder_config(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(
            embedder_provider="openai",
            embedder_model="text-embedding-3-small",
        )
        assert config["embedder"]["provider"] == "openai"
        assert config["embedder"]["config"]["model"] == "text-embedding-3-small"

    def test_ollama_embedder_config(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(
            embedder_provider="ollama",
            embedder_model="nomic-embed-text",
            ollama_base_url="http://localhost:11434",
        )
        assert config["embedder"]["provider"] == "ollama"
        assert config["embedder"]["config"]["model"] == "nomic-embed-text"
        assert config["embedder"]["config"]["ollama_base_url"] == "http://localhost:11434"

    def test_qdrant_vector_store_config(self, tmp_path):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(
            vector_store="qdrant",
            embedder_model="text-embedding-3-small",
            data_path=tmp_path,
        )
        assert config["vector_store"]["provider"] == "qdrant"
        assert config["vector_store"]["config"]["collection_name"] == "pocketpaw_memory"
        assert config["vector_store"]["config"]["embedding_model_dims"] == 1536
        assert str(tmp_path / "qdrant") in config["vector_store"]["config"]["path"]

    def test_chroma_vector_store_config(self, tmp_path):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(vector_store="chroma", data_path=tmp_path)
        assert config["vector_store"]["provider"] == "chroma"
        assert config["vector_store"]["config"]["collection_name"] == "pocketpaw_memory"
        assert str(tmp_path / "chroma") in config["vector_store"]["config"]["path"]

    def test_embedding_dims_by_model(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config_small = _build_mem0_config(
            vector_store="qdrant", embedder_model="text-embedding-3-small"
        )
        assert config_small["vector_store"]["config"]["embedding_model_dims"] == 1536

        config_large = _build_mem0_config(
            vector_store="qdrant", embedder_model="text-embedding-3-large"
        )
        assert config_large["vector_store"]["config"]["embedding_model_dims"] == 3072

        config_nomic = _build_mem0_config(vector_store="qdrant", embedder_model="nomic-embed-text")
        assert config_nomic["vector_store"]["config"]["embedding_model_dims"] == 768

    def test_unknown_embedder_defaults_to_1536(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(vector_store="qdrant", embedder_model="custom-model")
        assert config["vector_store"]["config"]["embedding_model_dims"] == 1536

    def test_config_has_version(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config()
        assert config["version"] == "v1.1"

    def test_anthropic_api_key_passed(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(llm_provider="anthropic", anthropic_api_key="sk-test-key")
        assert config["llm"]["config"]["api_key"] == "sk-test-key"

    def test_openai_api_key_passed_to_llm(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(llm_provider="openai", openai_api_key="sk-openai-key")
        assert config["llm"]["config"]["api_key"] == "sk-openai-key"

    def test_openai_api_key_passed_to_embedder(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(embedder_provider="openai", openai_api_key="sk-openai-key")
        assert config["embedder"]["config"]["api_key"] == "sk-openai-key"

    def test_no_api_key_when_none(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(llm_provider="anthropic")
        assert "api_key" not in config["llm"]["config"]

    def test_ollama_dims_auto_detection(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        # Unknown model with ollama provider — triggers auto-detection
        with patch("pocketclaw.memory.mem0_store._get_ollama_embedding_dims", return_value=512):
            config = _build_mem0_config(
                embedder_provider="ollama",
                embedder_model="custom-ollama-embed",
                vector_store="qdrant",
            )
        assert config["vector_store"]["config"]["embedding_model_dims"] == 512

    def test_qwen3_embedding_dims_known(self):
        from pocketclaw.memory.mem0_store import _build_mem0_config

        config = _build_mem0_config(embedder_model="qwen3-embedding:0.6b", vector_store="qdrant")
        assert config["vector_store"]["config"]["embedding_model_dims"] == 1024


# =========================================================================
# Mem0MemoryStore Tests (requires mem0ai or mocked)
# =========================================================================

try:
    import importlib.util

    HAS_MEM0 = importlib.util.find_spec("mem0") is not None
except Exception:
    HAS_MEM0 = False


@pytest.mark.skipif(not HAS_MEM0, reason="mem0ai not installed")
class TestMem0MemoryStore:
    """Tests for Mem0MemoryStore (requires mem0ai package)."""

    @pytest.fixture
    def mock_mem0_memory(self):
        """Create a mock Mem0 Memory instance."""
        mock_instance = MagicMock()

        # Setup default return values
        mock_instance.add.return_value = {
            "results": [{"id": "test-id-123", "memory": "test content", "event": "ADD"}]
        }
        mock_instance.get.return_value = {
            "id": "test-id-123",
            "memory": "test content",
            "metadata": {"pocketpaw_type": "long_term", "tags": ["test"]},
        }
        mock_instance.search.return_value = {
            "results": [
                {
                    "id": "test-id-123",
                    "memory": "test content",
                    "metadata": {"pocketpaw_type": "long_term", "tags": ["test"]},
                    "score": 0.95,
                }
            ]
        }
        mock_instance.get_all.return_value = {
            "results": [
                {
                    "id": "test-id-123",
                    "memory": "test content",
                    "metadata": {"pocketpaw_type": "long_term", "tags": ["test"]},
                }
            ]
        }
        mock_instance.delete.return_value = None
        mock_instance.delete_all.return_value = None

        return mock_instance

    @pytest.fixture
    def mem0_store(self, mock_mem0_memory, tmp_path):
        """Create a Mem0MemoryStore with mocked Memory."""
        from pocketclaw.memory.mem0_store import Mem0MemoryStore

        store = Mem0MemoryStore(
            user_id="test-user",
            data_path=tmp_path / "mem0_data",
            use_inference=False,
            llm_provider="anthropic",
            llm_model="claude-haiku-4-5-20251001",
            embedder_provider="openai",
            embedder_model="text-embedding-3-small",
            vector_store="qdrant",
        )
        # Inject mock - bypass lazy initialization
        store._memory = mock_mem0_memory
        store._initialized = True
        return store

    # --- Save tests ---

    async def test_save_long_term_memory(self, mem0_store, mock_mem0_memory):
        entry = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="User prefers dark mode",
            tags=["preferences"],
        )
        result_id = await mem0_store.save(entry)
        assert result_id == "test-id-123"
        mock_mem0_memory.add.assert_called_once()
        # Long-term should use user_id
        call_kwargs = mock_mem0_memory.add.call_args[1]
        assert call_kwargs.get("user_id") == "test-user"

    async def test_save_session_memory(self, mem0_store, mock_mem0_memory):
        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="Hello, how are you?",
            role="user",
            session_key="test-session",
        )
        result_id = await mem0_store.save(entry)
        assert result_id == "test-id-123"
        call_kwargs = mock_mem0_memory.add.call_args[1]
        assert call_kwargs.get("run_id") == "test-session"
        assert call_kwargs.get("infer") is False

    async def test_save_daily_memory(self, mem0_store, mock_mem0_memory):
        entry = MemoryEntry(
            id="",
            type=MemoryType.DAILY,
            content="Had a meeting about project X",
            tags=["work"],
        )
        result_id = await mem0_store.save(entry)
        assert result_id == "test-id-123"
        call_kwargs = mock_mem0_memory.add.call_args[1]
        assert call_kwargs.get("user_id") == "test-user"

    # --- Search tests ---

    async def test_search_memories(self, mem0_store, mock_mem0_memory):
        results = await mem0_store.search(query="dark mode", limit=5)
        assert len(results) == 1
        assert results[0].content == "test content"
        mock_mem0_memory.search.assert_called_once()

    async def test_search_without_query_uses_get_all(self, mem0_store, mock_mem0_memory):
        results = await mem0_store.search(query=None, limit=5)
        assert len(results) >= 0
        mock_mem0_memory.get_all.assert_called()

    async def test_search_with_tag_filter(self, mem0_store, mock_mem0_memory):
        # Search with matching tag
        results = await mem0_store.search(query="test", tags=["test"])
        assert len(results) == 1

    async def test_search_with_non_matching_tag(self, mem0_store, mock_mem0_memory):
        results = await mem0_store.search(query="test", tags=["nonexistent"])
        assert len(results) == 0

    # --- Get/Delete tests ---

    async def test_get_by_type(self, mem0_store, mock_mem0_memory):
        results = await mem0_store.get_by_type(MemoryType.LONG_TERM)
        assert len(results) >= 0
        mock_mem0_memory.get_all.assert_called()

    async def test_delete_memory(self, mem0_store, mock_mem0_memory):
        result = await mem0_store.delete("test-id-123")
        assert result is True
        mock_mem0_memory.delete.assert_called_once_with("test-id-123")

    async def test_delete_failure(self, mem0_store, mock_mem0_memory):
        mock_mem0_memory.delete.side_effect = Exception("Not found")
        result = await mem0_store.delete("bad-id")
        assert result is False

    # --- Session tests ---

    async def test_get_session(self, mem0_store, mock_mem0_memory):
        entries = await mem0_store.get_session("test-session")
        assert len(entries) >= 0
        mock_mem0_memory.get_all.assert_called()

    async def test_clear_session(self, mem0_store, mock_mem0_memory):
        count = await mem0_store.clear_session("test-session")
        assert count == 1
        mock_mem0_memory.delete_all.assert_called_once()

    # --- Auto-learn tests ---

    async def test_auto_learn(self, mem0_store, mock_mem0_memory):
        messages = [
            {"role": "user", "content": "I love Python programming"},
            {"role": "assistant", "content": "Great! Python is a wonderful language."},
        ]
        result = await mem0_store.auto_learn(messages)
        assert "results" in result
        assert len(result["results"]) == 1
        call_kwargs = mock_mem0_memory.add.call_args[1]
        assert call_kwargs.get("user_id") == "test-user"
        assert call_kwargs.get("infer") is True

    async def test_auto_learn_custom_user_id(self, mem0_store, mock_mem0_memory):
        messages = [{"role": "user", "content": "My name is Alice"}]
        await mem0_store.auto_learn(messages, user_id="alice")
        call_kwargs = mock_mem0_memory.add.call_args[1]
        assert call_kwargs.get("user_id") == "alice"

    async def test_auto_learn_empty_messages(self, mem0_store, mock_mem0_memory):
        result = await mem0_store.auto_learn([])
        assert result == {"results": []}
        mock_mem0_memory.add.assert_not_called()

    async def test_auto_learn_handles_error(self, mem0_store, mock_mem0_memory):
        mock_mem0_memory.add.side_effect = Exception("API error")
        result = await mem0_store.auto_learn([{"role": "user", "content": "test"}])
        assert "error" in result

    # --- Semantic search tests ---

    async def test_semantic_search(self, mem0_store, mock_mem0_memory):
        results = await mem0_store.semantic_search("programming")
        assert len(results) == 1
        assert results[0]["memory"] == "test content"
        assert results[0]["score"] == 0.95

    async def test_semantic_search_custom_user_id(self, mem0_store, mock_mem0_memory):
        await mem0_store.semantic_search("test", user_id="alice")
        call_kwargs = mock_mem0_memory.search.call_args[1]
        assert call_kwargs.get("user_id") == "alice"

    async def test_semantic_search_error(self, mem0_store, mock_mem0_memory):
        mock_mem0_memory.search.side_effect = Exception("Search error")
        results = await mem0_store.semantic_search("test")
        assert results == []

    # --- Stats tests ---

    async def test_get_memory_stats(self, mem0_store, mock_mem0_memory):
        stats = await mem0_store.get_memory_stats()
        assert "total_memories" in stats
        assert stats["user_id"] == "test-user"
        assert stats["backend"] == "mem0"
        assert stats["llm_provider"] == "anthropic"
        assert stats["embedder_provider"] == "openai"
        assert stats["vector_store"] == "qdrant"

    # --- Config tests ---

    def test_store_stores_provider_config(self, tmp_path):
        from pocketclaw.memory.mem0_store import Mem0MemoryStore

        store = Mem0MemoryStore(
            user_id="test",
            data_path=tmp_path,
            llm_provider="ollama",
            llm_model="llama3.1",
            embedder_provider="ollama",
            embedder_model="nomic-embed-text",
            vector_store="chroma",
            ollama_base_url="http://my-ollama:11434",
        )
        assert store._llm_provider == "ollama"
        assert store._llm_model == "llama3.1"
        assert store._embedder_provider == "ollama"
        assert store._embedder_model == "nomic-embed-text"
        assert store._vector_store == "chroma"
        assert store._ollama_base_url == "http://my-ollama:11434"


# =========================================================================
# MemoryEntry Conversion Tests (requires mem0ai for import)
# =========================================================================


@pytest.mark.skipif(not HAS_MEM0, reason="mem0ai not installed")
class TestMemoryEntryConversion:
    """Test conversion between Mem0 format and MemoryEntry."""

    def test_mem0_to_entry_conversion(self):
        from pocketclaw.memory.mem0_store import Mem0MemoryStore

        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
        mem0_item = {
            "id": "test-id",
            "memory": "Test memory content",
            "metadata": {
                "pocketpaw_type": "long_term",
                "tags": ["test", "example"],
                "created_at": "2026-02-04T10:00:00",
                "custom_field": "custom_value",
            },
        }
        entry = store._mem0_to_entry(mem0_item)
        assert entry.id == "test-id"
        assert entry.content == "Test memory content"
        assert entry.type == MemoryType.LONG_TERM
        assert "test" in entry.tags
        assert "custom_field" in entry.metadata

    def test_mem0_to_entry_handles_missing_type(self):
        from pocketclaw.memory.mem0_store import Mem0MemoryStore

        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
        mem0_item = {"id": "test-id", "memory": "Test content", "metadata": {}}
        entry = store._mem0_to_entry(mem0_item)
        assert entry.type == MemoryType.LONG_TERM

    def test_mem0_to_entry_handles_session_type(self):
        from pocketclaw.memory.mem0_store import Mem0MemoryStore

        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
        mem0_item = {
            "id": "test-id",
            "memory": "Hello",
            "metadata": {
                "pocketpaw_type": "session",
                "role": "user",
                "session_key": "telegram:123",
            },
        }
        entry = store._mem0_to_entry(mem0_item)
        assert entry.type == MemoryType.SESSION
        assert entry.role == "user"
        assert entry.session_key == "telegram:123"

    def test_mem0_to_entry_handles_invalid_timestamp(self):
        from pocketclaw.memory.mem0_store import Mem0MemoryStore

        store = Mem0MemoryStore.__new__(Mem0MemoryStore)
        mem0_item = {
            "id": "test-id",
            "memory": "content",
            "metadata": {"created_at": "not-a-date"},
        }
        entry = store._mem0_to_entry(mem0_item)
        assert entry.created_at is not None  # Should fall back to datetime.now()


# =========================================================================
# MemoryManager Auto-Learn & Semantic Context Tests
# =========================================================================


class TestMemoryManagerAutoLearn:
    """Test auto-learn and semantic context features."""

    async def test_auto_learn_with_file_backend(self):
        """Auto-learn should be a no-op for file backend."""
        manager = MemoryManager(backend="file")
        result = await manager.auto_learn([{"role": "user", "content": "test"}])
        assert result == {}

    async def test_auto_learn_with_mem0_store(self):
        """Auto-learn should delegate to mem0 store."""
        mock_store = MagicMock()
        mock_store.auto_learn = AsyncMock(return_value={"results": [{"id": "x"}]})
        manager = MemoryManager(store=mock_store)
        result = await manager.auto_learn([{"role": "user", "content": "I love Python"}])
        assert result == {"results": [{"id": "x"}]}
        mock_store.auto_learn.assert_called_once()

    async def test_get_semantic_context_with_file_backend(self):
        """Semantic context should fall back to get_context_for_agent for file."""
        mock_store = MagicMock(spec=["get_by_type"])
        mock_store.get_by_type = AsyncMock(return_value=[])
        manager = MemoryManager(store=mock_store)
        context = await manager.get_semantic_context("test query")
        assert isinstance(context, str)

    async def test_get_semantic_context_with_mem0_store(self):
        """Semantic context should use semantic_search for mem0."""
        mock_store = MagicMock()
        mock_store.semantic_search = AsyncMock(
            return_value=[
                {"memory": "User likes Python", "id": "1", "score": 0.9},
                {"memory": "User is a data scientist", "id": "2", "score": 0.8},
            ]
        )
        manager = MemoryManager(store=mock_store)
        context = await manager.get_semantic_context("programming")
        assert "User likes Python" in context
        assert "User is a data scientist" in context
        assert "Relevant Memories" in context

    async def test_get_semantic_context_empty_results(self):
        """Semantic context should fall back when no results."""
        mock_store = MagicMock()
        mock_store.semantic_search = AsyncMock(return_value=[])
        mock_store.get_by_type = AsyncMock(return_value=[])
        manager = MemoryManager(store=mock_store)
        context = await manager.get_semantic_context("test")
        assert isinstance(context, str)


# =========================================================================
# Settings Config Tests
# =========================================================================


class TestMemorySettings:
    """Test memory-related settings in config."""

    def test_default_memory_settings(self):
        from pocketclaw.config import Settings

        settings = Settings()
        assert settings.memory_backend == "file"
        assert settings.memory_use_inference is True
        assert settings.mem0_llm_provider == "anthropic"
        assert settings.mem0_llm_model == "claude-haiku-4-5-20251001"
        assert settings.mem0_embedder_provider == "openai"
        assert settings.mem0_embedder_model == "text-embedding-3-small"
        assert settings.mem0_vector_store == "qdrant"
        assert settings.mem0_ollama_base_url == "http://localhost:11434"
        assert settings.mem0_auto_learn is True

    def test_memory_settings_saved(self, tmp_path):
        """Memory settings should be included in save()."""
        import json

        from pocketclaw.config import Settings

        settings = Settings()
        settings.memory_backend = "mem0"
        settings.mem0_llm_provider = "ollama"
        settings.mem0_auto_learn = False

        # Temporarily override config path
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")

        with patch("pocketclaw.config.get_config_path", return_value=config_path):
            settings.save()

        saved = json.loads(config_path.read_text())
        assert saved["memory_backend"] == "mem0"
        assert saved["mem0_llm_provider"] == "ollama"
        assert saved["mem0_auto_learn"] is False
        assert "mem0_llm_model" in saved
        assert "mem0_embedder_provider" in saved
        assert "mem0_embedder_model" in saved
        assert "mem0_vector_store" in saved
        assert "mem0_ollama_base_url" in saved


# =========================================================================
# Context Builder Tests
# =========================================================================


class TestContextBuilderWithMem0:
    """Test AgentContextBuilder with mem0 integration."""

    async def test_build_prompt_with_user_query(self):
        """Context builder should pass user_query for semantic search."""
        from pocketclaw.bootstrap.context_builder import AgentContextBuilder

        mock_memory = MagicMock()
        mock_memory.get_semantic_context = AsyncMock(
            return_value="## Relevant Memories\n- User likes Python"
        )

        mock_bootstrap = MagicMock()
        mock_bootstrap.get_context = AsyncMock()
        mock_context = MagicMock()
        mock_context.to_system_prompt.return_value = "You are PocketPaw."
        mock_bootstrap.get_context.return_value = mock_context

        builder = AgentContextBuilder(
            bootstrap_provider=mock_bootstrap,
            memory_manager=mock_memory,
        )
        prompt = await builder.build_system_prompt(user_query="tell me about Python")
        assert "PocketPaw" in prompt
        assert "Python" in prompt
        mock_memory.get_semantic_context.assert_called_once_with(
            "tell me about Python", sender_id=None
        )

    async def test_build_prompt_without_user_query(self):
        """Without user_query, should use standard context."""
        from pocketclaw.bootstrap.context_builder import AgentContextBuilder

        mock_memory = MagicMock()
        mock_memory.get_context_for_agent = AsyncMock(return_value="some context")

        mock_bootstrap = MagicMock()
        mock_bootstrap.get_context = AsyncMock()
        mock_context = MagicMock()
        mock_context.to_system_prompt.return_value = "You are PocketPaw."
        mock_bootstrap.get_context.return_value = mock_context

        builder = AgentContextBuilder(
            bootstrap_provider=mock_bootstrap,
            memory_manager=mock_memory,
        )
        await builder.build_system_prompt()
        mock_memory.get_context_for_agent.assert_called_once_with(sender_id=None)
