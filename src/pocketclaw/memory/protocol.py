# Memory storage protocol - defines the interface for swappable backends.
# Created: 2026-02-02 - Memory System

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, Any


class MemoryType(str, Enum):
    """Types of memory entries."""

    LONG_TERM = "long_term"  # Facts, preferences, important info
    DAILY = "daily"  # Daily notes and events
    SESSION = "session"  # Conversation history


@dataclass
class MemoryEntry:
    """A single memory entry."""

    id: str
    type: MemoryType
    content: str
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # For session memories
    role: str | None = None  # "user", "assistant", "system"
    session_key: str | None = None


class MemoryStoreProtocol(Protocol):
    """Protocol for memory storage backends.

    Implement this to create custom storage backends (SQLite, Redis, etc.)
    """

    async def save(self, entry: MemoryEntry) -> str:
        """Save a memory entry, return its ID."""
        ...

    async def get(self, entry_id: str) -> MemoryEntry | None:
        """Get a memory entry by ID."""
        ...

    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry."""
        ...

    async def search(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Search memories by query, type, or tags."""
        ...

    async def get_by_type(
        self,
        memory_type: MemoryType,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        """Get all memories of a specific type."""
        ...

    async def get_session(self, session_key: str) -> list[MemoryEntry]:
        """Get session history for a specific session."""
        ...

    async def clear_session(self, session_key: str) -> int:
        """Clear session history, return count deleted."""
        ...
