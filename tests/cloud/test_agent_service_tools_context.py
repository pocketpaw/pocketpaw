"""Toolset assembly + context block helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.chat.agent_service import (
    ScopeContext,
    ScopeKind,
    assemble_toolset,
    build_context_block,
    build_knowledge_context,
)

from pocketpaw.ripple._design import RIPPLE_DESIGN_RULES


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
    """Plain-chat scope must embed the slim inline ripple system prompt
    (~6 core widgets + chat.send loop + UI-FIRST decision rule). The
    full widget catalog now lives behind the get_inline_widget_help
    MCP tool — see test_inline_widget_help_* for that surface."""
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
    # Slim core must be present.
    assert "<ripple>" in block
    assert "ui-spec" in block
    assert '"version": "1.0"' in block
    # Six core widgets named in the catalog.
    for node in ("text", "heading", "stat", "button", "table", "flex"):
        assert node in block, f"core widget {node!r} missing from slim prompt"
    # chat.send loop is still in.
    assert "chat.send" in block
    # The slim prompt MUST point the agent at the tool for the long tail.
    assert "get_inline_widget_help" in block, (
        "slim prompt must teach the agent about the on-demand catalog tool"
    )
    # The full catalog content is GONE from the prompt — verify by
    # checking for content that ONLY appeared in RIPPLE_DESIGN_RULES,
    # not by checking for widget names (the slim prompt names some
    # non-core widgets in the "call the tool for these" pointer).
    assert RIPPLE_DESIGN_RULES not in block, "full RIPPLE_DESIGN_RULES leaked back into the prompt"
    # Sentinel: a known catalog-only phrase that should NOT be in slim.
    assert "# CANONICAL SHAPES" not in block, (
        "catalog content sentinel found in slim prompt — full design rules may have leaked"
    )
    # Slim prompt should be dramatically smaller than the full catalog.
    assert len(block) < len(RIPPLE_DESIGN_RULES), (
        f"slim prompt (chars={len(block)}) should be smaller than the "
        f"full catalog (chars={len(RIPPLE_DESIGN_RULES)})"
    )


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
    from pocketpaw.ripple._inline_core import widget_help

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
    from pocketpaw.ripple._inline_core import widget_help

    assert widget_help() == RIPPLE_DESIGN_RULES
    assert widget_help([]) == RIPPLE_DESIGN_RULES
    assert widget_help([" ", ""]) == RIPPLE_DESIGN_RULES


def test_main_chat_prompt_delegates_pocket_work_not_inlines_it():
    """In plain chat on claude_agent_sdk, the system prompt teaches the agent
    to delegate pocket work to the ``pocket_specialist__create`` MCP tool.
    It must NOT carry the full POCKET_CREATION_PROMPT_MCP — that lives in
    the specialist tool now."""
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )
    block = build_context_block(ctx, backend_name="claude_agent_sdk")
    # Delegation rule present and points at the specialist MCP tool.
    assert "<pocket-delegation>" in block
    assert "pocket_specialist__create" in block
    # Full pocket creation prompt is NOT inlined.
    assert "<list-before-create>" not in block, (
        "full pocket creation prompt leaked into main-chat system prompt — "
        "should be in the pocket_specialist__create MCP tool only"
    )


def test_pocket_delegation_rule_points_at_specialist_mcp_tool():
    """The delegation rule must teach the agent to call the
    ``pocket_specialist__create`` MCP tool. The legacy native-subagent
    Agent-tool path has been removed."""
    from pocketpaw.ripple._pockets import POCKET_DELEGATION_RULE

    assert "pocket_specialist__create" in POCKET_DELEGATION_RULE
    # Legacy native-subagent kwarg shape must be gone.
    assert 'subagent_type="pocket_specialist"' not in POCKET_DELEGATION_RULE
    assert "subagent_type='pocket_specialist'" not in POCKET_DELEGATION_RULE
    # Should NOT reference the abandoned custom MCP tool name either.
    assert "delegate_to_pocket_specialist" not in POCKET_DELEGATION_RULE


def test_pocket_create_branch_also_uses_delegation():
    """Phase 3 regression guard: pocket_create intent must NOT receive
    the full POCKET_CREATION_PROMPT_MCP — the specialist owns that. The
    main agent gets the slim inline prompt + delegation rule, same as
    plain chat. Otherwise the agent would be instructed to call
    create_pocket directly, which is filtered off its allowlist."""
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        intent="pocket_create",
    )
    block = build_context_block(ctx, backend_name="claude_agent_sdk")
    # Slim core prompt and delegation rule are present.
    assert "<ripple>" in block
    assert "<pocket-delegation>" in block
    # The full pocket creation prompt is NOT in the main agent's prompt.
    assert "<list-before-create>" not in block, (
        "POCKET_CREATION_PROMPT_MCP leaked into main agent prompt under "
        "pocket_create intent — should be on the specialist only"
    )


def test_pocket_id_branch_also_uses_delegation():
    """Same regression guard for the pocket_id (interaction) branch.
    Heavy interaction prompt belongs on the specialist; main agent gets
    delegation rule and a <current-pocket> tag for context."""
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="pocket-abc",
    )
    block = build_context_block(ctx, backend_name="claude_agent_sdk")
    assert "<ripple>" in block
    assert "<pocket-delegation>" in block
    # The heavy interaction prompt should NOT be inlined.
    assert "<pocket-workflow>" not in block, (
        "POCKET_INTERACTION_PROMPT_MCP leaked into main agent prompt — "
        "should be on the specialist only"
    )
    # But the active pocket id tag IS present (so the agent knows which
    # pocket to mention when delegating).
    assert '<current-pocket id="pocket-abc"' in block


def test_non_subagent_backend_uses_inline_pocket_prompts():
    """codex_cli, openai_agents, google_adk, etc. don't have a native
    subagent integration. They still get the calling-agent creation
    prompt — which post-Task-11 is the STEP 0 delegate-to-specialist
    block (the CLI specialist tool from Task 10 is universal).
    POCKET_INTERACTION_PROMPT remains inline for pocket_id mode."""
    ctx_create = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        intent="pocket_create",
    )
    block = build_context_block(ctx_create, backend_name="codex_cli")
    # STEP 0 delegate-to-specialist block IS present (sentinel from
    # the CLI creation prompt).
    assert "DELEGATE TO SPECIALIST" in block
    assert "cloud_pocket_specialist_create" in block
    # Subagent-style delegation rule is NOT (subagents aren't a
    # concept on this backend).
    assert "<pocket-delegation>" not in block


def test_non_subagent_backend_pocket_id_inlines_interaction_prompt():
    """Same gate for pocket_id mode on non-subagent backends."""
    ctx_edit = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="pocket-abc",
    )
    block = build_context_block(ctx_edit, backend_name="codex_cli")
    # The pocket id was substituted into the interaction prompt.
    assert "pocket-abc" in block
    # Heavy interaction guidance IS present.
    assert "<pocket-workflow>" in block
    # Delegation rule is NOT.
    assert "<pocket-delegation>" not in block


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
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
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
        "pocketpaw_ee.cloud.agents.knowledge.KnowledgeService.search_context_for_scope",
        AsyncMock(side_effect=RuntimeError("kb down")),
    ):
        out = await build_knowledge_context(ctx, user_message="hello")

    assert "<scope>group g1</scope>" in out
    assert "<knowledge-base>" not in out
