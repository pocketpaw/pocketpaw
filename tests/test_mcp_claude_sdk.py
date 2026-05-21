"""Tests for MCP + Claude Agent SDK integration — Sprint 17.

Updated: 2026-05-21 — refactor/gate-planner-mcp. ``_strip_builtin_servers``
  now also drops ``pocketpaw_planner`` so external-config assertions stay
  correct after the planner became a built-in in-process MCP server
  (fixes #1150). Added ``TestPlannerMCPGate`` covering the opt-in policy
  gate: the planner is absent unless the tool policy explicitly allows it.

All SDK imports are mocked.
"""

from unittest.mock import patch

from ee.agent.pocket_specialist.mcp_tool import (
    SERVER_NAME as _POCKET_SPECIALIST_MCP_SERVER_NAME,
)
from pocketpaw.agents.claude_sdk import ClaudeAgentSDK
from pocketpaw.agents.sdk_mcp_planner import SERVER_NAME as _PLANNER_MCP_SERVER_NAME
from pocketpaw.agents.sdk_mcp_pocket import SERVER_NAME as _POCKET_MCP_SERVER_NAME
from pocketpaw.agents.sdk_mcp_tasks import SERVER_NAME as _TASKS_MCP_SERVER_NAME
from pocketpaw.config import Settings
from pocketpaw.mcp.config import MCPServerConfig


def _strip_builtin_servers(result: dict) -> dict:
    """Drop always-on in-process MCP servers so external-config assertions stay focused.

    Note: ``pocketpaw_planner`` is a built-in but is *not* always-on — it is
    gated behind an explicit policy opt-in. It is stripped here so the
    external-config assertions remain correct regardless of whether a given
    test happens to opt the planner in.
    """
    out = dict(result)
    out.pop(_POCKET_MCP_SERVER_NAME, None)
    out.pop(_POCKET_SPECIALIST_MCP_SERVER_NAME, None)
    out.pop(_TASKS_MCP_SERVER_NAME, None)
    out.pop(_PLANNER_MCP_SERVER_NAME, None)
    return out


