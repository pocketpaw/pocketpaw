---
{
  "title": "Agent Knowledge Service (ee/cloud/agents/knowledge.py)",
  "summary": "A thin service wrapper that scopes the core KnowledgeEngine to individual agents. Each agent gets its own isolated knowledge namespace via `agent:{agent_id}` scoping, enabling per-agent text, URL, and file ingestion plus search.",
  "concepts": [
    "KnowledgeService",
    "KnowledgeEngine",
    "RAG",
    "agent scoping",
    "text ingestion",
    "URL ingestion",
    "search"
  ],
  "categories": [
    "enterprise",
    "cloud",
    "knowledge management",
    "agents"
  ],
  "source_docs": [
    "f1827701a9a101de"
  ],
  "backlinks": null,
  "word_count": 343,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Agent Knowledge Service

## Purpose

`KnowledgeService` is a stateless service class that adapts the core `pocketpaw.knowledge.KnowledgeEngine` for use in the cloud agents domain. It adds agent-level scoping so each agent's knowledge base is isolated from others.

## Scoping Strategy

Every method creates a `KnowledgeEngine(scope=f"agent:{agent_id}")`. This scope string partitions the underlying storage so that:
- Agent A's ingested documents don't appear in Agent B's searches
- Clearing one agent's knowledge doesn't affect another
- Stats are reported per-agent

## Methods

### Ingestion

| Method | Input | Error Handling |
|--------|-------|----------------|
| `ingest_text()` | Raw text + source label | No try/catch — propagates to caller |
| `ingest_url()` | URL string | Catches all exceptions, returns `{"error": ...}` |
| `ingest_file()` | File path | Catches all exceptions, returns `{"error": ...}` |

The inconsistent error handling is notable: `ingest_text` will raise exceptions while `ingest_url` and `ingest_file` swallow them and return error dicts. This is likely because URL and file ingestion have more failure modes (network errors, unsupported formats) and the caller (the router) needs a graceful degradation path.

### Search

- `search()` — returns a list of summary strings (or first 500 chars of content if no summary)
- `search_context()` — returns a pre-formatted string suitable for injection into an agent's prompt. This is the primary integration point for RAG (retrieval-augmented generation).

### Maintenance

- `clear()` — wipes all knowledge for an agent
- `stats()` — returns storage statistics (synchronous, no DB queries needed)
- `lint()` — checks knowledge base health, returns issues as dicts

## Deferred Import

The `KnowledgeEngine` import is inside `_engine()` rather than at module level. This prevents circular imports since the knowledge engine may reference cloud models, and it avoids loading the engine's dependencies when the agents module is merely imported but not used.

## Known Gaps

- `_engine()` creates a new `KnowledgeEngine` instance on every call. If instantiation is expensive (e.g., index loading), this could benefit from caching per agent_id.
- `ingest_text` lacks the try/except pattern used by `ingest_url` and `ingest_file`, creating inconsistent error handling for callers.