---
{
  "title": "MemoryManager — High-Level Memory Facade and Singleton",
  "summary": "`MemoryManager` is the application-facing facade over any `MemoryStoreProtocol` backend, providing agent-friendly methods like `get_context_for_agent`, `auto_learn`, and `get_graph_snapshot`. It is accessed as a singleton via `get_memory_manager()`.",
  "concepts": [
    "MemoryManager",
    "singleton",
    "get_context_for_agent",
    "auto_learn",
    "knowledge graph",
    "session management",
    "user scoping",
    "force_reload",
    "context injection",
    "Mem0",
    "FileMemoryStore"
  ],
  "categories": [
    "Memory System",
    "Agent Infrastructure"
  ],
  "source_docs": [
    "84108a55cce2b118"
  ],
  "backlinks": null,
  "word_count": 435,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Raw store operations (`save`, `search`, `get_session`) are protocol primitives. Agent code needs higher-level operations: get all relevant context as a single string, automatically extract facts from a conversation, or retrieve a graph visualisation. `MemoryManager` provides these without leaking storage details into agent code.

## Singleton and Configuration

```python
def get_memory_manager(force_reload: bool = False) -> MemoryManager:
```

The singleton is built from application config on first access. `force_reload=True` is used by tests and by the dashboard's config-reload endpoint to pick up changed settings (e.g., switching from file backend to Mem0) without restarting the process.

## Key Methods

### get_context_for_agent

Assembles a formatted context string from long-term and daily memories, capped at `max_chars`. This is injected into agent system prompts. The cap prevents context from blowing out the model's context window.

### auto_learn

Analyses a completed conversation and extracts facts worth persisting. The file backend's `_file_auto_learn` delegates to an LLM for extraction; the Mem0 backend's own fact extraction runs automatically on save. This dual-path exists because Mem0 has built-in LLM-powered consolidation while the file backend does not.

### get_graph_snapshot / get_graph_svg

Returns the entity-relationship graph for a user's memories, optionally filtered by a query. `get_graph_svg` renders it as SVG for the dashboard's graph view. These methods bypass `MemoryStoreProtocol` and call `FileMemoryStore` directly — they are not available when using the Mem0 backend.

### Session Management

`resolve_session_key`, `set_session_alias`, and `list_sessions_for_chat` wrap the store's alias system. `search_sessions` enables the dashboard's session search feature.

## User Scoping

`_resolve_user_id(sender_id)` maps an incoming message sender ID to a stable user identity. When `sender_id` is absent, it falls back to the configured `user_id`. This prevents memories from one user leaking into another user's context in multi-user deployments.

## Context Injection

`get_context_for_agent` is called by the agent router before each LLM call to build the system prompt context block. It combines:

- Up to `long_term_limit` long-term memory entries, each capped at `entry_max_chars`
- Up to `daily_limit` entries from today's daily notes
- The whole block truncated to `max_chars`

The multi-level capping strategy prevents any single long memory from crowding out all others, while the total cap prevents context from blowing out the model's context window. Without these caps, a user who has stored thousands of long-term facts could inadvertently fill the entire context with memory and leave no room for the actual conversation.

## Known Gaps

Graph methods silently do nothing when the active backend is Mem0, rather than raising `NotImplementedError`. This makes the dashboard's graph panel quietly empty for Mem0 users. `prune_memories` is defined but the pruning threshold (`older_than_days`) is not validated — a value of 0 would delete all memories.