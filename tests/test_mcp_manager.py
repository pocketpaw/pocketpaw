"""Tests for MCP Manager — Sprint 16.

All MCP SDK imports are mocked since mcp is an optional dependency.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pocketpaw.mcp.config import MCPServerConfig, load_mcp_config, save_mcp_config
from pocketpaw.mcp.manager import MCPManager, MCPToolInfo, get_mcp_manager

# ======================================================================
# MCPServerConfig tests
# ======================================================================


class TestMCPServerConfig:
    def test_default_values(self):
        cfg = MCPServerConfig(name="test")
        assert cfg.name == "test"
        assert cfg.transport == "stdio"
        assert cfg.command == ""
        assert cfg.args == []
        assert cfg.url == ""
        assert cfg.env == {}
        assert cfg.enabled is True
        assert cfg.timeout == 30

    def test_to_dict(self):
        cfg = MCPServerConfig(
            name="fs",
            transport="stdio",
            command="npx",
            args=["-y", "@mcp/server-fs", "/home"],
            env={"NODE_ENV": "production"},
            enabled=True,
            timeout=60,
        )
        d = cfg.to_dict()
        assert d["name"] == "fs"
        assert d["command"] == "npx"
        assert d["args"] == ["-y", "@mcp/server-fs", "/home"]
        assert d["env"] == {"NODE_ENV": "production"}
        assert d["timeout"] == 60

    def test_from_dict(self):
        data = {
            "name": "github",
            "transport": "http",
            "url": "http://localhost:9000",
            "enabled": False,
        }
        cfg = MCPServerConfig.from_dict(data)
        assert cfg.name == "github"
        assert cfg.transport == "http"
        assert cfg.url == "http://localhost:9000"
        assert cfg.enabled is False
        # defaults
        assert cfg.command == ""
        assert cfg.args == []
        assert cfg.timeout == 30

    def test_from_dict_missing_name(self):
        cfg = MCPServerConfig.from_dict({})
        assert cfg.name == ""

    def test_roundtrip(self):
        cfg = MCPServerConfig(
            name="roundtrip",
            transport="stdio",
            command="node",
            args=["server.js"],
            env={"KEY": "VAL"},
            enabled=True,
            timeout=15,
        )
        restored = MCPServerConfig.from_dict(cfg.to_dict())
        assert restored.name == cfg.name
        assert restored.command == cfg.command
        assert restored.args == cfg.args
        assert restored.env == cfg.env
        assert restored.timeout == cfg.timeout


# ======================================================================
# load_mcp_config / save_mcp_config tests
# ======================================================================


class TestMCPConfig:
    def test_load_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        result = load_mcp_config()
        assert result == []

    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        configs = [
            MCPServerConfig(name="a", command="cmd-a"),
            MCPServerConfig(name="b", command="cmd-b", enabled=False),
        ]
        save_mcp_config(configs)

        loaded = load_mcp_config()
        assert len(loaded) == 2
        assert loaded[0].name == "a"
        assert loaded[1].name == "b"
        assert loaded[1].enabled is False

    def test_load_corrupt_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        (tmp_path / "mcp_servers.json").write_text("not json")
        result = load_mcp_config()
        assert result == []

    def test_load_missing_servers_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        (tmp_path / "mcp_servers.json").write_text(json.dumps({"other": 1}))
        result = load_mcp_config()
        assert result == []

    def test_save_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        save_mcp_config([MCPServerConfig(name="x")])
        assert (tmp_path / "mcp_servers.json").exists()


# ======================================================================
# MCPToolInfo tests
# ======================================================================


class TestMCPToolInfo:
    def test_basic(self):
        t = MCPToolInfo(server_name="fs", name="read_file", description="Read a file")
        assert t.server_name == "fs"
        assert t.name == "read_file"
        assert t.description == "Read a file"
        assert t.input_schema == {}


# ======================================================================
# MCPManager tests
# ======================================================================


class TestMCPManager:
    def test_get_server_status_empty(self):
        mgr = MCPManager()
        with patch("pocketpaw.mcp.manager.load_mcp_config", return_value=[]):
            assert mgr.get_server_status() == {}

    def test_get_server_status_includes_config_servers(self):
        """Servers from config file appear even if never started."""
        mgr = MCPManager()
        configs = [
            MCPServerConfig(name="saved-server", transport="stdio", enabled=True),
            MCPServerConfig(name="disabled-one", transport="http", enabled=False),
        ]
        with patch("pocketpaw.mcp.manager.load_mcp_config", return_value=configs):
            status = mgr.get_server_status()
        assert "saved-server" in status
        assert status["saved-server"]["connected"] is False
        assert status["saved-server"]["enabled"] is True
        assert "disabled-one" in status
        assert status["disabled-one"]["enabled"] is False

    def test_get_all_tools_empty(self):
        mgr = MCPManager()
        assert mgr.get_all_tools() == []

    def test_discover_tools_unknown_server(self):
        mgr = MCPManager()
        assert mgr.discover_tools("nonexistent") == []

    async def test_stop_server_not_running(self):
        mgr = MCPManager()
        assert await mgr.stop_server("unknown") is False

    async def test_stop_all_empty(self):
        mgr = MCPManager()
        await mgr.stop_all()  # should not raise

    async def test_call_tool_not_connected(self):
        mgr = MCPManager()
        result = await mgr.call_tool("ghost", "read", {})
        assert "not connected" in result

    @patch("pocketpaw.mcp.manager.load_mcp_config")
    async def test_start_enabled_servers(self, mock_load):
        mgr = MCPManager()
        cfg_enabled = MCPServerConfig(name="a", enabled=True)
        cfg_disabled = MCPServerConfig(name="b", enabled=False)
        mock_load.return_value = [cfg_enabled, cfg_disabled]

        with patch.object(mgr, "start_server", new_callable=AsyncMock) as mock_start:
            await mgr.start_enabled_servers()
            # Only enabled server should be started
            mock_start.assert_called_once_with(cfg_enabled)

    async def test_start_server_unknown_transport(self):
        mgr = MCPManager()
        cfg = MCPServerConfig(name="weird", transport="grpc")
        result = await mgr.start_server(cfg)
        assert result is False
        with patch("pocketpaw.mcp.manager.load_mcp_config", return_value=[]):
            status = mgr.get_server_status()
        assert "grpc" in status["weird"]["error"]

    async def test_start_server_stdio_success(self):
        """Test successful stdio connection with fully mocked MCP SDK."""
        mgr = MCPManager()
        cfg = MCPServerConfig(name="fs", transport="stdio", command="npx", args=["server"])

        # Mock tool returned by list_tools
        mock_tool = SimpleNamespace(
            name="read_file",
            description="Read a file",
            inputSchema={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        mock_list_result = SimpleNamespace(tools=[mock_tool])

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.list_tools = AsyncMock(return_value=mock_list_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
        mock_ctx.__aexit__ = AsyncMock()

        import pocketpaw.mcp.manager as mgr_mod

        original_connect = mgr_mod.MCPManager._connect_stdio

        async def patched_connect(self_inner, state):
            state.client = mock_ctx
            state.read_stream = AsyncMock()
            state.write_stream = AsyncMock()
            state.session = mock_session

        mgr_mod.MCPManager._connect_stdio = patched_connect
        try:
            result = await mgr.start_server(cfg)
            assert result is True
            with patch("pocketpaw.mcp.manager.load_mcp_config", return_value=[]):
                assert mgr.get_server_status()["fs"]["connected"] is True
                assert mgr.get_server_status()["fs"]["tool_count"] == 1

            tools = mgr.discover_tools("fs")
            assert len(tools) == 1
            assert tools[0].name == "read_file"

            all_tools = mgr.get_all_tools()
            assert len(all_tools) == 1
        finally:
            mgr_mod.MCPManager._connect_stdio = original_connect

    async def test_start_server_already_connected(self):
        """Starting an already-connected server should return True."""
        mgr = MCPManager()
        from pocketpaw.mcp.manager import _ServerState

        cfg = MCPServerConfig(name="dup")
        state = _ServerState(config=cfg, connected=True)
        mgr._servers["dup"] = state
        result = await mgr.start_server(cfg)
        assert result is True

    async def test_stop_server_running(self):
        """Stop a 'connected' server."""
        mgr = MCPManager()
        from pocketpaw.mcp.manager import _ServerState

        cfg = MCPServerConfig(name="fs")
        state = _ServerState(config=cfg, connected=True)
        state.session = AsyncMock()
        state.session.__aexit__ = AsyncMock()
        state.client = AsyncMock()
        state.client.__aexit__ = AsyncMock()
        mgr._servers["fs"] = state

        result = await mgr.stop_server("fs")
        assert result is True
        assert "fs" not in mgr._servers

    async def test_call_tool_success(self):
        """Test successful tool call."""
        mgr = MCPManager()
        from pocketpaw.mcp.manager import _ServerState

        cfg = MCPServerConfig(name="fs")
        mock_session = AsyncMock()
        block = SimpleNamespace(text="hello world")
        mock_session.call_tool = AsyncMock(return_value=SimpleNamespace(content=[block]))

        state = _ServerState(config=cfg, session=mock_session, connected=True)
        mgr._servers["fs"] = state

        result = await mgr.call_tool("fs", "greet", {"name": "Alice"})
        assert result == "hello world"
        mock_session.call_tool.assert_called_once_with("greet", {"name": "Alice"})

    async def test_call_tool_error(self):
        """Test tool call that raises."""
        mgr = MCPManager()
        from pocketpaw.mcp.manager import _ServerState

        cfg = MCPServerConfig(name="err")
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=RuntimeError("boom"))

        state = _ServerState(config=cfg, session=mock_session, connected=True)
        mgr._servers["err"] = state

        result = await mgr.call_tool("err", "fail", {})
        assert "Error" in result
        assert "boom" in result

    async def test_call_tool_no_text(self):
        """Tool result with no text blocks returns '(no output)'."""
        mgr = MCPManager()
        from pocketpaw.mcp.manager import _ServerState

        cfg = MCPServerConfig(name="empty")
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(
            return_value=SimpleNamespace(content=[SimpleNamespace(image="data:...")])
        )
        state = _ServerState(config=cfg, session=mock_session, connected=True)
        mgr._servers["empty"] = state

        result = await mgr.call_tool("empty", "img", {})
        assert result == "(no output)"


# ======================================================================
# Config management methods on MCPManager
# ======================================================================


class TestMCPManagerConfigMethods:
    def test_add_server_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        monkeypatch.setattr("pocketpaw.mcp.manager.load_mcp_config", load_mcp_config)
        monkeypatch.setattr("pocketpaw.mcp.manager.save_mcp_config", save_mcp_config)

        mgr = MCPManager()
        cfg = MCPServerConfig(name="new-server", command="npx")
        mgr.add_server_config(cfg)

        loaded = load_mcp_config()
        assert len(loaded) == 1
        assert loaded[0].name == "new-server"

    def test_add_server_config_replaces(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        monkeypatch.setattr("pocketpaw.mcp.manager.load_mcp_config", load_mcp_config)
        monkeypatch.setattr("pocketpaw.mcp.manager.save_mcp_config", save_mcp_config)

        mgr = MCPManager()
        mgr.add_server_config(MCPServerConfig(name="s", command="old"))
        mgr.add_server_config(MCPServerConfig(name="s", command="new"))

        loaded = load_mcp_config()
        assert len(loaded) == 1
        assert loaded[0].command == "new"

    def test_remove_server_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        monkeypatch.setattr("pocketpaw.mcp.manager.load_mcp_config", load_mcp_config)
        monkeypatch.setattr("pocketpaw.mcp.manager.save_mcp_config", save_mcp_config)

        mgr = MCPManager()
        mgr.add_server_config(MCPServerConfig(name="a"))
        mgr.add_server_config(MCPServerConfig(name="b"))

        assert mgr.remove_server_config("a") is True
        assert mgr.remove_server_config("a") is False  # already gone

        loaded = load_mcp_config()
        assert len(loaded) == 1
        assert loaded[0].name == "b"

    def test_toggle_server_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pocketpaw.mcp.config.get_config_dir", lambda: tmp_path)
        monkeypatch.setattr("pocketpaw.mcp.manager.load_mcp_config", load_mcp_config)
        monkeypatch.setattr("pocketpaw.mcp.manager.save_mcp_config", save_mcp_config)

        mgr = MCPManager()
        mgr.add_server_config(MCPServerConfig(name="t", enabled=True))

        assert mgr.toggle_server_config("t") is False  # toggled to disabled
        assert mgr.toggle_server_config("t") is True  # toggled back
        assert mgr.toggle_server_config("ghost") is None  # not found


# ======================================================================
# Singleton tests
# ======================================================================


class TestHTTPAutoDetect:
    """Tests for transport='http' auto-detect (Streamable HTTP → SSE fallback)."""

    async def test_http_transport_tries_streamable_first(self):
        """transport='http' should try Streamable HTTP and succeed."""
        mgr = MCPManager()
        cfg = MCPServerConfig(name="modern", transport="http", url="https://example.com/mcp")

        with (
            patch.object(mgr, "_connect_streamable_http", new_callable=AsyncMock),
            patch.object(mgr, "_connect_sse", new_callable=AsyncMock) as mock_sse,
            patch.object(
                mgr,
                "_discover_tools",
                new_callable=AsyncMock,
            ),
        ):
            result = await mgr.start_server(cfg)
            assert result is True
            # SSE should NOT have been called
            mock_sse.assert_not_called()

    async def test_http_transport_falls_back_to_sse(self):
        """transport='http' should fall back to SSE when Streamable HTTP fails."""
        mgr = MCPManager()
        cfg = MCPServerConfig(name="legacy", transport="http", url="https://example.com/mcp")

        with (
            patch.object(
                mgr,
                "_connect_streamable_http",
                new_callable=AsyncMock,
                side_effect=RuntimeError("405 Method Not Allowed"),
            ),
            patch.object(mgr, "_connect_sse", new_callable=AsyncMock),
            patch.object(mgr, "_discover_tools", new_callable=AsyncMock),
            patch.object(mgr, "_cleanup_state", new_callable=AsyncMock),
        ):
            result = await mgr.start_server(cfg)
            assert result is True

    async def test_http_transport_no_fallback_on_timeout(self):
        """transport='http' should NOT fall back to SSE on timeout."""
        mgr = MCPManager()
        cfg = MCPServerConfig(
            name="slow", transport="http", url="https://example.com/mcp", timeout=1
        )

        with (
            patch.object(
                mgr,
                "_connect_remote_with_timeout",
                new_callable=AsyncMock,
                side_effect=TimeoutError("Connection timed out"),
            ),
        ):
            result = await mgr.start_server(cfg)
            assert result is False
            status = mgr._servers["slow"]
            assert "timed out" in status.error


class TestGetMCPManager:
    def test_returns_same_instance(self):
        import pocketpaw.mcp.manager as mod

        mod._manager = None  # reset
        a = get_mcp_manager()
        b = get_mcp_manager()
        assert a is b
        mod._manager = None  # cleanup
