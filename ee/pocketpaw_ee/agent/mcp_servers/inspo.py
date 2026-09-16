# inspo.py — in-process MCP server exposing design research over real shipped
# websites to the cloud chat backend.
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
# EE→OSS boundary: imports ``httpx`` and ``pocketpaw.config`` only; the surface
# service loads INSPO_TOOL_IDS as a plain frozenset[str] inside a try/except.
"""Agent-side MCP surface for design research over real shipped websites."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_inspo"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
RESEARCH_PAGE_DESIGN_TOOL_ID = f"mcp__{SERVER_NAME}__research_page_design"
GET_REFERENCE_DESIGN_SYSTEM_TOOL_ID = f"mcp__{SERVER_NAME}__get_reference_design_system"

INSPO_TOOL_IDS = (
    RESEARCH_PAGE_DESIGN_TOOL_ID,
    GET_REFERENCE_DESIGN_SYSTEM_TOOL_ID,
)

# The upstream tool each of ours calls. Ours are named for the JOB (the house
# style — cf. ``search_stock_images``), theirs for their catalogue, and keeping
# the map explicit is what makes an upstream rename a one-line fix here.
_UPSTREAM = {
    "research_page_design": "recommend",
    "get_reference_design_system": "get_design_system",
}

# A create turn is a person waiting. The archive is a free third-party service
# with no SLA, so it gets a short leash and the caller proceeds without it —
# the preamble's ROBUSTNESS rule already covers a tool that errors.
_TIMEOUT_SECONDS = 20.0

# Upstream accepts a token ceiling on every list-shaped tool. A create preamble
# is already large and the agent needs a macrostructure and a handful of
# exemplars, not the whole shortlist rendered long.
_MAX_TOKENS = 1200


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


def _endpoint() -> str:
    """The archive's MCP endpoint.

    Overridable because the hosted service rate-limits PER IP, and a
    multi-tenant deploy is a single egress IP for every tenant it serves. The
    upstream is MIT-licensed with a documented self-host path, so a deploy that
    outgrows the hosted instance points this at its own without a code change.
    """
    try:
        from pocketpaw.config import get_settings

        url = (getattr(get_settings(), "inspo_mcp_url", "") or "").strip()
        if url:
            return url
    except Exception:  # noqa: BLE001 — config must never break a tool call
        pass
    return "https://inspomcp.dev/api/mcp"


async def _call_upstream(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One JSON-RPC ``tools/call`` against the archive.

    Returns the decoded tool result, or raises. The caller turns any failure
    into an ``_error_response`` — this never returns a half-result, because a
    partial design reference is worse than none: the agent would build on it.
    """
    import httpx

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        # The server may answer either way; asking for both is what lets it
        # choose, and a plain JSON body is what it returns for a stateless call.
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(_endpoint(), json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()

    if "error" in body:
        raise RuntimeError(str(body["error"].get("message", body["error"])))

    result = body.get("result") or {}
    # MCP wraps a tool result as ``content:[{type:"text", text:"<json>"}]``.
    # Unwrap to the payload the model should actually read; if it is not JSON,
    # hand back the text as-is rather than failing on a format change.
    for block in result.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                return {"text": text}
    return result


async def _research_handler(args: dict) -> dict:
    """MCP handler for ``inspo__research_page_design``."""
    brief = args.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        return _error_response("research_page_design requires a non-empty `brief`.")

    try:
        body = await _call_upstream(
            _UPSTREAM["research_page_design"],
            {"brief": brief.strip(), "maxTokens": _MAX_TOKENS},
        )
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
        body = await _call_upstream(
            _UPSTREAM["get_reference_design_system"], {"slug": slug.strip()}
        )
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
