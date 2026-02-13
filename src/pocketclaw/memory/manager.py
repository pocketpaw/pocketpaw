# Memory manager - high-level interface for memory operations.
# Created: 2026-02-02
# Updated: 2026-02-04 - Added Mem0 backend support
# Updated: 2026-02-07 - Configurable providers, auto-learn, semantic context - Memory System
# Updated: 2026-02-11 - Sender-scoped memory isolation

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pocketclaw.memory.file_store import FileMemoryStore
from pocketclaw.memory.protocol import MemoryEntry, MemoryStoreProtocol, MemoryType

logger = logging.getLogger(__name__)


def create_memory_store(
    backend: str = "file",
    base_path: Path | None = None,
    user_id: str = "default",
    use_inference: bool = True,
    llm_provider: str = "anthropic",
    llm_model: str = "claude-haiku-4-5-20251001",
    embedder_provider: str = "openai",
    embedder_model: str = "text-embedding-3-small",
    vector_store: str = "qdrant",
    ollama_base_url: str = "http://localhost:11434",
    anthropic_api_key: str | None = None,
    openai_api_key: str | None = None,
) -> MemoryStoreProtocol:
    """
    Factory function to create the appropriate memory store.

    Args:
        backend: Backend type - 'file' or 'mem0'
        base_path: Base path for storage
        user_id: User ID for mem0 scoping
        use_inference: Whether to use LLM inference (mem0 only)
        llm_provider: LLM provider for mem0 ('anthropic', 'openai', 'ollama')
        llm_model: LLM model name for mem0
        embedder_provider: Embedder provider ('openai', 'ollama', 'huggingface')
        embedder_model: Embedding model name
        vector_store: Vector store ('qdrant' or 'chroma')
        ollama_base_url: Ollama base URL (when using ollama)

    Returns:
        MemoryStoreProtocol implementation
    """
    if backend == "mem0":
        try:
            # Check if mem0 is actually available before creating store
            import importlib.util

            if importlib.util.find_spec("mem0") is None:
                raise ImportError("mem0ai not installed")

            from pocketclaw.memory.mem0_store import Mem0MemoryStore

            logger.info("Using Mem0 memory backend (semantic search enabled)")
            return Mem0MemoryStore(
                user_id=user_id,
                data_path=base_path,
                use_inference=use_inference,
                llm_provider=llm_provider,
                llm_model=llm_model,
                embedder_provider=embedder_provider,
                embedder_model=embedder_model,
                vector_store=vector_store,
                ollama_base_url=ollama_base_url,
                anthropic_api_key=anthropic_api_key,
                openai_api_key=openai_api_key,
            )
        except ImportError:
            logger.warning(
                "mem0ai not installed, falling back to file backend. "
                "Install with: pip install pocketpaw[memory]"
            )
            return FileMemoryStore(base_path)
    else:
        logger.info("Using file-based memory backend")
        return FileMemoryStore(base_path)


