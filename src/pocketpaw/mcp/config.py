"""MCP server configuration — load/save from ~/.pocketpaw/mcp_servers.json.

Created: 2026-02-07
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pocketpaw.config import get_config_dir

logger = logging.getLogger(__name__)

MCP_CONFIG_FILENAME = "mcp_servers.json"


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""

    name: str
    transport: str = "stdio"  # "stdio", "http" (SSE), or "streamable-http"
    command: str = ""  # For stdio: executable command
    args: list[str] = field(default_factory=list)  # For stdio: command arguments
    url: str = ""  # For http: server URL
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: int = 30  # Connection timeout in seconds
    # Legacy: original registry identifier (e.g. "@cmd8/excalidraw-mcp@0.1.4").
    # Kept for backward compatibility with servers installed from the now-removed
    # MCP Registry tab. Not used by new installations.
    registry_ref: str = ""
    oauth: bool = False  # True if server uses OAuth authentication

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "url": self.url,
            "env": self.env,
            "enabled": self.enabled,
            "timeout": self.timeout,
        }
        if self.registry_ref:
            d["registry_ref"] = self.registry_ref
        if self.oauth:
            d["oauth"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict) -> MCPServerConfig:
        return cls(
            name=data.get("name", ""),
            transport=data.get("transport", "stdio"),
            command=data.get("command", ""),
            args=data.get("args", []),
            url=data.get("url", ""),
            env=data.get("env", {}),
            enabled=data.get("enabled", True),
            timeout=data.get("timeout", 30),
            registry_ref=data.get("registry_ref", ""),
            oauth=data.get("oauth", False),
        )


def _get_mcp_config_path() -> Path:
    return get_config_dir() / MCP_CONFIG_FILENAME


def load_mcp_config() -> list[MCPServerConfig]:
    """Load MCP server configs from disk."""
    path = _get_mcp_config_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        servers = data.get("servers", [])
        return [MCPServerConfig.from_dict(s) for s in servers]
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Failed to load MCP config: %s", e)
        return []


def save_mcp_config(configs: list[MCPServerConfig]) -> None:
    """Save MCP server configs to disk."""
    path = _get_mcp_config_path()
    data = {"servers": [c.to_dict() for c in configs]}
    path.write_text(json.dumps(data, indent=2))
    logger.info("Saved %d MCP server configs", len(configs))
