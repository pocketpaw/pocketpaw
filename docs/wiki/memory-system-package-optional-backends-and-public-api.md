---
{
  "title": "Memory System Package — Optional Backends and Public API",
  "summary": "The `pocketpaw.memory` package exposes PocketPaw's memory system, providing two storage backends unified behind a common protocol: `FileMemoryStore` (always available) and `Mem0MemoryStore` (optional, requires `mem0ai`). It supports session history, long-term facts, and daily notes.",
  "concepts": [
    "memory system",
    "MemoryType",
    "MemoryEntry",
    "MemoryStoreProtocol",
    "FileMemoryStore",
    "Mem0MemoryStore",
    "optional import",
    "MemoryManager",
    "session memory",
    "long-term memory"
  ],
  "categories": [
    "Memory System",
    "Package Architecture"
  ],
  "source_docs": [
    "0544210d34a4365f"
  ],
  "backlinks": null,
  "word_count": 462,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw agents need memory to maintain context across conversation turns and across sessions. The memory package provides three types of storage: session memory (conversation history), long-term memory (facts, preferences, and important information), and daily notes (dated journal entries).

The package was created on 2026-02-02 and gained Mem0 backend support on 2026-02-04.

## Backend Selection

```python
try:
    from pocketpaw.memory.mem0_store import Mem0MemoryStore
    _HAS_MEM0 = True
except ImportError:
    Mem0MemoryStore = None
    _HAS_MEM0 = False
```

The optional import pattern prevents a hard dependency on `mem0ai`. Users who do not install that package get the file-based backend automatically. Exporting `Mem0MemoryStore = None` rather than excluding it from `__all__` means callers can check `if Mem0MemoryStore is not None` without catching `ImportError` themselves.

## Exported Symbols

| Symbol | Role |
|--------|------|
| `MemoryType` | Enum: `LONG_TERM`, `DAILY`, `SESSION` |
| `MemoryEntry` | Dataclass for a single memory record |
| `MemoryStoreProtocol` | Protocol that both backends implement |
| `FileMemoryStore` | Markdown/SQLite file-based backend |
| `Mem0MemoryStore` | Mem0 semantic memory backend (optional) |
| `MemoryManager` | High-level facade over any backend |
| `get_memory_manager()` | Singleton factory |
| `create_memory_store()` | Factory for creating a store with config |

## Design Rationale

Hiding backend selection behind `create_memory_store()` and `get_memory_manager()` means application code never calls `FileMemoryStore()` or `Mem0MemoryStore()` directly. This keeps the backend swap transparent: changing the backend in config does not require changing call sites.

## Three Memory Types in Practice

**Session memory** is added every conversation turn — it is the raw message history keyed by session ID. It is short-lived and can be cleared between sessions. **Long-term memory** is added explicitly by the agent when it detects something worth remembering (a user preference, a project constraint, a name). **Daily notes** are timestamped entries that accumulate over time, useful for daily standups or reflecting on what happened today.

These three types intentionally mirror how humans store information: working memory (session), semantic memory (long-term facts), and episodic memory (daily events). Having the types as a first-class enum on `MemoryEntry` means queries can be scoped to exactly the type needed: `search(query, memory_type=LONG_TERM)` returns only facts, not session noise.

## Layered Architecture

The package exposes three layers of abstraction, each building on the previous:

1. **Protocol layer** (`MemoryStoreProtocol`, `MemoryEntry`, `MemoryType`) — the contract
2. **Backend layer** (`FileMemoryStore`, `Mem0MemoryStore`) — the implementations
3. **Facade layer** (`MemoryManager`, `get_memory_manager`) — the agent-friendly interface

Agents always interact with layer 3. The backend is selected at startup and injected into the manager. This means agent code written against `MemoryManager` works identically with both backends without any conditional logic.

## Known Gaps

`_HAS_MEM0` is module-level state set once at import time. If `mem0ai` is installed after the module is first imported (e.g., in a long-running test suite), the flag will be stale.