class MemoryManager:
    """
    High-level memory management facade.

    Provides convenient methods for common memory operations
    while delegating to the underlying store.

    Usage:
        memory = MemoryManager()

        # Remember something long-term
        await memory.remember("User prefers dark mode", tags=["preferences", "ui"])

        # Add daily note
        await memory.note("Had meeting about project X")

        # Get context for agent
        context = await memory.get_context_for_agent()
    """

    def __init__(
        self,
        store: MemoryStoreProtocol | None = None,
        base_path: Path | None = None,
        backend: str = "file",
        user_id: str = "default",
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
        """
        Initialize memory manager.

        Args:
            store: Custom store implementation. If None, creates based on backend.
            base_path: Base path for storage.
            backend: Backend type - 'file' or 'mem0'.
            user_id: User ID for mem0 scoping.
            use_inference: Whether to use LLM inference (mem0 only).
            llm_provider: LLM provider for mem0.
            llm_model: LLM model for mem0.
            embedder_provider: Embedder provider for mem0.
            embedder_model: Embedding model for mem0.
            vector_store: Vector store for mem0.
            ollama_base_url: Ollama base URL for mem0.
        """
        if store:
            self._store = store
        else:
            self._store = create_memory_store(
                backend=backend,
                base_path=base_path,
                user_id=user_id,
                use_inference=use_inference,
                llm_provider=llm_provider,
                llm_model=llm_model,
                embedder_provider=embedder_provider,
                embedder_model=embedder_model,
                vector_store=vector_store,
                ollama_base_url=ollama_base_url,
                anthropic_api_key=anthropic_api_key,
                openai_api_key=openai_api_key,
            )

    # =========================================================================
    # User Scoping
    # =========================================================================

    def _resolve_user_id(self, sender_id: str | None = None) -> str:
        """Map sender_id to a memory-scoped user_id.

        - No sender_id or no owner_id configured → "default" (backward compat)
        - sender_id == owner_id → "default" (owner's memory space)
        - Otherwise → sha256(sender_id)[:16] (safe for filesystem paths)
        """
        if not sender_id:
            return "default"

        from pocketclaw.config import get_settings

        settings = get_settings()
        if not settings.owner_id:
            return "default"
        if sender_id == settings.owner_id:
            return "default"

        return hashlib.sha256(sender_id.encode()).hexdigest()[:16]

    # =========================================================================
    # High-Level Operations
    # =========================================================================

    async def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        header: str | None = None,
        sender_id: str | None = None,
    ) -> str:
        """
        Store a long-term memory.

        Args:
            content: The content to remember.
            tags: Optional tags for categorization.
            header: Optional header/title for the memory.

        Returns:
            The memory entry ID.
        """
        user_id = self._resolve_user_id(sender_id)
        entry = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content=content,
            tags=tags or [],
            metadata={"header": header or "Memory", "user_id": user_id},
        )
        return await self._store.save(entry)

    async def note(
        self,
        content: str,
        tags: list[str] | None = None,
    ) -> str:
        """
        Add a daily note.

        Args:
            content: The note content.
            tags: Optional tags.

        Returns:
            The note entry ID.
        """
        entry = MemoryEntry(
            id="",
            type=MemoryType.DAILY,
            content=content,
            tags=tags or [],
            metadata={"header": datetime.now(tz=UTC).strftime("%H:%M")},
        )
        return await self._store.save(entry)

    async def add_to_session(
        self,
        session_key: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Add a message to session history.

        Args:
            session_key: The session identifier.
            role: Message role (user, assistant, system).
            content: Message content.
            metadata: Optional metadata.

        Returns:
            The entry ID.
        """
        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content=content,
            role=role,
            session_key=session_key,
            metadata=metadata or {},
        )
        return await self._store.save(entry)

    async def get_session_history(
        self,
        session_key: str,
        limit: int = 50,
    ) -> list[dict[str, str]]:
        """
        Get session history in LLM message format.

        Returns:
            List of {"role": "...", "content": "..."} dicts.
        """
        entries = await self._store.get_session(session_key)
        return [{"role": e.role or "user", "content": e.content} for e in entries[-limit:]]

    async def search(
        self,
        query: str,
        limit: int = 5,
    ) -> list[MemoryEntry]:
        """Search all memories."""
        return await self._store.search(query=query, limit=limit)

    async def get_context_for_agent(
        self,
        max_chars: int = 8000,
        long_term_limit: int = 50,
        daily_limit: int = 20,
        entry_max_chars: int = 500,
        sender_id: str | None = None,
    ) -> str:
        """
        Get memory context for injection into agent system prompt.

        Returns a formatted string with relevant memories.
        """
        parts = []
        user_id = self._resolve_user_id(sender_id)

        # Long-term memories (scoped to user)
        long_term = await self._store.get_by_type(
            MemoryType.LONG_TERM, limit=long_term_limit, user_id=user_id
        )
        if long_term:
            parts.append("## Long-term Memory\n")
            for entry in long_term:
                parts.append(f"- {entry.content[:entry_max_chars]}")

        # Today's notes
        daily = await self._store.get_by_type(MemoryType.DAILY, limit=daily_limit)
        if daily:
            parts.append("\n## Today's Notes\n")
            for entry in daily:
                parts.append(f"- {entry.content[:entry_max_chars]}")

        context = "\n".join(parts)

        # Truncate if too long
        if len(context) > max_chars:
            context = context[:max_chars] + "\n...(truncated)"

        return context

    async def get_compacted_history(
        self,
        session_key: str,
        recent_window: int = 10,
        char_budget: int = 8000,
        summary_chars: int = 150,
        llm_summarize: bool = False,
    ) -> list[dict[str, str]]:
        """Get session history with compaction.

        Keeps the last `recent_window` messages verbatim and collapses
        older messages into condensed one-liner extracts (Tier 1) or an
        LLM-generated summary (Tier 2, opt-in).

        Args:
            session_key: The session identifier.
            recent_window: Number of recent messages to keep verbatim.
            char_budget: Max total characters for the returned history.
            summary_chars: Max chars per older message extract (Tier 1).
            llm_summarize: Use LLM to summarize older messages (Tier 2).

        Returns:
            List of {"role": "...", "content": "..."} dicts.
        """
        entries = await self._store.get_session(session_key)
        if not entries:
            return []

        all_messages = [{"role": e.role or "user", "content": e.content} for e in entries]

        # Split into older and recent
        split_point = max(0, len(all_messages) - recent_window)
        older = all_messages[:split_point]
        recent = all_messages[split_point:]

        if not older:
            return self._enforce_budget(recent, char_budget)

        # Tier 2: Try LLM summary if enabled
        summary_block: str | None = None
        if llm_summarize:
            summary_block = await self._get_or_create_llm_summary(
                session_key, older, len(all_messages)
            )

        # Tier 1 fallback: one-liner extracts
        if summary_block is None:
            lines = []
            for msg in older:
                role = msg["role"].capitalize()
                text = msg["content"].replace("\n", " ").strip()
                if len(text) > summary_chars:
                    # Truncate at word boundary
                    truncated = text[:summary_chars].rsplit(" ", 1)[0]
                    text = truncated + "..."
                lines.append(f"{role}: {text}")
            summary_block = "\n".join(lines)

        compacted = [{"role": "user", "content": f"[Earlier conversation]\n{summary_block}"}]
        compacted.extend(recent)

        return self._enforce_budget(compacted, char_budget)

    @staticmethod
    def _enforce_budget(messages: list[dict[str, str]], char_budget: int) -> list[dict[str, str]]:
        """Drop oldest messages until total chars fit within budget.

        If a single message exceeds the budget, truncate it.
        """
        total = sum(len(m["content"]) for m in messages)
        if total <= char_budget:
            return messages

        # Drop from oldest until within budget
        result = list(messages)
        while len(result) > 1 and sum(len(m["content"]) for m in result) > char_budget:
            result.pop(0)

        # If single remaining message still exceeds budget, truncate it
        if result and len(result[0]["content"]) > char_budget:
            result[0] = {
                "role": result[0]["role"],
                "content": result[0]["content"][:char_budget],
            }

        return result

    async def _get_or_create_llm_summary(
        self,
        session_key: str,
        older_entries: list[dict[str, str]],
        current_total: int,
    ) -> str | None:
        """Get cached or create new LLM summary of older messages.

        Returns None on any error (caller falls back to Tier 1).
        """
        try:
            # Need sessions_path from the store (FileMemoryStore has it, Mem0 may not)
            if not hasattr(self._store, "sessions_path"):
                return None

            safe_key = session_key.replace(":", "_").replace("/", "_")
            cache_path: Path = self._store.sessions_path / f"{safe_key}_compaction.json"

            # Check cache
            if cache_path.exists():
                import json

                cache = json.loads(cache_path.read_text())
                if cache.get("watermark") == current_total:
                    return cache["summary"]

            # Build input for LLM (cap at 4000 chars)
            lines = []
            for msg in older_entries:
                lines.append(f"{msg['role'].capitalize()}: {msg['content']}")
            input_text = "\n".join(lines)
            if len(input_text) > 4000:
                input_text = input_text[:4000]

            # Call Haiku via AsyncAnthropic
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic()
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Summarize the following conversation in 2-3 sentences. "
                            "Focus on key topics discussed, decisions made, and any "
                            "important context.\n\n"
                            f"{input_text}"
                        ),
                    }
                ],
            )
            summary = response.content[0].text

            # Write cache
            import json

            cache_path.write_text(
                json.dumps(
                    {
                        "watermark": current_total,
                        "summary": summary,
                        "older_count": len(older_entries),
                    },
                    indent=2,
                )
            )

            return summary

        except Exception:
            logger.debug("LLM summary failed, falling back to Tier 1", exc_info=True)
            return None

    async def auto_learn(
        self,
        messages: list[dict[str, str]],
        user_id: str | None = None,
        file_auto_learn: bool = False,
        sender_id: str | None = None,
    ) -> dict:
        """Extract and evolve long-term facts from a conversation.

        Works with mem0 backend natively. For file backend, uses LLM-based
        extraction when file_auto_learn=True.

        Args:
            messages: Recent conversation messages [{"role": "...", "content": "..."}].
            user_id: User ID for scoping (deprecated, use sender_id).
            file_auto_learn: Enable LLM extraction for file backend.
            sender_id: Sender ID for memory scoping.

        Returns:
            Result dict (or empty dict if nothing extracted).
        """
        resolved = user_id or self._resolve_user_id(sender_id)
        if hasattr(self._store, "auto_learn"):
            return await self._store.auto_learn(messages, user_id=resolved)

        # File backend: use LLM-based fact extraction
        if file_auto_learn:
            return await self._file_auto_learn(messages, sender_id=sender_id)

        return {}

    async def _file_auto_learn(
        self, messages: list[dict[str, str]], sender_id: str | None = None
    ) -> dict:
        """Extract facts from conversation using Haiku and save to file backend."""
        try:
            from anthropic import AsyncAnthropic

            convo = "\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in messages)
            if len(convo) > 4000:
                convo = convo[:4000]

            client = AsyncAnthropic()
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Extract factual information about the user from this "
                            "conversation. Return a JSON array of short fact strings. "
                            "Only include concrete facts (name, preferences, projects, "
                            "personal info). Return [] if no new facts.\n\n"
                            f"{convo}"
                        ),
                    }
                ],
            )

            import json

            text = response.content[0].text.strip()
            # Handle markdown code blocks
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            facts = json.loads(text)

            if not isinstance(facts, list):
                return {}

            saved = 0
            for fact in facts:
                if isinstance(fact, str) and fact.strip():
                    await self.remember(fact.strip(), tags=["auto-learned"], sender_id=sender_id)
                    saved += 1

            return {"results": [{"fact": f} for f in facts[:saved]]}

        except Exception:
            logger.debug("File auto-learn failed", exc_info=True)
            return {}

    async def get_semantic_context(
        self, query: str, limit: int = 5, sender_id: str | None = None
    ) -> str:
        """Get semantically relevant memory context for a user query.

        Uses mem0 semantic search to find the most relevant memories
        for the current conversation. Falls back to get_context_for_agent()
        for file backend or on any error.

        Args:
            query: The user's current message/query.
            limit: Max memories to include.
            sender_id: Sender ID for memory scoping.

        Returns:
            Formatted context string for system prompt injection.
        """
        user_id = self._resolve_user_id(sender_id)
        if hasattr(self._store, "semantic_search"):
            try:
                results = await self._store.semantic_search(query, user_id=user_id, limit=limit)
                if results:
                    parts = ["## Relevant Memories\n"]
                    for item in results:
                        memory_text = item.get("memory", "")
                        if memory_text:
                            parts.append(f"- {memory_text}")
                    return "\n".join(parts)
            except Exception:
                logger.debug(
                    "Semantic search failed, falling back to standard context",
                    exc_info=True,
                )

        # Fall back to standard context
        return await self.get_context_for_agent(sender_id=sender_id)

    async def clear_session(self, session_key: str) -> int:
        """Clear session history."""
        return await self._store.clear_session(session_key)

    async def delete_session(self, session_key: str) -> bool:
        """Delete a session entirely (file, compaction cache, index entry)."""
        if hasattr(self._store, "delete_session"):
            return await self._store.delete_session(session_key)
        # Fallback: clear is the best we can do
        count = await self._store.clear_session(session_key)
        return count > 0

    async def update_session_title(self, session_key: str, title: str) -> bool:
        """Update a session's title in the index."""
        if hasattr(self._store, "update_session_title"):
            return await self._store.update_session_title(session_key, title)
        return False

    # =========================================================================
    # Session Aliases (pass-through for file store)
    # =========================================================================

    async def resolve_session_key(self, session_key: str) -> str:
        """Resolve a session key through the alias table.

        Returns the aliased target if one exists, otherwise the original key.
        Only works with stores that support aliases (FileMemoryStore).
        """
        if hasattr(self._store, "resolve_session_alias"):
            return await self._store.resolve_session_alias(session_key)
        return session_key

    async def set_session_alias(self, source_key: str, target_key: str) -> None:
        """Set a session alias (source_key -> target_key)."""
        if hasattr(self._store, "set_session_alias"):
            await self._store.set_session_alias(source_key, target_key)

    async def list_sessions_for_chat(self, session_key: str) -> list[dict]:
        """List all sessions associated with a chat, sorted by last_activity desc.

        Returns list of dicts with keys: session_key, title, last_activity,
        message_count, preview, is_active.
        """
        if not hasattr(self._store, "get_session_keys_for_chat"):
            return []

        keys = await self._store.get_session_keys_for_chat(session_key)

        # Load session index metadata
        if not hasattr(self._store, "_load_session_index"):
            return []

        index = self._store._load_session_index()

        # Find which key is currently active (aliased target)
        active_key = session_key
        if hasattr(self._store, "resolve_session_alias"):
            active_key = await self._store.resolve_session_alias(session_key)

        sessions = []
        for key in keys:
            safe_key = key.replace(":", "_").replace("/", "_")
            meta = index.get(safe_key, {})
            sessions.append(
                {
                    "session_key": key,
                    "title": meta.get("title", "New Chat"),
                    "last_activity": meta.get("last_activity", ""),
                    "message_count": meta.get("message_count", 0),
                    "preview": meta.get("preview", ""),
                    "is_active": key == active_key,
                }
            )

        # Sort by last_activity descending (most recent first)
        sessions.sort(key=lambda s: s["last_activity"], reverse=True)
        return sessions


# Singleton
_manager: MemoryManager | None = None


def get_memory_manager(force_reload: bool = False) -> MemoryManager:
    """
    Get the global memory manager instance.

    Uses configuration from Settings to determine backend.

    Args:
        force_reload: Force recreation of the manager.

    Returns:
        MemoryManager instance
    """
    global _manager

    if _manager is None or force_reload:
        from pocketclaw.config import get_settings

        settings = get_settings()
        _manager = MemoryManager(
            backend=settings.memory_backend,
            use_inference=settings.memory_use_inference,
            llm_provider=settings.mem0_llm_provider,
            llm_model=settings.mem0_llm_model,
            embedder_provider=settings.mem0_embedder_provider,
            embedder_model=settings.mem0_embedder_model,
            vector_store=settings.mem0_vector_store,
            ollama_base_url=settings.mem0_ollama_base_url,
            anthropic_api_key=settings.anthropic_api_key,
            openai_api_key=settings.openai_api_key,
        )

        from pocketclaw.lifecycle import register

        def _reset():
            global _manager
            _manager = None

        register("memory_manager", reset=_reset)

    return _manager
