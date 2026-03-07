# Memory System
# Created: 2026-02-02
# Updated: 2026-02-04 - Added Mem0 backend support
# Provides session persistence, long-term memory, and daily notes.

from pocketpaw.memory.file_store import FileMemoryStore
from pocketpaw.memory.manager import MemoryManager, create_memory_store, get_memory_manager
from pocketpaw.memory.protocol import MemoryEntry, MemoryStoreProtocol, MemoryType

# Mem0 store is optional - requires mem0ai package
try:
    from pocketpaw.memory.mem0_store import Mem0MemoryStore

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
