# tests/cloud/test_loom_mcp.py — loom codebase-orientation MCP server (BS-1).
#
# Created: 2026-06-10 (feat/belt-loom-mcp, Belt & Pulley stations thin slice).
# Guards the loom MCP wiring for the cloud chat (claude_agent_sdk) backend:
#   * LOOM_TOOL_IDS — the 5 namespaced tool ids (mcp__loom__orient etc.) the
#     surface allow-list + the SDK allowlist machinery key on.
#   * CloudLoomMcpProvider.build_server() — returns None when loom_model_path is
#     unset, None when the binary can't be resolved, and the correct
#     ("loom", <stdio config dict>) when both the model file and binary exist.
#   * _resolve_loom_bin — explicit path → PATH → ~/go/bin/loom discovery order.
#   * Path A registration: the stdio config dict the provider returns flows
#     through the real ClaudeAgentSDK._get_mcp_servers registration loop with
#     ``type: "stdio"`` intact (the spike verdict — no loop change needed).
#
# build_loom_server reads its config through pocketpaw.config.get_settings (a
# cached singleton, the same pattern the sibling media / sites servers use), so
# every test that needs loom enabled patches get_settings — not the SDK's
# injected Settings instance.

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pocketpaw_ee.agent.mcp_servers.loom as loom
from pocketpaw_ee.extensions import CloudLoomMcpProvider

from pocketpaw.agents.claude_sdk import OPT_IN_MCP_SERVERS, ClaudeAgentSDK
from pocketpaw.config import Settings


def _settings(*, model_path: str | None, loom_bin: str = "loom") -> MagicMock:
    """A stand-in for get_settings() carrying only the loom fields the server
    reads."""
    return MagicMock(loom_model_path=model_path, loom_bin=loom_bin)


# --- tool ids ---------------------------------------------------------------


def test_loom_tool_ids_are_namespaced() -> None:
    """The 5 tool ids use the ``mcp__<server>__<tool>`` form the Claude Code
    allowlist machinery matches, under the binary's own server name ``loom``."""
    assert loom.SERVER_NAME == "loom"
    assert loom.ORIENT_TOOL_ID == "mcp__loom__orient"
    assert loom.LOCATE_TOOL_ID == "mcp__loom__locate"
    assert loom.WHY_TOOL_ID == "mcp__loom__why"
    assert loom.WHAT_DEPENDS_ON_TOOL_ID == "mcp__loom__what_depends_on"
    assert loom.BOUNDARIES_TOOL_ID == "mcp__loom__boundaries"
    assert loom.LOOM_TOOL_IDS == (
        loom.ORIENT_TOOL_ID,
        loom.LOCATE_TOOL_ID,
        loom.WHY_TOOL_ID,
        loom.WHAT_DEPENDS_ON_TOOL_ID,
        loom.BOUNDARIES_TOOL_ID,
    )


def test_provider_tool_ids_match_module() -> None:
    """The provider surfaces exactly the module's tool ids for the allowlist."""
    assert CloudLoomMcpProvider().tool_ids() == list(loom.LOOM_TOOL_IDS)


# --- build_server: disabled / degraded paths -------------------------------


def test_build_server_none_when_model_path_unset() -> None:
    """loom_model_path unset → loom disabled → build returns None (chat keeps
    working without orientation)."""
    with patch("pocketpaw.config.get_settings", return_value=_settings(model_path=None)):
        assert CloudLoomMcpProvider().build_server() is None
        assert loom.build_loom_server() is None


def test_build_server_none_when_model_file_missing(tmp_path) -> None:
    """A configured world-model path that does not exist → None, not a crash."""
    missing = str(tmp_path / "does-not-exist.json")
    with patch("pocketpaw.config.get_settings", return_value=_settings(model_path=missing)):
        assert loom.build_loom_server() is None


def test_build_server_none_when_binary_missing(tmp_path) -> None:
    """A real model file but an unresolvable binary → None (binary-missing
    degrades gracefully)."""
    model = tmp_path / "worldmodel.json"
    model.write_text("{}")
    # A bogus binary name that is neither an existing file, on PATH, nor at
    # ~/go/bin. Also patch ~/go/bin discovery off via a HOME with no go/bin.
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    with (
        patch(
            "pocketpaw.config.get_settings",
            return_value=_settings(model_path=str(model), loom_bin="loom-nonexistent-xyz"),
        ),
        patch("pocketpaw_ee.agent.mcp_servers.loom.Path.home", return_value=empty_home),
    ):
        assert loom.build_loom_server() is None


# --- build_server: happy path ----------------------------------------------


