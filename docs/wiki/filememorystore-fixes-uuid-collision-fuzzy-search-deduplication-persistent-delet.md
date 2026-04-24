---
{
  "title": "FileMemoryStore Fixes: UUID Collision, Fuzzy Search, Deduplication, Persistent Delete, and Graph Tests",
  "summary": "This comprehensive test file covers a series of incremental fixes to `FileMemoryStore`, PocketPaw's markdown-based memory backend. It addresses UUID collisions, broken substring search, single-day file loading, lost deletions across restarts, and missing context limits — plus later phases covering vector semantic search, graph indexing, SVG escaping, and SQLite variable limit handling.",
  "concepts": [
    "FileMemoryStore",
    "UUID collision",
    "deterministic ID",
    "fuzzy search",
    "word overlap",
    "persistent delete",
    "ForgetTool",
    "auto-learn",
    "context limits",
    "vector search",
    "SQLite variable limit",
    "graph snapshot",
    "SVG HTML escaping",
    "MemoryManager",
    "deduplication"
  ],
  "categories": [
    "testing",
    "memory system",
    "storage",
    "security",
    "test"
  ],
  "source_docs": [
    "d77da050d8c45273"
  ],
  "backlinks": null,
  "word_count": 730,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Background

`FileMemoryStore` stores memories as markdown files partitioned by type (long-term, daily, session). Each fix addressed a concrete production failure; the tests are organized by step number to reflect that history.

## Step 1: UUID Collision (TestUUIDCollision)

The original ID generation hashed only the header, so two entries both titled `"Memory"` would receive the same ID and the second would silently overwrite the first. The fix — `_make_deterministic_id(path, header, body)` — includes the body content in the hash.

Tests verify:
- Two entries with the same header but different bodies get different IDs.
- All three survive a simulated restart (new `FileMemoryStore` instance from the same directory).
- Same content always produces the same ID (content-addressability for deduplication).

## Step 2: Daily File Indexing (TestDailyFileIndexing)

The original loader only opened today's daily file. Memories written yesterday would vanish after midnight. The fix scans all `YYYY-MM-DD.md` files in the base directory.

Tests write past-dated files directly and verify they are present in `get_by_type(MemoryType.DAILY)` after a fresh store is constructed.

## Step 3: Fuzzy Search (TestFuzzySearch)

The broken substring match would miss `"name"` inside `"User's name is Rohit"` because it compared the full query string rather than individual tokens. The replacement is word-overlap scoring:

- Stop words (`the`, `is`, `a`) are excluded by `_tokenize()`.
- Each token in the query is matched against tokens in the entry's header + content.
- Results are ranked by descending overlap ratio.

The `test_ranking_order` test is particularly important: it verifies that `"Python backend developer Google"` ranks `"Python backend developer at Google"` (4/4 overlap) above `"Python developer"` (2/4 overlap).

## Step 1 Dedup (TestDeduplication)

Saving identical content twice must not grow the index. The idempotency guard returns the existing ID and does nothing if the deterministic ID is already present. The cross-restart variant (`test_dedup_across_restart`) verifies the guard survives reloading from disk.

## Step 4: Persistent Delete (TestPersistentDelete)

Deletion originally only removed the in-memory index entry; the underlying markdown file was not modified, so the entry would reappear after restart. The fix rewrites the markdown file without the deleted entry (or removes the file entirely if empty).

Three tests cover surgical deletion (only the target is removed), cleanup of the last entry in a file, and cross-restart persistence of the deletion.

## Step 5: ForgetTool (TestForgetTool)

`ForgetTool` wraps the memory search and delete operations into a tool the agent can invoke. Tests verify the tool definition (`name == "forget"`, required `query` parameter), that it deletes matching entries (1 of 2 memories matching `"name Rohit"`), and that it belongs to the `group:memory` policy group for correct trust-level enforcement.

## Step 6: Context Limits (TestContextLimits)

The original limit was 10 long-term memories at 200 characters each. The fix raised these to allow more context. Tests assert:
- 25 memories all appear in context.
- Entries truncate at 500 characters, not 200.
- A `max_chars` parameter hard-caps the total context length with a `"...(truncated)"` suffix.

## Step 7: LLM Auto-Learn (TestFileAutoLearn)

`_file_auto_learn` sends a conversation turn to Claude Haiku and saves the extracted facts. Tests mock `anthropic.AsyncAnthropic` and verify fact extraction, graceful degradation when no API key is available (returns `{}`), and deduplication of auto-learned facts.

## Phase 1: Vector Semantic Search (TestFileVectorSemanticSearch)

With `vector_enabled=True` and `embedding_provider="hash"`, the store indexes embeddings in a SQLite database. Tests cover semantic search returning relevant results, deletion removing the vector record, and `MemoryManager.get_semantic_context()` wrapping the results in a `"Relevant Memories"` section.

## Phase 2/3: Graph, Edits, Pruning (TestFileGraphAndManagement)

Key tests:
- Graph DB is not created when the feature is disabled.
- `get_graph_snapshot(limit=500)` does not crash SQLite with `too many SQL variables` (the fix uses a temporary table for `NOT IN` with >999 IDs).
- `_cleanup_orphan_records` removes vector rows not in the memory index without crashing when there are 1050+ valid entries.
- `update_entry` re-indexes content in both vector and graph stores.
- `prune_memories` removes daily entries older than a configurable threshold.
- `clear_session` handles malformed session JSON gracefully.

## Graph SVG Escaping (TestGraphSVGHtmlEscaping)

Entity names containing `>`, `&`, or `<` would produce malformed SVG. The tests insert entities with special characters directly into the graph DB and verify that the rendered SVG contains proper HTML entities (`&gt;`, `&amp;`, `&lt;`).

## Known Gaps

The `embedding_provider="hash"` used in tests produces deterministic but low-quality embeddings; semantic search tests may pass even with a broken semantic model. No test covers concurrent writes to the same markdown file.