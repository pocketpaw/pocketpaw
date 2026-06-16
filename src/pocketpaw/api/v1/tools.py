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

    # OAuth connection status — check which services have saved tokens
    # and whether they are still valid (not expired without a refresh path).
    # Also sync valid tokens into the connector registry so the Data Sources
    # tab and agent tools see the same "connected" status.
    oauth_status: dict[str, str] = {}
    try:
        import time as _time

        from pocketpaw.clients.token_store import TokenStore
        from pocketpaw.config import Settings

        settings = Settings.load()
        store = TokenStore()
        has_google_creds = bool(settings.google_oauth_client_id)

        # Map OAuth service names to connector names — used to sync the
        # connector registry whenever we find valid tokens.
        _OAUTH_SVC_TO_CONNECTOR: dict[str, str] = {
            "google_gmail": "gmail",
            "google_calendar": "gcalendar",
            "google_drive": "google_drive",
            "google_docs": "gdocs",
            "spotify": "spotify",
        }

        for svc in ("google_gmail", "google_calendar", "google_drive", "google_docs", "spotify"):
            tokens = store.load(svc)
            if tokens and tokens.access_token:
                # If the token has an expires_at that is past due AND there
                # is no refresh_token to auto-renew, report it as "expired".
                # A valid refresh_token means auto-refresh will handle it
                # on next use, so we still show "connected".
                if (
                    tokens.expires_at
                    and tokens.expires_at < _time.time()
                    and not tokens.refresh_token
                ):
                    oauth_status[svc] = "expired"
                else:
                    oauth_status[svc] = "connected"
                    # Sync: ensure the connector registry also knows this
                    # connector is connected. This bridges the gap between
                    # the OAuth token store (checked by this endpoint) and
                    # the connector registry state store (checked by the
                    # Data Sources tab and agent tools).
                    try:
                        connector_name = _OAUTH_SVC_TO_CONNECTOR.get(svc)
                        if connector_name:
                            from pocketpaw.api.v1.connectors import (
                                _get_registry as _get_connector_registry,
                            )

                            reg = _get_connector_registry()
                            # Only auto-connect if the registry doesn't already
                            # have it — avoids redundant writes.
                            is_connected = any(
                                s["name"] == connector_name and s["status"].value == "connected"
                                for s in reg.status("default")
                            )
                            if not is_connected:
                                scopes = " ".join(tokens.scopes) if tokens.scopes else svc
                                await reg.connect("default", connector_name, {"scope": scopes})
                    except Exception as sync_exc:
                        logger.debug("OAuth→registry sync failed for %s: %s", svc, sync_exc)
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
