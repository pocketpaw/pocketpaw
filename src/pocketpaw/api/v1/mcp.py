# MCP router — MCP server CRUD, presets, test.
# Created: 2026-02-20

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.common import StatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["MCP"], dependencies=[Depends(require_scope("admin"))])


@router.get("/mcp/status")
async def get_mcp_status():
    """Get status of all configured MCP servers."""
    from pocketpaw.mcp.manager import get_mcp_manager

    mgr = get_mcp_manager()
    return mgr.get_server_status()


@router.post("/mcp/add", response_model=StatusResponse)
async def add_mcp_server(request: Request):
    """Add a new MCP server configuration and optionally start it."""
    from pocketpaw.mcp.config import MCPServerConfig
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    config = MCPServerConfig(
        name=data.get("name", ""),
        transport=data.get("transport", "stdio"),
        command=data.get("command", ""),
        args=data.get("args", []),
        url=data.get("url", ""),
        env=data.get("env", {}),
        enabled=data.get("enabled", True),
    )
    if not config.name:
        raise HTTPException(status_code=400, detail="Server name is required")

    mgr = get_mcp_manager()
    mgr.add_server_config(config)

    if config.enabled:
        try:
            await mgr.start_server(config)
        except Exception as e:
            logger.warning("Failed to auto-start MCP server '%s': %s", config.name, e)

    return StatusResponse()


@router.post("/mcp/remove")
async def remove_mcp_server(request: Request):
    """Remove an MCP server config and stop it if running."""
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    name = data.get("name", "")

    mgr = get_mcp_manager()
    await mgr.stop_server(name)
    removed = mgr.remove_server_config(name)
    if not removed:
        return {"error": f"Server '{name}' not found"}
    return {"status": "ok"}


@router.post("/mcp/toggle")
async def toggle_mcp_server(request: Request):
    """Toggle an MCP server: start if stopped, stop if running."""
    from pocketpaw.mcp.config import load_mcp_config
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    name = data.get("name", "")

    mgr = get_mcp_manager()
    status = mgr.get_server_status()
    server_info = status.get(name)

    if server_info is None:
        return {"error": f"Server '{name}' not found"}

    if server_info["connected"]:
        mgr.toggle_server_config(name)
        await mgr.stop_server(name)
        return {"status": "ok", "enabled": False}
    else:
        configs = load_mcp_config()
        config = next((c for c in configs if c.name == name), None)
        if not config:
            return {"error": f"No config found for '{name}'"}
        if not config.enabled:
            mgr.toggle_server_config(name)
        connected = await mgr.start_server(config)
        return {"status": "ok", "enabled": True, "connected": connected}


@router.post("/mcp/test")
async def test_mcp_server(request: Request):
    """Test an MCP server connection and return discovered tools."""
    from pocketpaw.mcp.config import MCPServerConfig
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    config = MCPServerConfig(
        name=data.get("name", "test"),
        transport=data.get("transport", "stdio"),
        command=data.get("command", ""),
        args=data.get("args", []),
        url=data.get("url", ""),
        env=data.get("env", {}),
    )

    mgr = get_mcp_manager()
    success = await mgr.start_server(config)
    if not success:
        status = mgr.get_server_status().get(config.name, {})
        return {"connected": False, "error": status.get("error", "Unknown error"), "tools": []}

    tools = mgr.discover_tools(config.name)
    await mgr.stop_server(config.name)
    return {
        "connected": True,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
    }


@router.get("/mcp/presets")
async def list_mcp_presets():
    """Return all MCP presets with installed flag."""
    from pocketpaw.mcp.config import load_mcp_config
    from pocketpaw.mcp.presets import get_all_presets

    installed_names = {c.name for c in load_mcp_config()}
    presets = get_all_presets()
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "icon": p.icon,
            "category": p.category,
            "package": p.package,
            "transport": p.transport,
            "url": p.url,
            "docs_url": p.docs_url,
            "needs_args": p.needs_args,
            "oauth": p.oauth,
            "installed": p.id in installed_names,
            "env_keys": [
                {
                    "key": e.key,
                    "label": e.label,
                    "required": e.required,
                    "placeholder": e.placeholder,
                    "secret": e.secret,
                }
                for e in p.env_keys
            ],
        }
        for p in presets
    ]


@router.post("/mcp/presets/install")
async def install_mcp_preset(request: Request):
    """Install an MCP preset by ID with user-supplied env vars."""
    from pocketpaw.mcp.manager import get_mcp_manager
    from pocketpaw.mcp.presets import get_preset, preset_to_config

    data = await request.json()
    preset_id = data.get("preset_id", "")
    env = data.get("env", {})
    extra_args = data.get("extra_args", None)

    preset = get_preset(preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail=f"Unknown preset: {preset_id}")

    missing = [ek.key for ek in preset.env_keys if ek.required and not env.get(ek.key)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required env vars: {', '.join(missing)}",
        )

    config = preset_to_config(preset, env=env, extra_args=extra_args)
    mgr = get_mcp_manager()
    mgr.add_server_config(config)
    connected = await mgr.start_server(config)
    tools = mgr.discover_tools(config.name) if connected else []

    return {
        "status": "ok",
        "connected": connected,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
    }


@router.get("/mcp/oauth/callback")
async def mcp_oauth_callback(code: str = "", state: str = ""):
    """OAuth callback endpoint for MCP providers."""
    from pocketpaw.mcp.manager import set_oauth_callback_result

    if not code or not state:
        return HTMLResponse(
            "<html><body><h3>Missing code or state parameter.</h3></body></html>",
            status_code=400,
        )

    resolved = set_oauth_callback_result(state, code)
    if resolved:
        return HTMLResponse(
            "<html><body>"
            "<h3>Authenticated! You can close this tab.</h3>"
            "<script>window.close()</script>"
            "</body></html>"
        )
    return HTMLResponse(
        "<html><body><h3>OAuth flow expired or not found.</h3></body></html>",
        status_code=400,
    )
