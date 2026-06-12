# test_agent_service_tools_context.py — Toolset assembly + context block helpers.
#
# Modified: 2026-06-05 (feat/surface-profile-bias-kill) — Added two surface-gate
# tests for the "ripple-default bias" RED phase:
#   * test_sites_surface_omits_ripple_block (RED driver) — proves that a /sites
#     surface ScopeContext must NOT carry INLINE_RIPPLE_SYSTEM_PROMPT. Fails
#     today because build_behavior_instructions never gates the ripple block on
#     surface, so the agent on /sites still gets the full "default to ui-spec"
#     LAW and writes ripple instead of hand-authored Svelte.
#   * test_non_sites_surface_keeps_ripple_block (no-regression guard) — locks in
#     current behavior for non-sites / surface-less scopes so the fix doesn't
#     over-omit. Passes today.
#
# Modified: 2026-06-05 (feat/sites-svelte-engine) — make the SITES gate
# META-AWARE. PR 1's resolver omitted ripple for ALL /sites, but only the
# svelte-CREATE mode should. These tests pin the three modes through
# build_behavior_instructions:
#   * test_sites_svelte_create_omits_ripple_block (guard) — engine="svelte",
#     no pocket_id → ripple block ABSENT (hand-authored Svelte). Passes today.
#   * test_sites_ripple_create_keeps_ripple_block (RED driver) — engine None/
#     "ripple", no pocket_id → ripple block PRESENT (authors a ripple page).
#     Fails today: PR 1 wrongly omits it for every /sites meta.
#   * test_sites_refine_keeps_ripple_block (RED driver) — pocket_id set → ripple
#     block PRESENT (edits an existing ripple spec). Fails today, same reason.
# The existing ``test_sites_surface_omits_ripple_block`` used a bare
# SurfaceMeta() (now the ripple-create mode that KEEPS ripple) so it encoded the
# over-reach — it is updated to construct a svelte-engine surface so it stays a
# valid svelte-omit assertion. No test in this file asserts "all SITES omit
# ripple" any more. ``_sites_surface_ctx`` now takes an optional ``meta``.
#
# Modified: 2026-06-12 (fix/pocket-anchored-chat-context) — added the
# ``<pocket-summary>`` block suite. A pocket-anchored chat on ANY pocket type
# now gets an orientation block (name, description, template/pattern, ui node
# types, state keys, sources, legacy widgets count + the "call get_pocket"
# hint) rendered from ``ScopeContext.pocket_summary``. Tests pin: non-home
# anchored pocket gets the block; home keeps HOME_POCKET_PROMPT + backend
# summary AND gains the block additively (byte-compatible prefix); a pocket
# with no rippleSpec degrades gracefully; un-anchored scopes are unchanged.
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
from pocketpaw_ee.cloud.surface import (
    SurfaceContext,
    SurfaceKind,
    SurfaceMeta,
    resolve_profile,
)

from pocketpaw.ripple import INLINE_RIPPLE_SYSTEM_PROMPT
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
    # The interaction prompt IS inlined — post-#1163 the calling-agent
    # interaction prompt is the slim delegation flow (<pocket-interaction>),
    # while the heavy <pocket-workflow> block lives on the edit specialist
    # only (see test_subagent_backend... above, line ~275).
    assert "<pocket-interaction>" in block
    assert "<pocket-workflow>" not in block
    # The subagent-style delegation rule is NOT (codex_cli has no
    # native subagent concept).
    assert "<pocket-delegation>" not in block


def test_home_pocket_scope_injects_home_prompt():
    """When the resolved scope's pocket has ``type == "home"``, the
    behavior instructions must carry HOME_POCKET_PROMPT so the agent
    knows it is on the user's home surface."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="home-pocket-1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="home-pocket-1",
        pocket_type="home",
    )
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    assert "<home-pocket>" in block, (
        "HOME_POCKET_PROMPT must be injected for a type='home' pocket scope"
    )


def test_non_home_pocket_scope_omits_home_prompt():
    """A normal (non-home) pocket scope must NOT receive HOME_POCKET_PROMPT."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="pocket-abc",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="pocket-abc",
        pocket_type="custom",
    )
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    assert "<home-pocket>" not in block, "HOME_POCKET_PROMPT leaked into a non-home pocket scope"


