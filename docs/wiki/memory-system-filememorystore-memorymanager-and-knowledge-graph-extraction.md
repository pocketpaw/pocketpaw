---
{
  "title": "Memory System: FileMemoryStore, MemoryManager, and Knowledge Graph Extraction",
  "summary": "The core memory test suite validates PocketPaw's `FileMemoryStore` and `MemoryManager` — covering long-term and session memory CRUD, the agent context assembly flow, and an extensive suite for the knowledge graph extraction system that builds entity-relationship graphs from conversation text using conservative regex patterns.",
  "concepts": [
    "FileMemoryStore",
    "MemoryManager",
    "MemoryEntry",
    "MemoryType",
    "knowledge graph",
    "entity extraction",
    "relationship extraction",
    "confidence threshold",
    "session history",
    "long-term memory",
    "get_context_for_agent",
    "regex patterns",
    "entity canonicalization"
  ],
  "categories": [
    "memory system",
    "knowledge graph",
    "test"
  ],
  "source_docs": [
    "d99f64189ad39cbb"
  ],
  "backlinks": null,
  "word_count": 481,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's memory system stores three kinds of data: long-term facts (persisted across sessions), daily summaries, and session history (per-conversation message log). The `MemoryManager` facade provides a clean API over `FileMemoryStore`, and a knowledge graph extractor builds a lightweight entity-relationship graph from stored memories to augment agent context.

## MemoryEntry Dataclass

`TestMemoryEntry` validates the `MemoryEntry` dataclass — the fundamental unit of storage. Key fields: `id`, `type` (`MemoryType` enum: `LONG_TERM`, `DAILY`, `SESSION`), `content`, `tags`, `metadata`, `role`, and `session_key`. The `role` and `session_key` fields are only meaningful for `SESSION` type entries and default to empty, which avoids `None` checks in consuming code.

## FileMemoryStore

`TestFileMemoryStore` uses async tests with a temp-path fixture:

- **Save and retrieve long-term**: saved entry gets a generated ID; the backing file is created on first save.
- **Session save**: adds a message to a session-keyed log.
- **Clear session**: removes all messages for a session key without affecting other sessions.
- **Search**: full-text search over long-term memories.

All operations are `async` — the store uses an async lock internally to serialize file writes.

## MemoryManager Facade

`TestMemoryManager` tests the higher-level API:

- **`remember(text)`**: saves a long-term memory entry.
- **`note(text)`**: saves a daily note.
- **`session_flow`**: add messages, retrieve history, clear.
- **`get_context_for_agent`**: assembles a formatted context string from long-term memories and session history for injection into the LLM prompt.

## Integration Test

`TestMemoryIntegration.test_full_workflow` exercises the complete lifecycle: save a long-term memory, add session messages, retrieve context, clear session, verify long-term memory persists. This is the "golden path" test that guards against regressions in the assembly logic.

## Knowledge Graph Extraction

`TestGraphExtraction` is the largest section, covering `FileMemoryStore`'s built-in graph extraction:

**Entity extraction:**
- `test_entity_blacklist_filtering`: common words (`the`, `is`, `in`, etc.) are excluded from the entity list.
- `test_valid_entity_candidates`: title-case words and tech terms are recognized as entities.
- `test_entity_length_validation`: very short (< 2 chars) and very long (> 50 chars) candidates are rejected.
- `test_self_loop_prevention`: an entity cannot have a relationship with itself.
- `test_entity_canonicalization`: different casings of the same entity name are normalized.

**Relationship extraction** uses named patterns:
- `uses pattern`, `depends_on pattern`, `built_on pattern`, `is_a pattern`, `part_of pattern`, `implements pattern` — each tested with representative text.

**Confidence threshold:**
- `test_confidence_threshold_blocks_low_confidence_edges`: edges below the configured confidence threshold are not stored.
- `test_stores_only_high_confidence_edges`: only high-confidence relationships appear in the graph.

**Relation normalization:**
- `test_relation_normalization_schema` verifies that relationship types are normalized to a canonical schema (e.g., `"uses"` not `"use"` or `"using"`).

The conservative extraction approach (named patterns + blacklist + length + confidence threshold) prioritizes precision over recall — it is better to miss a relationship than to add a noisy one that pollutes agent context.

## Known Gaps

- `TestFileMemoryStore.test_search` tests basic substring matching; semantic/fuzzy search is not covered here (that is the Mem0 backend's domain).
- The Windows SQLite WAL file cleanup note in the fixture (`shutil.rmtree(tmpdir, ignore_errors=True)`) indicates a known test flakiness on Windows.