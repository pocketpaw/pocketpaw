# Mem0-based memory store implementation.
# Created: 2026-02-04
# Updated: 2026-02-07 — Configurable LLM/embedder/vector providers, auto-learn
#
# Provides semantic memory with LLM-powered fact extraction and search.
#
# Mem0 features:
# - Vector-based semantic search (Qdrant/Chroma)
# - LLM-powered fact extraction and consolidation
# - Memory evolution (updates existing memories instead of duplicating)
# - Configurable LLM (Anthropic/OpenAI/Ollama) and embedder providers

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from pocketclaw.memory.protocol import MemoryEntry, MemoryType

logger = logging.getLogger(__name__)

# Embedding dimensions by model
_EMBEDDING_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "mxbai-embed-large": 1024,
    "mxbai-embed-large:latest": 1024,
    "all-minilm": 384,
    "all-minilm:latest": 384,
    "snowflake-arctic-embed": 1024,
    "qwen3-embedding:0.6b": 1024,
    "qwen3-embedding:latest": 1024,
}


def _get_ollama_embedding_dims(model: str, base_url: str) -> int | None:
    """Query Ollama for the actual embedding dimensions of a model."""
    try:
        import httpx

        resp = httpx.post(
            f"{base_url}/api/embeddings",
            json={"model": model, "prompt": "dim check"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            dims = len(resp.json().get("embedding", []))
            if dims > 0:
                logger.info("Auto-detected %d dims for Ollama model %s", dims, model)
                return dims
    except Exception as e:
        logger.debug("Could not auto-detect embedding dims for %s: %s", model, e)
    return None


def _build_mem0_config(
    llm_provider: str = "anthropic",
    llm_model: str = "claude-haiku-4-5-20251001",
    embedder_provider: str = "openai",
    embedder_model: str = "text-embedding-3-small",
    vector_store: str = "qdrant",
    ollama_base_url: str = "http://localhost:11434",
    data_path: Path | None = None,
    anthropic_api_key: str | None = None,
    openai_api_key: str | None = None,
) -> dict:
    """Build a mem0 config dict from PocketPaw settings.

    Returns a dict suitable for Memory.from_config().
    """
    data_path = data_path or (Path.home() / ".pocketclaw" / "mem0_data")

    # --- LLM config ---
    llm_config: dict[str, Any] = {"provider": llm_provider, "config": {}}
    if llm_provider == "ollama":
        llm_config["config"] = {
            "model": llm_model,
            "temperature": 0,
            "max_tokens": 2000,
            "ollama_base_url": ollama_base_url,
        }
    elif llm_provider == "anthropic":
        cfg = {"model": llm_model, "temperature": 0, "max_tokens": 2000}
        if anthropic_api_key:
            cfg["api_key"] = anthropic_api_key
        llm_config["config"] = cfg
    elif llm_provider == "openai":
        cfg = {"model": llm_model, "temperature": 0, "max_tokens": 2000}
        if openai_api_key:
            cfg["api_key"] = openai_api_key
        llm_config["config"] = cfg
    else:
        llm_config["config"] = {"model": llm_model, "temperature": 0, "max_tokens": 2000}

    # --- Embedder config ---
    embedding_dims = _EMBEDDING_DIMS.get(embedder_model)
    if embedding_dims is None and embedder_provider == "ollama":
        embedding_dims = _get_ollama_embedding_dims(embedder_model, ollama_base_url)
    if embedding_dims is None:
        embedding_dims = 1536  # OpenAI default fallback
    embedder_config: dict[str, Any] = {"provider": embedder_provider, "config": {}}
    if embedder_provider == "ollama":
        embedder_config["config"] = {
            "model": embedder_model,
            "ollama_base_url": ollama_base_url,
        }
    elif embedder_provider == "huggingface":
        embedder_config["config"] = {"model": embedder_model}
    else:
        # openai default
        cfg = {"model": embedder_model}
        if openai_api_key:
            cfg["api_key"] = openai_api_key
        embedder_config["config"] = cfg

    # --- Vector store config ---
    vs_config: dict[str, Any] = {"provider": vector_store, "config": {}}
    if vector_store == "qdrant":
        vs_config["config"] = {
            "collection_name": "pocketpaw_memory",
            "path": str(data_path / "qdrant"),
            "embedding_model_dims": embedding_dims,
        }
    elif vector_store == "chroma":
        vs_config["config"] = {
            "collection_name": "pocketpaw_memory",
            "path": str(data_path / "chroma"),
        }

    return {
        "llm": llm_config,
        "embedder": embedder_config,
        "vector_store": vs_config,
        "version": "v1.1",
    }


class Mem0MemoryStore:
    """
    Mem0-based memory store implementing MemoryStoreProtocol.

    Uses Mem0 for semantic memory with:
    - Vector search for similarity-based retrieval
    - LLM-powered fact extraction (configurable provider)
    - Memory consolidation and evolution

    Mapping to Mem0 concepts:
    - LONG_TERM memories -> user_id scoped (persistent facts)
    - DAILY memories -> user_id scoped with date metadata
    - SESSION memories -> run_id scoped (conversation history)
    """

    def __init__(
        self,
        user_id: str = "default",
        agent_id: str = "pocketpaw",
        data_path: Path | None = None,
        use_inference: bool = True,
        llm_provider: str = "anthropic",
        llm_model: str = "claude-haiku-4-5-20251001",
        embedder_provider: str = "openai",
        embedder_model: str = "text-embedding-3-small",
        vector_store: str = "qdrant",
        ollama_base_url: str = "http://localhost:11434",
        anthropic_api_key: str | None = None,
        openai_api_key: str | None = None,
    ):
        self.user_id = user_id
        self.agent_id = agent_id
        self.use_inference = use_inference
        self._data_path = data_path or (Path.home() / ".pocketclaw" / "mem0_data")
        self._data_path.mkdir(parents=True, exist_ok=True)

        # Provider configuration
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._embedder_provider = embedder_provider
        self._embedder_model = embedder_model
        self._vector_store = vector_store
        self._ollama_base_url = ollama_base_url
        self._anthropic_api_key = anthropic_api_key
        self._openai_api_key = openai_api_key

        # Lazy initialization
        self._memory = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazily initialize Mem0 client using Memory.from_config()."""
        if self._initialized:
            return

        try:
            from mem0 import Memory

            config = _build_mem0_config(
                llm_provider=self._llm_provider,
                llm_model=self._llm_model,
                embedder_provider=self._embedder_provider,
                embedder_model=self._embedder_model,
                vector_store=self._vector_store,
                ollama_base_url=self._ollama_base_url,
                data_path=self._data_path,
                anthropic_api_key=self._anthropic_api_key,
                openai_api_key=self._openai_api_key,
            )

            self._memory = Memory.from_config(config)
            self._initialized = True
            logger.info(
                "Mem0 initialized (llm=%s/%s, embedder=%s/%s, store=%s) at %s",
                self._llm_provider,
                self._llm_model,
                self._embedder_provider,
                self._embedder_model,
                self._vector_store,
                self._data_path,
            )

        except ImportError:
            raise ImportError(
                "mem0ai package not installed. Install with: pip install pocketpaw[memory]"
            )
        except Exception as e:
            logger.error(f"Failed to initialize Mem0: {e}")
            raise

    async def _run_sync(self, func, *args, **kwargs):
        """Run a synchronous function in the executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    # =========================================================================
    # MemoryStoreProtocol Implementation
    # =========================================================================

    async def save(self, entry: MemoryEntry) -> str:
        """Save a memory entry using Mem0."""
        self._ensure_initialized()

        # Build metadata
        metadata = {
            "pocketpaw_type": entry.type.value,
            "tags": entry.tags,
            "created_at": entry.created_at.isoformat(),
            **entry.metadata,
        }

        # Determine scoping based on memory type
        if entry.type == MemoryType.SESSION:
            # Session memories use run_id for conversation isolation
            result = await self._run_sync(
                self._memory.add,
                entry.content,
                run_id=entry.session_key or "default_session",
                metadata=metadata,
                infer=False,  # Don't extract facts from conversation - store raw
            )
        elif entry.type == MemoryType.DAILY:
            # Daily notes scoped to user with date
            metadata["date"] = datetime.now(tz=UTC).date().isoformat()
            result = await self._run_sync(
                self._memory.add,
                entry.content,
                user_id=self.user_id,
                metadata=metadata,
                infer=self.use_inference,
            )
        else:
            # Long-term memories - use full inference for fact extraction
            # Use per-sender user_id if set in metadata, else default
            uid = entry.metadata.get("user_id") or self.user_id
            result = await self._run_sync(
                self._memory.add,
                entry.content,
                user_id=uid,
                metadata=metadata,
                infer=self.use_inference,
            )

        # Extract memory ID from result
        if result and "results" in result and result["results"]:
            entry.id = result["results"][0].get("id", entry.id)

        logger.debug(f"Saved memory: {entry.id} ({entry.type.value})")
        return entry.id or ""

    async def get(self, entry_id: str) -> MemoryEntry | None:
        """Get a memory entry by ID."""
        self._ensure_initialized()

        try:
            result = await self._run_sync(self._memory.get, entry_id)
            if result:
                return self._mem0_to_entry(result)
        except Exception as e:
            logger.warning(f"Failed to get memory {entry_id}: {e}")

        return None

    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry."""
        self._ensure_initialized()

        try:
            await self._run_sync(self._memory.delete, entry_id)
            return True
        except Exception as e:
            logger.warning(f"Failed to delete memory {entry_id}: {e}")
            return False

    async def search(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Search memories using semantic search."""
        self._ensure_initialized()

        if not query:
            # Without a query, fall back to get_all with filters
            return await self._get_filtered(memory_type, tags, limit)

        # Build filters
        filters = {}
        if memory_type:
            filters["pocketpaw_type"] = memory_type.value

        try:
            result = await self._run_sync(
                self._memory.search,
                query,
                user_id=self.user_id,
                limit=limit,
                filters=filters if filters else None,
            )

            entries = []
            for item in result.get("results", []):
                entry = self._mem0_to_entry(item)
                # Filter by tags if specified
                if tags and not any(t in entry.tags for t in tags):
                    continue
                entries.append(entry)

            return entries[:limit]

        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []

    async def _get_filtered(
        self,
        memory_type: MemoryType | None,
        tags: list[str] | None,
        limit: int,
    ) -> list[MemoryEntry]:
        """Get memories with filters (no semantic search)."""
        filters = {}
        if memory_type:
            filters["pocketpaw_type"] = memory_type.value

        try:
            result = await self._run_sync(
                self._memory.get_all,
                user_id=self.user_id,
                limit=limit * 2,  # Get extra to filter
                filters=filters if filters else None,
            )

            entries = []
            for item in result.get("results", []):
                entry = self._mem0_to_entry(item)
                if tags and not any(t in entry.tags for t in tags):
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break

            return entries

        except Exception as e:
            logger.error(f"Get filtered failed: {e}")
            return []

    async def get_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100,
        **kwargs,
    ) -> list[MemoryEntry]:
        """Get all memories of a specific type.

        Accepts optional user_id kwarg for scoped retrieval.
        """
        user_id = kwargs.get("user_id")
        if user_id and user_id != "default":
            # Override user_id for scoped retrieval
            old_uid = self.user_id
            self.user_id = user_id
            try:
                return await self._get_filtered(memory_type, None, limit)
            finally:
                self.user_id = old_uid
        return await self._get_filtered(memory_type, None, limit)

    async def get_session(self, session_key: str) -> list[MemoryEntry]:
        """Get session history for a specific session."""
        self._ensure_initialized()

        try:
            result = await self._run_sync(
                self._memory.get_all,
                run_id=session_key,
                limit=1000,
            )

            entries = []
            for item in result.get("results", []):
                entry = self._mem0_to_entry(item)
                entry.session_key = session_key
                entries.append(entry)

            # Sort by creation time
            entries.sort(key=lambda e: e.created_at)
            return entries

        except Exception as e:
            logger.error(f"Get session failed: {e}")
            return []

    async def clear_session(self, session_key: str) -> int:
        """Clear session history."""
        self._ensure_initialized()

        try:
            # Get all session memories first to count
            result = await self._run_sync(
                self._memory.get_all,
                run_id=session_key,
                limit=1000,
            )
            count = len(result.get("results", []))

            # Delete all
            await self._run_sync(
                self._memory.delete_all,
                run_id=session_key,
            )

            return count

        except Exception as e:
            logger.error(f"Clear session failed: {e}")
            return 0

    # =========================================================================
    # Auto-Learn: Extract facts from conversations
    # =========================================================================

    async def auto_learn(self, messages: list[dict[str, str]], user_id: str | None = None) -> dict:
        """Feed a conversation to mem0 to extract and evolve long-term facts.

        This is the core auto-learning feature. After each conversation turn,
        the conversation is fed to mem0 with infer=True to:
        1. Extract candidate facts via LLM
        2. Compare against existing memories via vector similarity
        3. ADD new facts, UPDATE existing ones, or DELETE outdated ones

        Args:
            messages: List of {"role": "...", "content": "..."} dicts.
            user_id: User ID for scoping. Defaults to self.user_id.

        Returns:
            Mem0 add result dict with extracted/updated memory IDs.
        """
        self._ensure_initialized()

        if not messages:
            return {"results": []}

        uid = user_id or self.user_id

        try:
            result = await self._run_sync(
                self._memory.add,
                messages,
                user_id=uid,
                infer=True,
            )
            added = len(result.get("results", []))
            logger.debug("Auto-learn extracted %d facts for user=%s", added, uid)
            return result

        except Exception as e:
            logger.warning("Auto-learn failed: %s", e)
            return {"results": [], "error": str(e)}

    async def semantic_search(
        self, query: str, user_id: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Search memories semantically and return raw mem0 results.

        Useful for context injection — returns the raw mem0 result dicts
        (with scores) rather than converting to MemoryEntry.

        Args:
            query: Natural language search query.
            user_id: User ID scope. Defaults to self.user_id.
            limit: Max results.

        Returns:
            List of mem0 result dicts with 'memory', 'id', 'score' keys.
        """
        self._ensure_initialized()

        try:
            result = await self._run_sync(
                self._memory.search,
                query,
                user_id=user_id or self.user_id,
                limit=limit,
            )
            return result.get("results", [])
        except Exception as e:
            logger.warning("Semantic search failed: %s", e)
            return []

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _mem0_to_entry(self, mem0_item: dict) -> MemoryEntry:
        """Convert Mem0 memory item to MemoryEntry."""
        metadata = mem0_item.get("metadata", {})

        # Parse memory type
        type_str = metadata.get("pocketpaw_type", "long_term")
        try:
            mem_type = MemoryType(type_str)
        except ValueError:
            mem_type = MemoryType.LONG_TERM

        # Parse timestamps
        created_str = metadata.get("created_at")
        try:
            created_at = (
                datetime.fromisoformat(created_str) if created_str else datetime.now(tz=UTC)
            )
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            created_at = datetime.now(tz=UTC)

        # Extract role for session memories
        role = metadata.get("role")

        return MemoryEntry(
            id=mem0_item.get("id", ""),
            type=mem_type,
            content=mem0_item.get("memory", ""),
            created_at=created_at,
            updated_at=datetime.now(tz=UTC),
            tags=metadata.get("tags", []),
            metadata={
                k: v
                for k, v in metadata.items()
                if k not in ("pocketpaw_type", "tags", "created_at", "role")
            },
            role=role,
            session_key=metadata.get("session_key"),
        )

    # =========================================================================
    # Stats
    # =========================================================================

    async def get_memory_stats(self) -> dict[str, Any]:
        """Get statistics about stored memories."""
        self._ensure_initialized()

        try:
            all_memories = await self._run_sync(
                self._memory.get_all,
                user_id=self.user_id,
                limit=10000,
            )

            results = (all_memories or {}).get("results", [])

            # Count by type
            type_counts: dict[str, int] = {}
            for item in results:
                mem_type = item.get("metadata", {}).get("pocketpaw_type", "unknown")
                type_counts[mem_type] = type_counts.get(mem_type, 0) + 1

            return {
                "total_memories": len(results),
                "by_type": type_counts,
                "user_id": self.user_id,
                "backend": "mem0",
                "llm_provider": self._llm_provider,
                "llm_model": self._llm_model,
                "embedder_provider": self._embedder_provider,
                "embedder_model": self._embedder_model,
                "vector_store": self._vector_store,
                "data_path": str(self._data_path),
            }

        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {"error": str(e)}
