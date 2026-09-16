# Refero design-research tool — curated visual styles and real product screens
# from shipped websites, for grounding generated UI in evidence rather than
# model defaults.
#
# Created: 2026-09-15 (feat/refero-design-research). Paw Sites' anti-slop
# surface is built almost entirely from PROHIBITIONS — the 2026-09-12 audit
# counted 32 of them, concentrated in one skill embedded on the create path
# only. A prohibition list bounds how bad output gets; a reference sets what
# good looks like. Refero supplies the reference: a style carries colour ROLES,
# a type scale, spacing, elevation and explicit do/don't rules extracted from a
# page that actually shipped.
#
# SHAPE: this module is the single code path, surfaced two ways — the
# ``ReferoStylesTool`` / ``ReferoScreensTool`` BaseTools here for the non-SDK
# backends (pydantic_ai and friends), and an EE in-process MCP server
# (``ee/pocketpaw_ee/agent/mcp_servers/refero.py``) for the claude_agent_sdk
# backend, which cannot see a plain BaseTool. Exactly the split
# ``stock_images.py`` uses, and for the same reason.
#
# TRANSPORT: Refero's documented surface is an MCP endpoint, not a REST API, so
# this speaks MCP to it — JSON-RPC over streamable HTTP with ``httpx`` (a CORE
# dependency) rather than a real MCP client library, because ``fastmcp`` ships
# only with the ``mcp`` extra and OSS core cannot require it. The handshake we
# need is small: ``initialize`` -> ``notifications/initialized`` -> ``tools/call``,
# with the session id echoed back in a header.
#
# DEGRADATION: no token configured -> ``[]`` and a debug log, never an
# exception. Same contract as stock images: a site build must proceed WITHOUT
# design research rather than fail because an optional integration is absent.
# Refero requires a paid plan, so the unconfigured case is the common one.

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://api.refero.design/mcp"

# Tests inject an ``httpx.MockTransport`` here so the JSON-RPC exchange is
# exercised without live network — the same seam ``stock_images`` exposes.
_TRANSPORT: httpx.BaseTransport | None = None

# Bound every call so a slow upstream cannot stall a site build. Refero's style
# detail responses are large, so this is looser than the stock-image timeout.
_TIMEOUT_SECONDS = 20.0

# The MCP protocol revision this client speaks. Refero's server accepted this
# revision when the integration was written; a server that requires a newer one
# answers ``initialize`` with an error, which degrades to [] like any other
# failure rather than raising into a chat turn.
_PROTOCOL_VERSION = "2025-06-18"

_SESSION_HEADER = "Mcp-Session-Id"


class ReferoError(RuntimeError):
    """A Refero call failed. Caught at every public entry point — callers of the
    module-level helpers get ``[]`` / ``{}``, never an exception."""


def _token() -> str:
    """The configured Refero API token, or "" when the integration is off."""
    try:
        return (getattr(get_settings(), "refero_api_token", "") or "").strip()
    except Exception:  # noqa: BLE001 — config trouble must not raise into a turn
        return ""


def _endpoint() -> str:
    try:
        return (getattr(get_settings(), "refero_endpoint", "") or "").strip() or _DEFAULT_ENDPOINT
    except Exception:  # noqa: BLE001
        return _DEFAULT_ENDPOINT


def is_configured() -> bool:
    """Whether a Refero token is present. Callers use this to decide whether to
    ADVERTISE design research at all, so an unconfigured deploy does not tell an
    agent to go call a tool that will return nothing."""
    return bool(_token())


def _client() -> httpx.Client:
    return httpx.Client(transport=_TRANSPORT, timeout=_TIMEOUT_SECONDS)


def _parse_rpc(response: httpx.Response) -> dict[str, Any]:
    """Pull the JSON-RPC payload out of a streamable-HTTP MCP response.

    Streamable HTTP may answer a POST with either a plain JSON body or an SSE
    stream carrying one ``data:`` frame, depending on what the server prefers —
    both are legal for a single request/response exchange, so both are handled
    here rather than assuming the one we happened to see first.
    """
    body = response.text or ""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in body.splitlines():
            if line.startswith("data:"):
                chunk = line[len("data:") :].strip()
                if chunk:
                    return json.loads(chunk)
        raise ReferoError("event-stream response carried no data frame")
    if not body.strip():
        return {}
    return json.loads(body)


