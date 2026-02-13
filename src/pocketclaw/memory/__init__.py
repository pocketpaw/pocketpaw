# Memory System
# Created: 2026-02-02
# Updated: 2026-02-04 - Added Mem0 backend support
# Provides session persistence, long-term memory, and daily notes.

from pocketclaw.memory.protocol import MemoryType, MemoryEntry, MemoryStoreProtocol
from pocketclaw.memory.file_store import FileMemoryStore
from pocketclaw.memory.manager import MemoryManager, get_memory_manager, create_memory_store

# Mem0 store is optional - requires mem0ai package
try:
    from pocketclaw.memory.mem0_store import Mem0MemoryStore

    _HAS_MEM0 = True
except ImportError:
    Mem0MemoryStore = None  # type: ignore
    _HAS_MEM0 = False

__all__ = [
    "MemoryType",
    "MemoryEntry",
    "MemoryStoreProtocol",
    "FileMemoryStore",
    "Mem0MemoryStore",
    "MemoryManager",
    "get_memory_manager",
    "create_memory_store",
]