class TestClaudeSDKMCPServers:
    """Test _get_mcp_servers method."""

    def _make_sdk(self, **overrides) -> ClaudeAgentSDK:
        """Create a ClaudeAgentSDK with SDK imports mocked out."""
        settings = Settings(
            anthropic_api_key="test-key",
            tool_profile="full",
            **overrides,
        )
        with patch.object(ClaudeAgentSDK, "_initialize"):
            sdk = ClaudeAgentSDK(settings)
            sdk._sdk_available = False  # don't need real SDK
        return sdk

    def test_no_mcp_configs(self):
        sdk = self._make_sdk()
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _strip_builtin_servers(result) == {}

    def test_enabled_stdio_server_passes(self):
        sdk = self._make_sdk()
        cfgs = [
            MCPServerConfig(name="fs", transport="stdio", command="npx", args=["server"]),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        external = _strip_builtin_servers(result)
        assert len(external) == 1
        assert "fs" in external
        assert external["fs"]["type"] == "stdio"
        assert external["fs"]["command"] == "npx"
        assert external["fs"]["args"] == ["server"]

    def test_disabled_server_filtered_out(self):
        sdk = self._make_sdk()
        cfgs = [
            MCPServerConfig(name="fs", transport="stdio", command="npx", enabled=False),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        assert _strip_builtin_servers(result) == {}

    def test_http_server_passes(self):
        """HTTP servers are supported by Claude SDK."""
        sdk = self._make_sdk()
        cfgs = [
            MCPServerConfig(name="remote", transport="http", url="http://localhost:9000"),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        assert "remote" in result
        assert result["remote"]["type"] == "http"
        assert result["remote"]["url"] == "http://localhost:9000"

    def test_http_server_without_url_skipped(self):
        """HTTP server with no url is skipped."""
        sdk = self._make_sdk()
        cfgs = [
            MCPServerConfig(name="bad", transport="http", url=""),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        assert _strip_builtin_servers(result) == {}

    def test_sse_server_passes(self):
        """SSE servers are supported by Claude SDK."""
        sdk = self._make_sdk()
        cfgs = [
            MCPServerConfig(name="notion", transport="sse", url="https://mcp.notion.com/sse"),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        assert "notion" in result
        assert result["notion"]["type"] == "sse"

    def test_policy_denies_server(self):
        sdk = self._make_sdk(tools_deny=["mcp:fs:*"])
        cfgs = [
            MCPServerConfig(name="fs", transport="stdio", command="npx"),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        assert _strip_builtin_servers(result) == {}

    def test_policy_denies_group_mcp(self):
        sdk = self._make_sdk(tools_deny=["group:mcp"])
        cfgs = [
            MCPServerConfig(name="fs", transport="stdio", command="npx"),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        assert _strip_builtin_servers(result) == {}

    def test_env_passed_through(self):
        sdk = self._make_sdk()
        cfgs = [
            MCPServerConfig(
                name="gh",
                transport="stdio",
                command="npx",
                args=["server"],
                env={"GITHUB_TOKEN": "abc"},
            ),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        assert result["gh"]["env"] == {"GITHUB_TOKEN": "abc"}

    def test_multiple_servers_mixed(self):
        sdk = self._make_sdk()
        cfgs = [
            MCPServerConfig(name="fs", transport="stdio", command="npx", enabled=True),
            MCPServerConfig(name="off", transport="stdio", command="npx", enabled=False),
            MCPServerConfig(name="web", transport="http", url="http://x"),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        external = _strip_builtin_servers(result)
        assert len(external) == 2
        assert "fs" in external
        assert "web" in external

    def test_mcp_import_error_returns_empty(self):
        """If mcp module is not installed, return empty dict."""
        sdk = self._make_sdk()
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "pocketpaw.mcp" in name:
                raise ImportError("no mcp")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            result = sdk._get_mcp_servers()
        assert result == {}

    def test_empty_env_and_args_omitted(self):
        """Empty env/args should not be included in the server config."""
        sdk = self._make_sdk()
        cfgs = [
            MCPServerConfig(name="mem", transport="stdio", command="npx", args=[], env={}),
        ]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=cfgs):
            result = sdk._get_mcp_servers()
        assert "mem" in result
        assert "env" not in result["mem"]
        assert "args" not in result["mem"]
        assert result["mem"]["type"] == "stdio"
        assert result["mem"]["command"] == "npx"


class TestPlannerMCPGate:
    """The ``pocketpaw_planner`` MCP server must be opt-in, not ambient.

    Default posture (profile ``full``, no allow list) is allow-by-default for
    MCP servers. The planner is gated on an *explicit* policy opt-in so the
    ``plan_project`` tool schema is only loaded into agent contexts where
    planning is relevant — not every agent run.
    """

    def _make_sdk(self, **overrides) -> ClaudeAgentSDK:
        settings = Settings(
            anthropic_api_key="test-key",
            tool_profile="full",
            **overrides,
        )
        with patch.object(ClaudeAgentSDK, "_initialize"):
            sdk = ClaudeAgentSDK(settings)
            sdk._sdk_available = False
        return sdk

    def test_planner_absent_by_default(self):
        """Default policy (full, no allow list) must NOT load the planner."""
        sdk = self._make_sdk()
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME not in result

    def test_planner_present_when_explicitly_allowed(self):
        """An explicit ``mcp:pocketpaw_planner:*`` allow entry opts it in."""
        sdk = self._make_sdk(tools_allow=[f"mcp:{_PLANNER_MCP_SERVER_NAME}:*"])
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME in result
        assert result[_PLANNER_MCP_SERVER_NAME]["type"] == "sdk"

    def test_planner_present_when_group_mcp_allowed(self):
        """``group:mcp`` in the allow list also opts the planner in."""
        sdk = self._make_sdk(tools_allow=["group:mcp"])
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME in result

    def test_planner_absent_when_denied(self):
        """A deny entry blocks the planner even with an explicit allow."""
        sdk = self._make_sdk(
            tools_allow=[f"mcp:{_PLANNER_MCP_SERVER_NAME}:*"],
            tools_deny=[f"mcp:{_PLANNER_MCP_SERVER_NAME}:*"],
        )
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME not in result

    def test_unrelated_allow_does_not_load_planner(self):
        """Opting in some other MCP server must not leak the planner."""
        sdk = self._make_sdk(tools_allow=["mcp:fs:*"])
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME not in result
