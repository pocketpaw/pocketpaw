# Memory manager - high-level interface for memory operations.
# Created: 2026-02-02
# Updated: 2026-02-04 - Added Mem0 backend support
# Part of Nanobot Pattern Adoption - Memory System

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketclaw.memory.protocol import MemoryStoreProtocol, MemoryEntry, MemoryType
from pocketclaw.memory.file_store import FileMemoryStore

logger = logging.getLogger(__name__)


def create_memory_store(
    backend: str = "file",
    base_path: Path | None = None,
    user_id: str = "default",
    use_inference: bool = True,
) -> MemoryStoreProtocol:
    """
    Factory function to create the appropriate memory store.

    Args:
        backend: Backend type - 'file' or 'mem0'
        base_path: Base path for storage
        user_id: User ID for mem0 scoping
        use_inference: Whether to use LLM inference (mem0 only)

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
            )
        except ImportError:
            logger.warning(
                "mem0ai not installed, falling back to file backend. Install with: pip install mem0ai"
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
    ):
        """
        Initialize memory manager.

        Args:
            store: Custom store implementation. If None, creates based on backend.
            base_path: Base path for storage.
            backend: Backend type - 'file' or 'mem0'.
            user_id: User ID for mem0 scoping.
            use_inference: Whether to use LLM inference (mem0 only).
        """
        if store:
            self._store = store
        else:
            self._store = create_memory_store(
                backend=backend,
                base_path=base_path,
                user_id=user_id,
                use_inference=use_inference,
            )

    # =========================================================================
    # High-Level Operations
    # =========================================================================

    async def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        header: str | None = None,
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
        entry = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content=content,
            tags=tags or [],
            metadata={"header": header or "Memory"},
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
            metadata={"header": datetime.now().strftime("%H:%M")},
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

    async def get_context_for_agent(self, max_chars: int = 4000) -> str:
        """
        Get memory context for injection into agent system prompt.

        Returns a formatted string with relevant memories.
        """
        parts = []

        # Long-term memories
        long_term = await self._store.get_by_type(MemoryType.LONG_TERM, limit=10)
        if long_term:
            parts.append("## Long-term Memory\n")
            for entry in long_term:
                parts.append(f"- {entry.content[:200]}")

        # Today's notes
        daily = await self._store.get_by_type(MemoryType.DAILY, limit=5)
        if daily:
            parts.append("\n## Today's Notes\n")
            for entry in daily:
                parts.append(f"- {entry.content[:200]}")

        context = "\n".join(parts)

        # Truncate if too long
        if len(context) > max_chars:
            context = context[:max_chars] + "\n...(truncated)"

        return context

    async def clear_session(self, session_key: str) -> int:
        """Clear session history."""
        return await self._store.clear_session(session_key)

    async def list_sessions(self) -> list[str]:
        """List active sessions (if supported by backend)."""
        if hasattr(self._store, "list_sessions"):
            return await self._store.list_sessions()
        return []


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
        )

    return _manager
