---
{
  "title": "Memory Entry and Settings Schemas",
  "summary": "Defines the Pydantic models for PocketPaw's memory system — a single memory entry with tagging support, and a configuration response that exposes the pluggable memory backend settings including optional mem0 AI-inference configuration. The settings model reflects the system's multi-backend memory architecture.",
  "concepts": [
    "MemoryEntry",
    "MemorySettingsResponse",
    "mem0",
    "memory backend",
    "vector store",
    "embeddings",
    "semantic recall",
    "auto-learn",
    "Ollama",
    "Pydantic"
  ],
  "categories": [
    "api-schemas",
    "memory",
    "configuration",
    "ai-backends"
  ],
  "source_docs": [
    "85146d9d76d11690"
  ],
  "backlinks": null,
  "word_count": 459,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's memory subsystem allows agents to persist and retrieve information across conversations. This file defines the schema layer for memory entries and the configuration that governs how memory is stored and retrieved.

## Models

### `MemoryEntry`

```python
class MemoryEntry(BaseModel):
    id: str
    content: str
    timestamp: str
    tags: list[str] = []
```

A single memory record. `id` is a stable identifier used for retrieval and deletion. `content` holds the remembered text. `timestamp` is a string — keeping timezone handling in the application layer rather than forcing UTC normalisation at the schema boundary. `tags` enable faceted recall: an agent can retrieve memories by tag (e.g. `"user_preference"`, `"project_context"`) rather than only by full-text search.

### `MemorySettingsResponse`

```python
class MemorySettingsResponse(BaseModel):
    memory_backend: str = "file"
    memory_use_inference: bool = False
    mem0_llm_provider: str = ""
    mem0_llm_model: str = ""
    mem0_embedder_provider: str = ""
    mem0_embedder_model: str = ""
    mem0_vector_store: str = ""
    mem0_ollama_base_url: str = ""
    mem0_auto_learn: bool = False
```

This model reveals PocketPaw's pluggable memory architecture. Two backends are implied:

**File backend** (`memory_backend: str = "file"`) — the default, persisting memories as local files. Requires no external dependencies and works offline.

**mem0 backend** — an AI-powered memory layer that uses embeddings and vector search for semantic recall. When active, the agent can retrieve memories by meaning rather than exact match. The `mem0_*` fields configure:

- **LLM provider + model** — used for memory extraction and summarisation (e.g. extracting a preference from a long conversation).
- **Embedder provider + model** — converts memory content to vectors for similarity search.
- **Vector store** — the database that holds embeddings (e.g. Chroma, Qdrant, Pinecone).
- **Ollama base URL** — supports local LLM inference for memory processing, keeping data on-premises.
- **`mem0_auto_learn`** — when `True`, the agent automatically extracts and stores memories from conversations without explicit `remember` calls.

## Architectural Significance

The `memory_use_inference` flag is the main toggle. When `False`, memory is simple key-value storage with tags. When `True`, the agent uses the LLM and embedder to understand and retrieve memories semantically — but at the cost of LLM API calls on every memory operation. This tradeoff (capability vs. cost/latency) is surfaced explicitly in the settings so operators can make an informed choice.

The `mem0_ollama_base_url` field is particularly significant: it allows the entire memory pipeline to run locally, satisfying data-residency requirements for enterprise deployments that cannot send conversation data to external LLM providers.

## Known Gaps

- `MemorySettingsResponse` documents read settings but there is no corresponding `MemorySettingsUpdateRequest` in this file — settings updates are handled through the generic `SettingsUpdateRequest` in `settings.py`, which loses the typed validation benefit.
- No pagination on `MemoryEntry` retrieval — a list endpoint returning all entries would scale poorly with large memory stores.
- `memory_backend` is an unconstrained string with no `Literal["file", "mem0"]` guard.