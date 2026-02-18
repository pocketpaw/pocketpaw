"""MCP OAuth Token Storage — file-based persistence for MCP OAuth tokens.

Implements the MCP SDK's ``TokenStorage`` protocol for persisting OAuth tokens
and client registration info to ``~/.pocketpaw/mcp_oauth/{server_name}.json``.

Created: 2026-02-17
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from pocketpaw.config import get_config_dir

logger = logging.getLogger(__name__)


def _get_oauth_dir() -> Path:
    """Get/create the MCP OAuth token directory."""
    d = get_config_dir() / "mcp_oauth"
    d.mkdir(exist_ok=True)
    return d


class MCPTokenStorage:
    """File-based token storage for MCP OAuth at ~/.pocketpaw/mcp_oauth/{name}.json.

    Stores both OAuth tokens and dynamic client registration info.
    Files are chmod 0600 (owner-only read/write).
    """

    def __init__(self, server_name: str) -> None:
        self._server_name = server_name
        self._path = _get_oauth_dir() / f"{server_name}.json"

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load MCP OAuth data for %s: %s", self._server_name, e)
            return {}

    def _save(self, data: dict) -> None:
        self._path.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    async def get_tokens(self):
        """Get stored OAuth tokens."""
        from mcp.shared.auth import OAuthToken

        data = self._load()
        tokens_data = data.get("tokens")
        if not tokens_data:
            return None
        try:
            return OAuthToken.model_validate(tokens_data)
        except Exception as e:
            logger.warning("Failed to parse MCP OAuth tokens for %s: %s", self._server_name, e)
            return None

    async def set_tokens(self, tokens) -> None:
        """Store OAuth tokens."""
        data = self._load()
        data["tokens"] = tokens.model_dump()
        self._save(data)
        logger.debug("Saved MCP OAuth tokens for %s", self._server_name)

    async def get_client_info(self):
        """Get stored client registration info."""
        from mcp.shared.auth import OAuthClientInformationFull

        data = self._load()
        client_data = data.get("client_info")
        if not client_data:
            return None
        try:
            return OAuthClientInformationFull.model_validate(client_data)
        except Exception as e:
            logger.warning("Failed to parse MCP OAuth client info for %s: %s", self._server_name, e)
            return None

    async def set_client_info(self, client_info) -> None:
        """Store client registration info."""
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json")
        self._save(data)
        logger.debug("Saved MCP OAuth client info for %s", self._server_name)
