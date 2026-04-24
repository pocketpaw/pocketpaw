---
{
  "title": "ChromaAdapter: Pluggable Chroma Vector Store with Multi-Provider Embeddings",
  "summary": "ChromaAdapter wraps ChromaDB's persistent local vector store behind the `VectorStoreProtocol` interface and supports five embedding providers—Chroma's default SentenceTransformer, HuggingFace, OpenAI, Google Gemini, and Voyage AI. Blocking Chroma calls are pushed off the asyncio event loop via `asyncio.to_thread`, keeping the agent loop non-blocking.",
  "concepts": [
    "ChromaAdapter",
    "VectorStoreProtocol",
    "ChromaDB",
    "embedding function",
    "asyncio.to_thread",
    "from_settings",
    "structural subtyping",
    "SentenceTransformer",
    "OpenAI embeddings",
    "HuggingFace"
  ],
  "categories": [
    "vectordb",
    "semantic search",
    "embeddings"
  ],
  "source_docs": [
    "d367bece95de0093"
  ],
  "backlinks": null,
  "word_count": 473,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ChromaAdapter` is PocketPaw's production vector store implementation. It gives the agent loop and semantic search tools access to a local persistent embedding index without requiring an external vector database service. By supporting multiple embedding providers through a single configuration flag, it lets operators choose between local (free, private) and cloud (higher quality) embedding models without changing application code.

## Structural Subtyping Instead of Inheritance

The class is explicitly not declared as inheriting from `VectorStoreProtocol`. The module comment reads: "We no longer inherit from VectorStoreProtocol here. The @runtime_checkable on the protocol handles the check automatically." Because the protocol is `@runtime_checkable`, `isinstance(adapter, VectorStoreProtocol)` returns `True` as long as `ChromaAdapter` implements the required methods—no inheritance needed. This avoids the diamond-inheritance problem and keeps the adapter decoupled from the protocol's internal module.

## `_get_embedding_function`: Provider Dispatch

The `_get_embedding_function` private function builds a Chroma-compatible embedding function based on a `provider` string:

- `"default"` — Chroma's built-in SentenceTransformer (`all-MiniLM-L6-v2`), zero configuration, runs locally.
- `"huggingface"` — Any HuggingFace model ID (e.g., `BAAI/bge-small-en-v1.5`), downloaded on first use.
- `"openai"` — OpenAI text embedding models, requires `OPENAI_API_KEY`.
- `"google"` — Gemini Embedding 2, multimodal (text + images), requires `GOOGLE_API_KEY`.
- `"voyage"` — Voyage AI models with multimodal support, requires `VOYAGE_API_KEY`.

Each provider's import is deferred inside the `if` branch, so the module is importable without all five embedding libraries present. Only the selected provider's package needs to be installed.

## `from_settings` Factory

`ChromaAdapter.from_settings(settings)` reads `settings.vectordb_path` and the embedding configuration and constructs the adapter. This factory method keeps construction consistent and makes testing with a temporary directory path straightforward:

```python
adapter = ChromaAdapter.from_settings(app_settings)
await adapter.add("doc_1", "The agent loop processes tool calls.", metadata={"source": "docs"})
results = await adapter.search("how does tool calling work", limit=5)
```

## Async via `asyncio.to_thread`

Chroma's client is synchronous. Rather than blocking the asyncio event loop during `add`, `search`, and `delete` calls—which involve disk I/O and embedding inference—each method wraps the synchronous Chroma call in `asyncio.to_thread`. This matches the pattern used in `S3StorageAdapter` and keeps the entire agent turn non-blocking.

## Metadata Storage

The `add` method accepts an optional `metadata` dict that Chroma persists alongside the embedding vector. This enables filtered search (e.g., retrieve only documents from a specific source or date range) when Chroma's `where` clause filtering is applied by downstream tooling.

## Known Gaps

- `search` returns `list[str]` (document text) with relevance scores discarded, preventing callers from implementing score-based filtering or ranking.
- `get_by_id` is declared on `VectorStoreProtocol` but its implementation in `ChromaAdapter` is not confirmed in the AST extract—if absent, `isinstance` checks pass but the method raises `AttributeError` when called.
- Collection names are fixed at construction time; multiple namespaced collections (e.g., per-user or per-workspace) require separate `ChromaAdapter` instances.
- No batching for `add`: inserting thousands of documents makes one Chroma call per document; bulk-insert support would significantly improve indexing throughput for large knowledge bases.
