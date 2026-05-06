"""Toolset assembly + context block helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    assemble_toolset,
    build_context_block,
    build_knowledge_context,
)


def _pocket_ctx(specs: list[dict]) -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="p1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=specs,
    )


def test_assemble_toolset_base_only_for_non_pocket():
    ctx = ScopeContext(
        kind=ScopeKind.GROUP,
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )
    base = [{"kind": "builtin", "id": "web_fetch"}]
    assert assemble_toolset(ctx, base=base) == base


def test_assemble_toolset_merges_pocket_tools_dedupes_by_identity():
    base = [{"kind": "builtin", "id": "web_fetch"}]
    extra = [
        {"kind": "builtin", "id": "web_fetch"},  # duplicate — dropped
        {"kind": "mcp", "server": "notion", "name": "search_pages"},
    ]
    ctx = _pocket_ctx(extra)
    merged = assemble_toolset(ctx, base=base)
    assert len(merged) == 2
    assert merged[0] == base[0]
    assert merged[1] == extra[1]


def test_build_context_block_has_scope_and_members():
    ctx = ScopeContext(
        kind=ScopeKind.GROUP,
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )
    block = build_context_block(ctx)
    assert "<scope>group g1</scope>" in block
    assert "u1" in block and "u2" in block


def test_build_context_block_includes_ripple_hint():
    """Agents need to know they can emit ui-spec blocks for inline UI,
    including interactive buttons that drive the conversation loop via
    `chat.send`."""
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )
    block = build_context_block(ctx)
    assert "<ripple>" in block
    assert "ui-spec" in block
    # Sanity-check the canonical shape and the chat-inline node allowlist.
    assert '"version": "1.0"' in block
    for node in ("flex", "grid", "heading", "text", "stat", "chart", "table"):
        assert node in block, f"node type {node!r} missing from Ripple hint"
    # Chart specifics — the agent needs to know all 10 chart kinds + the
    # canonical Ripple shape (props.type), not just the legacy chartType alias.
    for kind in ("bar", "line", "area", "pie", "donut", "candlestick",
                 "sparkline", "heatmap", "gauge", "radar"):
        assert kind in block, f"chart kind {kind!r} missing from Ripple hint"
    # Candlestick data points need the OHLC shape called out.
    assert "open" in block and "close" in block and "high" in block and "low" in block
    # Table specifics — data-of-objects is the preferred shape; columns
    # remain mandatory; variant should be advertised.
    assert "columns" in block
    assert '"variant"' in block or "`variant`" in block
    for v in ("default", "compact", "striped", "minimal"):
        assert v in block, f"table variant {v!r} missing from Ripple hint"
    # Buttons / interactive nodes must NOT be advertised in chat-inline specs.
    assert "button" not in block.lower() or "do not include `button`" in block.lower()


@pytest.mark.asyncio
async def test_build_knowledge_context_includes_workspace_kb_hits_and_file_refs():
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )

    calls: list[tuple[str, str, int]] = []

    async def _fake_search(scope: str, query: str, limit: int = 3) -> str:
        calls.append((scope, query, limit))
        if scope == "workspace:w1":
            return "retrieved snippet for uploaded report"
        return ""

    with patch(
        "ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
        AsyncMock(side_effect=_fake_search),
    ):
        out = await build_knowledge_context(
            ctx,
            user_message="summarize this upload",
            attachments=[
                {
                    "type": "file",
                    "name": "Q4_Report.pdf",
                    "url": "/api/v1/uploads/f1",
                }
            ],
            mentions=[{"type": "file", "id": "f1", "display_name": "Q4_Report.pdf"}],
        )

    assert "<knowledge-base>" in out
    assert "workspace:w1" in out
    assert "retrieved snippet for uploaded report" in out
    assert any("Q4_Report.pdf" in query for _scope, query, _limit in calls)


@pytest.mark.asyncio
async def test_build_knowledge_context_falls_back_to_scope_block_on_kb_failure():
    ctx = ScopeContext(
        kind=ScopeKind.GROUP,
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )

    with patch(
        "ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
        AsyncMock(side_effect=RuntimeError("kb down")),
    ):
        out = await build_knowledge_context(ctx, user_message="hello")

    assert "<scope>group g1</scope>" in out
    assert "<knowledge-base>" not in out
