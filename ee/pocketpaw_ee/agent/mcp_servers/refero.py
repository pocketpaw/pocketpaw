# refero.py — in-process MCP server exposing Refero design research (curated
# visual styles + real product screens) to the claude_agent_sdk cloud chat
# backend.
#
# Updated: 2026-09-27 (feat/sites-visual-research) — NEW ``view_reference``: the
# screenshot of a screen (by uuid) or a style preview (by ``preview_url``) comes
# back as IMAGE blocks the agent can look at. The search tools only ever returned
# URLs the agent had no way to open, and reproducing a rejected site by hand
# showed that opening the pictures is what changed the result. Full-page shots
# are tiled (``pocketpaw.tools.builtin.reference_images``) so every section stays
# legible to the model.
#
# Created: 2026-09-15 (feat/refero-design-research). The site-authoring skills
# (pocketpaw-create-svelte-site, pocketpaw-create-react-site,
# pocketpaw-design-taste) run on the claude_agent_sdk backend, which only sees
# IN-PROCESS MCP servers — a plain BaseTool is invisible to it, the same reason
# stock_images.py / site_media.py / sites_create.py exist. So design research
# MUST be surfaced here for site authoring to reach it.
#
# WHY IT EARNS ITS PLACE: the /sites anti-slop surface is built from
# PROHIBITIONS — the 2026-09-12 audit counted 32, concentrated in one skill
# embedded on the create path only. A prohibition list bounds how bad the output
# gets; a reference sets what good looks like. A Refero style carries colour
# ROLES, a type scale, spacing, elevation and explicit do/don't rules taken from
# a page that shipped.
#
# What this file does: clones the stock_images.py shape — one
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants and the ``_error_response`` /
# ``_success_response`` helpers. Like stock images this is a PURE READ: it
# searches and returns references, needs no workspace/user identity, persists
# nothing and binds no session. Every tool wraps the OSS-core
# ``pocketpaw.tools.builtin.refero`` helpers, so this backend and the BaseTool
# surface share ONE code path.
#
# DEGRADATION: unconfigured (no token) returns an empty result set, never an
# error — Refero needs a paid plan, so that is the common case, and a site build
# must proceed WITHOUT design research rather than fail. The tool descriptions
# say so explicitly, because an agent told "search returned nothing" will
# otherwise retry or invent a citation.
#
# EE→OSS boundary: imports the helpers from src/pocketpaw (allowed — EE depends
# on OSS core), and the surface service loads REFERO_TOOL_IDS as a plain
# frozenset[str] inside a try/except.
"""Agent-side MCP surface for Refero design research (styles + screens)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_refero"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
SEARCH_STYLES_TOOL_ID = f"mcp__{SERVER_NAME}__search_styles"
GET_STYLE_TOOL_ID = f"mcp__{SERVER_NAME}__get_style"
SEARCH_SCREENS_TOOL_ID = f"mcp__{SERVER_NAME}__search_screens"
VIEW_REFERENCE_TOOL_ID = f"mcp__{SERVER_NAME}__view_reference"

REFERO_TOOL_IDS = (
    SEARCH_STYLES_TOOL_ID,
    GET_STYLE_TOOL_ID,
    SEARCH_SCREENS_TOOL_ID,
    VIEW_REFERENCE_TOOL_ID,
)


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


async def _run(fn: Any, *args: Any) -> Any:
    """Run the (synchronous, network-bound) Refero call off the event loop so the
    httpx exchange does not block the chat turn."""
    import asyncio

    return await asyncio.to_thread(fn, *args)


async def _search_styles_handler(args: dict) -> dict:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error_response("search_styles requires a non-empty `query`.")
    limit = args.get("limit")
    if not isinstance(limit, int):
        limit = 6

    try:
        from pocketpaw.tools.builtin.refero import search_styles

        results = await _run(search_styles, query.strip(), limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("refero: style search failed", exc_info=True)
        return _error_response(f"design style search failed: {exc}")

    return _success_response({"ok": True, "count": len(results), "results": results})


async def _get_style_handler(args: dict) -> dict:
    style_id = args.get("style_id")
    if not isinstance(style_id, str) or not style_id.strip():
        return _error_response("get_style requires a `style_id` uuid from search_styles.")

    try:
        from pocketpaw.tools.builtin.refero import get_style

        style = await _run(get_style, style_id.strip())
    except Exception as exc:  # noqa: BLE001
        logger.warning("refero: style fetch failed", exc_info=True)
        return _error_response(f"design style fetch failed: {exc}")

    return _success_response({"ok": bool(style), "style": style})


async def _search_screens_handler(args: dict) -> dict:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return _error_response("search_screens requires a non-empty `query`.")
    limit = args.get("limit")
    if not isinstance(limit, int):
        limit = 6

    try:
        from pocketpaw.tools.builtin.refero import search_screens

        results = await _run(search_screens, query.strip(), "web", limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("refero: screen search failed", exc_info=True)
        return _error_response(f"design screen search failed: {exc}")

    return _success_response({"ok": True, "count": len(results), "results": results})


async def _view_reference_handler(args: dict) -> dict:
    """Screen uuid -> Refero's own screenshot; style ``preview_url`` -> a fetch from
    Refero's image host. Either way the answer is image blocks plus one line of text
    saying what they show."""
    from pocketpaw.tools.builtin import reference_images
    from pocketpaw.tools.builtin.refero import IMAGE_HOSTS, get_screen_image

    screen_id = args.get("screen_id")
    preview_url = args.get("preview_url")
    try:
        if isinstance(screen_id, str) and screen_id.strip():
            data = await _run(get_screen_image, screen_id.strip(), "full")
            if not data:
                return _error_response(
                    "no screenshot came back for that screen — it may not exist, or Refero "
                    "is unavailable. Carry on from the text you already have."
                )
            label = f"screen {screen_id.strip()}"
        elif isinstance(preview_url, str) and preview_url.strip():
            data = await reference_images.fetch_image(preview_url.strip(), IMAGE_HOSTS)
            label = "style preview"
        else:
            return _error_response(
                "view_reference needs a `screen_id` (from search_screens) or a "
                "`preview_url` (from search_styles)."
            )
        tiles = reference_images.to_tiles(data)
    except reference_images.ReferenceImageError as exc:
        return _error_response(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("refero: view_reference failed", exc_info=True)
        return _error_response(f"could not load the reference image: {exc}")

    note = (
        f"{label}: {len(tiles)} image(s), top of the page first. Read the composition "
        "(what carries the fold, where the product visual sits, how much colour is "
        "spent and where), not the copy."
    )
    return {"content": [*reference_images.image_blocks(tiles), {"type": "text", "text": note}]}


def build_refero_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for design research, or ``None`` if
    the Claude Agent SDK isn't installed. Matches the ``(name, server)`` / ``None``
    shape of ``build_stock_server`` so the backend's MCP registration loop treats
    it identically."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_refero MCP disabled")
        return None

    @tool(
        "search_styles",
        (
            "Research REAL design systems from shipped websites BEFORE choosing a "
            "visual direction for a page. Returns curated styles — typography, colour "
            "ROLES, spacing, surfaces, imagery guidance and explicit do/don't rules — "
            "extracted from pages that actually shipped. Args: `query` (required — an "
            "aesthetic, industry, audience or named product, e.g. 'editorial "
            "monochrome SaaS landing page', 'premium fintech restrained typography', "
            "'Linear dark developer tool'), optional `limit` (default 6). Returns "
            "{ok, count, results:[{uuid, title, url, preview_url, description}]}. "
            "Search SEVERAL angles before committing, then expand the strongest with "
            "`get_style`. Do not copy one source wholesale and do not average several "
            "into a safe middle — pick one direction and borrow only narrow details "
            "from the rest. An empty `results` means Refero is not configured or "
            "nothing matched: proceed on your own design judgement, do NOT retry and "
            "do NOT claim a reference you did not receive."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Aesthetic, industry, audience, or named product.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many styles to return (default 6).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def search_styles_tool(args):  # type: ignore[no-untyped-def]
        return await _search_styles_handler(args)

    @tool(
        "get_style",
        (
            "Expand ONE style uuid from `search_styles` into its full design system: "
            "north-star thesis, colours WITH THEIR ROLES, type scale, spacing, radius "
            "and elevation, component treatments, imagery guidance, and do/don't "
            "rules. Args: `style_id` (required — a uuid from search_styles). This is "
            "the call that actually gives you something to build from; a search result "
            "alone is just a description. PRESERVE ROLES: if a colour is marked "
            "CTA-only or a face is marked display-only, use it only there or omit it. "
            "If the style depends on photography or illustration, honour that media "
            "role with real or generated assets, or an intentional placeholder — do "
            "not fake it with a decorative box."
        ),
        {
            "type": "object",
            "properties": {
                "style_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "A style uuid returned by search_styles.",
                }
            },
            "required": ["style_id"],
            "additionalProperties": False,
        },
    )
    async def get_style_tool(args):  # type: ignore[no-untyped-def]
        return await _get_style_handler(args)

    @tool(
        "search_screens",
        (
            "Research REAL product screens for CONCRETE interface decisions — page "
            "structure, content hierarchy, states, and which components a shipped "
            "product actually used. Args: `query` (required — describe what is ON the "
            "screen: 'pricing page annual monthly toggle', 'feature comparison table', "
            "'testimonial wall', 'dashboard empty state'), optional `limit` (default "
            "6). Returns {ok, count, results:[{uuid, source, page_url, thumbnail_url, "
            "page_types, ux_patterns, ui_elements, description}]}. Use AFTER "
            "`search_styles`: styles set the visual language, screens settle the "
            "structure. Web only, which is what a Paw Site is. An empty `results` "
            "means Refero is not configured or nothing matched — proceed without it."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "What is on the screen — page type, component, or state.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many screens to return (default 6).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def search_screens_tool(args):  # type: ignore[no-untyped-def]
        return await _search_screens_handler(args)

    @tool(
        "view_reference",
        (
            "LOOK at a reference: returns the actual screenshot as images you can see. "
            "Pass `screen_id` (a uuid from `search_screens`) for the full captured page, "
            "or `preview_url` (from a `search_styles` result) for a style's preview. "
            "Text descriptions miss what matters most, so open the 3-5 strongest "
            "references before you decide the look: the hero composition, whether the "
            "product is shown, how light or dark the page is, where colour is spent. "
            "Long pages come back as several images, top first."
        ),
        {
            "type": "object",
            "properties": {
                "screen_id": {
                    "type": "string",
                    "description": "A screen uuid returned by search_screens.",
                },
                "preview_url": {
                    "type": "string",
                    "description": "A preview_url returned by search_styles.",
                },
            },
            "additionalProperties": False,
        },
    )
    async def view_reference_tool(args):  # type: ignore[no-untyped-def]
        return await _view_reference_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[search_styles_tool, get_style_tool, search_screens_tool, view_reference_tool],
    )
    return SERVER_NAME, server


__all__ = [
    "GET_STYLE_TOOL_ID",
    "REFERO_TOOL_IDS",
    "SEARCH_SCREENS_TOOL_ID",
    "SEARCH_STYLES_TOOL_ID",
    "VIEW_REFERENCE_TOOL_ID",
    "SERVER_NAME",
    "build_refero_server",
]
