"""Toolset assembly + context block helpers."""

from __future__ import annotations

from ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    assemble_toolset,
    build_context_block,
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
    # canonical Ripple shape (props.type), not the legacy chartType alias.
    for kind in (
        "bar",
        "line",
        "area",
        "pie",
        "donut",
        "candlestick",
        "sparkline",
        "heatmap",
        "gauge",
        "radar",
    ):
        assert kind in block, f"chart kind {kind!r} missing from Ripple hint"
    # Candlestick data points need the OHLC shape called out.
    assert "open" in block and "close" in block and "high" in block and "low" in block
    # Table specifics — data-of-objects is the preferred shape; columns
    # remain mandatory; variant should be advertised.
    assert "columns" in block
    assert '"variant"' in block or "`variant`" in block
    for v in ("default", "compact", "striped", "minimal"):
        assert v in block, f"table variant {v!r} missing from Ripple hint"
    # Driven-UI loop — chat.send round-trip must be documented; clicks
    # round-trip as the user's next message.
    assert "chat.send" in block, "chat.send target missing from Ripple hint"
    assert "on_click" in block, "on_click handler missing from Ripple hint"
    assert "emit" in block, "emit action missing from Ripple hint"


def test_build_context_block_has_stable_static_prefix():
    """Anthropic prompt caching keys off prefix. The static ripple/pocket
    portion of the system prompt must come BEFORE per-turn dynamic tags
    (<scope>, <participants>, KB context) so it caches across turns."""
    a = build_context_block(
        ScopeContext(
            kind=ScopeKind.GROUP,
            scope_id="g1",
            session_id="s1",
            workspace_id="w1",
            user_id="u1",
            members=["u1"],
            target_agent_id="a1",
            agent_ids_in_scope=["a1"],
        )
    )
    b = build_context_block(
        ScopeContext(
            kind=ScopeKind.GROUP,
            scope_id="g2",
            session_id="s2",
            workspace_id="w1",
            user_id="u2",
            members=["u2", "u3"],
            target_agent_id="a1",
            agent_ids_in_scope=["a1"],
        )
    )
    # Must be at least as long as the longest plausible dynamic preamble
    # (scope + participants tags are ~60 chars). 1000 is a conservative
    # floor; the full static block is several thousand chars.
    static_prefix_floor = 1000
    assert a[:static_prefix_floor] == b[:static_prefix_floor], (
        "Static prefix differs across builds — prompt caching will miss. "
        f"a starts: {a[:200]!r}; b starts: {b[:200]!r}"
    )
    assert "<scope>" in a and "<participants>" in a


def test_inline_widget_help_returns_catalog_for_known_types():
    from ee.ripple._design import RIPPLE_DESIGN_RULES
    from ee.ripple._inline_core import widget_help

    out = widget_help(["chart"])
    # Some chart specifics must appear when 'chart' is asked for.
    assert "chart" in out.lower()
    assert any(kind in out for kind in ("bar", "line", "pie")), (
        "chart kinds must come back when caller asks for chart"
    )
    # Canonical chart shape from the CANONICAL SHAPES section must be present
    # (this is what the agent actually copies into its spec).
    assert '"type": "chart"' in out or "type: chart" in out.lower(), (
        "canonical chart schema must be included in chart-specific help"
    )
    # Toolkit / expression section is always pulled in.
    assert "{state." in out, "expression toolkit must always be included"
    # And it must be a subset, not the entire catalog (proving the splitter
    # actually filtered something out).
    assert len(out) < len(RIPPLE_DESIGN_RULES), (
        "filtered help should be smaller than the full catalog"
    )


def test_inline_widget_help_no_args_returns_full_catalog():
    from ee.ripple._design import RIPPLE_DESIGN_RULES
    from ee.ripple._inline_core import widget_help

    assert widget_help() == RIPPLE_DESIGN_RULES
    assert widget_help([]) == RIPPLE_DESIGN_RULES
    assert widget_help([" ", ""]) == RIPPLE_DESIGN_RULES
