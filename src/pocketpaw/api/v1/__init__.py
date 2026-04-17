# API v1 router aggregation.
# Created: 2026-02-20
# Updated: 2026-03-30 — Added Automations router (enterprise, rule-based pocket automations).
#
# mount_v1_routers(app) registers all domain routers at /api/v1/ (canonical).
# Existing dashboard.py endpoints at /api/ remain as backward-compat aliases.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Domain routers — imported lazily inside mount_v1_routers() to avoid circular imports.
_V1_ROUTERS: list[tuple[str, str, str]] = [
    # (module_path, attr_name, tag)
    ("pocketpaw.api.v1.auth", "router", "Auth"),
    ("pocketpaw.api.v1.sessions", "router", "Sessions"),
    ("pocketpaw.api.v1.health", "router", "Health"),
    ("pocketpaw.api.v1.identity", "router", "Identity"),
    ("pocketpaw.api.v1.settings", "router", "Settings"),
    ("pocketpaw.api.v1.channels", "router", "Channels"),
    ("pocketpaw.api.v1.memory", "router", "Memory"),
    ("pocketpaw.api.v1.mcp", "router", "MCP"),
    ("pocketpaw.api.v1.skills", "router", "Skills"),
    ("pocketpaw.api.v1.webhooks", "router", "Webhooks"),
    ("pocketpaw.api.v1.backends", "router", "Backends"),
    ("pocketpaw.api.v1.api_keys", "router", "API Keys"),
    ("pocketpaw.api.v1.oauth2", "router", "OAuth2"),
    ("pocketpaw.api.v1.chat", "router", "Chat"),
    ("pocketpaw.api.v1.reminders", "router", "Reminders"),
    ("pocketpaw.api.v1.intentions", "router", "Intentions"),
    ("pocketpaw.api.v1.files", "router", "Files"),
    ("pocketpaw.api.v1.plan_mode", "router", "Plan Mode"),
    ("pocketpaw.api.v1.remote", "router", "Remote"),
    ("pocketpaw.api.v1.telegram", "router", "Telegram"),
    ("pocketpaw.api.v1.events", "router", "Events"),
    ("pocketpaw.api.v1.kits", "router", "Kits"),
    ("pocketpaw.api.v1.metrics", "router", "Metrics"),
    ("pocketpaw.api.v1.agent_status", "router", "Status"),
    ("pocketpaw.api.v1.soul", "router", "Soul"),
    ("pocketpaw.api.v1.pockets", "router", "Pockets"),
    ("pocketpaw.api.v1.connectors", "router", "Connectors"),
    ("pocketpaw.api.v1.tools", "router", "Tools"),
    ("pocketpaw.api.v1.oauth_integrations", "router", "OAuth Integrations"),
    ("pocketpaw.audit.router", "router", "Audit"),
]

# Enterprise API routes (require ee/ module) — skipped silently when ee/ is absent.
_EE_ROUTERS: list[tuple[str, str, str]] = [
    ("ee.fabric.router", "router", "Fabric"),
    ("ee.instinct.router", "router", "Instinct"),
    ("pocketpaw.ee.automations.router", "router", "Automations"),
]


def mount_v1_routers(app: FastAPI) -> None:
    """Mount all v1 domain routers on *app*.

    Each router is mounted at ``/api/v1/<prefix>`` (canonical).
    The original ``/api/`` endpoints in dashboard.py remain as backward-compat aliases.
    """
    import importlib

    from fastapi import APIRouter

    _CRITICAL_ROUTERS = {"Auth", "Chat", "Health", "Sessions"}

    for module_path, attr_name, tag in _V1_ROUTERS:
        try:
            mod = importlib.import_module(module_path)
            router: APIRouter = getattr(mod, attr_name)

            # Canonical v1 mount
            app.include_router(router, prefix="/api/v1")

            logger.debug("Mounted v1 router: %s (%s)", module_path, tag)
        except Exception:
            if tag in _CRITICAL_ROUTERS:
                logger.error(
                    "CRITICAL: Failed to mount required v1 router %s", module_path, exc_info=True
                )
                raise
            logger.warning("Failed to mount v1 router %s", module_path, exc_info=True)

    # Enterprise routers — optional, never critical
    for module_path, attr_name, tag in _EE_ROUTERS:
        try:
            mod = importlib.import_module(module_path)
            router: APIRouter = getattr(mod, attr_name)
            app.include_router(router, prefix="/api/v1")
            logger.debug("Mounted ee router: %s (%s)", module_path, tag)
        except ImportError:
            logger.debug("Skipping ee router %s (ee/ not available)", module_path)
        except Exception:
            logger.warning("Failed to mount ee router %s", module_path, exc_info=True)
