# test_knowledge_aggregator.py — Unit tests for the workspace KB aggregator.
# Created: 2026-04-19 (Cluster C / PR1) — Pins the aggregator's merge and
# scope-filter contract before it becomes the /knowledge/articles endpoint's
# only test harness. See ee/cloud/kb/workspace_aggregator.py for the code
# under test.
"""Unit tests for the workspace-level KB aggregator.

These tests operate on the pure-Python aggregator module — no FastAPI, no
Beanie, no subprocess. They lock in:

- Merge: workspace + every agent scope feed into the result.
- Filter: ``agent_filter='workspace'`` drops all agent scopes;
  ``agent_filter='<agent_id>'`` keeps only that agent's scope.
- Dedup: same (scope, id) pair collapses to one row.
- Sort: newest-first on ``updated_at`` with ``None`` last.
- Resilience: a kb binary that returns a non-list for one scope does not
  sink the aggregator — that scope is skipped and the rest flow.
"""

from __future__ import annotations

import pytest

from ee.cloud.kb.workspace_aggregator import (
    AggregatedArticle,
    aggregate_workspace_articles,
)


def _make_rows(count: int, prefix: str, base_ts: str = "2026-04-19T09:00:00Z") -> list[dict]:
    return [
        {
            "id": f"{prefix}-{i}",
            "title": f"{prefix} article {i}",
            "source": f"{prefix}-source-{i}",
            "updated_at": f"2026-04-{10 + i:02d}T12:00:00Z",
        }
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_merges_workspace_and_agent_scopes():
    ws_rows = _make_rows(2, "ws")
    a1_rows = _make_rows(3, "a1")
    a2_rows = _make_rows(1, "a2")

    def fake_kb_list(scope: str):
        return {
            "workspace:ws1": ws_rows,
            "agent:a1": a1_rows,
            "agent:a2": a2_rows,
        }.get(scope, [])

    articles = await aggregate_workspace_articles(
        workspace_id="ws1",
        agent_ids=["a1", "a2"],
        kb_list=fake_kb_list,
    )

    assert len(articles) == 2 + 3 + 1
    scopes = {a.scope for a in articles}
    assert scopes == {"workspace:ws1", "agent:a1", "agent:a2"}


@pytest.mark.asyncio
async def test_agent_filter_workspace_keyword_drops_agents():
    ws_rows = _make_rows(2, "ws")
    a1_rows = _make_rows(3, "a1")

    def fake_kb_list(scope: str):
        return {"workspace:ws1": ws_rows, "agent:a1": a1_rows}.get(scope, [])

    articles = await aggregate_workspace_articles(
        workspace_id="ws1",
        agent_ids=["a1"],
        kb_list=fake_kb_list,
        agent_filter="workspace",
    )

    assert len(articles) == 2
    assert all(a.scope == "workspace:ws1" for a in articles)


@pytest.mark.asyncio
async def test_agent_filter_specific_agent_isolates_scope():
    a1_rows = _make_rows(2, "a1")
    a2_rows = _make_rows(2, "a2")

    def fake_kb_list(scope: str):
        return {"agent:a1": a1_rows, "agent:a2": a2_rows}.get(scope, [])

    articles = await aggregate_workspace_articles(
        workspace_id="ws1",
        agent_ids=["a1", "a2"],
        kb_list=fake_kb_list,
        agent_filter="a2",
    )

    assert len(articles) == 2
    assert all(a.agent_id == "a2" for a in articles)


@pytest.mark.asyncio
async def test_dedupe_by_scope_and_id():
    dupes = _make_rows(1, "ws") * 2
    ws_rows = dupes + _make_rows(1, "ws")

    def fake_kb_list(scope: str):
        return ws_rows if scope == "workspace:ws1" else []

    articles = await aggregate_workspace_articles(
        workspace_id="ws1",
        agent_ids=[],
        kb_list=fake_kb_list,
    )

    # ws-0 appeared 3 times (twice via dupes, once via the third row),
    # ws-1 does not exist because _make_rows(1, "ws") only makes ws-0.
    # So we should end with exactly one row.
    assert len(articles) == 1
    assert articles[0].id == "ws-0"


@pytest.mark.asyncio
async def test_newest_first_ordering():
    rows = [
        {"id": "old", "title": "old", "updated_at": "2026-04-01T00:00:00Z"},
        {"id": "new", "title": "new", "updated_at": "2026-04-19T00:00:00Z"},
        {"id": "mid", "title": "mid", "updated_at": "2026-04-10T00:00:00Z"},
    ]

    def fake_kb_list(scope: str):
        return rows if scope == "workspace:ws1" else []

    articles = await aggregate_workspace_articles(
        workspace_id="ws1",
        agent_ids=[],
        kb_list=fake_kb_list,
    )
    assert [a.id for a in articles] == ["new", "mid", "old"]


@pytest.mark.asyncio
async def test_non_list_rows_are_skipped():
    def fake_kb_list(scope: str):
        # Workspace scope returns a string (kb binary degenerate case), agent
        # scope returns a real list. Only the agent rows should land.
        if scope == "workspace:ws1":
            return "oops not a list"  # type: ignore[return-value]
        return _make_rows(2, "agent-a")

    articles = await aggregate_workspace_articles(
        workspace_id="ws1",
        agent_ids=["a"],
        kb_list=fake_kb_list,
    )
    assert len(articles) == 2
    assert all(a.agent_id == "a" for a in articles)


@pytest.mark.asyncio
async def test_rows_missing_id_are_dropped():
    rows = [
        {"title": "no-id"},
        {"id": "keep", "title": "keep"},
        {"id": "", "title": "empty-id"},
    ]

    def fake_kb_list(scope: str):
        return rows if scope == "workspace:ws1" else []

    articles = await aggregate_workspace_articles(
        workspace_id="ws1",
        agent_ids=[],
        kb_list=fake_kb_list,
    )
    assert [a.id for a in articles] == ["keep"]


@pytest.mark.asyncio
async def test_async_kb_list_is_awaited():
    async def async_kb_list(scope: str):
        if scope == "workspace:ws1":
            return _make_rows(1, "ws")
        return []

    articles = await aggregate_workspace_articles(
        workspace_id="ws1",
        agent_ids=[],
        kb_list=async_kb_list,
    )
    assert len(articles) == 1
    assert articles[0].scope == "workspace:ws1"


def test_aggregated_article_to_dict_shape():
    article = AggregatedArticle(
        id="a1",
        title="Title",
        source="src",
        scope="workspace:ws1",
        agent_id=None,
        updated_at="2026-04-19T00:00:00Z",
    )
    assert article.to_dict() == {
        "id": "a1",
        "title": "Title",
        "source": "src",
        "scope": "workspace:ws1",
        "agent_id": None,
        "updated_at": "2026-04-19T00:00:00Z",
    }
