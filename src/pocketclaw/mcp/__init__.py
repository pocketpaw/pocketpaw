"""MCP (Model Context Protocol) Client Support.

Allows PocketPaw to connect to any MCP server and use its tools,
without needing custom tool implementations.

Created: 2026-02-07
"""

from pocketclaw.mcp.config import MCPServerConfig, load_mcp_config, save_mcp_config
from pocketclaw.mcp.manager import MCPManager, MCPToolInfo, get_mcp_manager

__all__ = [
    "MCPManager",
    "MCPServerConfig",
    "MCPToolInfo",
    "get_mcp_manager",
    "load_mcp_config",
    "save_mcp_config",
]