def test_home_pocket_prompt_injected_for_cli_backend_too():
    """The home-pocket case is backend-agnostic — a CLI backend in a
    type='home' scope also gets HOME_POCKET_PROMPT."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="home-pocket-1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="home-pocket-1",
        pocket_type="home",
    )
    block = build_behavior_instructions(ctx, backend_name="codex_cli")
    assert "<home-pocket>" in block


def test_home_pocket_scope_omits_specialist_delegation_rule():
    """The home agent mutates widgets directly via ``add_widget`` — it does
    NOT delegate to the pocket specialist. ``POCKET_DELEGATION_RULE`` and
    ``HOME_POCKET_PROMPT`` contradict each other (one says "never call
    add_widget, delegate"; the other says "call add_widget"). For a
    type='home' scope on an MCP backend the delegation rule must be dropped
    so the agent gets exactly one consistent widget-creation instruction."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="home-pocket-1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="home-pocket-1",
        pocket_type="home",
    )
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    # The home prompt is present...
    assert "<home-pocket>" in block
    # ...and the contradicting delegation rule is NOT.
    assert "<pocket-delegation>" not in block, (
        "POCKET_DELEGATION_RULE contradicts HOME_POCKET_PROMPT — it must "
        "not be emitted for a type='home' scope"
    )


def test_non_home_pocket_scope_keeps_specialist_delegation_rule():
    """A normal (non-home) MCP pocket scope still gets the delegation rule —
    the home-only drop must not regress ordinary pocket chats."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="pocket-abc",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="pocket-abc",
        pocket_type="custom",
    )
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    assert "<pocket-delegation>" in block


# ---------------------------------------------------------------------------
# Surface gate — the "ripple-default bias" (feat/surface-profile-bias-kill).
#
# build_behavior_instructions injects the full ~20k-char
# INLINE_RIPPLE_SYSTEM_PROMPT ("default to ui-spec / use the widget" LAW) on
# every turn, gated only on backend + pocket_type, NEVER on surface. On the
# /sites surface the user is describe-to-creating a hand-authored Svelte Paw
# Site, so the ripple LAW is actively wrong — it biases the agent toward
# emitting a ui-spec instead of writing Svelte. The fix adds a
# SurfaceProfile-driven branch that OMITS the ripple block when the surface is
# SITES. These two tests encode that desired behavior.
# ---------------------------------------------------------------------------


def _sites_surface_ctx(meta: SurfaceMeta | None = None) -> ScopeContext:
    """A SESSION scope whose resolved surface is /sites.

    Surface arrives ONLY via the optional ``surface_context`` field — there is
    no ``surface_kind`` shortcut on ScopeContext. Tenancy (workspace_id,
    user_id) is required on SurfaceContext at construction per the entity
    rules, and ``meta`` / ``preamble`` are required positional-ish fields, so
    populate them explicitly.

    ``meta`` selects the /sites mode (feat/sites-svelte-engine):
    ``SurfaceMeta(engine="svelte")`` is svelte-create (ripple omitted),
    ``SurfaceMeta()`` / ``SurfaceMeta(engine="ripple")`` is ripple-create
    (ripple kept), ``SurfaceMeta(pocket_id=...)`` is refine (ripple kept).
    Defaults to svelte-create so a bare call is the ripple-OMIT case.

    entity-rooms chunk ①: ``build_behavior_instructions`` now reads the
    PRE-RESOLVED ``ctx.resolved_profile`` (the run-driver resolves it once),
    NOT ``surface_context`` directly. Mirror the run-driver here by also
    stamping ``resolved_profile`` from the pure ``resolve_profile`` lookup —
    the no-entity (no pocket override) case, where the resolved profile is just
    the surface base.
    """
    resolved_meta = meta if meta is not None else SurfaceMeta(engine="svelte")
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        surface_context=SurfaceContext(
            workspace_id="w1",
            user_id="u1",
            kind=SurfaceKind.SITES,
            meta=resolved_meta,
            preamble="",
        ),
        resolved_profile=resolve_profile(SurfaceKind.SITES, resolved_meta),
    )


def test_sites_surface_omits_ripple_block():
    """RED driver: on the /sites SVELTE-CREATE surface the agent is
    hand-authoring a Svelte Paw Site, so it must NOT receive
    INLINE_RIPPLE_SYSTEM_PROMPT (the "default to ui-spec / use the widget" LAW).

    NOTE (feat/sites-svelte-engine): this test now pins the svelte-create mode
    explicitly (``engine="svelte"``). Its PR-1 form used a bare ``SurfaceMeta()``
    — which now resolves to the ripple-create mode that KEEPS ripple — so it had
    become the over-reach ("all /sites omit ripple"). Pinned to svelte so it
    stays a valid svelte-omit assertion alongside the new ripple-create /
    refine "keep" tests below."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    block = build_behavior_instructions(
        _sites_surface_ctx(SurfaceMeta(engine="svelte")), backend_name="claude_agent_sdk"
    )
    # The whole inline ripple prompt must be gone.
    assert INLINE_RIPPLE_SYSTEM_PROMPT not in block, (
        "INLINE_RIPPLE_SYSTEM_PROMPT must be omitted on the /sites svelte-create "
        "surface — its 'default to ui-spec' LAW biases the agent away from "
        "hand-authored Svelte"
    )
    # A couple of distinctive ripple phrases must also be absent, so a future
    # refactor that splits the block can't silently leak the LAW back in.
    assert "<ripple>" not in block, "the <ripple> framing tag leaked onto /sites svelte-create"
    assert "Default to ui-spec whenever the answer has structure" not in block, (
        "the ripple 'default to ui-spec' decision rule leaked onto /sites svelte-create"
    )


