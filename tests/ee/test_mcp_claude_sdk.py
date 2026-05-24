"""Tests for MCP + Claude Agent SDK integration — Sprint 17.

Updated: 2026-05-21 — refactor/gate-planner-mcp. Expanded the
  ``_strip_builtin_servers`` docstring to explain why ``pocketpaw_planner``
  is stripped even though it is opt-in rather than always-on. Added
  ``TestPlannerMCPGate`` covering the opt-in gate: the planner loads only
  when an injected ``ToolPolicy`` names it in ``mcp_servers_allow``.
Updated: 2026-05-22 (#1174) — added ``TestMcpToolAllowlist``: the resolved
  in-process MCP tool-id allowlist (``_collect_mcp_tool_ids``) includes the
  cloud ``pocketpaw_pocket`` server's writable ``add_widget`` tool, so the
  home-pocket agent on this backend can pin real widgets.

All SDK imports are mocked.
"""

from unittest.mock import patch

from pocketpaw_ee.agent.mcp_servers.planner import SERVER_NAME as _PLANNER_MCP_SERVER_NAME
from pocketpaw_ee.agent.mcp_servers.pockets import SERVER_NAME as _POCKET_MCP_SERVER_NAME
from pocketpaw_ee.agent.mcp_servers.tasks import SERVER_NAME as _TASKS_MCP_SERVER_NAME
from pocketpaw_ee.agent.pocket_specialist.mcp_tool import (
    SERVER_NAME as _POCKET_SPECIALIST_MCP_SERVER_NAME,
)

from pocketpaw.agents.claude_sdk import ClaudeAgentSDK
from pocketpaw.agents.sdk_mcp_widgets import SERVER_NAME as _WIDGETS_MCP_SERVER_NAME
from pocketpaw.config import Settings
from pocketpaw.mcp.config import MCPServerConfig
from pocketpaw.tools.policy import ToolPolicy


def _strip_builtin_servers(result: dict) -> dict:
    """Drop always-on in-process MCP servers so external-config assertions stay focused.

    Note: ``pocketpaw_planner`` is a built-in but is *not* always-on — it is
    gated behind an explicit policy opt-in. It is stripped here so the
    external-config assertions remain correct regardless of whether a given
    test happens to opt the planner in.
    """
    out = dict(result)
    out.pop(_WIDGETS_MCP_SERVER_NAME, None)
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

    The opt-in flows through a per-agent ``ToolPolicy`` whose
    ``mcp_servers_allow`` frozenset names the planner. AgentPool builds that
    policy from the agent's ``tools`` field and injects it into the Claude
    SDK backend. With no such policy the planner schema never loads.
    """

    def _make_sdk(self, policy: ToolPolicy | None = None, **overrides) -> ClaudeAgentSDK:
        settings = Settings(
            anthropic_api_key="test-key",
            tool_profile="full",
            **overrides,
        )
        with patch.object(ClaudeAgentSDK, "_initialize"):
            sdk = ClaudeAgentSDK(settings, policy=policy)
            sdk._sdk_available = False
        return sdk

    @staticmethod
    def _planner_policy(**kwargs) -> ToolPolicy:
        """A ToolPolicy that opts the planner in via ``mcp_servers_allow``."""
        return ToolPolicy(mcp_servers_allow=frozenset({_PLANNER_MCP_SERVER_NAME}), **kwargs)

    def test_planner_absent_by_default(self):
        """No injected policy → default policy → planner not loaded."""
        sdk = self._make_sdk()
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME not in result

    def test_planner_absent_when_policy_does_not_opt_in(self):
        """An injected policy with an empty ``mcp_servers_allow`` keeps it off."""
        sdk = self._make_sdk(policy=ToolPolicy())
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME not in result

    def test_planner_present_when_policy_opts_in(self):
        """A policy whose ``mcp_servers_allow`` names the planner loads it."""
        sdk = self._make_sdk(policy=self._planner_policy())
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME in result
        assert result[_PLANNER_MCP_SERVER_NAME]["type"] == "sdk"

    def test_planner_absent_when_denied(self):
        """A deny entry blocks the planner even when ``mcp_servers_allow``
        names it."""
        sdk = self._make_sdk(
            policy=self._planner_policy(deny=[f"mcp:{_PLANNER_MCP_SERVER_NAME}:*"])
        )
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME not in result

    def test_tools_allow_does_not_opt_planner_in(self):
        """An ``mcp:*`` entry in ``tools_allow`` must NOT opt the planner in —
        ``mcp_servers_allow`` is the only opt-in channel."""
        sdk = self._make_sdk(tools_allow=[f"mcp:{_PLANNER_MCP_SERVER_NAME}:*"])
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            result = sdk._get_mcp_servers()
        assert _PLANNER_MCP_SERVER_NAME not in result


class TestMcpToolAllowlist:
    """The in-process MCP tool-id allowlist the claude_agent_sdk backend
    builds. An MCP tool is only callable when its id is on this list, so the
    home-pocket agent's ``add_widget`` tool must appear here."""

    def _make_sdk(self) -> ClaudeAgentSDK:
        settings = Settings(anthropic_api_key="test-key", tool_profile="full")
        with patch.object(ClaudeAgentSDK, "_initialize"):
            sdk = ClaudeAgentSDK(settings)
            sdk._sdk_available = False
        return sdk

    def test_allowlist_includes_writable_add_widget_tool(self):
        """The resolved tool list carries the cloud ``add_widget`` MCP tool —
        the home agent's widget-creation tool — alongside the read tools."""
        from pocketpaw_ee.agent.mcp_servers.pockets import (
            ADD_WIDGET_TOOL_ID,
            GET_POCKET_TOOL_ID,
        )

        sdk = self._make_sdk()
        ids = sdk._collect_mcp_tool_ids()
        assert ADD_WIDGET_TOOL_ID in ids, (
            "the writable add_widget tool must be on the allowlist or the home agent cannot call it"
        )
        # The read tools are still there — add_widget is additive.
        assert GET_POCKET_TOOL_ID in ids

    def test_allowlist_excludes_opt_in_planner_by_default(self):
        """Opt-in servers stay off the allowlist unless the policy opts them
        in — the loop the writable tool rides must keep that gate."""
        sdk = self._make_sdk()
        ids = sdk._collect_mcp_tool_ids()
        assert not any(f"__{_PLANNER_MCP_SERVER_NAME}__" in t for t in ids)
