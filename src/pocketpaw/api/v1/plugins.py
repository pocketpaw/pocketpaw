# src/pocketpaw/api/v1/plugins.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — Plugins router.
# Updated: 2026-06-08 (feat/plugin-installer-mcp) — install now also
# registers + starts a bundle's MCP servers (#1357).
# Updated: 2026-06-08 (feat/plugin-installer-listremove, #1358) — added
# GET /api/v1/plugins (list installed plugins) and POST /api/v1/plugins/remove
# ({"name"} → uninstall skills + MCP servers, drop the registry entry).
# Unknown plugin maps to 404. Both admin-scoped, audited in the installer.
# POST /api/v1/plugins/install clones a .claude-plugin repo, installs its
# skills and MCP servers, and returns a step-by-step PluginInstallReport.
# Admin-scoped, mirroring api/v1/mcp.py.
"""REST surface for the Plugin Installer (skills + MCP)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from pocketpaw.api.deps import require_scope
from pocketpaw.plugins.installer import PluginInstaller, PluginInstallError
from pocketpaw.plugins.models import (
    InstalledPlugin,
    PluginInstallReport,
    PluginRemoveReport,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Plugins"], dependencies=[Depends(require_scope("admin"))])


@router.post("/plugins/install", response_model=PluginInstallReport)
async def install_plugin(request: Request) -> PluginInstallReport:
    """Install a .claude-plugin's skills from a GitHub source.

    Body: ``{"source": "owner/repo"}`` (also ``owner/repo/subdir`` or a
    GitHub URL). Returns a :class:`PluginInstallReport`.
    """
    data = await request.json()
    source = (data.get("source") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="Missing 'source' field")

    try:
        return await PluginInstaller().install(source)
    except PluginInstallError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception:
        logger.exception("Plugin install failed")
        raise HTTPException(status_code=500, detail="Plugin install failed")


@router.get("/plugins", response_model=list[InstalledPlugin])
async def list_plugins() -> list[InstalledPlugin]:
    """List installed plugins from the registry.

    Returns each plugin's name, version, source, installed skills, namespaced
    MCP servers, and install timestamp.
    """
    return PluginInstaller().list_plugins()


@router.post("/plugins/remove", response_model=PluginRemoveReport)
async def remove_plugin(request: Request) -> PluginRemoveReport:
    """Uninstall a plugin's skills and MCP servers, drop its registry entry.

    Body: ``{"name": "my-plugin"}``. Returns a :class:`PluginRemoveReport`.
    An unknown plugin maps to 404; per-component cleanup failures surface as
    ``failed`` steps in the report rather than aborting the request.
    """
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Missing 'name' field")

    try:
        return await PluginInstaller().remove(name)
    except PluginInstallError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception:
        logger.exception("Plugin remove failed")
        raise HTTPException(status_code=500, detail="Plugin remove failed")
