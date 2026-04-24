---
{
  "title": "FileMemoryStore — Markdown, SQLite, and Vector Memory Backend",
  "summary": "`FileMemoryStore` is PocketPaw's default memory backend, storing long-term and daily memories as human-readable Markdown files with optional semantic search via SQLite vector columns or ChromaDB and an optional knowledge graph for entity-relationship indexing.",
  "concepts": [
    "FileMemoryStore",
    "Markdown files",
    "SQLite vector",
    "ChromaDB",
    "semantic search",
    "knowledge graph",
    "session index",
    "session aliases",
    "backfill",
    "cosine similarity",
    "vector migration"
  ],
  "categories": [
    "Memory System",
    "Storage Backend"
  ],
  "source_docs": [
    "bb502e25030e6a2a"
  ],
  "backlinks": null,
  "word_count": 454,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`FileMemoryStore` was designed to be usable without any external services. The core store is plain Markdown files (human-readable and git-diff-able), backed by a JSON session index. Vector search and graph indexing are additive features that activate only when the required libraries are present.

## Storage Layout

```
~/.pocketpaw/memory/
  long_term/
    <entry_id>.md
  daily/
    2026-04-23.md
  sessions/
    <session_key>.json
  session_index.json
  aliases.json
  vector.db           # SQLite vector store (optional)
```

## Vector Search

Vector search is initialised in `_initialize_vector_backend()`. The store supports two backends:

- **ChromaDB** — external vector database (`chromadb` package)
- **SQLite vector** — built-in via a custom `vec0` extension, with automatic schema migration via versioned `_migrate_vector_schema_*` methods

The SQLite path uses `_hash_embedding()` as a deterministic fallback when no embedding model is configured, enabling basic similarity matching without an API key.

`_backfill_missing_vector_records()` runs on startup to index any memories saved before vector search was enabled, preventing a two-tier search experience where old memories are invisible to semantic queries.

## Knowledge Graph

`_initialize_graph_store()` sets up an entity-relationship graph. Entities and relationships are extracted from memory content during `_index_graph_record`. Validation helpers (`_is_valid_entity_candidate`, `_is_valid_relationship_candidate`) and scoring (`_score_relationship_candidate`) filter out noise — short strings, stopwords, and low-confidence relationships — to keep the graph clean.

## Session Aliases

Session keys can be aliased to each other via `set_session_alias` and `resolve_session_alias`. This supports chat continuations across different identifiers (e.g., a Slack thread ID aliased to a Discord channel ID). Aliases are persisted in `aliases.json`.

## Cosine Similarity

`_cosine_similarity` is implemented in pure Python as a fallback. When a vector extension is available, the store delegates to it for performance.

## Why Markdown?

Markdown was chosen over a pure database format so that long-term memories are human-readable and debuggable without tooling. Operators can open `~/.pocketpaw/memory/long_term/` in any text editor to inspect or hand-edit memories. This also means memories are diffable in git if the user chooses to back up their config directory. The trade-off is slower search compared to a database — which is why vector search is available as an opt-in accelerator.

## Vector Schema Migration

The SQLite vector store uses a versioned schema, tracked via `PRAGMA user_version`. On startup, `_get_sqlite_user_version` reads the current version and `_migrate_vector_schema` applies any pending migrations. The v0-to-v1 migration (`_migrate_vector_schema_v0_to_v1`) adds new columns without dropping existing data. This pattern ensures that users upgrading PocketPaw do not lose their vector index and do not need to re-embed all their memories.

## Known Gaps

SQLite vector migration is one-directional (v0 to v1); there is no rollback path. The graph store is not exposed via `MemoryStoreProtocol` — it is accessed through `MemoryManager.get_graph_snapshot()` which calls `FileMemoryStore` directly, bypassing the protocol abstraction. Embedding calls are async but backed by synchronous HTTP in some providers, which can block the event loop under high load.