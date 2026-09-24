# inspo.py — in-process MCP server exposing design research over real shipped
# websites to the cloud chat backend.
#
# Changed: 2026-09-24 (feat/inspo-backend-parity). The upstream call (endpoint,
# JSON-RPC POST, result unwrapping, the ``_UPSTREAM`` map) moved to the OSS core
# ``pocketpaw.tools.builtin.inspo`` so the non-SDK backends can reach Inspo
# through BaseTools. This server now wraps those helpers — one code path, two
# surfaces, the split refero.py already uses. The MCP envelopes, tool ids and
# error messages are unchanged.
#
# Created: 2026-09-16 (feat/sites-bundled-design-research). The /sites create
# preamble embeds a design system and a craft system: between them they cover
# HOW to build a page and WHAT NOT to do. Neither is a source of evidence, so
# "commit to a direction" resolved against model defaults, which is where the
# sameness in generated sites came from. A prohibition bounds how bad the output
# gets; it never says what good looks like.
#
# This wraps inspomcp.dev — an archive of ~2,300 captured pages across ~830 real
# shipped sites, each carrying a DESIGN.md extracted from the live DOM
# (role-tagged palette, type ramp, spacing scale, macrostructure). MIT, free,
# unauthenticated, every endpoint read-only.
#
# WHY BUNDLED RATHER THAN AN EXTERNAL SERVER. It was first wired as an external
# MCP server behind ``POCKETPAW_SITES_MCP_SERVERS`` (#2204). That took two
# switches — install the server, then grant it — and both default off, so
# deploying the code changed nothing and the first deploy researched nothing
# with no error anywhere. Worse, the two switches were independent: granting
# without installing put the instruction in the preamble with no tools behind
# it. As a bundled in-process server it is simply present, like stock, palette
# and icons, and the preamble can name its tools unconditionally because they
# are unconditionally there.
#
# Clones the ``stock_images.py`` shape exactly — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, and the ``_error_response`` /
# ``_success_response`` helpers. Like stock this is a PURE READ: no workspace or
# user identity, nothing persisted, no session bound.
#
# TRANSPORT: plain JSON-RPC over one HTTP POST, not an MCP client session.
# Verified 2026-09-16 that the server answers ``tools/call`` with no
# ``initialize`` and no session id, which means no handshake, no session
# lifecycle, and none of the anyio cancel-scope trouble a nested client session
# brings inside an already-running event loop. One request per call, one
# timeout, nothing to leak.
#
# EE→OSS boundary: imports the helpers from ``pocketpaw.tools.builtin.inspo``
# (allowed — EE depends on OSS core, as refero.py does); the surface
# service loads INSPO_TOOL_IDS as a plain frozenset[str] inside a try/except.
"""Agent-side MCP surface for design research over real shipped websites."""

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw.tools.builtin import inspo as _inspo

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_inspo"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
RESEARCH_PAGE_DESIGN_TOOL_ID = f"mcp__{SERVER_NAME}__research_page_design"
GET_REFERENCE_DESIGN_SYSTEM_TOOL_ID = f"mcp__{SERVER_NAME}__get_reference_design_system"

INSPO_TOOL_IDS = (
    RESEARCH_PAGE_DESIGN_TOOL_ID,
    GET_REFERENCE_DESIGN_SYSTEM_TOOL_ID,
)

# The upstream map and endpoint live in the OSS core so the BaseTools share them.
# Re-exported under their old names so this module's contract is unchanged.
_UPSTREAM = _inspo._UPSTREAM
_endpoint = _inspo.endpoint


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


async def _research_handler(args: dict) -> dict:
    """MCP handler for ``inspo__research_page_design``."""
    brief = args.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        return _error_response("research_page_design requires a non-empty `brief`.")

    try:
        body = await _inspo.research_page_design(brief)
    except Exception as exc:  # noqa: BLE001
        logger.warning("inspo: research_page_design failed", exc_info=True)
        return _error_response(
            f"design research unavailable ({exc}). Proceed on your own inference — "
            "do not retry and do not stall the build."
        )

    return _success_response({"ok": True, "reference": body})


async def _design_system_handler(args: dict) -> dict:
    """MCP handler for ``inspo__get_reference_design_system``."""
    slug = args.get("slug")
    if not isinstance(slug, str) or not slug.strip():
        return _error_response(
            "get_reference_design_system requires a `slug` from a research_page_design result."
        )

    try:
        body = await _inspo.get_reference_design_system(slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("inspo: get_reference_design_system failed", exc_info=True)
        return _error_response(
            f"design system lookup unavailable ({exc}). Proceed with the structure you "
            "already have."
        )

    return _success_response({"ok": True, "slug": slug.strip(), "design_system": body})


def build_inspo_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for design research, or return
    ``None`` if the Claude Agent SDK isn't installed. Matches the ``(name,
    server)`` / ``None`` shape of ``build_stock_server`` so the backend's MCP
    registration loop treats it identically."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_inspo MCP disabled")
        return None

    @tool(
        "research_page_design",
        (
            "Look up how REAL shipped websites in this category are actually "
            "built, before you design one. Returns a macrostructure pick with "
            "the reasoning behind it, a shortlist of runners-up, and real "
            "exemplar sites with slugs you can pass to "
            "`get_reference_design_system`. Use it ONCE per site, after you have "
            "committed to an aesthetic direction and BEFORE you write tokens or "
            "markup. Args: `brief` (required — the site in plain words, e.g. "
            "'landing page for a family dental clinic'). These are real pages, "
            "so take their COMPOSITION (which sections, in what order, what "
            "carries the fold) and not their compliance — your own design system "
            "still outranks anything here on a visual value, and copying a "
            "returned palette is how two similar briefs end up identical. If it "
            "errors, proceed on your own inference without retrying."
        ),
        {
            "type": "object",
            "properties": {
                "brief": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The site in plain words — what it is for and who it serves.",
                },
            },
            "required": ["brief"],
            "additionalProperties": False,
        },
    )
    async def research_page_design_tool(args):  # type: ignore[no-untyped-def]
        return await _research_handler(args)

    @tool(
        "get_reference_design_system",
        (
            "The DESIGN.md for one real site, extracted from its live DOM: "
            "actual fonts, frequency-ranked palette with the role each colour "
            "plays, type ramp, spacing scale, CSS variables, container width. "
            "Args: `slug` (required — an exemplar slug from a "
            "`research_page_design` result). Read it for RELATIONSHIPS — how "
            "many type sizes a real page uses, where its accent is actually "
            "spent, how far its scale travels — not for values to copy. One "
            "follow-up call at most."
        ),
        {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "minLength": 1,
                    "description": "An exemplar slug returned by research_page_design.",
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    )
    async def get_reference_design_system_tool(args):  # type: ignore[no-untyped-def]
        return await _design_system_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[research_page_design_tool, get_reference_design_system_tool],
    )
    return SERVER_NAME, server


__all__ = [
    "GET_REFERENCE_DESIGN_SYSTEM_TOOL_ID",
    "INSPO_TOOL_IDS",
    "RESEARCH_PAGE_DESIGN_TOOL_ID",
    "SERVER_NAME",
    "build_inspo_server",
]
