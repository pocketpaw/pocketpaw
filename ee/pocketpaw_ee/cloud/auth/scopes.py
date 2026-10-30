# 2026-10-03 (fix/paw-key-scopes): scopes ARE enforced now, in
#   ``_core/context.request_context`` for paw_ bearers.
# 2026-10-01 (CN-2): docstring no longer claims ``require_scope`` checks
#   ``ctx.scopes`` — the ee dependency that did was deleted.
"""API-key scope registry.

Scopes are coarse permissions attached to API keys. JWT-authenticated
requests carry ``ctx.scopes is None``; API-key requests carry the key's
concrete list on ``RequestContext.scopes``. ``request_context`` enforces it:
``_core/context._API_KEY_SCOPES`` maps each route family plus method to one of
these scopes, and a key that lacks it gets a 403. A family with no entry is
refused to every key. Routes that authenticate with the JWT-only dependencies
(``current_active_user`` and friends) never accept a ``paw_`` key at all.
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