def test_non_sites_surface_keeps_ripple_block():
    """No-regression guard (NOT a RED driver): a non-sites surface — and a
    surface-less scope — must KEEP INLINE_RIPPLE_SYSTEM_PROMPT. This locks in
    today's behavior so the SITES omission fix doesn't over-omit and strip
    ripple from ordinary chat surfaces. Passes today."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    # A non-sites resolved surface (the /pockets index) keeps ripple.
    pockets_ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        surface_context=SurfaceContext(
            workspace_id="w1",
            user_id="u1",
            kind=SurfaceKind.POCKETS_LIST,
            meta=SurfaceMeta(),
            preamble="",
        ),
        resolved_profile=resolve_profile(SurfaceKind.POCKETS_LIST, SurfaceMeta()),
    )
    block = build_behavior_instructions(pockets_ctx, backend_name="claude_agent_sdk")
    assert INLINE_RIPPLE_SYSTEM_PROMPT in block, (
        "a non-sites surface must keep INLINE_RIPPLE_SYSTEM_PROMPT — the SITES "
        "omission must not regress ordinary chat surfaces"
    )

    # And the legacy surface-less path (surface_context is None) also keeps it.
    legacy_ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        session_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )
    legacy_block = build_behavior_instructions(legacy_ctx, backend_name="claude_agent_sdk")
    assert INLINE_RIPPLE_SYSTEM_PROMPT in legacy_block, (
        "the surface-less legacy path must keep INLINE_RIPPLE_SYSTEM_PROMPT"
    )


# ---------------------------------------------------------------------------
# /sites is META-AWARE in build_behavior_instructions too (feat/sites-svelte-engine).
#
# The ripple block must be omitted ONLY for the svelte-create mode. The
# ripple-create and refine modes both AUTHOR/EDIT a ripple landing spec, so they
# must KEEP INLINE_RIPPLE_SYSTEM_PROMPT. These three tests pin each mode.
# ---------------------------------------------------------------------------


def test_sites_svelte_create_omits_ripple_block():
    """GUARD (passes today): the svelte-CREATE /sites surface (engine="svelte",
    no pocket_id) hand-authors SvelteKit, so INLINE_RIPPLE_SYSTEM_PROMPT must be
    ABSENT. This is the engine-scoped form of the bias-kill — the ONLY /sites
    mode that drops ripple."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    block = build_behavior_instructions(
        _sites_surface_ctx(SurfaceMeta(engine="svelte")), backend_name="claude_agent_sdk"
    )
    assert INLINE_RIPPLE_SYSTEM_PROMPT not in block, (
        "INLINE_RIPPLE_SYSTEM_PROMPT must be omitted on the /sites svelte-create surface"
    )
    assert "<ripple>" not in block, "the <ripple> framing tag leaked onto /sites svelte-create"


