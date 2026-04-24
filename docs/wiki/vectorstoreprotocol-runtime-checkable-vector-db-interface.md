---
{
  "title": "VectorStoreProtocol: Runtime-Checkable Vector DB Interface",
  "summary": "VectorStoreProtocol defines the minimal async interface every vector store adapter must satisfy—`add`, `search`, `delete`, and `get_by_id`—using Python's `@runtime_checkable Protocol` so that implementations can be verified via `isinstance` without requiring inheritance. This creates a clean dependency boundary between the agent loop and any specific vector database backend.",
  "concepts": [
    "VectorStoreProtocol",
    "Protocol",
    "runtime_checkable",
    "isinstance",
    "structural typing",
    "vector store",
    "add",
    "search",
    "delete",
    "get_by_id"
  ],
  "categories": [
    "vectordb",
    "interfaces",
    "dependency injection"
  ],
  "source_docs": [
    "ac39d8d8c68de81b"
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

`VectorStoreProtocol` is PocketPaw's structural type contract for vector database adapters. It exists to decouple the agent loop and semantic search tools from any specific vector database implementation. Code that depends on `VectorStoreProtocol` does not import Chroma, Pinecone, or any embedding library—it only depends on this small protocol module.

## Why `@runtime_checkable`?

Python's `Protocol` is a structural typing construct: a class that implements all protocol methods satisfies it without explicit inheritance. The `@runtime_checkable` decorator extends this to runtime: `isinstance(obj, VectorStoreProtocol)` returns `True` if `obj` has all the required methods. This is used in two scenarios:

1. **Startup validation**: the app can assert that the configured adapter satisfies the protocol before accepting requests, catching misconfiguration early.
2. **Adapter injection**: dependency injection code can verify an injected adapter at runtime without importing the concrete class.

## Interface Methods

```python
async def add(self, doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> None: ...
async def search(self, query: str, limit: int = 5) -> list[str]: ...
async def delete(self, doc_id: str) -> None: ...
async def get_by_id(self, doc_id: str) -> str | None: ...
```

**`add`** upserts a document: if `doc_id` already exists, the embedding and metadata are updated. This idempotency guarantee prevents duplicate entries when an indexing job retries after a transient failure.

**`search`** returns document text strings ranked by embedding similarity. Returning strings rather than IDs or tuples keeps the interface simple for the primary use case: inject context into an agent prompt.

**`delete`** removes a document by ID. It is expected to be idempotent—deleting a non-existent `doc_id` should not raise.

**`get_by_id`** retrieves a specific document by ID without a similarity search, returning `None` if not found. This is used for exact-match lookups (e.g., "has this document already been indexed?") and avoids running an embedding model for a lookup that doesn't require semantic similarity.

## Dependency Boundary Value

The protocol's placement in `vectordb/protocol.py`—separate from `chroma_adapter.py`—means that code can import just the interface without pulling in ChromaDB or any ML dependency:

```python
# This import has zero ML dependencies
from pocketpaw.vectordb import VectorStoreProtocol

def configure_agent(store: VectorStoreProtocol) -> None:
    ...
```

This is the fundamental reason the protocol exists as a separate file rather than being defined inline in the adapter module.

## Known Gaps

- `search` returns `list[str]` with no relevance scores. Callers implementing re-ranking or threshold filtering have no signal to work with. A richer return type (`list[tuple[str, float]]` or a `SearchResult` dataclass) would enable score-based cutoffs without breaking the interface for callers that don't need scores.
- The protocol does not define a `count` or `list_all` method. Agents that need to know how many documents are indexed, or enumerate all stored documents, have no protocol-level way to do so.
- No batch variants of `add` or `delete` are specified. Bulk operations require looping at the call site, which is inefficient for large indexing jobs or bulk deletions.
