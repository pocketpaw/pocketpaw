# Tools router — list registered tools, MCP tools, and tool groups.
# Created: 2026-03-31

from __future__ import annotations

import logging

from fastapi import APIRouter

from pocketpaw.tools.policy import TOOL_GROUPS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Tools"])


@router.get("/tools")
async def list_tools():
    """Return all registered builtin tools, MCP tools, and tool groups.

    Response shape::

        {
            "tools": [{"name": str, "description": str, "trust_level": str}, ...],
            "mcp_tools": [{"server": str, "name": str, "status": str}, ...],
            "groups": {"group:fs": ["read_file", ...], ...}
        }
    """
    # Builtin tools — imported lazily to avoid circular imports at module load time.
    from pocketpaw.tools.cli import _TOOLS

    tools = sorted(
        [
            {
                "name": tool.definition.name,
                "description": tool.definition.description,
                "trust_level": tool.definition.trust_level,
            }
            for tool in _TOOLS.values()
        ],
        key=lambda t: t["name"],
    )

    # MCP tools — optional; manager may not be initialised yet.
    mcp_tools: list[dict] = []
    try:
        from pocketpaw.mcp.manager import get_mcp_manager

        mgr = get_mcp_manager()
        for tool_info in mgr.get_all_tools():
            mcp_tools.append(
                {
                    "server": tool_info.server_name,
                    "name": tool_info.name,
                    "status": "connected",
                }
            )
    except Exception:
        logger.debug("MCP manager not available for tools listing", exc_info=True)

    # OAuth connection status — check each service's token validity.
    # Uses OAuthManager.get_valid_token() which auto-refreshes expired
    # access tokens and only returns None when the refresh token is also
    # invalid or missing.
    oauth_status: dict[str, str] = {}
    try:
        from pocketpaw.clients.oauth import OAuthManager
        from pocketpaw.clients.token_store import TokenStore
        from pocketpaw.config import Settings

        settings = Settings.load()
        manager = OAuthManager(TokenStore())
        has_google_creds = bool(settings.google_oauth_client_id)
        has_spotify_creds = bool(settings.spotify_client_id)

        for svc in ("google_gmail", "google_calendar", "google_drive", "google_docs", "spotify"):
            if svc.startswith("google_") and not has_google_creds:
                oauth_status[svc] = "not_configured"
                continue
            if svc == "spotify" and not has_spotify_creds:
                oauth_status[svc] = "not_configured"
                continue

            token = await manager.get_valid_token(
                service=svc,
                client_id=settings.google_oauth_client_id or "",
                client_secret=settings.google_oauth_client_secret or "",
                provider="spotify" if svc == "spotify" else "google",
            )
            oauth_status[svc] = "connected" if token else "disconnected"
    except Exception:
        logger.debug("OAuth status check failed", exc_info=True)

    return {
        "tools": tools,
        "mcp_tools": mcp_tools,
        "groups": TOOL_GROUPS,
        "oauth_status": oauth_status,
    }