def test_sites_ripple_create_keeps_ripple_block():
    """RED DRIVER (fails today): the ripple-CREATE /sites surface (engine None
    or "ripple", no pocket_id) AUTHORS a ripple marketing landing page, so it
    MUST KEEP INLINE_RIPPLE_SYSTEM_PROMPT. PR 1 wrongly omits the ripple block
    for every /sites meta, so this fails — the resolver returns ripple_mode="off"
    and the gate strips the block even though the agent needs the widget LAW to
    author the ripple spec."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    for meta in (SurfaceMeta(engine=None), SurfaceMeta(engine="ripple")):
        block = build_behavior_instructions(
            _sites_surface_ctx(meta), backend_name="claude_agent_sdk"
        )
        assert INLINE_RIPPLE_SYSTEM_PROMPT in block, (
            f"ripple-create /sites meta {meta!r} must KEEP INLINE_RIPPLE_SYSTEM_PROMPT "
            "— it authors a ripple landing page and needs the widget LAW"
        )


def test_sites_refine_keeps_ripple_block():
    """RED DRIVER (fails today): the REFINE /sites surface (pocket_id set) edits
    the existing RIPPLE landing spec via pocket_specialist__edit, so it MUST KEEP
    INLINE_RIPPLE_SYSTEM_PROMPT. Refine wins over engine — even with
    ``engine="svelte"`` stamped, a pocket_id means refine. PR 1 wrongly omits the
    ripple block for every /sites meta, so this fails today."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    for meta in (
        SurfaceMeta(pocket_id="pkt_1"),
        SurfaceMeta(pocket_id="pkt_1", engine="svelte"),  # refine wins over engine
    ):
        block = build_behavior_instructions(
            _sites_surface_ctx(meta), backend_name="claude_agent_sdk"
        )
        assert INLINE_RIPPLE_SYSTEM_PROMPT in block, (
            f"refine /sites meta {meta!r} must KEEP INLINE_RIPPLE_SYSTEM_PROMPT — "
            "it edits an existing ripple landing spec"
        )


# ---------------------------------------------------------------------------
# Home agent backend-summary surfacing (feat/home-agent-source-authoring).
#
# Mirrors how the pocket_specialist surfaces a non-secret backend summary so
# it knows whether a backend is configured before authoring a `sources`
# block. The home agent must SEE the configured backend + its base_url so it
# stops claiming "no integration wired up" and authors a source on add_widget.
# ---------------------------------------------------------------------------


def _home_ctx(backend_summary):
    return ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="home-pocket-1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="home-pocket-1",
        pocket_type="home",
        backend_summary=backend_summary,
    )


def test_home_prompt_surfaces_configured_backend_summary():
    """When the resolved home scope carries a configured backend summary, the
    behavior instructions must render the base_url so the agent can SEE the
    backend exists (and stop saying "no integration wired up")."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = _home_ctx(
        {"configured": True, "base_url": "https://api.acme.test", "auth_type": "bearer"}
    )
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    assert "<home-pocket>" in block
    # The configured base_url is rendered into the prompt verbatim.
    assert "https://api.acme.test" in block
    # The literal token never leaks.
    assert "__BACKEND_SUMMARY__" not in block


def test_home_prompt_renders_not_configured_when_no_backend():
    """A home scope with an explicit `configured: False` summary renders the
    "not configured" state — the agent must NOT author a source then."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = _home_ctx({"configured": False})
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    assert "<home-pocket>" in block
    assert "not configured" in block
    assert "__BACKEND_SUMMARY__" not in block


