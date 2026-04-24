---
{
  "title": "Knowledge Router: Multi-Scope Listing, Filtering, and Cross-Workspace Isolation Tests",
  "summary": "This integration test suite exercises the `/api/v1/knowledge/articles` FastAPI endpoint end-to-end, verifying that it merges workspace and agent scopes, supports `agent_id` filtering, rejects cross-workspace queries with HTTP 403, and returns empty results (not a data leak) for unknown agent IDs.",
  "concepts": [
    "knowledge router",
    "GET /api/v1/knowledge/articles",
    "cross-workspace isolation",
    "tenant isolation",
    "agent_id filter",
    "workspace filter",
    "403 guard",
    "monkeypatch",
    "RBAC bypass",
    "empty list security"
  ],
  "categories": [
    "testing",
    "security",
    "knowledge management",
    "API",
    "test"
  ],
  "source_docs": [
    "d2cac31ca1672aaa"
  ],
  "backlinks": null,
  "word_count": 451,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The knowledge router exposes a single `GET /api/v1/knowledge/articles` endpoint that aggregates knowledge base articles across a workspace and its agents. This test file exercises that endpoint end-to-end through FastAPI's `TestClient`, with the kb binary and agent ID lookup monkeypatched to avoid external dependencies.

## Fixture Design

The `client` fixture monkeypatches two module-level functions in `knowledge_router_module`:

- **`_call_kb_list`** — The function that queries the kb binary for a given scope. Replaced with a dict lookup into `fake_rows`.
- **`_list_workspace_agent_ids`** — The async function that fetches agent IDs for a workspace from the database. Replaced with a hardcoded list `["agent-1", "agent-2"]` for `ws-alpha`.

Auth and RBAC are bypassed by overriding `current_active_user` with a fake user that owns `ws-alpha`, and by replacing the `check_workspace_action` guard with a no-op. The `require_license` dependency is also overridden.

This approach tests the routing, aggregation, and security logic without a database, making tests fast and deterministic.

## Test Cases

**`test_list_workspace_articles_unions_scopes`** — The baseline: querying without any filter returns all 4 articles (2 workspace + 1 agent-1 + 1 agent-2). The response body includes `total`, `articles`, and `agent_ids` fields. All three scopes are represented.

**`test_filter_by_workspace_keyword`** — `?agent_id=workspace` returns only the 2 workspace-scoped articles. All returned articles have `scope == "workspace:ws-alpha"`. This is the same filter as in the aggregator unit tests, confirmed to work end-to-end through HTTP.

**`test_filter_by_agent_id`** — `?agent_id=agent-1` returns the 1 article from `agent:agent-1`. The article's `agent_id` field is `"agent-1"`.

**`test_cross_workspace_id_rejected`** — `?workspace_id=ws-beta` returns HTTP 403 with a `"must match"` detail message. The route only serves the authenticated user's active workspace. This is a critical tenant isolation control: a user cannot query another workspace's knowledge by guessing a workspace ID.

**`test_unknown_agent_returns_empty_not_leak`** — `?agent_id=some-other-agent` returns HTTP 200 with `total: 0` and an empty `articles` list. Crucially, it does NOT return 404 or an error that would reveal whether `some-other-agent` exists. The response includes the real agent IDs for the workspace, so the caller can see which agents are valid — but learns nothing about agents in other workspaces.

## Security Design

The cross-workspace block and the unknown-agent empty-return are both security-motivated:

- **403 on workspace_id mismatch** — Prevents a user from crafting a request to read another tenant's knowledge base by guessing workspace IDs.
- **Empty list for unknown agents** — Prevents a user from probing agent existence in other workspaces. If the route returned 404 for agents not in the current workspace, an attacker could enumerate agents by scanning IDs.

## Known Gaps

- **No test for `kb.read` RBAC failure** — The fixture bypasses RBAC entirely. There is no test for what happens when `check_workspace_action` raises `Forbidden`.
- **No pagination test** — The endpoint likely supports `limit`/`offset` or cursor pagination, but this is not exercised here.
