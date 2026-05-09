"""Pocket specialist subagent: integration of system prompt + tool surface.

These tests do NOT spin up a real Claude conversation — they verify the
static contract: main agent prompt teaches Agent-tool delegation; main
agent allowlist filters out pocket mutation tools; specialist's tool
list includes mutation tools; specialist's system prompt carries the
full pocket prompts.
"""


def test_delegation_rule_lists_correct_subagent_name():
    """Cross-file contract: the subagent name in POCKET_DELEGATION_RULE
    must match the dict key used to register it in claude_sdk.py.
    Typos here would leave the agent unable to invoke the specialist."""
    from ee.ripple._pockets import POCKET_DELEGATION_RULE
    from pocketpaw.agents.claude_sdk import _POCKET_SPECIALIST_NAME

    assert _POCKET_SPECIALIST_NAME in POCKET_DELEGATION_RULE, (
        f"registered subagent name {_POCKET_SPECIALIST_NAME!r} not "
        "in POCKET_DELEGATION_RULE — rule and registration drifted"
    )
    assert (
        f'subagent_type="{_POCKET_SPECIALIST_NAME}"' in POCKET_DELEGATION_RULE
        or f"subagent_type='{_POCKET_SPECIALIST_NAME}'" in POCKET_DELEGATION_RULE
    ), "rule must teach the canonical Agent-tool kwarg shape"


def test_main_agent_keeps_read_only_pocket_tools():
    """The read-only pocket tool IDs must NOT appear in the mutation
    filter — they stay on the main agent's allowlist so it can answer
    conversational queries about pockets without delegating."""
    from pocketpaw.agents.claude_sdk import _POCKET_MUTATION_TOOL_IDS

    read_only_ids = {
        "mcp__pocketpaw_pocket__list_pockets",
        "mcp__pocketpaw_pocket__get_pocket",
    }
    leaked = read_only_ids & _POCKET_MUTATION_TOOL_IDS
    assert not leaked, f"read-only tool IDs ended up in the mutation filter: {leaked}"


def test_specialist_system_prompt_includes_full_pocket_prompts():
    """The specialist's system prompt is the full pocket-mode text —
    anything less defeats the architecture (specialist is SUPPOSED to
    carry the heavy prompt so the main agent doesn't).

    Post-Task-11: the specialist runs the dedicated POCKET_SPECIALIST_PROMPT
    (list_pockets / validate_spec / persist_pocket workflow), not the
    calling-agent delegation block."""
    from ee.ripple._pockets import POCKET_SPECIALIST_PROMPT
    from pocketpaw.agents.claude_sdk import _pocket_specialist_system_prompt

    sp = _pocket_specialist_system_prompt()
    # Spot-check canonical chunks from the specialist prompt.
    assert "<rippleSpec-is-the-canvas>" in sp
    # Toolkit / expression-language section must be present in the
    # specialist (it's part of RIPPLE_DESIGN_RULES).
    assert "Toolkit" in sp or "expression language" in sp
    # And the full specialist prompt is fully embedded.
    assert POCKET_SPECIALIST_PROMPT in sp


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


def test_agent_tool_is_in_policy_map():
    """Agent must be explicitly in _TOOL_POLICY_MAP or it's blocked
    for non-full tool profiles, silently preventing specialist
    delegation. Without this test, removing the entry would only
    surface in production for restricted-profile users."""
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    assert "Agent" in ClaudeSDKBackend._TOOL_POLICY_MAP, (
        "Agent tool must have an explicit policy-map entry"
    )
