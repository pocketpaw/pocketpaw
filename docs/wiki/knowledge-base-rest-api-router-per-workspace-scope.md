---
{
  "title": "Knowledge Base REST API Router (Per-Workspace Scope)",
  "summary": "The primary FastAPI router for workspace-scoped knowledge base operations — search, ingest, lint, and article browsing — all delegating to the kb Go binary. This router replaced a pure-Python knowledge_base package and defines the REST surface consumed by the wiki pocket template.",
  "concepts": [
    "FastAPI router",
    "knowledge base",
    "kb-go binary",
    "BM25 search",
    "text ingestion",
    "URL ingestion",
    "scope resolution",
    "workspace scope",
    "lint",
    "kb.read",
    "kb.write",
    "CloudError",
    "enterprise license"
  ],
  "categories": [
    "knowledge-base",
    "API routing",
    "enterprise",
    "search"
  ],
  "source_docs": [
    "256818605a66ae7f"
  ],
  "backlinks": null,
  "word_count": 508,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`router.py` is the main knowledge base API router, mounted at `/kb` within the enterprise cloud layer. It provides five categories of endpoints: search, text ingestion, URL ingestion, lint/health checks, and individual article retrieval. All operations delegate to the kb Go binary via the `_kb()` bridge function imported from `ee.cloud.agents.knowledge`.

The router was migrated from a pure-Python knowledge base package to the Go binary to gain performance, a unified BM25 index format, and CLI compatibility with agent workflows. The REST API surface was preserved across the migration so existing clients required no changes.

## Scope Resolution

All KB operations are scoped. The `_scope()` helper computes the canonical scope string:

```python
def _scope(workspace_id: str, override: str | None = None) -> str:
    return override or f"workspace:{workspace_id}"
```

Every request body can optionally include a `scope` override. When absent, the scope defaults to `workspace:{workspace_id}` derived from the authenticated session. This design allows power users and agents to write into named sub-scopes (e.g., `agent:{id}`) while the workspace-level defaults remain safe for ordinary consumers.

## Endpoints

**POST /kb/search** — Accepts a `SearchRequest` with `query`, optional `scope`, and `limit`. Calls `_kb("search", ...)`. Non-list responses from the binary are coerced to `[]`.

**POST /kb/ingest/text** — Accepts `IngestTextRequest` with `text`, `source`, and optional `scope`. The `source` field is passed through to the binary to tag where the content came from (e.g., `"manual"`, `"confluence"`, `"github"`).

**POST /kb/ingest/url** — Accepts `IngestUrlRequest`. Fetches the URL via the async `_extract_url()` helper first, then passes the extracted text to `_kb("ingest", ...)`. The two-step fetch-then-ingest design keeps the URL fetching in Python (where async HTTP libraries and authentication live) while the indexing happens in the binary.

**POST /kb/lint** — Runs `_kb("lint", ...)` against the scope to surface structural issues in the KB (duplicate articles, empty content, missing metadata, etc.).

**GET /kb/article/{article_id}** — Fetches a single article by ID via the binary's `get` command.

## Auth and License Gating

All endpoints require a valid enterprise license (router-level `require_license` dependency). Individual endpoints additionally require either `kb.read` or `kb.write` actions. The action-based gating means fine-grained permission models can allow read-only KB consumers without write access.

## Error Handling Strategy

Ingest endpoints wrap the `_kb()` call in a try/except and raise a structured `CloudError(500, "kb.ingest_failed", ...)` on failure. This surfaces a machine-readable error code to the client rather than a raw exception string, which matters for frontend error handling. The search and lint endpoints take a softer approach — non-list returns are coerced to `[]` silently — because empty results are a valid outcome, whereas ingest failures are always actionable.

## Known Gaps

- `_extract_url()` is an async function but `_kb()` is synchronous. The ingest/url endpoint awaits the fetch then blocks the event loop during the binary call. Under high concurrency this can stall the async loop.
- There is no per-article update endpoint — the binary's `ingest` command is idempotent by source identifier, so re-ingesting the same source overwrites the article. There is no explicit `PATCH /kb/article/{id}` surface.
- No bulk ingest endpoint exists; clients must call `/ingest/text` or `/ingest/url` once per document.