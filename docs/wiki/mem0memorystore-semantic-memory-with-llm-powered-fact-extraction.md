---
{
  "title": "Mem0MemoryStore — Semantic Memory with LLM-Powered Fact Extraction",
  "summary": "`Mem0MemoryStore` wraps the `mem0ai` library to provide vector-based semantic search, LLM-powered fact extraction and consolidation, and memory evolution (updating existing memories instead of duplicating them), implementing `MemoryStoreProtocol` as a drop-in replacement for `FileMemoryStore`.",
  "concepts": [
    "Mem0MemoryStore",
    "mem0ai",
    "LLM fact extraction",
    "memory consolidation",
    "semantic search",
    "async wrapper",
    "run_in_executor",
    "vector store",
    "Qdrant",
    "Ollama",
    "lazy initialisation",
    "_RESERVED_METADATA_KEYS"
  ],
  "categories": [
    "Memory System",
    "Storage Backend"
  ],
  "source_docs": [
    "eb5614398685cc85"
  ],
  "backlinks": null,
  "word_count": 427,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The file-based backend stores memories verbatim. Mem0 takes a different approach: when you save a conversation message, it runs an LLM to extract facts, then consolidates them with existing memories to avoid duplication. Over time, the memory store becomes a distilled knowledge base rather than a raw conversation log.

## Lazy Initialisation

```python
def _ensure_initialized(self) -> None:
```

Mem0 initialisation (loading the LLM client, connecting to the vector store) is deferred until the first operation. This prevents import-time failures if `mem0ai` is installed but misconfigured, and avoids slowing down application startup when Mem0 is not the active backend.

## Async Wrapper Pattern

Mem0's SDK is synchronous. All async methods use:

```python
async def _run_sync(self, func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))
```

This runs Mem0 calls in a thread pool, preventing them from blocking the asyncio event loop. The `functools.partial` binding ensures arguments are captured correctly for the executor.

## Configurable Providers

The store accepts `llm_provider`, `embedder_provider`, `vector_store`, `ollama_base_url`, `anthropic_api_key`, and `openai_api_key`. This allows operators to run fully local deployments (Ollama LLM + Qdrant) or cloud deployments (Anthropic + managed Qdrant) without changing application code.

Embedding dimensions are pre-declared in `_EMBEDDING_DIMS` to match the configured model, preventing dimension mismatch errors when switching models.

## Metadata Handling

`_RESERVED_METADATA_KEYS` lists fields stored as dedicated dataclass fields on `MemoryEntry` (`pocketpaw_type`, `tags`, `created_at`, `role`). When converting a Mem0 item back to `MemoryEntry` via `_mem0_to_entry`, these keys are excluded from the generic `metadata` dict to avoid duplication.

## Memory Evolution

Mem0's most distinctive feature is memory evolution: instead of storing every save as a new entry, Mem0 runs an LLM to compare the new content against existing memories and decide whether to add a new entry, update an existing one, or discard the content as a duplicate. This means the memory store converges toward a compact, non-redundant knowledge base over time rather than growing unboundedly.

For example, if the user first says 'I prefer TypeScript' and later says 'I always use TypeScript for new projects', Mem0 will update the existing preference record rather than creating a second one. The `FileMemoryStore` cannot do this — it relies on `auto_learn` running periodically to consolidate.

## Known Gaps

`get_session`, `clear_session`, and `add_to_session` are implemented but session memory is not Mem0's primary design target — it stores sessions as tagged long-term memories, which may be consolidated and modified by Mem0's fact extraction. Graph features (`get_graph_snapshot`, `get_graph_svg`) are not supported and return empty results silently. No migration path from `FileMemoryStore` to `Mem0MemoryStore` exists — switching backends loses all existing memories.