# stock_images.py — in-process MCP server exposing free stock-photo search
# (Pexels + Unsplash) to the claude_agent_sdk cloud chat backend.
#
# Created: 2026-07-04 (feat/paw-sites-stock-imagery). The Svelte-track
# site-authoring skill (pocketpaw-create-svelte-site) runs on the
# claude_agent_sdk backend, which only sees in-process MCP servers — a plain
# BaseTool is invisible to it (same reason media.py / sites_create.py exist). So
# the stock-photo capability MUST be surfaced here for site authoring to reach it.
#
# What this file does: clones the media.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, and the ``_error_response`` /
# ``_success_response`` helpers. Unlike media.py this is a PURE READ: it searches
# providers and returns URLs, so it needs NO workspace/user identity, persists
# nothing, and binds no session. The single tool wraps the OSS-core
# ``pocketpaw.tools.builtin.stock_images.search_stock_images`` helper (one code
# path shared with the non-SDK BaseTool surface). Tool id namespaces as
# ``mcp__pocketpaw_stock__search_stock_images`` so the Claude Code allowlist
# machinery matches it.
#
# EE→OSS boundary: this module imports the search helper from src/pocketpaw
# (allowed — EE depends on OSS core), and the surface service loads
# STOCK_TOOL_IDS as a plain frozenset[str] inside a try/except.
"""Agent-side MCP surface for free stock-photo search (Pexels + Unsplash)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_stock"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
SEARCH_STOCK_IMAGES_TOOL_ID = f"mcp__{SERVER_NAME}__search_stock_images"

STOCK_TOOL_IDS = (SEARCH_STOCK_IMAGES_TOOL_ID,)


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


async def _search_handler(args: dict) -> dict:
    """MCP handler for ``stock__search_stock_images``.

    Pure read: searches the configured stock providers via the shared OSS-core
    helper and returns the normalized results. No identity/tenant context is
    required (nothing is persisted). Returns ``{ok:true, results:[...]}``; an
    empty ``results`` means no provider is configured or nothing matched — the
    caller should proceed without imagery, not treat it as an error.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error_response("search_stock_images requires a non-empty `query`.")

    orientation = args.get("orientation")
    if not isinstance(orientation, str) or not orientation.strip():
        orientation = "landscape"
    count = args.get("count")
    if not isinstance(count, int):
        count = 5

    try:
        from pocketpaw.tools.builtin.stock_images import search_stock_images

        results = await _run_search(search_stock_images, query, orientation, count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stock: search failed", exc_info=True)
        return _error_response(f"stock image search failed: {exc}")

    return _success_response({"ok": True, "count": len(results), "results": results})


async def _run_search(fn: Any, query: str, orientation: str, count: int) -> list[dict]:
    """Run the (synchronous, network-bound) provider search off the event loop so
    the httpx calls don't block the chat turn."""
    import asyncio

    return await asyncio.to_thread(fn, query, orientation, count)


def build_stock_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for stock-photo search, or return
    ``None`` if the Claude Agent SDK isn't installed. Matches the ``(name,
    server)`` / ``None`` shape of ``build_media_server`` so the backend's MCP
    registration loop treats it identically."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_stock MCP disabled")
        return None

    @tool(
        "search_stock_images",
        (
            "Search free royalty-free stock photos (Pexels + Unsplash) and return "
            "ready-to-embed image URLs. Use when building a site or page that "
            "needs real photography — e.g. a hero photo or section imagery on a "
            "generated marketing site. Args: `query` (required — what the photo "
            "shows; prefer generic descriptive subjects like 'modern dental "
            "office' over 'dentist in Akron'), optional `orientation` "
            "('landscape' default, 'portrait', 'square') and `count` (default 5, "
            "max 30). Returns {ok, count, results:[{url, alt, credit, credit_url, "
            "provider, width, height}]}. Embed `url` directly in your markup and "
            "render the `credit` line near the image. An empty `results` means no "
            "provider key is configured or nothing matched — proceed WITHOUT "
            "imagery, do not fabricate an image URL."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "What the photo should show (generic descriptive subject).",
                },
                "orientation": {
                    "type": "string",
                    "description": "Shape hint: 'landscape' (default), 'portrait', or 'square'.",
                },
                "count": {
                    "type": "integer",
                    "description": "How many photos to return (default 5, max 30).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def search_stock_images_tool(args):  # type: ignore[no-untyped-def]
        return await _search_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[search_stock_images_tool],
    )
    return SERVER_NAME, server


__all__ = [
    "SEARCH_STOCK_IMAGES_TOOL_ID",
    "STOCK_TOOL_IDS",
    "SERVER_NAME",
    "build_stock_server",
]