def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Run one Refero MCP tool call and return its parsed result payload.

    Synchronous and network-bound on purpose — the async callers hand it to
    ``asyncio.to_thread`` so the httpx calls never block a chat turn, matching
    how ``stock_images`` bridges the same boundary.
    """
    token = _token()
    if not token:
        raise ReferoError("no Refero API token configured")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # Accept BOTH, because the server picks — see ``_parse_rpc``.
        "Accept": "application/json, text/event-stream",
    }
    url = _endpoint()

    with _client() as client:
        init = client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pocketpaw", "version": "1.0.0"},
                },
            },
        )
        if init.status_code == 401:
            raise ReferoError("Refero rejected the token (401) — check the plan and the token")
        init.raise_for_status()
        payload = _parse_rpc(init)
        if "error" in payload:
            raise ReferoError(f"initialize failed: {payload['error']}")

        # The session id is how the server ties the call below to the handshake
        # above. Absent on servers that keep no session, which is fine.
        session = init.headers.get(_SESSION_HEADER)
        if session:
            headers[_SESSION_HEADER] = session
            # Best-effort: a server that does not want the notification simply
            # ignores it, and one that does would otherwise reject `tools/call`.
            try:
                client.post(
                    url,
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            except Exception:  # noqa: BLE001
                logger.debug("refero: initialized notification failed", exc_info=True)

        called = client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        called.raise_for_status()
        result = _parse_rpc(called)

    if "error" in result:
        raise ReferoError(f"{tool_name} failed: {result['error']}")
    return _unwrap(result.get("result"))


def _unwrap(result: Any) -> Any:
    """Turn an MCP tool result into plain data.

    MCP returns ``{"content": [{"type": "text", "text": ...}]}``. Refero's text
    is JSON when we ask for ``response_format="json"``, so decode it when we
    can and hand back the raw string when we cannot — a caller that gets prose
    is better off than one that gets an exception.
    """
    if not isinstance(result, dict):
        return result
    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]
    parts = result.get("content")
    if not isinstance(parts, list):
        return result
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    joined = "\n".join(t for t in texts if t)
    if not joined:
        return result
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, TypeError):
        return joined


def _records(payload: Any, limit: int) -> list[dict[str, Any]]:
    """Normalize a Refero search payload down to a capped list of records."""
    if isinstance(payload, dict):
        rows = payload.get("records")
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)][: max(1, limit)]


def search_styles(query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Search curated design styles. Returns ``[]`` on any failure.

    Styles are the layer to start from for visual direction — typography,
    colour roles, spacing, surfaces and do/don't rules. Each record carries the
    ``uuid`` that :func:`get_style` expands into the full system.
    """
    try:
        payload = _call_tool("refero_search_styles", {"query": query, "response_format": "json"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("refero: style search failed: %s", exc)
        return []
    return [
        {
            "uuid": r.get("uuid"),
            "title": r.get("title"),
            "url": r.get("url"),
            "preview_url": r.get("preview_url"),
            "description": r.get("description"),
        }
        for r in _records(payload, limit)
    ]


def get_style(style_id: str) -> dict[str, Any]:
    """Expand one style UUID into its full design system. ``{}`` on failure.

    Returned verbatim rather than reshaped: the value is in fields like
    ``northStar``, ``colors`` (with ROLES attached), ``typeScale``, ``dos`` and
    ``donts``, and trimming them to a fixed schema is how the role information
    — the part an agent can actually hold onto — gets lost.
    """
    try:
        payload = _call_tool("refero_get_style", {"style_id": style_id, "response_format": "json"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("refero: style fetch failed: %s", exc)
        return {}
    return payload if isinstance(payload, dict) else {"style": payload}


def search_screens(query: str, platform: str = "web", limit: int = 6) -> list[dict[str, Any]]:
    """Search real product screens for concrete UI decisions. ``[]`` on failure.

    ``platform`` is required by Refero and must be ``web`` or ``ios``; anything
    else is coerced to ``web``, which is the only one a Paw Site can be.
    """
    if platform not in ("web", "ios"):
        platform = "web"
    try:
        payload = _call_tool(
            "refero_search_screens",
            {"query": query, "platform": platform, "response_format": "json"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("refero: screen search failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for r in _records(payload, limit):
        site = r.get("site") if isinstance(r.get("site"), dict) else {}
        content = r.get("content") if isinstance(r.get("content"), dict) else {}
        out.append(
            {
                "uuid": r.get("uuid"),
                "source": site.get("name") or site.get("domain"),
                "page_url": r.get("page_url"),
                "thumbnail_url": r.get("thumbnail_url"),
                "page_types": r.get("page_types"),
                "ux_patterns": r.get("ux_patterns"),
                "ui_elements": r.get("ui_elements"),
                "description": content.get("description"),
            }
        )
    return out


class ReferoStylesTool(BaseTool):
    """Design-direction research for the non-SDK agent backends.

    The claude_agent_sdk backend reaches the SAME ``search_styles`` /
    ``get_style`` helpers through the EE in-process MCP server, so there is one
    code path and two surfaces.
    """

    @property
    def name(self) -> str:
        return "refero_design_styles"

    @property
    def description(self) -> str:
        return (
            "Research REAL design systems from shipped websites before choosing a "
            "visual direction. Search returns curated styles — typography, colour "
            "ROLES, spacing, surfaces and explicit do/don't rules — extracted from "
            "pages that actually shipped. Pass `query` to search (e.g. 'editorial "
            "monochrome SaaS landing page', 'premium fintech restrained typography'), "
            "or pass `style_id` (a uuid from an earlier search) to get that style's "
            "FULL system. Search several angles before committing, and do not copy one "
            "source wholesale: these are reference ingredients, not templates. If a "
            "colour or font is marked for one role, keep it in that role or omit it. "
            "Returns an empty result when Refero is not configured — proceed on your "
            "own judgement and do not invent a citation."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What visual direction to look for — aesthetic, industry, "
                        "audience or a named product ('Linear dark developer tool')."
                    ),
                },
                "style_id": {
                    "type": "string",
                    "description": (
                        "A style uuid from an earlier search. When set, returns that "
                        "one style's full system instead of searching."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "How many styles to return when searching (default 6).",
                    "default": 6,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        query: str = "",
        style_id: str = "",
        limit: int = 6,
    ) -> str:
        import asyncio

        if style_id and style_id.strip():
            style = await asyncio.to_thread(get_style, style_id.strip())
            return json.dumps({"ok": bool(style), "style": style}, default=str)
        if not query or not query.strip():
            return json.dumps(
                {"ok": False, "error": "refero_design_styles needs `query` or `style_id`."}
            )
        results = await asyncio.to_thread(search_styles, query.strip(), limit)
        return json.dumps({"ok": True, "count": len(results), "results": results}, default=str)


class ReferoScreensTool(BaseTool):
    """Concrete-UI research for the non-SDK agent backends."""

    @property
    def name(self) -> str:
        return "refero_design_screens"

    @property
    def description(self) -> str:
        return (
            "Research REAL product screens for concrete interface decisions — page "
            "structure, content hierarchy, states, and which components a shipped "
            "product actually used. Pass `query` describing what is ON the screen "
            "('pricing page annual monthly toggle', 'feature comparison table', "
            "'dashboard empty state'). Use this AFTER styles: styles set the visual "
            "language, screens settle the structure. Returns an empty result when "
            "Refero is not configured."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What is on the screen — a page type, component, state or "
                        "on-screen text. Concrete beats abstract."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "How many screens to return (default 6).",
                    "default": 6,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str = "", limit: int = 6) -> str:
        import asyncio

        if not query or not query.strip():
            return json.dumps({"ok": False, "error": "refero_design_screens needs a `query`."})
        results = await asyncio.to_thread(search_screens, query.strip(), "web", limit)
        return json.dumps({"ok": True, "count": len(results), "results": results}, default=str)


__all__ = [
    "ReferoError",
    "ReferoScreensTool",
    "ReferoStylesTool",
    "get_style",
    "is_configured",
    "search_screens",
    "search_styles",
]
