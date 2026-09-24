# Inspo design-research tool — how real shipped websites in a category are
# actually built, for grounding a generated page's composition in evidence
# rather than model defaults.
#
# Created: 2026-09-24 (feat/inspo-backend-parity). Inspo first shipped as an EE
# in-process MCP server only (``ee/pocketpaw_ee/agent/mcp_servers/inspo.py``),
# which only the claude_agent_sdk backend can see, so pydantic_ai and the other
# backends had no design research over real pages at all while Refero already
# reached them through BaseTools. The upstream call moved HERE so both surfaces
# share one code path, and ``InspoResearchTool`` / ``InspoDesignSystemTool``
# expose it to the non-SDK backends. Exactly the split ``refero.py`` and
# ``stock_images.py`` use, and for the same reason.
#
# UPSTREAM: inspomcp.dev — an archive of ~2,300 captured pages across ~830 real
# shipped sites, each carrying a DESIGN.md extracted from the live DOM
# (role-tagged palette, type ramp, spacing scale, macrostructure). MIT, free,
# unauthenticated, every endpoint read-only. It rate-limits PER IP, so
# ``inspo_mcp_url`` lets a deploy point at a self-hosted instance.
#
# TRANSPORT: plain JSON-RPC over one HTTP POST, not an MCP client session. The
# server answers ``tools/call`` with no ``initialize`` and no session id, so
# there is no handshake and nothing to leak. One request per call, one timeout.
#
# FAILURE CONTRACT: the helpers RAISE, because the EE server surfaces the
# upstream reason in its error envelope. The BaseTools catch everything and
# answer ``ok: false`` with a "proceed without it" message — a create turn must
# keep moving when a free service with no SLA is down.
"""Design research over an archive of real shipped websites (Inspo)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "https://inspomcp.dev/api/mcp"

# Tests inject an ``httpx.MockTransport`` here so the JSON-RPC exchange is
# exercised without live network — the same seam ``refero`` exposes.
_TRANSPORT: httpx.AsyncBaseTransport | None = None

# A create turn is a person waiting, and the archive has no SLA.
_TIMEOUT_SECONDS = 20.0

# Upstream accepts a token ceiling on every list-shaped tool. A create preamble
# is already large and the agent needs a macrostructure and a handful of
# exemplars, not the whole shortlist rendered long.
_MAX_TOKENS = 1200

# Our job-named helpers -> the upstream catalogue tool each one calls. Keeping
# the map explicit makes an upstream rename a one-line fix.
_UPSTREAM = {
    "research_page_design": "recommend",
    "get_reference_design_system": "get_design_system",
}


def endpoint() -> str:
    """The archive's MCP endpoint — ``inspo_mcp_url`` when set, else the hosted one.

    Read per call, not cached, so an override takes effect without a restart
    (``get_settings`` itself is cached; tests clear it)."""
    try:
        url = (getattr(get_settings(), "inspo_mcp_url", "") or "").strip()
        if url:
            return url
    except Exception:  # noqa: BLE001 — config must never break a tool call
        pass
    return _DEFAULT_ENDPOINT


async def call_upstream(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One JSON-RPC ``tools/call`` against the archive.

    Returns the decoded tool result, or raises. Never returns a half-result,
    because a partial design reference is worse than none: the agent would
    build on it.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        # The server may answer either way; asking for both lets it choose, and
        # a plain JSON body is what it returns for a stateless call.
        "Accept": "application/json, text/event-stream",
    }
    client_kwargs: dict[str, Any] = {"timeout": _TIMEOUT_SECONDS}
    if _TRANSPORT is not None:
        client_kwargs["transport"] = _TRANSPORT
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint(), json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
    if "error" in body:
        raise RuntimeError(str(body["error"].get("message", body["error"])))
    result = body.get("result") or {}
    # MCP wraps a tool result as ``content:[{type:"text", text:"<json>"}]``.
    # Unwrap to the payload the model should read; if it is not JSON, hand back
    # the text as-is rather than failing on a format change.
    for block in result.get("content", []):
        if block.get("type") == "text":
            text = block.get("text", "")
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                return {"text": text}
    return result


async def research_page_design(brief: str) -> dict[str, Any]:
    """A macrostructure pick, runners-up and real exemplar slugs for ``brief``.

    Raises on any upstream failure."""
    return await call_upstream(
        _UPSTREAM["research_page_design"],
        {"brief": brief.strip(), "maxTokens": _MAX_TOKENS},
    )


async def get_reference_design_system(slug: str) -> dict[str, Any]:
    """The DESIGN.md for one exemplar ``slug``. Raises on any upstream failure."""
    return await call_upstream(_UPSTREAM["get_reference_design_system"], {"slug": slug.strip()})


class InspoResearchTool(BaseTool):
    """Page-composition research for the non-SDK agent backends.

    The claude_agent_sdk backend reaches the SAME ``research_page_design`` helper
    through the EE ``pocketpaw_inspo`` MCP server, so there is one code path and
    two surfaces.
    """

    @property
    def name(self) -> str:
        return "inspo_research_page_design"

    @property
    def description(self) -> str:
        return (
            "Look up how REAL shipped websites in this category are actually built, "
            "before you design one. Returns a macrostructure pick with the reasoning "
            "behind it, a shortlist of runners-up, and real exemplar sites with slugs "
            "you can pass to `inspo_reference_design_system`. Use it ONCE per site, "
            "after you have committed to an aesthetic direction and BEFORE you write "
            "tokens or markup. These are real pages, so take their COMPOSITION (which "
            "sections, in what order, what carries the fold) and not their compliance "
            "— your own design system still outranks anything here on a visual value. "
            "If it errors, proceed on your own inference without retrying."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "brief": {
                    "type": "string",
                    "description": "The site in plain words — what it is for and who it serves.",
                },
            },
            "required": ["brief"],
        }

    async def execute(self, brief: str = "") -> str:
        if not brief or not brief.strip():
            return json.dumps(
                {"ok": False, "error": "inspo_research_page_design needs a non-empty `brief`."}
            )
        try:
            body = await research_page_design(brief)
        except Exception as exc:  # noqa: BLE001 — a create turn must keep moving
            logger.warning("inspo: research_page_design failed: %s", exc)
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        f"design research unavailable ({exc}). Proceed on your own "
                        "inference — do not retry and do not stall the build."
                    ),
                }
            )
        return json.dumps({"ok": True, "reference": body}, default=str)


class InspoDesignSystemTool(BaseTool):
    """One exemplar's extracted design system, for the non-SDK agent backends."""

    @property
    def name(self) -> str:
        return "inspo_reference_design_system"

    @property
    def description(self) -> str:
        return (
            "The DESIGN.md for one real site, extracted from its live DOM: actual "
            "fonts, frequency-ranked palette with the role each colour plays, type "
            "ramp, spacing scale, CSS variables, container width. Pass `slug` — an "
            "exemplar slug from an `inspo_research_page_design` result. Read it for "
            "RELATIONSHIPS (how many type sizes a real page uses, where its accent is "
            "actually spent) and not for values to copy. One follow-up call at most."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "An exemplar slug returned by inspo_research_page_design.",
                },
            },
            "required": ["slug"],
        }

    async def execute(self, slug: str = "") -> str:
        if not slug or not slug.strip():
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "inspo_reference_design_system needs a `slug` from an "
                        "inspo_research_page_design result."
                    ),
                }
            )
        try:
            body = await get_reference_design_system(slug)
        except Exception as exc:  # noqa: BLE001 — a create turn must keep moving
            logger.warning("inspo: get_reference_design_system failed: %s", exc)
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        f"design system lookup unavailable ({exc}). Proceed with the "
                        "structure you already have."
                    ),
                }
            )
        return json.dumps({"ok": True, "slug": slug.strip(), "design_system": body}, default=str)


__all__ = [
    "InspoDesignSystemTool",
    "InspoResearchTool",
    "call_upstream",
    "endpoint",
    "get_reference_design_system",
    "research_page_design",
]
