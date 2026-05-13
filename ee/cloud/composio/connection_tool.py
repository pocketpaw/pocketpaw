"""Composio connection-initiation helper — emits a Connect URL the agent
hands to the user when a toolkit isn't authorized yet.

We expose this as an extra MCP tool alongside the concrete Composio
tools (``GMAIL_SEND_EMAIL`` etc.). When the agent calls e.g.
``GMAIL_SEND_EMAIL`` and Composio returns ``ConnectedAccountNotFound``,
the agent's next move is to call ``initiate_connection(toolkit="gmail")``
which returns the Composio Connect URL — the user clicks the URL,
authorizes, and the original tool call retries.

Why this isn't in Composio's default tool set: ``composio.tools.get``
returns *executable* tools for the user's already-connected accounts.
Connection initiation is an admin/auth flow that lives on a different
API surface (``c.connected_accounts.link`` + ``c.auth_configs.list``).
We wrap that surface in a single agent-callable tool.

Auth config resolution: we look up the existing Composio-managed
auth config for the toolkit slug. The admin needs to have created one
(or marked one as managed) at least once via the Composio dashboard
or via ``c.auth_configs.create``. If none exists, we surface a clear
error directing the admin to set one up.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _find_auth_config_id_for_toolkit(client: Any, toolkit_slug: str) -> str | None:
    """Return the id of an existing Composio auth config for the toolkit.

    Picks the first ``ENABLED`` config whose ``toolkit.slug`` matches.
    Returns ``None`` if nothing is found — caller should surface a
    "no auth config; admin must create one" error to the agent so it
    doesn't waste a turn retrying.
    """
    try:
        response = client.auth_configs.list()
    except Exception:  # noqa: BLE001
        logger.exception("composio.auth_configs.list failed")
        return None

    items: Any
    if isinstance(response, dict):
        items = response.get("items") or []
    else:
        items = getattr(response, "items", None) or []

    target = toolkit_slug.lower()
    for item in items:
        tk = item.get("toolkit") if isinstance(item, dict) else getattr(item, "toolkit", None)
        if tk is None:
            continue
        slug = tk.get("slug") if isinstance(tk, dict) else getattr(tk, "slug", None)
        if not slug or str(slug).lower() != target:
            continue
        status = item.get("status") if isinstance(item, dict) else getattr(item, "status", None)
        if status and str(status).upper() != "ENABLED":
            continue
        cfg_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if cfg_id:
            return str(cfg_id)
    return None


def initiate_connection_sync(client: Any, *, user_id: str, toolkit_slug: str) -> dict[str, Any]:
    """Generate a Connect URL for ``user_id`` + ``toolkit_slug``.

    Returns an envelope:
        ``{ok: True, redirect_url, connection_id, toolkit}`` on success, or
        ``{ok: False, error: <human-readable>, toolkit}`` on failure.

    The agent calls this as a tool; we keep the response shape stable
    so the LLM has a consistent contract regardless of the auth-config
    state on the Composio side.
    """
    auth_config_id = _find_auth_config_id_for_toolkit(client, toolkit_slug)
    if auth_config_id is None:
        return {
            "ok": False,
            "toolkit": toolkit_slug,
            "error": (
                f"No Composio-managed auth config found for toolkit "
                f"'{toolkit_slug}'. An admin must create one in the Composio "
                f"dashboard (or via c.auth_configs.create) before users can "
                f"connect this integration."
            ),
        }

    try:
        request = client.connected_accounts.link(user_id=user_id, auth_config_id=auth_config_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("composio.connected_accounts.link failed for %s", toolkit_slug)
        return {
            "ok": False,
            "toolkit": toolkit_slug,
            "error": f"Composio failed to issue a Connect Link: {exc}",
        }

    redirect = getattr(request, "redirect_url", None) or (
        request.get("redirect_url") if isinstance(request, dict) else None
    )
    connection_id = getattr(request, "id", None) or (
        request.get("id") if isinstance(request, dict) else None
    )
    if not redirect:
        return {
            "ok": False,
            "toolkit": toolkit_slug,
            "error": "Composio returned no redirect_url for the Connect Link.",
        }

    return {
        "ok": True,
        "toolkit": toolkit_slug,
        "redirect_url": str(redirect),
        "connection_id": str(connection_id) if connection_id else None,
    }


__all__ = ["initiate_connection_sync"]
