"""In-process SDK MCP server exposing ripple widget-spec lookups to backends.

The ``get_widget_spec`` / ``get_inline_widget_help`` tools let an agent fetch
the canonical prop schema for a ripple widget before composing a ui-spec, so it
never guesses prop names. ``start_flow`` scaffolds a complete multi-step Chain
Flow from a tiny descriptor so the agent never hand-authors a nested
chain / chain_map tree. Pure core: they read the ripple manifest, the
inline-widget catalog, and the flow-template builder (``pocketpaw.ripple``),
which carry no cloud dependency.

Split out of the old ``sdk_mcp_pocket.py`` in the OSS-EE split (Phase 3b). That
file also carried cloud ``get_pocket`` / ``list_pockets`` tools, which moved to
``pocketpaw_ee.agent.mcp_servers.pockets``. The two surfaces are now separate
in-process MCP servers — ``pocketpaw_widgets`` (this one, core) and
``pocketpaw_pocket`` (EE).

Changes:
  - 2026-05-31 (fix/bridge-start-flow-to-chat, RFC 13): added the
    ``start_flow`` tool. The RFC 13 M3 authoring tool shipped only in the
    runtime builtin registry (``tools/builtin/flow_tool.py``); it was never
    bridged into the cloud chat agent's MCP surface, so the home/cloud agent
    could not call it and hand-authored a flat ``set``-stepped spec instead
    (the anti-pattern start_flow exists to prevent). Hosting it on this
    already-ambient core server is the minimal reachable bridge — it rides
    the same registration + allowlist path as ``get_inline_widget_help``.
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
START_FLOW_TOOL_ID = f"mcp__{SERVER_NAME}__start_flow"

WIDGET_TOOL_IDS = (
    GET_WIDGET_SPEC_TOOL_ID,
    GET_INLINE_WIDGET_HELP_TOOL_ID,
    START_FLOW_TOOL_ID,
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


def _text_result(text: str, *, is_error: bool = False) -> dict:
    """Shape an SDK MCP tool result from a single text block."""
    out: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


async def _start_flow_handler(args: dict) -> dict:
    """Expand a flow descriptor into a ``{version, ui}`` Chain Flow doc.

    Backs the ``start_flow`` MCP tool. Shares the deterministic
    ``pocketpaw.ripple._flows.build_flow`` builder with the runtime
    ``StartFlowTool`` so the cloud agent and the runtime registry scaffold
    identical trees. The model emits only ``flow_type`` (+ optional ``domain``
    / ``config``); Python owns the recursively-nested chain / chain_map tree.

    Args:
      flow_type: required template name (one of ``FLOW_TYPES``).
      domain:    optional forward-compatible hint; does not change the tree.
      config:    optional per-template copy overrides (shape-stable). May
                 arrive as a JSON string from some callers — coerced here.
    """
    import json

    from pocketpaw.ripple._flows import FLOW_TYPES, build_flow

    flow_type = args.get("flow_type") or ""
    if not isinstance(flow_type, str) or not flow_type:
        valid = ", ".join(sorted(FLOW_TYPES))
        return _text_result(
            f"Error: start_flow needs a `flow_type`. Known templates: {valid}.",
            is_error=True,
        )

    domain = args.get("domain")
    if domain is not None and not isinstance(domain, str):
        domain = None

    config = args.get("config")
    # `config` may arrive as a JSON string from callers that can't pass a
    # nested object through a flat signature. Coerce it, matching StartFlowTool.
    if isinstance(config, str):
        try:
            config = json.loads(config) if config.strip() else None
        except json.JSONDecodeError:
            return _text_result("Error: `config` was a string but not valid JSON.", is_error=True)
    if config is not None and not isinstance(config, dict):
        return _text_result("Error: `config` must be an object (key/value map).", is_error=True)

    try:
        doc = build_flow(flow_type, domain=domain, config=config)
    except ValueError as exc:
        # Unknown flow_type — surface the valid set so the model can retry.
        return _text_result(f"Error: {exc}", is_error=True)

    return _text_result(json.dumps(doc, ensure_ascii=False))


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

    @tool(
        "start_flow",
        (
            "Scaffold a complete multi-step flow (wizard / intake / survey / "
            "any step-by-step or collect-then-act sequence) from a tiny "
            "descriptor. DO NOT hand-author nested chain / chain_map step "
            "trees and DO NOT fake a multi-step flow with a single `set`-"
            "stepped spec — both are fragile and render only the first step. "
            "Pick a `flow_type` template and this tool returns the ENTIRE "
            "nested flow tree as a {version, ui} doc, ready to drop verbatim "
            "into a ```ui-spec fenced block. The flow then advances entirely "
            "client-side with no further model calls between steps.\n\n"
            "Templates (`flow_type`):\n"
            "- `onboarding_wizard` — pick a goal -> enter workspace details "
            "(branches on the goal) -> confirm. A new-user setup wizard.\n"
            "- `due_diligence_intake` — pick a deal stage -> stage-specific "
            "financials (branches on the stage) -> risk flags -> review. A "
            "multi-step vertical intake; the same shape works for a survey.\n\n"
            "Optional `config` tweaks copy without changing the tree's shape "
            "(onboarding_wizard accepts {product_name, goals}; "
            "due_diligence_intake accepts {company_name, submit_event}). When "
            "in doubt, pass just `flow_type`. Returns the JSON doc to emit — "
            "wrap it verbatim in a ```ui-spec fence; do not edit the chain / "
            "chain_map structure."
        ),
        {
            "type": "object",
            "properties": {
                "flow_type": {
                    "type": "string",
                    "enum": list(_flow_types_for_schema()),
                    "description": (
                        "Which flow template to scaffold. The builder owns "
                        "the full nested step tree for this template."
                    ),
                },
                "domain": {
                    "type": "string",
                    "description": (
                        "Optional domain hint (e.g. 'fintech', 'saas'). "
                        "Reserved for a future data-fresh path; does not "
                        "change the tree today."
                    ),
                },
                "config": {
                    "type": "object",
                    "description": (
                        "Optional per-template copy overrides. Shape-stable: "
                        "tweaks labels/text, never the chain structure."
                    ),
                },
            },
            "required": ["flow_type"],
        },
    )
    async def start_flow(args):  # type: ignore[no-untyped-def]
        return await _start_flow_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[get_widget_spec, get_inline_widget_help, start_flow],
    )
    return SERVER_NAME, server


def _flow_types_for_schema() -> tuple[str, ...]:
    """The known flow templates, for the ``start_flow`` enum.

    Imported lazily so the module stays importable without the ripple flow
    builder loaded at definition time; falls back to an empty tuple (the SDK
    accepts any string then) if the import is unavailable.
    """
    try:
        from pocketpaw.ripple._flows import FLOW_TYPES

        return tuple(FLOW_TYPES)
    except Exception:  # pragma: no cover - defensive
        return ()


__all__ = [
    "GET_INLINE_WIDGET_HELP_TOOL_ID",
    "GET_WIDGET_SPEC_TOOL_ID",
    "SERVER_NAME",
    "START_FLOW_TOOL_ID",
    "WIDGET_TOOL_IDS",
    "build_widgets_context_server",
]