def test_home_prompt_renders_unknown_when_summary_absent():
    """No backend summary on the scope renders the "unknown — call get_pocket"
    fallback rather than asserting there is no backend."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = _home_ctx(None)
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    assert "<home-pocket>" in block
    assert "configured state unknown" in block
    assert "__BACKEND_SUMMARY__" not in block


def test_home_prompt_carries_source_authoring_guidance():
    """The home prompt must teach the agent to author a `widget.sources` GET
    binding when a backend is configured — borrowed from the specialist's
    live-data guidance — and the data-shape rule (bind a field path / scalar,
    never a whole object)."""
    from pocketpaw.ripple import HOME_POCKET_PROMPT

    assert "sources" in HOME_POCKET_PROMPT
    # The read-only GET binding shape.
    assert "refresh" in HOME_POCKET_PROMPT and "pocket_open" in HOME_POCKET_PROMPT
    # The data-shape rule the smoke test surfaced.
    assert "field path" in HOME_POCKET_PROMPT.lower() or "field-path" in HOME_POCKET_PROMPT.lower()


def _session_ctx() -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
    )


def _force_settings(monkeypatch, **overrides):
    """Make ``Settings.load()`` return a controlled instance for the
    Composio gating checks inside ``build_behavior_instructions``.

    Clears the Composio env vars first so the result depends only on the
    explicit overrides, not on whatever the host machine has exported.
    """
    from pocketpaw.config import Settings

    for var in (
        "POCKETPAW_COMPOSIO_API_KEY",
        "POCKETPAW_COMPOSIO_ENTERPRISE_ID",
        "POCKETPAW_COMPOSIO_TOOLKITS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None, **overrides)
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: s))
    return s


def test_composio_rules_injected_when_enabled_without_toolkits(monkeypatch):
    """Credentials set but no toolkit allow-list. ``providers`` falls back
    to the discovery meta-tools (COMPOSIO_SEARCH_TOOLS et al.), so the
    agent DOES have Composio tools — the auth/search rules must be
    injected (the search-fallback rule matters most in this mode)."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    _force_settings(
        monkeypatch,
        composio_api_key="ck_test",
        composio_enterprise_id="ent_acme",
        composio_toolkits=[],
    )
    block = build_behavior_instructions(_session_ctx(), backend_name="claude_agent_sdk")
    assert "<composio-auth-flow>" in block
    assert "<composio-search-fallback>" in block
    assert "<runtime-identity>" in block


def test_composio_rules_injected_when_toolkits_configured(monkeypatch):
    """Credentials AND a non-empty toolkit allow-list → concrete tools +
    meta-tools, so the auth/search guidance is injected."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    _force_settings(
        monkeypatch,
        composio_api_key="ck_test",
        composio_enterprise_id="ent_acme",
        composio_toolkits=["gmail", "slack"],
    )
    block = build_behavior_instructions(_session_ctx(), backend_name="claude_agent_sdk")
    assert "<composio-auth-flow>" in block
    assert "<composio-search-fallback>" in block


def test_composio_rules_omitted_when_disabled(monkeypatch):
    """No credentials → Composio is off, no tools at all. The auth/search
    rules must NOT be injected, but the always-on identity rule (which
    tells the agent to say so plainly) still is."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    _force_settings(monkeypatch)  # bare Settings: no api_key / enterprise_id
    block = build_behavior_instructions(_session_ctx(), backend_name="claude_agent_sdk")
    assert "<composio-auth-flow>" not in block
    assert "<composio-search-fallback>" not in block
    assert "<runtime-identity>" in block


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


# ---------------------------------------------------------------------------
# <pocket-summary> block — pocket-anchored chat orientation
# (fix/pocket-anchored-chat-context: the agent must never conclude a
# template-composed pocket is "an empty shell" because widgets[] is empty)
# ---------------------------------------------------------------------------


def _applications_pocket_summary() -> dict:
    """The summary data the resolvers stash for the bug-transcript pocket."""
    from pocketpaw_ee.cloud.pockets.spec_ops import summarize_ripple_spec

    spec = {
        "version": "1.0",
        "ui": {
            "id": "n_root0000",
            "type": "flex",
            "children": [
                {"id": "n_header00", "type": "page-header"},
                {"id": "n_grid0001", "type": "grid"},
                {"id": "n_grid0002", "type": "grid"},
            ],
        },
        "state": {"selected_id": None, "applications": [], "queue_total": 0},
        "sources": {
            "applications": {
                "method": "GET",
                "path": "/applications?status=open",
                "bind": "state.applications",
            }
        },
    }
    return {
        "name": "Applications",
        "description": "Triage queue for inbound applications",
        "type": "custom",
        "template_slug": "applications-triage",
        "pattern": "dashboard",
        "ripple": summarize_ripple_spec(spec, widgets_count=0),
    }


def _anchored_ctx(pocket_summary, *, pocket_type="custom", backend_summary=None) -> ScopeContext:
    return ScopeContext(
        kind=ScopeKind.POCKET,
        scope_id="pocket-apps-1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_id="pocket-apps-1",
        pocket_type=pocket_type,
        backend_summary=backend_summary,
        pocket_summary=pocket_summary,
    )


