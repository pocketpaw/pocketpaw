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
    from pocketpaw.ripple._pockets import POCKET_DELEGATION_RULE

    assert "pocket_specialist__create" in POCKET_DELEGATION_RULE, (
        "delegation rule must reference the canonical MCP tool name"
    )
    # Legacy native-subagent kwarg shape must be gone.
    assert 'subagent_type="pocket_specialist"' not in POCKET_DELEGATION_RULE
    assert "subagent_type='pocket_specialist'" not in POCKET_DELEGATION_RULE


def test_widget_spec_mcp_server_is_read_only():
    """The core ``pocketpaw_widgets`` server exposes ONLY read tools — it
    serves the catalog/manifest, nothing mutable.

    The OSS-EE split (Phase 3b) split the old ``pocketpaw_pocket`` server in
    two: ripple widget-spec tools (core ``pocketpaw_widgets``, this one) and
    the cloud pocket-context tools (EE ``pocketpaw_pocket``)."""
    from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS

    assert set(WIDGET_TOOL_IDS) == {
        "mcp__pocketpaw_widgets__get_widget_spec",
        "mcp__pocketpaw_widgets__get_inline_widget_help",
    }, f"drift in widget MCP tool surface: {set(WIDGET_TOOL_IDS)}"


def test_pocket_mcp_server_surface():
    """The cloud ``pocketpaw_pocket`` server exposes the two read tools plus
    the writable ``add_widget`` / ``update_widget`` tools — the home agent's
    widget-creation and refresh paths. rippleSpec *design* mutations still
    flow through the ``pocket_specialist__create`` / ``__edit`` tools;
    ``add_widget`` / ``update_widget`` only touch a pocket's ``widgets[]``
    array, a different surface.
    """
    # The cloud pocket-context server is EE-only — skip when absent.
    try:
        from pocketpaw_ee.agent.mcp_servers.pockets import POCKET_TOOL_IDS
    except ImportError:
        return
    assert set(POCKET_TOOL_IDS) == {
        "mcp__pocketpaw_pocket__get_pocket",
        "mcp__pocketpaw_pocket__list_pockets",
        "mcp__pocketpaw_pocket__add_widget",
        "mcp__pocketpaw_pocket__update_widget",
    }, f"drift in pocket MCP tool surface: {set(POCKET_TOOL_IDS)}"


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
