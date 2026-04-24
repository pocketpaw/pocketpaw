---
{
  "title": "ChromaAdapter: Vector Database Integration for Semantic Memory Storage",
  "summary": "The `ChromaAdapter` wraps ChromaDB to provide async add, search, get-by-ID, delete, and upsert operations for PocketPaw's semantic memory tier. Tests are conditionally skipped when `chromadb` is not installed, and cover the core CRUD operations, graceful handling of small collections during search, upsert (duplicate ID update), and optional metadata storage.",
  "concepts": [
    "ChromaAdapter",
    "ChromaDB",
    "vector_database",
    "semantic_memory",
    "upsert",
    "optional_dependency",
    "metadata",
    "search",
    "add",
    "delete",
    "get_by_id"
  ],
  "categories": [
    "memory-system",
    "testing",
    "vector-database",
    "test"
  ],
  "source_docs": [
    "2524c4b86ac462f4"
  ],
  "backlinks": null,
  "word_count": 396,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ChromaAdapter` is PocketPaw's interface to ChromaDB, the embedded vector database used for semantic memory retrieval. It is an optional component — only relevant when PocketPaw is configured with a local vector store rather than a cloud memory service.

## Conditional Skip for Optional Dependency

The test file begins with `pytest.importorskip("chromadb")`, which automatically skips the entire module if ChromaDB is not installed. This is the correct pattern for optional dependencies: tests are not marked as failures in environments without ChromaDB, but they run and protect the adapter's behavior in environments where it is available.

## Core Operations

**Add and Search**: `adapter.add(id, text)` stores a document, and `adapter.search(query)` retrieves the closest matches. The test verifies the added text appears in search results.

**Delete**: After deletion, `get_by_id` returns `None` for the deleted document.

**Get by ID**: Fetches a specific document by its exact ID, returns `None` for unknown IDs.

## Small Collection Handling

ChromaDB raises `NotEnoughElements` if `n_results` exceeds the collection size. The search test seeds the collection first and uses `limit=1` to avoid this error:

```python
async def test_search_no_results(adapter):
    await adapter.add("initial_doc", "The quick brown fox...")
    results = await adapter.search("quantum computing in space", limit=1)
    assert len(results) <= 1
    assert isinstance(results, list)
```

The comment acknowledges that vector search always returns the closest match — the test is verifying the plumbing does not crash, not that the semantic relevance is zero.

## Upsert for Duplicate IDs

ChromaDB's default `add` behavior raises an error on duplicate IDs. The adapter uses upsert semantics instead: adding a document with an existing ID updates it rather than raising. This is essential for memory update workflows where an agent revises an existing memory entry.

```python
async def test_duplicate_ids(adapter):
    await adapter.add("dup", "First version")
    await adapter.add("dup", "Updated version")
    assert await adapter.get_by_id("dup") == "Updated version"
```

## Metadata Support

`add` accepts an optional `metadata` dict (e.g., `{"source": "test_file", "priority": "high"}`). The test confirms the content is stored and retrievable by ID. Metadata enables filtered queries and provenance tracking in more advanced memory retrieval workflows.

## Fixture Isolation

The `adapter` fixture creates a fresh `ChromaAdapter` pointing to `tmp_path / "test_db"` for each test, ensuring tests are fully isolated and do not share database state.

## Known Gaps

No TODOs. The metadata test does not verify that metadata is returned alongside content — it only confirms the content is stored correctly. Metadata-filtered search queries are not yet tested.