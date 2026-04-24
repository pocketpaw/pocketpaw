---
{
  "title": "Workspace-Level Knowledge Browser Router",
  "summary": "A FastAPI router mounted at `/api/v1/knowledge/*` that fans out KB article listings across every agent in a workspace. It exists as a separate router from `/api/v1/kb/*` because it has different aggregation semantics — workspace-wide rollup rather than single-scope access.",
  "concepts": [
    "FastAPI router",
    "knowledge base",
    "workspace aggregation",
    "tenant isolation",
    "kb.read permission",
    "lazy import",
    "kb-go binary",
    "multi-tenant",
    "agent scoping",
    "AggregatedArticle",
    "enterprise license",
    "require_action_any_workspace"
  ],
  "categories": [
    "knowledge-base",
    "API routing",
    "enterprise",
    "multi-tenancy"
  ],
  "source_docs": [
    "6f64dcb311215f66"
  ],
  "backlinks": null,
  "word_count": 597,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `knowledge_router.py` module implements a FastAPI router at the prefix `/knowledge`, mounted as part of the enterprise cloud layer. Its sole endpoint, `GET /articles`, surfaces a unified view of all knowledge base articles that exist within a workspace — both the workspace-scoped KB and the per-agent KBs for every agent that belongs to that workspace.

This router was introduced alongside `workspace_aggregator.py` in Cluster C / PR 1 to power the workspace KB browser UI. It lives under `/api/v1/knowledge/*` rather than being added to the existing `/api/v1/kb/*` router intentionally: the two routers have fundamentally different scoping semantics. The `/kb/` router operates on a single explicit scope at a time, whereas this router always fans out across all scopes in the workspace.

## Auth Model and Tenant Isolation

All routes require a valid enterprise license via `require_license` as a router-level dependency. Individual endpoints additionally require the `kb.read` action via `require_action_any_workspace`. This dependency pins the caller to their active workspace — it does not permit cross-workspace access.

The `workspace_id` query parameter is intentionally defensive: if a caller passes a `workspace_id` that differs from their resolved active workspace, the endpoint returns HTTP 403 rather than serving a different workspace's data. This prevents a class of multi-tenant data leakage where a client-controlled query parameter could redirect reads to a workspace the caller belongs to but is not currently acting within.

Similarly, when `agent_id` is provided and does not resolve to a known agent within the active workspace, the endpoint returns an empty list rather than a 404 or an error. This avoids leaking the existence of agents in other workspaces to an authenticated caller who happens to guess a valid agent ID.

## Lazy Import Pattern for Testability

The `_list_workspace_agent_ids` helper uses a lazy import of the `Agent` Beanie document. The import is deferred inside the function body so that tests can patch this function without triggering Beanie/MongoDB initialization. This is a pragmatic workaround for the global-state side effects of Beanie's document registry — importing a document model at module level forces initialization of the ODM during the test collection phase, which breaks unit tests that don't need a live database.

The `_call_kb_list` helper follows a similar pattern, importing `_kb` lazily so the kb-go binary bridge is not loaded at import time.

## Error Handling in kb-go Calls

When the kb-go binary raises an exception for a given scope, `_call_kb_list` catches it broadly, logs a debug-level message, and returns an empty list. The broad catch (`BLE001` is explicitly suppressed) is intentional: the kb binary can fail for transient reasons (process startup latency, empty index files, filesystem permissions) and it would be wrong to bubble those failures up as HTTP 500s when the workspace KB browser is loading. Graceful degradation — showing an empty list for a failed scope — is preferable to a broken page.

## Request/Response Shape

The response is a plain dict with three keys:
- `articles` — serialized `AggregatedArticle` dicts from `workspace_aggregator`
- `total` — article count
- `agent_ids` — the resolved list of agent IDs for the workspace (useful for the UI's agent filter dropdown)

## Known Gaps

- The `agent_ids` list is always resolved even when `agent_id` is set to `"workspace"`, which means an unnecessary MongoDB query is always made. This is low overhead but could be short-circuited.
- No pagination support — large workspaces with many agents and articles will return the full result set in one response.
- The `_call_kb_list` function is synchronous even though the router is async; it blocks the event loop for the duration of the subprocess call to kb-go.