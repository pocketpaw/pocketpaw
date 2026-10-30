# icons.py — in-process MCP server exposing free open-source icon search
# (Iconify) to the claude_agent_sdk cloud chat backend.
#
# Created: 2026-07-06 (feat/sites-crew-icons, SC-6). The Svelte-track
# site-authoring skill (pocketpaw-create-svelte-site) runs on the
# claude_agent_sdk backend, which only sees in-process MCP servers — a plain
# BaseTool is invisible to it (same reason media.py / stock_images.py exist). A
# generated site needs real iconography the same way it needs real photography,
# so the icon-search capability MUST be surfaced here for site authoring to reach
# it.
#
# What this file does: clones the stock_images.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, and the ``_error_response`` /
# ``_success_response`` helpers. Like stock_images.py this is a PURE READ: it
# searches the provider and returns URLs, so it needs NO workspace/user identity,
# persists nothing, and binds no session. Tool id namespaces as
# ``mcp__pocketpaw_icons__search_icons`` so the Claude Code allowlist machinery
# matches it.
#
# Provider: the Iconify public API (https://api.iconify.design) — free, keyless,
# 200k+ open-source icons. Search: GET /search?query=<q>&limit=<n> returns
# ``{icons: ["prefix:name", ...]}``; each icon's SVG lives at
# ``/<prefix>/<name>.svg``. Fail-soft: any provider error or an empty query
# returns an ``_error_response`` and never raises.
#
# The raw search helper is inlined here (unlike stock_images.py, which split its
# helper into OSS core for BaseTool reuse) to keep this task tight — there is no
# non-SDK icon surface to share with. It is kept cleanly separated
# (``_run_search`` / ``_icon_svg_url``) so it can be lifted into core later if a
# BaseTool surface appears.
"""Agent-side MCP surface for free open-source icon search (Iconify)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_icons"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
SEARCH_ICONS_TOOL_ID = f"mcp__{SERVER_NAME}__search_icons"

ICON_TOOL_IDS = (SEARCH_ICONS_TOOL_ID,)

_ICONIFY_BASE = "https://api.iconify.design"
_SEARCH_ENDPOINT = f"{_ICONIFY_BASE}/search"

# Bound the provider call so a slow/hanging API can't stall a site build.
_TIMEOUT_SECONDS = 10.0

# Tests inject an ``httpx.MockTransport`` here so the provider HTTP call is
# exercised without live network (same seam stock_images.py exposes). Production
# leaves it None (real network).
_TRANSPORT: httpx.BaseTransport | None = None


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: Any) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _icon_svg_url(icon_id: str) -> str:
    """Map an Iconify ``"prefix:name"`` id to its hotlink-ready SVG URL."""
    prefix, _, name = icon_id.partition(":")
    return f"{_ICONIFY_BASE}/{prefix}/{name}.svg"


async def _run_search(query: str, limit: int, style: str | None) -> list[dict[str, Any]]:
    """Query the Iconify search API and normalize results.

    Returns up to ``limit`` icons, each ``{name, id, prefix, url}`` where ``url``
    is a real https SVG hotlink. Raises on transport/HTTP error — the caller's
    handler maps that to a soft ``_error_response``.
    """
    params: dict[str, Any] = {"query": query, "limit": limit}
    # Iconify accepts a ``prefixes`` / ``palette`` filter; ``style`` is a light
    # hint we pass through as ``category`` so a caller asking for e.g. "outline"
    # narrows the set. Iconify ignores unknown filters, so this is safe.
    if style:
        params["category"] = style

    async with httpx.AsyncClient(transport=_TRANSPORT, timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(_SEARCH_ENDPOINT, params=params)
        resp.raise_for_status()
        icon_ids = resp.json().get("icons", [])

    out: list[dict[str, Any]] = []
    for icon_id in icon_ids:
        if not isinstance(icon_id, str) or ":" not in icon_id:
            continue
        prefix, _, name = icon_id.partition(":")
        out.append(
            {
                "name": name,
                "id": icon_id,
                "prefix": prefix,
                "url": _icon_svg_url(icon_id),
            }
        )
    return out[:limit]


async def _search_handler(args: dict) -> dict:
    """MCP handler for ``icons__search_icons``.

    Pure read: searches Iconify and returns the normalized icons. No
    identity/tenant context is required (nothing is persisted). Returns
    ``{ok:true, count, icons:[...]}``; an empty ``icons`` means nothing matched —
    the caller should proceed without that icon, not treat it as an error. Any
    provider error is caught and returned as an ``_error_response``, never raised.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error_response("search_icons requires a non-empty `query`.")
    query = query.strip()

    limit = args.get("limit")
    if not isinstance(limit, int) or limit < 1:
        limit = 12
    limit = min(limit, 60)

    style = args.get("style") or args.get("set")
    if not isinstance(style, str) or not style.strip():
        style = None
    else:
        style = style.strip()

    try:
        icons = await _run_search(query, limit, style)
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise into the agent
        logger.warning("icons: search failed for %r", query, exc_info=True)
        return _error_response(f"icon search failed: {exc}")

    return _success_response({"ok": True, "count": len(icons), "icons": icons})


def build_icons_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for icon search, or return ``None`` if
    the Claude Agent SDK isn't installed. Matches the ``(name, server)`` / ``None``
    shape of ``build_stock_server`` so the backend's MCP registration loop treats
    it identically."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_icons MCP disabled")
        return None

    @tool(
        "search_icons",
        (
            "Search free open-source icons (Iconify, 200k+ icons) and return "
            "ready-to-embed SVG URLs. Use when building a site or page that needs "
            "iconography — e.g. feature-list bullets, service cards, or nav "
            "affordances on a generated marketing site. Args: `query` (required — "
            "what the icon depicts, prefer a plain noun/verb like 'calendar', "
            "'shield check', 'tooth'), optional `limit` (default 12, max 60) and "
            "optional `style` hint (an Iconify set/category like 'outline', "
            "'solid', or a set prefix like 'lucide' / 'mdi'). Returns {ok, count, "
            "icons:[{name, id, prefix, url}]}. Embed `url` directly as an <img "
            "src> or fetch the SVG markup to inline it. An empty `icons` means "
            "nothing matched — pick a broader query or proceed WITHOUT the icon, "
            "do not fabricate an icon URL."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "What the icon depicts (plain noun/verb, e.g. 'calendar').",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many icons to return (default 12, max 60).",
                },
                "style": {
                    "type": "string",
                    "description": (
                        "Optional style/set hint — e.g. 'outline', 'solid', or a "
                        "set prefix like 'lucide' / 'mdi'."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def search_icons_tool(args):  # type: ignore[no-untyped-def]
        return await _search_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[search_icons_tool],
    )
    return SERVER_NAME, server


__all__ = [
    "SEARCH_ICONS_TOOL_ID",
    "ICON_TOOL_IDS",
    "SERVER_NAME",
    "build_icons_server",
]
