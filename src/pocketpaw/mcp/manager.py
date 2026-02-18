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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from pocketpaw.mcp.config import MCPServerConfig, load_mcp_config, save_mcp_config

logger = logging.getLogger(__name__)

# OAuth callback coordination: state -> Future[(code, state)]
_oauth_pending: dict[str, asyncio.Future] = {}

# WebSocket broadcast function injected by dashboard at startup
_ws_broadcast: Callable | None = None


def set_ws_broadcast(fn: Callable) -> None:
    """Set the WebSocket broadcast function (called by dashboard at startup)."""
    global _ws_broadcast
    _ws_broadcast = fn


def set_oauth_callback_result(state: str, code: str) -> bool:
    """Resolve a pending OAuth Future with the authorization code.

    Called by the dashboard callback endpoint when the OAuth provider
    redirects back with code + state params.

    Returns True if a pending Future was found and resolved.
    """
    future = _oauth_pending.get(state)
    if future and not future.done():
        future.set_result((code, state))
        return True
    logger.warning("No pending OAuth flow for state=%s", state[:16])
    return False


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


_UNHELPFUL_ERRORS = {
    "Attempted to exit a cancel scope that isn't the current tasks's current cancel scope",
}


def _extract_root_error(exc: BaseException) -> str:
    """Unwrap ExceptionGroup / BaseExceptionGroup to find the real error.

    MCP stdio transport wraps subprocess crashes in anyio cancel-scope errors
    that are uninformative (e.g. "Attempted to exit a cancel scope…").
    Walk the exception tree to find a concrete root-cause message.
    """
    # Collect all leaf exceptions from exception groups
    leaves: list[BaseException] = []

    def _collect(e: BaseException) -> None:
        if isinstance(e, (ExceptionGroup, BaseExceptionGroup)):
            for sub in e.exceptions:
                _collect(sub)
        elif e.__cause__:
            _collect(e.__cause__)
        else:
            leaves.append(e)

    _collect(exc)

    # Pick the most useful message (skip unhelpful anyio internals)
    for leaf in leaves:
        msg = str(leaf).strip()
        if msg and msg not in _UNHELPFUL_ERRORS:
            return msg

    # Fallback: if everything is unhelpful, use the top-level message and
    # hint that the server process crashed.
    top = str(exc).strip()
    if top in _UNHELPFUL_ERRORS:
        return "Server process crashed during startup (check terminal for details)"
    return top


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

    @staticmethod
    def _make_oauth_auth(config: MCPServerConfig):
        """Create an httpx.Auth for OAuth-based MCP servers.

        Uses the MCP SDK's OAuthClientProvider which handles:
        - OAuth 2.1 metadata discovery
        - CIMD (Client ID Metadata Document) for servers that support it
        - Dynamic client registration as fallback
        - PKCE authorization code flow
        - Token refresh
        """
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import OAuthClientMetadata

        from pocketpaw.config import Settings
        from pocketpaw.mcp.oauth_store import MCPTokenStorage

        settings = Settings.load()
        port = settings.web_port or 8888

        storage = MCPTokenStorage(config.name)

        client_metadata = OAuthClientMetadata(
            client_name="PocketPaw",
            redirect_uris=[f"http://localhost:{port}/api/mcp/oauth/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

        # CIMD URL — servers that support client_id_metadata_document_supported
        # will use this URL as the client_id instead of dynamic registration.
        cimd_url = (settings.mcp_client_metadata_url or "").strip() or None

        # Shared mutable state between the two closures
        _flow_state: dict[str, str] = {}

        async def redirect_handler(auth_url: str) -> None:
            """Called by SDK with the authorization URL — broadcast to frontend."""
            parsed = urlparse(auth_url)
            params = parse_qs(parsed.query)
            state_values = params.get("state", [])
            state = state_values[0] if state_values else ""

            if state:
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                _oauth_pending[state] = future
                _flow_state["state"] = state

            if _ws_broadcast:
                await _ws_broadcast(
                    {
                        "type": "mcp_oauth_redirect",
                        "url": auth_url,
                        "server": config.name,
                    }
                )
            logger.info("OAuth redirect for MCP server '%s' — waiting for callback", config.name)

        async def callback_handler() -> tuple[str, str | None]:
            """Called by SDK to wait for the OAuth callback result."""
            state = _flow_state.get("state")
            if not state or state not in _oauth_pending:
                raise RuntimeError(f"No pending OAuth flow for server '{config.name}'")

            future = _oauth_pending[state]
            try:
                code, returned_state = await asyncio.wait_for(future, timeout=300)
                return (code, returned_state)
            finally:
                _oauth_pending.pop(state, None)

        return OAuthClientProvider(
            server_url=config.url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            client_metadata_url=cimd_url,
        )

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

            # Build OAuth auth if needed
            auth = None
            if config.oauth:
                try:
                    auth = self._make_oauth_auth(config)
                except Exception as e:
                    state.error = f"OAuth setup failed: {e}"
                    logger.error("OAuth setup failed for '%s': %s", config.name, e)
                    return False

            try:
                timeout = config.timeout or 30
                # OAuth flows need more time for user interaction
                connect_timeout = 300 if config.oauth else timeout

                if config.transport == "stdio":
                    await asyncio.wait_for(self._connect_stdio(state), timeout=timeout)
                elif config.transport == "streamable-http":
                    await self._connect_remote_with_timeout(
                        state,
                        connect_timeout,
                        lambda s: self._connect_streamable_http(s, auth=auth),
                    )
                elif config.transport == "sse":
                    await self._connect_remote_with_timeout(
                        state,
                        connect_timeout,
                        lambda s: self._connect_sse(s, auth=auth),
                    )
                elif config.transport == "http":
                    # Auto-detect: try Streamable HTTP first, fall back to SSE.
                    # Modern MCP servers use Streamable HTTP (POST-based);
                    # older ones use SSE (GET-based).
                    try:
                        await self._connect_remote_with_timeout(
                            state,
                            connect_timeout,
                            lambda s: self._connect_streamable_http(
                                s, auth=auth
                            ),
                        )
                    except TimeoutError:
                        raise  # Don't waste time retrying on timeout
                    except BaseException:
                        await self._cleanup_state(state)
                        state = _ServerState(config=config)
                        self._servers[config.name] = state
                        logger.debug(
                            "Streamable HTTP failed for '%s', trying SSE",
                            config.name,
                        )
                        await self._connect_remote_with_timeout(
                            state,
                            connect_timeout,
                            lambda s: self._connect_sse(s, auth=auth),
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
                effective_timeout = 300 if config.oauth else (config.timeout or 30)
                state.error = f"Connection timed out after {effective_timeout}s"
                state.connected = False
                await self._cleanup_state(state)
                logger.error("MCP server '%s' timed out after %ds", config.name, effective_timeout)
                return False
            except BaseException as e:
                # Catch BaseException to handle ExceptionGroup / BaseExceptionGroup
                # from anyio TaskGroup failures in the MCP library.
                root_msg = _extract_root_error(e)

                # Provide actionable hint for OAuth registration failures
                if config.oauth and "Registration failed" in root_msg:
                    root_msg = (
                        f"{root_msg}. "
                        "This server doesn't support dynamic client registration. "
                        "You can set mcp_client_metadata_url in Settings to a "
                        "publicly-hosted CIMD JSON file, or configure the server "
                        "with an API token instead of OAuth."
                    )

                state.error = root_msg
                state.connected = False
                await self._cleanup_state(state)
                logger.error("Failed to start MCP server '%s': %s", config.name, root_msg)
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

    async def _connect_sse(self, state: _ServerState, auth=None) -> None:
        """Connect to an MCP server via SSE (Server-Sent Events)."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        kwargs: dict[str, Any] = {"url": state.config.url}
        if auth is not None:
            kwargs["auth"] = auth
        ctx = sse_client(**kwargs)
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

    async def _connect_streamable_http(self, state: _ServerState, auth=None) -> None:
        """Connect to an MCP server via Streamable HTTP transport."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        kwargs: dict[str, Any] = {"url": state.config.url}
        if auth is not None:
            kwargs["auth"] = auth
        ctx = streamablehttp_client(**kwargs)
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
            info: dict = {
                "connected": False,
                "tool_count": 0,
                "error": "",
                "transport": cfg.transport,
                "enabled": cfg.enabled,
            }
            if cfg.registry_ref:
                info["registry_ref"] = cfg.registry_ref
            result[cfg.name] = info
        # Overlay runtime state for servers that have been started
        for name, state in self._servers.items():
            info = {
                "connected": state.connected,
                "tool_count": len(state.tools),
                "error": state.error,
                "transport": state.config.transport,
                "enabled": state.config.enabled,
            }
            if state.config.registry_ref:
                info["registry_ref"] = state.config.registry_ref
            result[name] = info
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

        from pocketpaw.lifecycle import register

        def _reset():
            global _manager
            _manager = None

        register("mcp_manager", shutdown=_manager.stop_all, reset=_reset)
    return _manager
