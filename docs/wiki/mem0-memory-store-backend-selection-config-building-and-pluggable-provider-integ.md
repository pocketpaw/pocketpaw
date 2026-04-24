---
{
  "title": "Mem0 Memory Store: Backend Selection, Config Building, and Pluggable Provider Integration",
  "summary": "The Mem0 store test suite validates PocketPaw's integration with the `mem0ai` package as an optional semantic memory backend, covering the factory function's backend selection and fallback logic, `_build_mem0_config` helper output for all supported LLM and embedder providers, and the `Mem0MemoryStore` CRUD operations including tag filtering and semantic search.",
  "concepts": [
    "Mem0",
    "mem0ai",
    "memory backend",
    "FileMemoryStore",
    "create_memory_store",
    "_build_mem0_config",
    "vector store",
    "Qdrant",
    "Chroma",
    "embedding dimensions",
    "Ollama",
    "Anthropic",
    "OpenAI",
    "semantic memory",
    "tag filtering"
  ],
  "categories": [
    "memory system",
    "LLM integration",
    "test"
  ],
  "source_docs": [
    "7a919ca54e9021ab"
  ],
  "backlinks": null,
  "word_count": 502,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's memory system supports multiple backends: a simple file store (always available) and an optional `mem0ai`-powered semantic store that uses vector search for intelligent recall. The `create_memory_store()` factory selects the backend based on configuration, and `_build_mem0_config()` constructs the mem0 configuration dict for each combination of LLM provider and vector store.

## Factory Function and Fallback

`TestCreateMemoryStore` establishes a critical safety property: the factory always returns a working store, even when the requested backend is unavailable.

- **Default**: returns `FileMemoryStore` (no config needed, always safe).
- **Explicit `backend="file"`**: same.
- **Unknown backend**: falls back to `FileMemoryStore` rather than raising. This prevents a misconfigured `memory_backend` setting from breaking the entire application.
- **Mem0 params passthrough**: even when the backend resolves to a file store (e.g., `mem0ai` not installed), the factory accepts mem0 configuration parameters without error. This allows config files to pre-specify mem0 settings that will activate when the package is installed.

## MemoryManager Backend Selection

`TestMemoryManagerBackendSelection` confirms the `MemoryManager` facade's three construction modes:

1. `backend="file"` → uses `FileMemoryStore`.
2. Custom `store=...` → uses the provided store directly (for testing and DI).
3. Mem0 params → accepted without raising.

## Config Building: LLM Providers

`TestBuildMem0Config` validates `_build_mem0_config()` for each combination:

- **Anthropic LLM**: sets `provider="anthropic"`, passes `api_key` from settings.
- **OpenAI LLM**: sets `provider="openai"`, passes OpenAI key to both LLM and embedder sections.
- **Ollama LLM**: sets `provider="ollama"`, uses `base_url` for local service; no API key.
- **OpenAI embedder**: sets `provider="openai"` in the embedder section with key.
- **Ollama embedder**: sets `provider="ollama"` with `base_url`.

API keys are only included when non-`None` — `test_no_api_key_when_none` verifies the key is absent from the config dict when not set, preventing mem0 from sending an empty string as a key.

## Config Building: Vector Stores

- **Qdrant**: `test_qdrant_vector_store_config` verifies the Qdrant connection config including `path` for local mode.
- **Chroma**: similar for ChromaDB local path.
- **Embedding dims**: different models have different embedding dimensions (e.g., `text-embedding-3-small` → 1536, `nomic-embed-text` → 768). `test_embedding_dims_by_model` and `test_unknown_embedder_defaults_to_1536` verify the lookup table and its safe default.
- **Qwen3 known dims**: `test_qwen3_embedding_dims_known` verifies a recently-added model entry.
- **Ollama auto-detection**: `test_ollama_dims_auto_detection` covers the path where Ollama embedding dimensions are detected at runtime rather than hardcoded.

## Mem0MemoryStore Operations

`TestMem0MemoryStore` mocks the `mem0ai.Memory` class and tests:

- **Save by type**: `LONG_TERM`, `SESSION`, and `DAILY` memories are saved with appropriate metadata tags.
- **Search**: `search_memories` calls `mem0.search()` and maps results.
- **Search without query**: uses `mem0.get_all()` when no query is provided.
- **Tag filtering**: results not matching the requested tag are excluded.
- **Non-matching tag**: empty list returned when no results match.
- **Get by type**: filters the result set by memory type.
- **Delete**: calls `mem0.delete()` with the correct ID; failure is surfaced rather than swallowed.

## Known Gaps

- The Ollama embedding dimension auto-detection path (`test_ollama_dims_auto_detection`) is mocked at a high level; the actual HTTP call to the Ollama `/api/embeddings` endpoint is not tested.
- There are no tests for concurrent writes to the mem0 backend — mem0ai's thread safety is assumed.