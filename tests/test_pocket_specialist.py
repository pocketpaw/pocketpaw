"""Pocket specialist: integration of delegation rule + tool surface.

These tests do NOT spin up a real Claude conversation — they verify the
static contract: main agent prompt teaches MCP-tool delegation; main
agent allowlist filters out pocket mutation tools; the new
``pocket_specialist__create`` MCP tool is the canonical entry point.
"""


def test_delegation_rule_points_at_mcp_tool():
    """Cross-file contract: POCKET_DELEGATION_RULE must direct the agent
    to the new ``pocket_specialist__create`` MCP tool — the legacy native
    subagent (``Agent(subagent_type="pocket_specialist")``) has been
    removed."""
    from ee.ripple._pockets import POCKET_DELEGATION_RULE

    assert "pocket_specialist__create" in POCKET_DELEGATION_RULE, (
        "delegation rule must reference the canonical MCP tool name"
    )
    # Legacy native-subagent kwarg shape must be gone.
    assert 'subagent_type="pocket_specialist"' not in POCKET_DELEGATION_RULE
    assert "subagent_type='pocket_specialist'" not in POCKET_DELEGATION_RULE


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


def test_main_agent_allowlist_excludes_pocket_mutation_tools():
    """The pocket mutation tool IDs must NOT appear in the main agent's
    allowed tool surface — they're owned by the pocket specialist.
    Without this filter, the main agent could call them directly and
    bypass the delegation rule.

    The set covers BOTH the original pocket-level mutators
    (``create_pocket``, ``update_pocket``, legacy widget ops) AND the
    granular ``rippleSpec.ui`` node ops (``add_node`` / ``replace_node`` /
    ``set_node_prop`` / ``move_node`` / ``remove_node``). The node ops
    operate directly on rendered UI; allowing them on the main agent
    would let it skip the specialist's list-before-create + validation
    workflow entirely.
    """
    from pocketpaw.agents.claude_sdk import _POCKET_MUTATION_TOOL_IDS

    expected = {
        "mcp__pocketpaw_pocket__create_pocket",
        "mcp__pocketpaw_pocket__update_pocket",
        "mcp__pocketpaw_pocket__add_widget",
        "mcp__pocketpaw_pocket__update_widget",
        "mcp__pocketpaw_pocket__remove_widget",
        "mcp__pocketpaw_pocket__add_node",
        "mcp__pocketpaw_pocket__replace_node",
        "mcp__pocketpaw_pocket__set_node_prop",
        "mcp__pocketpaw_pocket__move_node",
        "mcp__pocketpaw_pocket__remove_node",
        "mcp__pocketpaw_pocket__set_state",
        "mcp__pocketpaw_pocket__append_state",
        "mcp__pocketpaw_pocket__remove_state",
        "mcp__pocketpaw_pocket__patch_state",
    }
    assert _POCKET_MUTATION_TOOL_IDS == expected, (
        "drift between mutation-tool filter and the canonical name set"
    )


def test_legacy_subagent_helpers_removed():
    """The old native-subagent helpers must no longer be importable —
    they were the path the agent used to bypass the new MCP tool."""
    import pocketpaw.agents.claude_sdk as csdk

    assert not hasattr(csdk, "_POCKET_SPECIALIST_NAME"), (
        "legacy subagent registration constant should be removed"
    )
    assert not hasattr(csdk, "_pocket_specialist_system_prompt"), (
        "legacy subagent system-prompt helper should be removed"
    )
    assert not hasattr(csdk, "_build_pocket_specialist_agent_def"), (
        "legacy AgentDefinition builder should be removed"
    )


def test_agent_tool_is_in_policy_map():
    """Agent must be explicitly in _TOOL_POLICY_MAP or it's blocked
    for non-full tool profiles. The general-purpose claude_agent_sdk
    Agent capability stays available even though no pocket subagent
    is registered now."""
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    assert "Agent" in ClaudeSDKBackend._TOOL_POLICY_MAP, (
        "Agent tool must have an explicit policy-map entry"
    )
