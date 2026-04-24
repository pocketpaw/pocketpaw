---
{
  "title": "Workspace Knowledge Aggregator — Cross-Agent KB Merge",
  "summary": "A pure-Python aggregation layer that merges the workspace-scoped KB with every per-agent KB inside a workspace into a single sorted, deduplicated article stream. Designed to be thin, injectable, and unit-testable without a live database or kb binary.",
  "concepts": [
    "workspace aggregation",
    "kb-go binary",
    "AggregatedArticle",
    "scope merging",
    "deduplication",
    "dependency injection",
    "per-agent KB",
    "workspace KB",
    "sorting",
    "frozen dataclass",
    "agent filter"
  ],
  "categories": [
    "knowledge-base",
    "aggregation",
    "architecture"
  ],
  "source_docs": [
    "a865dfc5168532af"
  ],
  "backlinks": null,
  "word_count": 608,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`workspace_aggregator.py` solves a structural gap in the KB architecture: the existing `/api/v1/kb/articles` endpoint is single-scope — it only shows articles at the `workspace:{id}` level. Per-agent knowledge, which is stored at `agent:{agent_id}` scopes, was previously only visible through per-agent search. The workspace KB browser UI needed a unified view across all scopes in the workspace.

Rather than extending the existing KB router with aggregation logic, this module encapsulates the merge into a standalone, stateless function so the route handler stays thin and the merge logic is unit-testable with no external dependencies.

## AggregatedArticle Dataclass

```python
@dataclass(frozen=True)
class AggregatedArticle:
    id: str
    title: str
    source: str
    scope: str       # "workspace:{id}" or "agent:{agent_id}"
    agent_id: str | None
    updated_at: str | None
```

Using a frozen dataclass rather than a Pydantic model is intentional — `AggregatedArticle` is an internal value type used only within the aggregation pipeline, not a serialization boundary. It becomes a dict at the HTTP layer via `to_dict()`. The `frozen=True` flag makes instances hashable, which is used indirectly by the deduplication logic.

## Normalization: `_row_to_article`

The kb-go binary's `list` command returns JSON objects whose key names vary slightly between versions (`id` vs `article_id` vs `_id`, `title` vs `name`, `updated_at` vs `updatedAt` vs `modified`). The `_row_to_article` helper accepts a range of common key names so the aggregator survives minor binary version drift without a code change. If none of the expected ID keys are present, the row is silently dropped — an article without an ID cannot be usefully displayed.

## Deduplication: `_dedupe`

The kb binary occasionally returns the same row twice under boundary conditions (e.g., when a scope covers both a parent and child namespace that overlap). The `_dedupe` function uses a `(scope, id)` composite key to drop duplicates. The key is composite rather than just `id` because the same article ID could theoretically exist in two different scopes without being the same article.

## Sorting: `_sort_newest_first`

Articles are sorted by `updated_at` descending, with `None` values placed last. The sort is stable on ties, preserving the order in which scopes were enumerated (workspace first, then agents in ID order). This means workspace articles appear before agent articles when both lack timestamps.

## Dependency Injection via `kb_list`

`aggregate_workspace_articles` accepts a `kb_list` callable parameter instead of calling the binary directly:

```python
async def aggregate_workspace_articles(
    *,
    workspace_id: str,
    agent_ids: list[str],
    kb_list: Callable[[str], Awaitable[list[Any]] | list[Any]],
    agent_filter: str | None = None,
) -> list[AggregatedArticle]:
```

The `kb_list` callable can return either a coroutine or a plain list — the function detects which via `hasattr(result, "__await__")` and awaits accordingly. This allows both async and sync implementations to be passed in without wrapper boilerplate.

This design makes unit testing straightforward: pass in a dictionary lookup or lambda instead of the real `_call_kb_list`. The real route handler passes `_call_kb_list` from `knowledge_router.py`.

## Agent Filter Logic

When `agent_filter="workspace"`, only the workspace scope is enumerated. When `agent_filter=None`, all scopes (workspace + all agents) are included. When set to a specific agent ID, only that agent's scope is included. This logic is centralized here rather than in the router so the filtering behavior is easy to test in isolation.

## Known Gaps

- The `_sort_newest_first` function has a subtle bug candidate: it falls through to a `sorted(...)` call that sorts ascending on `updated_at` when the `articles` list is empty, which would be a no-op but may surprise readers.
- No pagination — the full merged list is returned. For workspaces with many agents and large indices this could be slow.
- `kb_list` is called serially per scope (one at a time in a loop), not concurrently. Parallel calls would reduce latency proportionally to the number of agents.