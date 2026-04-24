---
{
  "title": "Memory Store Protocol — Types and Backend Interface",
  "summary": "`protocol.py` defines the foundational types for PocketPaw's memory system: the `MemoryType` enum, the `MemoryEntry` dataclass, and the `MemoryStoreProtocol` structural Protocol that any storage backend must implement, keeping `FileMemoryStore` and `Mem0MemoryStore` interchangeable.",
  "concepts": [
    "MemoryStoreProtocol",
    "MemoryEntry",
    "MemoryType",
    "StrEnum",
    "Protocol",
    "structural typing",
    "session memory",
    "long-term memory",
    "daily notes",
    "backend interface",
    "dataclass"
  ],
  "categories": [
    "Memory System",
    "Protocol Design"
  ],
  "source_docs": [
    "91ce4c513466a150"
  ],
  "backlinks": null,
  "word_count": 519,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Without a shared protocol, `MemoryManager` would need to know which backend is active and call backend-specific methods directly. `MemoryStoreProtocol` eliminates this coupling — the manager calls `store.save(entry)` regardless of whether the store is file-based or Mem0-backed.

## MemoryType

```python
class MemoryType(StrEnum):
    LONG_TERM = "long_term"   # Facts, preferences, important info
    DAILY = "daily"           # Daily notes and events
    SESSION = "session"       # Conversation history
```

Using `StrEnum` (Python 3.11+) means `MemoryType` values serialize to plain strings in JSON, making stored memories human-readable without custom serialisers.

## MemoryEntry

```python
@dataclass
class MemoryEntry:
    id: str
    type: MemoryType
    content: str
    created_at: datetime
    updated_at: datetime
    tags: list[str]
    metadata: dict[str, Any]
    role: str | None          # "user" | "assistant" | "system"
    session_key: str | None
```

`role` and `session_key` are session-specific fields — they are `None` for long-term and daily memories. Keeping them on the shared dataclass avoids a class hierarchy, at the cost of optional fields that are meaningless for non-session entries.

`metadata` is an escape hatch for backend-specific data (e.g., Mem0 internal IDs, embedding checksums) that does not belong in the core schema.

## MemoryStoreProtocol

```python
class MemoryStoreProtocol(Protocol):
    async def save(self, entry: MemoryEntry) -> str: ...
    async def get(self, entry_id: str) -> MemoryEntry | None: ...
    async def delete(self, entry_id: str) -> bool: ...
    async def search(self, query, memory_type, tags, limit) -> list[MemoryEntry]: ...
    async def get_by_type(self, memory_type, limit, user_id) -> list[MemoryEntry]: ...
    async def get_session(self, session_key) -> list[MemoryEntry]: ...
    async def clear_session(self, session_key) -> int: ...
```

Using a structural `Protocol` (duck typing) rather than an abstract base class means third-party backends do not need to inherit from PocketPaw's classes — they only need matching method signatures. This is the standard Python approach for open, extensible interfaces.

## Design Decision: Protocol Over ABC

Python's `Protocol` was chosen over an abstract base class (`ABC`) for `MemoryStoreProtocol` because it enables structural (duck-typed) compatibility. A third-party library that happens to expose `save`, `delete`, and `search` methods with matching signatures will satisfy the protocol without importing or inheriting from PocketPaw code at all. This matters because PocketPaw aims to support custom backends built by the community.

Compare this to an `ABC` approach: with `ABC`, every custom backend would need to `import MemoryStoreProtocol` and subclass it, creating a hard dependency on PocketPaw's package. With `Protocol`, the backend is independent and can be developed, tested, and distributed separately.

## MemoryEntry as the Universal Record

A design goal of `MemoryEntry` is that it must work across all three memory types without subclassing. This means some fields are only meaningful for certain types (`role` and `session_key` for SESSION, ignored for others). The alternative — separate dataclasses per type — would have required the protocol to have separate methods per type, making it much harder for backends to implement and consumers to use.

## Known Gaps

`auto_learn` is not part of the protocol. Both backends implement it, but `MemoryManager` calls it directly on the concrete store type, breaking the abstraction for this operation. `update` (modify an existing entry's content) is absent from the protocol; it is implemented only on `FileMemoryStore` and accessed via `MemoryManager.update_memory`, which casts the store internally.