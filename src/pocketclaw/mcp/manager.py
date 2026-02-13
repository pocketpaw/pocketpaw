"""MCP Manager — lifecycle, tool discovery, and tool execution for MCP servers.

Singleton manager that handles:
- Starting/stopping MCP server subprocesses (stdio) or HTTP connections
- Tool discovery via session.list_tools()
- Tool execution via session.call_tool()
- Caching discovered tools for fast access

Created: 2026-02-07
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from pocketclaw.mcp.config import MCPServerConfig, load_mcp_config, save_mcp_config

logger = logging.getLogger(__name__)


@dataclass
class MCPToolInfo:
    """Metadata about a tool discovered from an MCP server."""

    server_name: str
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


@dataclass
class _ServerState:
    """Internal state for a connected MCP server."""

    config: MCPServerConfig
    session: Any = None  # mcp.ClientSession
    client: Any = None  # context manager
    read_stream: Any = None
    write_stream: Any = None
    tools: list[MCPToolInfo] = field(default_factory=list)
    error: str = ""
    connected: bool = False


class MCPManager:
    """Manages MCP server connections and tool invocations."""

    # Env vars safe to inherit from host → MCP subprocesses.
    # Excludes secrets (API keys, tokens) that subprocess shouldn't access.
    _SAFE_ENV_KEYS = {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "TERM",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        # Node.js / npm
        "NODE_PATH",
        "NODE_ENV",
        "NPM_CONFIG_PREFIX",
        "NVM_DIR",
        # Python
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        # Windows essentials
        "SYSTEMROOT",
        "COMSPEC",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
    }

    def __init__(self) -> None:
        self._servers: dict[str, _ServerState] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def _build_safe_env(cls, config_env: dict[str, str]) -> dict[str, str]:
        """Build a filtered env dict for MCP subprocesses.

        Only passes safe host env vars + explicit config overrides.
        This prevents leaking API keys, tokens, and credentials.
        """
        env = {}
        for key in cls._SAFE_ENV_KEYS:
            if key in os.environ:
                env[key] = os.environ[key]
        # Config-specified env vars always override (user explicitly wants these)
        env.update(config_env)
        return env

    async def start_server(self, config: MCPServerConfig) -> bool:
        """Start an MCP server and initialize its session.

        Returns True on success, False on failure.
        """
        async with self._lock:
            if config.name in self._servers and self._servers[config.name].connected:
                logger.info("MCP server '%s' already connected", config.name)
                return True

            state = _ServerState(config=config)
            self._servers[config.name] = state

            try:
                timeout = config.timeout or 30
                if config.transport == "stdio":
                    await asyncio.wait_for(self._connect_stdio(state), timeout=timeout)
                elif config.transport == "streamable-http":
                    # Streamable HTTP uses a different MCP SDK client than SSE.
                    await self._connect_remote_with_timeout(
                        state, timeout, self._connect_streamable_http
                    )
                elif config.transport == "http":
                    # HTTP/SSE connections use anyio cancel scopes internally.
                    # asyncio.wait_for cancels the task on timeout, which disrupts
                    # anyio's cancel scope cleanup and causes TaskGroup errors.
                    # Instead, run with a manual timeout that doesn't cancel the task.
                    await self._connect_remote_with_timeout(
                        state, timeout, self._connect_sse
                    )
                else:
                    state.error = f"Unknown transport: {config.transport}"
                    logger.error(state.error)
                    return False

                # Discover tools (also bounded by timeout)
                await asyncio.wait_for(self._discover_tools(state), timeout=timeout)
                state.connected = True
                logger.info(
                    "MCP server '%s' started — %d tools",
                    config.name,
                    len(state.tools),
                )
                return True

            except TimeoutError:
                state.error = f"Connection timed out after {timeout}s"
                state.connected = False
                await self._cleanup_state(state)
                logger.error("MCP server '%s' timed out after %ds", config.name, timeout)
                return False
            except BaseException as e:
                # Catch BaseException to handle ExceptionGroup / BaseExceptionGroup
                # from anyio TaskGroup failures in the MCP library.
                state.error = str(e)
                state.connected = False
                await self._cleanup_state(state)
                logger.error("Failed to start MCP server '%s': %s", config.name, e)
                return False

    async def _connect_stdio(self, state: _ServerState) -> None:
        """Connect to an MCP server via stdio subprocess."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = self._build_safe_env(state.config.env)
        params = StdioServerParameters(
            command=state.config.command,
            args=state.config.args,
            env=env,
        )

        # stdio_client is an async context manager — we enter it manually
        # and keep it alive until stop_server.
        # If session init fails, we must clean up the transport context.
        ctx = stdio_client(params)
        streams = await ctx.__aenter__()
        state.client = ctx
        state.read_stream = streams[0]
        state.write_stream = streams[1]

        try:
            session = ClientSession(state.read_stream, state.write_stream)
            await session.__aenter__()
            await session.initialize()
            state.session = session
        except Exception:
            await ctx.__aexit__(None, None, None)
            state.client = None
            raise

    async def _connect_remote_with_timeout(
        self,
        state: _ServerState,
        timeout: int,
        connect_fn: Any,
    ) -> None:
        """Connect to a remote MCP server with safe timeout handling.

        Unlike asyncio.wait_for, this approach doesn't cancel the connection task
        directly — which would disrupt anyio's cancel scope inside the MCP client and
        cause TaskGroup errors.  Instead we run the connection in a shielded task
        and handle timeout ourselves.
        """
        task = asyncio.ensure_future(connect_fn(state))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            # The shielded task is still running — cancel it and wait for cleanup.
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass
            # Clean up any partially-opened resources.
            await self._cleanup_state(state)
            raise

    async def _connect_sse(self, state: _ServerState) -> None:
        """Connect to an MCP server via SSE (Server-Sent Events)."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        ctx = sse_client(url=state.config.url)
        streams = await ctx.__aenter__()
        state.client = ctx
        state.read_stream = streams[0]
        state.write_stream = streams[1]

        try:
            session = ClientSession(state.read_stream, state.write_stream)
            await session.__aenter__()
            await session.initialize()
            state.session = session
        except Exception:
            await ctx.__aexit__(None, None, None)
            state.client = None
            raise

    async def _connect_streamable_http(self, state: _ServerState) -> None:
        """Connect to an MCP server via Streamable HTTP transport."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        ctx = streamablehttp_client(url=state.config.url)
        streams = await ctx.__aenter__()
        state.client = ctx
        # streamablehttp_client yields (read, write, get_session_id)
        state.read_stream = streams[0]
        state.write_stream = streams[1]

        try:
            session = ClientSession(state.read_stream, state.write_stream)
            await session.__aenter__()
            await session.initialize()
            state.session = session
        except Exception:
            await ctx.__aexit__(None, None, None)
            state.client = None
            raise

    async def _discover_tools(self, state: _ServerState) -> None:
        """Discover tools from a connected MCP session."""
        if not state.session:
            return
        result = await state.session.list_tools()
        state.tools = [
            MCPToolInfo(
                server_name=state.config.name,
                name=tool.name,
                description=getattr(tool, "description", "") or "",
                input_schema=getattr(tool, "inputSchema", {}) or {},
            )
            for tool in result.tools
        ]

    async def stop_server(self, name: str) -> bool:
        """Stop a running MCP server. Returns True if it was running."""
        async with self._lock:
            state = self._servers.pop(name, None)
            if state is None:
                return False
            await self._cleanup_state(state)
            logger.info("MCP server '%s' stopped", name)
            return True

    async def stop_all(self) -> None:
        """Stop all running MCP servers."""
        async with self._lock:
            for name in list(self._servers):
                state = self._servers.pop(name)
                await self._cleanup_state(state)
            logger.info("All MCP servers stopped")

    async def _cleanup_state(self, state: _ServerState) -> None:
        """Clean up a server state's resources."""
        try:
            if state.session:
                await state.session.__aexit__(None, None, None)
        except Exception as e:
            logger.debug("Error closing MCP session: %s", e)
        try:
            if state.client:
                await state.client.__aexit__(None, None, None)
        except Exception as e:
            logger.debug("Error closing MCP client: %s", e)
        state.connected = False

    def discover_tools(self, name: str) -> list[MCPToolInfo]:
        """Return cached tools for a given server (synchronous)."""
        state = self._servers.get(name)
        if state is None or not state.connected:
            return []
        return list(state.tools)

    def get_all_tools(self) -> list[MCPToolInfo]:
        """Return all tools from all connected servers."""
        tools: list[MCPToolInfo] = []
        for state in self._servers.values():
            if state.connected:
                tools.extend(state.tools)
        return tools

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> str:
        """Call a tool on a connected MCP server, returning the text result."""
        state = self._servers.get(server_name)
        if state is None or not state.connected or not state.session:
            return f"Error: MCP server '{server_name}' is not connected"

        try:
            result = await state.session.call_tool(tool_name, arguments or {})
            # Extract text from result content blocks
            texts = []
            for block in result.content:
                if hasattr(block, "text"):
                    texts.append(block.text)
            return "\n".join(texts) if texts else "(no output)"
        except Exception as e:
            logger.error("MCP tool call failed (%s/%s): %s", server_name, tool_name, e)
            return f"Error calling {tool_name}: {e}"

    def get_server_status(self) -> dict[str, dict]:
        """Return status dict for ALL configured servers.

        Merges config-file servers with runtime state so that servers
        that were never started (or were stopped) still appear in the UI.
        """
        result = {}
        # First, include all servers from the config file
        for cfg in load_mcp_config():
            result[cfg.name] = {
                "connected": False,
                "tool_count": 0,
                "error": "",
                "transport": cfg.transport,
                "enabled": cfg.enabled,
            }
        # Overlay runtime state for servers that have been started
        for name, state in self._servers.items():
            result[name] = {
                "connected": state.connected,
                "tool_count": len(state.tools),
                "error": state.error,
                "transport": state.config.transport,
                "enabled": state.config.enabled,
            }
        return result

    async def start_enabled_servers(self) -> None:
        """Start all enabled servers from config."""
        configs = load_mcp_config()
        for config in configs:
            if config.enabled:
                await self.start_server(config)

    def add_server_config(self, config: MCPServerConfig) -> None:
        """Add a server config and persist it."""
        configs = load_mcp_config()
        # Replace if name already exists
        configs = [c for c in configs if c.name != config.name]
        configs.append(config)
        save_mcp_config(configs)

    def remove_server_config(self, name: str) -> bool:
        """Remove a server config by name. Returns True if found."""
        configs = load_mcp_config()
        new_configs = [c for c in configs if c.name != name]
        if len(new_configs) == len(configs):
            return False
        save_mcp_config(new_configs)
        return True

    def toggle_server_config(self, name: str) -> bool | None:
        """Toggle enabled state of a server config. Returns new state or None if not found."""
        configs = load_mcp_config()
        for config in configs:
            if config.name == name:
                config.enabled = not config.enabled
                save_mcp_config(configs)
                return config.enabled
        return None


# Singleton
_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    """Get the singleton MCPManager instance."""
    global _manager
    if _manager is None:
        _manager = MCPManager()

        from pocketclaw.lifecycle import register

        def _reset():
            global _manager
            _manager = None

        register("mcp_manager", shutdown=_manager.stop_all, reset=_reset)
    return _manager