def test_build_server_returns_stdio_config_when_set(tmp_path) -> None:
    """Model file + resolvable binary → ("loom", <McpStdioServerConfig dict>)
    with the command resolved and the `mcp -model <path>` args."""
    model = tmp_path / "worldmodel.json"
    model.write_text("{}")
    fake_bin = tmp_path / "loom"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)

    with patch(
        "pocketpaw.config.get_settings",
        return_value=_settings(model_path=str(model), loom_bin=str(fake_bin)),
    ):
        built = loom.build_loom_server()

    assert built is not None
    name, config = built
    assert name == "loom"
    assert config["type"] == "stdio"
    assert config["command"] == str(fake_bin)
    assert config["args"] == ["mcp", "-model", str(model)]


# --- binary resolution order -----------------------------------------------


def test_resolve_bin_prefers_explicit_path(tmp_path) -> None:
    """An explicit executable path is returned verbatim (resolved)."""
    explicit = tmp_path / "loom"
    explicit.write_text("#!/bin/sh\n")
    explicit.chmod(0o755)
    assert loom._resolve_loom_bin(str(explicit)) == str(explicit)


def test_resolve_bin_falls_back_to_path(tmp_path) -> None:
    """A bare name not on disk resolves via PATH (shutil.which)."""
    found = str(tmp_path / "loom-on-path")
    with patch("pocketpaw_ee.agent.mcp_servers.loom.shutil.which", return_value=found):
        assert loom._resolve_loom_bin("loom") == found


def test_resolve_bin_falls_back_to_go_bin(tmp_path) -> None:
    """Not an explicit file and not on PATH → ~/go/bin/loom fallback."""
    home = tmp_path / "home"
    (home / "go" / "bin").mkdir(parents=True)
    go_loom = home / "go" / "bin" / "loom"
    go_loom.write_text("#!/bin/sh\n")
    go_loom.chmod(0o755)
    with (
        patch("pocketpaw_ee.agent.mcp_servers.loom.shutil.which", return_value=None),
        patch("pocketpaw_ee.agent.mcp_servers.loom.Path.home", return_value=home),
    ):
        assert loom._resolve_loom_bin("loom") == str(go_loom)


def test_resolve_bin_returns_none_when_nothing_found(tmp_path) -> None:
    """Nothing on disk matches → None so the caller degrades to None."""
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    with (
        patch("pocketpaw_ee.agent.mcp_servers.loom.shutil.which", return_value=None),
        patch("pocketpaw_ee.agent.mcp_servers.loom.Path.home", return_value=empty_home),
    ):
        assert loom._resolve_loom_bin("loom-nope") is None


# --- Path A: the registration loop accepts the stdio config shape ----------


class TestLoomRegistrationLoop:
    """The spike verdict, pinned as a test: the stdio CONFIG DICT the loom
    provider returns flows through the real ``_get_mcp_servers`` loop untouched
    (Path A — no loop change). The loop does ``servers[name] = cfg_entry`` with
    no transformation, so a dict registers identically to an SDK server object.
    """

    def _make_sdk(self) -> ClaudeAgentSDK:
        settings = Settings(anthropic_api_key="test-key", tool_profile="full")
        with patch.object(ClaudeAgentSDK, "_initialize"):
            sdk = ClaudeAgentSDK(settings)
            sdk._sdk_available = False
        return sdk

    def test_loom_is_ambient_not_opt_in(self) -> None:
        """loom is ambient — the /belt surface scopes it via its profile
        allowlist, so it must NOT be in the opt-in set."""
        assert loom.SERVER_NAME not in OPT_IN_MCP_SERVERS

    def test_registration_accepts_stdio_config(self, tmp_path) -> None:
        """When loom is enabled, the registration loop accepts the stdio config
        dict and keeps ``type: "stdio"`` intact — proving Path A end to end."""
        model = tmp_path / "worldmodel.json"
        model.write_text("{}")
        fake_bin = tmp_path / "loom"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)

        sdk = self._make_sdk()
        loom_settings = _settings(model_path=str(model), loom_bin=str(fake_bin))
        with (
            patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]),
            patch("pocketpaw.config.get_settings", return_value=loom_settings),
        ):
            servers = sdk._get_mcp_servers()

        assert loom.SERVER_NAME in servers
        entry = servers[loom.SERVER_NAME]
        assert entry["type"] == "stdio"
        assert entry["command"] == str(fake_bin)
        assert entry["args"] == ["mcp", "-model", str(model)]

    def test_loom_absent_when_disabled(self, tmp_path) -> None:
        """loom_model_path unset → loom not registered, other servers unaffected."""
        sdk = self._make_sdk()
        loom_settings = _settings(model_path=None)
        with (
            patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]),
            patch("pocketpaw.config.get_settings", return_value=loom_settings),
        ):
            servers = sdk._get_mcp_servers()
        assert loom.SERVER_NAME not in servers

    def test_loom_tool_ids_on_allowlist(self) -> None:
        """The loom tool ids are on the in-process allowlist by default (ambient,
        no opt-in) — the agent can call ``mcp__loom__orient`` once the surface
        allowlist permits it."""
        sdk = self._make_sdk()
        ids = sdk._collect_mcp_tool_ids()
        for tool_id in loom.LOOM_TOOL_IDS:
            assert tool_id in ids
