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

    # OAuth connection status — check which services have saved tokens.
    oauth_status: dict[str, str] = {}
    try:
        from pocketpaw.config import Settings
        from pocketpaw.integrations.token_store import TokenStore

        settings = Settings.load()
        store = TokenStore()
        has_google_creds = bool(settings.google_oauth_client_id)

        for svc in ("google_gmail", "google_calendar", "google_drive", "google_docs", "spotify"):
            tokens = store.load(svc)
            if tokens and tokens.access_token:
                oauth_status[svc] = "connected"
            elif svc.startswith("google_") and not has_google_creds:
                oauth_status[svc] = "not_configured"
            else:
                oauth_status[svc] = "disconnected"
    except Exception:
        logger.debug("OAuth status check failed", exc_info=True)

    return {
        "tools": tools,
        "mcp_tools": mcp_tools,
        "groups": TOOL_GROUPS,
        "oauth_status": oauth_status,
    }
