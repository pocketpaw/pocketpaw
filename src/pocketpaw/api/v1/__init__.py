# API v1 router aggregation.
# Created: 2026-02-20
# Updated: 2026-03-30 — Added Automations router (enterprise, rule-based pocket automations).
# Updated: 2026-04-16 (feat/fleet-rest-router) — Added Fleet router so
#   paw-enterprise's InstallFleetPanel can call GET /api/v1/fleet/templates
#   and POST /api/v1/fleet/install against a running pocketpaw instance.
# Updated: 2026-04-16 (feat/retrieval-journal-projection) — Added the
#   Retrieval router so UIs can surface the journal-backed retrieval +
#   graduation projection (supersedes held PRs #936 / #937).
# Updated: 2026-04-16 (feat/widget-journal-projection) — Added the
#   Widgets router — journal-backed widget graduation + co-occurrence
#   (supersedes held PRs #941 / #942).
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
    ("pocketpaw.api.v1.connectors", "router", "Connectors"),
    ("pocketpaw.api.v1.tools", "router", "Tools"),
    ("pocketpaw.api.v1.oauth_integrations", "router", "OAuth Integrations"),
    ("pocketpaw.api.v1.uploads", "router", "Uploads"),
    ("pocketpaw.audit.router", "router", "Audit"),
    # Cluster C / PR4 — canonical runtime audit surface. `/audit` stays as
    # a deprecated alias in audit.router and `/instinct/audit` stays in
    # ee.instinct.router; both forward semantically to this one.
    ("pocketpaw.audit.runtime_router", "router", "Runtime Audit"),
    # Moved to OSS core in the open-core split (Phase 2) — journal-backed
    # retrieval + widget projections + rule-based automations, no
    # multi-tenant cloud dependency.
    ("pocketpaw.retrieval.router", "router", "Retrieval"),
    ("pocketpaw.widget.router", "router", "Widgets"),
    ("pocketpaw.automations.router", "router", "Automations"),
]

# Enterprise API routers (Fabric, Fleet, Instinct, and the Pockets chat
# endpoint) are NOT listed here. They are mounted by the `pocketpaw.routes`
# provider (mount_cloud) alongside the rest of the cloud route tree, so the
# OSS core never imports `pocketpaw_ee`, statically or dynamically.


def mount_v1_routers(app: FastAPI) -> None:
    """Mount all core v1 domain routers on *app*.

    Each router is mounted at ``/api/v1/<prefix>`` (canonical).
    The original ``/api/`` endpoints in dashboard.py remain as backward-compat aliases.

    Enterprise routers are mounted separately via the ``pocketpaw.routes``
    extension provider — see ``dashboard.py`` / ``api/serve.py``.
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
