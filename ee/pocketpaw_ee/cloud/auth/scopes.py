# 2026-10-01 (CN-2): docstring no longer claims ``require_scope`` checks
#   ``ctx.scopes`` — the ee dependency that did was deleted.
"""API-key scope registry.

Scopes are coarse permissions attached to API keys. JWT-authenticated
requests carry ``ctx.scopes is None``; API-key requests carry the key's
concrete list on ``RequestContext.scopes``. Nothing enforces that list on
routes today (see ``_core/context.py``).
"""

from __future__ import annotations

AVAILABLE_SCOPES: dict[str, str] = {
    "chat.read": "Read chat messages and groups",
    "chat.send": "Send chat messages",
    "files.read": "List and download files",
    "files.write": "Upload files",
    "knowledge.read": "Query the knowledge base",
    "knowledge.write": "Add to / remove from the knowledge base",
    "agents.read": "List agents",
    "agents.write": "Create / update / delete agents",
    "workspace.read": "Read workspace metadata, members, plan",
    "audit.read": "Read the workspace audit log",
}

DEFAULT_READONLY_SCOPES = ["chat.read", "files.read", "workspace.read"]


def validate_scopes(scopes: list[str]) -> list[str]:
    out: list[str] = []
    for s in scopes:
        if s not in AVAILABLE_SCOPES:
            raise ValueError(f"unknown_scope: {s}")
        out.append(s)
    return out


__all__ = ["AVAILABLE_SCOPES", "DEFAULT_READONLY_SCOPES", "validate_scopes"]
