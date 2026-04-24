---
{
  "title": "Workspace KB Aggregator: Merge, Filter, Dedup, Sort, and Resilience Tests",
  "summary": "This test suite validates the `ee.cloud.kb.workspace_aggregator` module, which merges knowledge base articles from workspace and agent scopes into a single sorted, deduplicated list. It tests merge logic, agent filtering, deduplication by (scope, id), newest-first ordering, resilience when a scope returns malformed data, and the `AggregatedArticle.to_dict()` shape.",
  "concepts": [
    "workspace aggregator",
    "knowledge base",
    "aggregate_workspace_articles",
    "AggregatedArticle",
    "scope filtering",
    "deduplication",
    "newest-first",
    "resilience",
    "agent filter",
    "async kb_list"
  ],
  "categories": [
    "testing",
    "knowledge management",
    "data aggregation",
    "test"
  ],
  "source_docs": [
    "a408176b95d296d6"
  ],
  "backlinks": null,
  "word_count": 488,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw workspaces contain multiple knowledge bases: one for the workspace itself and one per agent. The `aggregate_workspace_articles` function collects articles from all these scopes, merges them into a unified view, and applies filters and deduplication. This makes it possible to browse all knowledge in a workspace through a single API endpoint.

## Why This Exists

Without an aggregator, the client would have to make N+1 requests (one per scope), merge the results itself, and handle deduplication. The aggregator moves this logic to the server, where it has access to the full list of agent IDs for a workspace and can make all kb queries concurrently.

## Core Contracts Pinned

**Merge (`test_merges_workspace_and_agent_scopes`)** — `aggregate_workspace_articles` queries `workspace:<workspace_id>` and `agent:<agent_id>` for every agent in the workspace. The total article count equals the sum across all scopes. The returned `scope` field on each article identifies its origin.

**Workspace filter (`test_agent_filter_workspace_keyword_drops_agents`)** — Passing `agent_filter="workspace"` drops all agent-scoped articles and returns only workspace articles. This supports a UI "workspace knowledge" filter.

**Agent filter (`test_agent_filter_specific_agent_isolates_scope`)** — Passing `agent_filter="<agent_id>"` returns only that agent's articles. This supports per-agent knowledge browsing.

**Deduplication (`test_dedupe_by_scope_and_id`)** — The same `(scope, id)` pair appearing multiple times collapses to one row. This matters because kb binaries can return duplicates when indexes are rebuilt or when the same article is indexed from multiple sources.

**Newest-first ordering (`test_newest_first_ordering`)** — Articles are sorted by `updated_at` descending. Articles with `None` for `updated_at` sort last. This matches the expected UX: recently updated articles appear at the top.

**Resilience (`test_non_list_rows_are_skipped`)** — If a kb binary returns a non-list for one scope (e.g., returns `None` or a dict due to a bug), that scope is skipped and the other scopes still contribute their articles. A single broken scope does not take down the aggregator.

**Missing ID drop (`test_rows_missing_id_are_dropped`)** — Articles without an `id` field are silently dropped. This prevents null entries in the merged list that could cause downstream errors.

**Async kb_list (`test_async_kb_list_is_awaited`)** — The `kb_list` parameter can be an async callable. The aggregator correctly `await`s it, supporting both sync and async kb backends.

**Article shape (`test_aggregated_article_to_dict_shape`)** — `AggregatedArticle.to_dict()` returns a dict with `id`, `scope`, `agent_id`, `title`, `updated_at`. This is the shape the REST endpoint serializes.

## Fake KB List Pattern

Tests inject a `fake_kb_list` callable instead of calling the real kb binary. This makes the aggregator logic testable without a running kb process:

```python
def fake_kb_list(scope: str):
    return {"workspace:ws1": ws_rows, "agent:a1": a1_rows}.get(scope, [])
```

The aggregator calls this with each scope string, making it easy to simulate any combination of responses.

## Known Gaps

- **No pagination test** — The aggregator returns all matching articles with no limit. If a workspace has many agents with large knowledge bases, the merged list could be very large. Pagination support is not tested.
- **No concurrency test** — The aggregator likely queries all scopes concurrently via `asyncio.gather`. There is no test for what happens if one scope's query is slow.
