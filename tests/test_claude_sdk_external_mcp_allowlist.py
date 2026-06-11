# tests/test_claude_sdk_external_mcp_allowlist.py — external MCP servers reach
# the SDK allowlist.
#
# External MCP servers from ``~/.pocketpaw/mcp_servers.json`` (``load_mcp_config``)
# are registered with the SDK in ``_get_mcp_servers`` but have no in-process
# ``tool_ids()`` provider, so before the fix their tools never reached
# ``allowed_tools`` and the SDK refused every call (a deployment's ``fabric``
# server was registered yet ``fabric_query`` was uncallable).
#
# ``_collect_mcp_tool_ids`` now appends a bare ``mcp__<server>`` entry (the
# Claude Code convention that admits all of a server's tools) for each enabled
# external server that passes the tool policy.
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.bypass_permissions = False
    settings.agent_backend = "claude_agent_sdk"
    settings.anthropic_api_key = "sk-ant-test-key"
    settings.claude_sdk_model = ""
    settings.claude_sdk_max_turns = 0
    settings.smart_routing_enabled = False
    settings.tool_profile = "full"
    settings.tools_allow = []
    settings.tools_deny = []
    settings.mcp_servers = {}
    settings.claude_sdk_provider = "anthropic"
    return settings


def _ext_cfg(name: str, *, enabled: bool = True):
    return SimpleNamespace(
        name=name,
        transport="stdio",
        command="python",
        args=["server.py"],
        env={},
        url="",
        enabled=enabled,
    )


def test_enabled_external_server_added_to_allowlist() -> None:
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    backend = ClaudeSDKBackend(_make_settings())

    with patch(
        "pocketpaw.mcp.config.load_mcp_config",
        return_value=[_ext_cfg("fabric")],
    ):
        ids = backend._collect_mcp_tool_ids()

    assert "mcp__fabric" in ids, (
        "an enabled external MCP server must be allowlisted wholesale so its "
        "tools are callable by the agent"
    )


def test_disabled_external_server_not_added() -> None:
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    backend = ClaudeSDKBackend(_make_settings())

    with patch(
        "pocketpaw.mcp.config.load_mcp_config",
        return_value=[_ext_cfg("fabric", enabled=False)],
    ):
        ids = backend._collect_mcp_tool_ids()

    assert "mcp__fabric" not in ids


def test_external_server_blocked_by_policy_not_added() -> None:
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    backend = ClaudeSDKBackend(_make_settings())
    # Deny the server at the policy layer — registration and allowlist must agree.
    backend._policy.is_mcp_server_allowed = lambda name: name != "fabric"  # type: ignore[method-assign]

    with patch(
        "pocketpaw.mcp.config.load_mcp_config",
        return_value=[_ext_cfg("fabric")],
    ):
        ids = backend._collect_mcp_tool_ids()

    assert "mcp__fabric" not in ids