def test_non_home_anchored_pocket_gets_pocket_summary_block():
    """THE bug fix: a non-home pocket-anchored chat must carry a
    <pocket-summary> orientation block — name, description, template,
    ui node types, state keys, sources, and the get_pocket hint."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    block = build_behavior_instructions(
        _anchored_ctx(_applications_pocket_summary()), backend_name="claude_agent_sdk"
    )
    assert "<pocket-summary>" in block and "</pocket-summary>" in block
    assert "Applications" in block
    assert "Triage queue for inbound applications" in block
    assert "applications-triage" in block
    assert "dashboard" in block
    # Layout: top-level node types from rippleSpec.ui.
    assert "page-header" in block and "grid" in block
    # State keys.
    assert "selected_id" in block
    # Sources: method, path, target state key.
    assert "GET /applications?status=open" in block
    assert "state.applications" in block
    # Legacy widgets array is called out as legacy, with its count.
    assert "widgets" in block and "legacy" in block
    # The hint pointing at the full read.
    assert "get_pocket" in block


def test_home_pocket_keeps_home_prompt_and_gains_summary_block_additively():
    """HOME pockets keep HOME_POCKET_PROMPT + backend summary EXACTLY as
    before; the <pocket-summary> block is purely additive (the old block is
    a byte-identical prefix of the new one)."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    backend = {"configured": True, "base_url": "https://api.acme.test", "auth_type": "bearer"}
    summary = _applications_pocket_summary()
    with_summary = build_behavior_instructions(
        _anchored_ctx(summary, pocket_type="home", backend_summary=backend),
        backend_name="claude_agent_sdk",
    )
    without_summary = build_behavior_instructions(
        _anchored_ctx(None, pocket_type="home", backend_summary=backend),
        backend_name="claude_agent_sdk",
    )
    assert "<home-pocket>" in with_summary
    assert "https://api.acme.test" in with_summary
    assert "<pocket-summary>" in with_summary
    # Additive: everything before the new block is byte-compatible.
    assert with_summary.startswith(without_summary)
    assert "<pocket-summary>" not in without_summary


def test_pocket_summary_block_degrades_without_ripple_spec():
    """A pocket with no rippleSpec still gets an orientation block (name /
    description / widgets count) — and never crashes the prompt build."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions
    from pocketpaw_ee.cloud.pockets.spec_ops import summarize_ripple_spec

    summary = {
        "name": "Plain Pocket",
        "description": "",
        "type": "custom",
        "template_slug": None,
        "pattern": None,
        "ripple": summarize_ripple_spec(None, widgets_count=4),
    }
    block = build_behavior_instructions(_anchored_ctx(summary), backend_name="claude_agent_sdk")
    assert "<pocket-summary>" in block
    assert "Plain Pocket" in block
    assert "no rippleSpec" in block
    assert "4" in block  # the legacy widgets[] count is surfaced


def test_unanchored_scope_emits_no_pocket_summary_block():
    """Plain chats (no pocket anchor → no pocket_summary) are unchanged."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    block = build_behavior_instructions(_session_ctx(), backend_name="claude_agent_sdk")
    assert "<pocket-summary>" not in block


def test_pocket_summary_block_suppressed_for_pocket_create_intent():
    """pocket_create intent is about a NEW pocket — the anchor pocket's
    summary would mislead, so it is gated off (mirrors the <current-pocket>
    tag gate in build_dynamic_context)."""
    from dataclasses import replace

    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions

    ctx = replace(_anchored_ctx(_applications_pocket_summary()), intent="pocket_create")
    block = build_behavior_instructions(ctx, backend_name="claude_agent_sdk")
    assert "<pocket-summary>" not in block


def test_pocket_summary_block_caps_description_length():
    """A degenerate, unbounded description must not bloat the system prompt —
    the rendered block clamps it (about-member-block precedent)."""
    from pocketpaw_ee.cloud.chat.agent_service import build_behavior_instructions
    from pocketpaw_ee.cloud.pockets.spec_ops import summarize_ripple_spec

    summary = {
        "name": "Bloaty",
        "description": "x" * 10_000,
        "type": "custom",
        "template_slug": None,
        "pattern": None,
        "ripple": summarize_ripple_spec(None),
    }
    block = build_behavior_instructions(_anchored_ctx(summary), backend_name="claude_agent_sdk")
    start = block.index("<pocket-summary>")
    end = block.index("</pocket-summary>") + len("</pocket-summary>")
    assert end - start < 2_500, "pocket-summary block must stay token-capped"
