"""In-process SDK MCP server exposing ripple widget-spec lookups to backends.

The ``get_widget_spec`` / ``get_inline_widget_help`` tools let an agent fetch
the canonical prop schema for a ripple widget before composing a ui-spec, so it
never guesses prop names. Pure core: they read the ripple manifest and the
inline-widget catalog (``pocketpaw.ripple``), which carry no cloud dependency.

Split out of the old ``sdk_mcp_pocket.py`` in the OSS-EE split (Phase 3b). That
file also carried cloud ``get_pocket`` / ``list_pockets`` tools, which moved to
``pocketpaw_ee.agent.mcp_servers.pockets``. The two surfaces are now separate
in-process MCP servers — ``pocketpaw_widgets`` (this one, core) and
``pocketpaw_pocket`` (EE).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_widgets"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
GET_WIDGET_SPEC_TOOL_ID = f"mcp__{SERVER_NAME}__get_widget_spec"
GET_INLINE_WIDGET_HELP_TOOL_ID = f"mcp__{SERVER_NAME}__get_inline_widget_help"

WIDGET_TOOL_IDS = (
    GET_WIDGET_SPEC_TOOL_ID,
    GET_INLINE_WIDGET_HELP_TOOL_ID,
)


async def _get_widget_spec_handler(args: dict) -> dict:
    """Fetch the manifest, filter to requested widget types, and return a
    formatted markdown reference. Backs the ``get_widget_spec`` MCP tool."""
    from pocketpaw.config import get_settings
    from pocketpaw.ripple.manifest import format_for_prompt, get_manifest

    raw_types = args.get("types") or []
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    requested = [t for t in raw_types if isinstance(t, str) and t]
    if not requested:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Error: pass `types` as a non-empty array of widget type names.",
                }
            ],
            "is_error": True,
        }

    settings = get_settings()
    manifest = await get_manifest(
        settings.ripple_manifest_url,
        ttl_seconds=settings.ripple_manifest_ttl_seconds,
    )
    if manifest is None:
        return {
            "content": [{"type": "text", "text": "Error: ripple manifest unavailable."}],
            "is_error": True,
        }

    widgets = manifest.get("widgets") or []
    by_type = {w.get("type"): w for w in widgets if w.get("type")}
    matched = [by_type[t] for t in requested if t in by_type]
    missing = [t for t in requested if t not in by_type]

    if not matched:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"No matching widgets. Unknown types: {', '.join(missing)}",
                }
            ],
            "is_error": True,
        }

    block = format_for_prompt({"widgets": matched})
    if missing:
        block += f"\n\n_Note: unknown types skipped: {', '.join(missing)}_"

    return {"content": [{"type": "text", "text": block}]}


async def _get_inline_widget_help_handler(args: dict) -> dict:
    """Handler for get_inline_widget_help — returns the slice of the
    chat-inline widget catalog matching the requested types.

    Args:
      types: list of widget kinds the agent intends to use
             (e.g. ["chart", "sparkline"]). Empty / missing → full
             catalog (rare — agent generally knows what it wants).
    """
    from pocketpaw.ripple._inline_core import widget_help

    types = args.get("types") or []
    if not isinstance(types, list):
        types = []
    return {"content": [{"type": "text", "text": widget_help([str(t) for t in types])}]}


def build_widgets_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None if the SDK is unavailable."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_widgets MCP disabled")
        return None

    @tool(
        "get_widget_spec",
        (
            "Get full props, types, and example ui-spec for one or more "
            "Ripple widgets. Pass ``types`` as an array of widget type names "
            "(e.g. ``['feed', 'timeline', 'stat']``). Returns a markdown "
            "reference with each widget's props schema and a runnable example. "
            "MANDATORY before composing a ui-spec for any widget not in the "
            "FREE LIST under WIDGET SPEC TOOL RULE — never guess prop names "
            "or shapes from the widget name. Batch types in a single call. "
            "Available types are listed under WIDGET CATALOG in the system "
            "prompt."
        ),
        {"types": list},
    )
    async def get_widget_spec(args):  # type: ignore[no-untyped-def]
        return await _get_widget_spec_handler(args)

    @tool(
        "get_inline_widget_help",
        "Return the chat-inline Ripple widget catalog. Call this BEFORE "
        "emitting any non-core widget in a ui-spec fence (anything "
        "beyond text/heading/stat/button/table/flex). Pass the widget "
        "types you intend to use; you receive the canonical prop "
        "schema for those widgets so the spec renders on the first "
        "try. Cheap, in-process, single round-trip.",
        {
            "type": "object",
            "properties": {
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Widget kinds you plan to use, e.g. "
                        "['chart', 'sparkline']. Empty returns the "
                        "full catalog."
                    ),
                }
            },
        },
    )
    async def get_inline_widget_help(args):  # type: ignore[no-untyped-def]
        return await _get_inline_widget_help_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[get_widget_spec, get_inline_widget_help],
    )
    return SERVER_NAME, server


__all__ = [
    "GET_INLINE_WIDGET_HELP_TOOL_ID",
    "GET_WIDGET_SPEC_TOOL_ID",
    "SERVER_NAME",
    "WIDGET_TOOL_IDS",
    "build_widgets_context_server",
]
