"""Pocket specialist subagent: integration of system prompt + tool surface.

These tests do NOT spin up a real Claude conversation — they verify the
static contract: main agent prompt teaches Agent-tool delegation; main
agent allowlist filters out pocket mutation tools; specialist's tool
list includes mutation tools; specialist's system prompt carries the
full pocket prompts.
"""


def test_delegation_rule_lists_correct_subagent_name():
    """The delegation rule must name the registered subagent exactly —
    typos here would leave the agent unable to invoke the specialist."""
    from ee.ripple._pockets import POCKET_DELEGATION_RULE

    assert "pocket_specialist" in POCKET_DELEGATION_RULE
    # And it must use the canonical Agent-tool kwarg shape.
    assert (
        'subagent_type="pocket_specialist"' in POCKET_DELEGATION_RULE
        or "subagent_type='pocket_specialist'" in POCKET_DELEGATION_RULE
    )


def test_delegation_rule_lists_read_only_pocket_tools_as_ok():
    """Read-only pocket tools must remain available to the main agent —
    listing pockets and answering questions about them is conversational,
    not delegation."""
    from ee.ripple._pockets import POCKET_DELEGATION_RULE

    assert "list_pockets" in POCKET_DELEGATION_RULE
    assert "get_pocket" in POCKET_DELEGATION_RULE


def test_specialist_system_prompt_includes_full_pocket_prompts():
    """The specialist's system prompt is the full pocket-mode text —
    anything less defeats the architecture (specialist is SUPPOSED to
    carry the heavy prompt so the main agent doesn't)."""
    from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP
    from pocketpaw.agents.claude_sdk import _pocket_specialist_system_prompt

    sp = _pocket_specialist_system_prompt()
    # Spot-check canonical chunks from the creation prompt.
    assert "<rippleSpec-is-the-canvas>" in sp
    # Toolkit / expression-language section must be present in the
    # specialist (it's part of RIPPLE_DESIGN_RULES).
    assert "Toolkit" in sp or "expression language" in sp
    # And the full creation prompt is fully embedded.
    assert POCKET_CREATION_PROMPT_MCP in sp


def test_specialist_prompt_resolves_pocket_id_token():
    """POCKET_INTERACTION_PROMPT_MCP carries a literal __POCKET_ID__
    placeholder that's normally replaced per-turn. The specialist's
    system prompt is set once at SDK init time, so the token would
    leak verbatim — the helper substitutes a directional placeholder."""
    from ee.ripple._pockets import POCKET_ID_TOKEN
    from pocketpaw.agents.claude_sdk import _pocket_specialist_system_prompt

    sp = _pocket_specialist_system_prompt()
    assert POCKET_ID_TOKEN not in sp, "literal POCKET_ID_TOKEN leaked into specialist system prompt"


def test_main_agent_allowlist_excludes_pocket_mutation_tools():
    """The pocket mutation tool IDs must NOT appear in the main agent's
    allowed tool surface — they live on the specialist instead. Without
    this filter, the main agent could call them directly and bypass the
    delegation rule."""
    from pocketpaw.agents.claude_sdk import _POCKET_MUTATION_TOOL_IDS

    expected = {
        "mcp__pocketpaw_pocket__create_pocket",
        "mcp__pocketpaw_pocket__update_pocket",
        "mcp__pocketpaw_pocket__add_widget",
        "mcp__pocketpaw_pocket__update_widget",
        "mcp__pocketpaw_pocket__remove_widget",
    }
    assert _POCKET_MUTATION_TOOL_IDS == expected, (
        "drift between mutation-tool filter and the canonical name set"
    )
