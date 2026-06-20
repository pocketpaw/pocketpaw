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
  - 2026-06-15 (feat/chain-flow-v2 — SPLIT-BRAIN FIX): this server's
    ``start_flow`` was still PRESET-ONLY (``flow_type`` required, ``steps``
    ignored) while the inline prompt now tells the model to author a FLAT
    step-graph. Because this is the ``start_flow`` the default
    ``claude_agent_sdk`` backend actually calls, every flat-graph authoring
    attempt was rejected at the tool boundary with "needs a flow_type" — the
    #1 cause of Chain Flow v2 flakiness. ``_start_flow_handler`` and the
    ``@tool`` definition are rewritten to MIRROR
    ``tools/builtin/flow_tool.py::execute``: a flat ``steps`` graph routes to
    ``build_flow_from_descriptor`` (the general path), a bare ``flow_type``
    routes to ``build_flow`` (the preset shorthand), JSON-string args are
    coerced, and a ``FlowBuildError`` is surfaced verbatim so the model can
    fix the graph and retry.
"""

from __future__ import annotations

import json
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


def _coerce_json_arg(value: Any, name: str) -> Any:
    """Coerce a JSON-string arg into a structure; pass through if already one.

    Mirrors ``StartFlowTool._coerce_json``: SDK / subprocess callers that can't
    pass a nested object through a flat signature send ``steps`` / ``complete``
    / ``config`` as a JSON string. Returns an ``Error: …`` string on bad JSON so
    the handler can relay it verbatim.
    """
    if value is None or not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return f"Error: `{name}` was a string but not valid JSON."


async def _start_flow_handler(args: dict) -> dict:
    """Expand a flow descriptor into a ``{version, ui}`` Chain Flow doc.

    Backs the ``start_flow`` MCP tool on the default ``claude_agent_sdk``
    backend. MIRRORS ``tools/builtin/flow_tool.py::StartFlowTool.execute`` so
    the SDK chat agent and the runtime builtin registry scaffold identical
    trees from identical input.

    Routing (flat graph takes precedence, then preset shorthand):
      - a flat ``steps`` graph (with ``flow`` / ``entry``, optional ``title`` /
        ``complete``) → :func:`build_flow_from_descriptor` (the GENERAL path the
        inline prompt teaches);
      - a bare ``flow_type`` (+ optional ``domain`` / ``config``) →
        :func:`build_flow` (the preset shorthand for the two known shapes).

    ``steps`` / ``complete`` / ``config`` may arrive as JSON strings — coerced.
    On a :class:`FlowBuildError` the precise, agent-readable message is surfaced
    verbatim (via ``is_error``) so the model can fix the flat graph and retry
    (genesis's forgiving-author loop), instead of getting a stack trace.
    """
    from pocketpaw.ripple._flows import (
        FLOW_TYPES,
        FlowBuildError,
        build_flow,
        build_flow_from_descriptor,
    )

    # Coerce JSON-string structures back to objects (matches StartFlowTool).
    steps = _coerce_json_arg(args.get("steps"), "steps")
    if isinstance(steps, str):  # coercion failed → it returned an Error string
        return _text_result(steps, is_error=True)
    complete = _coerce_json_arg(args.get("complete"), "complete")
    if isinstance(complete, str) and complete.startswith("Error:"):
        return _text_result(complete, is_error=True)
    config = _coerce_json_arg(args.get("config"), "config")
    if isinstance(config, str) and config.startswith("Error:"):
        return _text_result(config, is_error=True)

    # Route 1: a flat `steps` graph (the general authoring path).
    if steps is not None:
        if not isinstance(steps, list):
            return _text_result("Error: `steps` must be an array of step objects.", is_error=True)
        flow = args.get("flow")
        entry = args.get("entry")
        title = args.get("title")
        descriptor: dict[str, Any] = {
            "flow": flow if isinstance(flow, str) and flow else "flow",
            "entry": entry if isinstance(entry, str) else "",
            "steps": steps,
        }
        if isinstance(title, str) and title:
            descriptor["title"] = title
        if complete is not None:
            descriptor["complete"] = complete
        try:
            doc = build_flow_from_descriptor(descriptor)
        except FlowBuildError as exc:
            # Precise, agent-readable: the model fixes the flat graph and retries.
            return _text_result(f"Error: {exc}", is_error=True)
        return _text_result(_dump_flow_doc(doc))

    # Route 2: the optional `flow_type` preset shorthand.
    flow_type = args.get("flow_type") or ""
    if isinstance(flow_type, str) and flow_type:
        if config is not None and not isinstance(config, dict):
            return _text_result("Error: `config` must be an object (key/value map).", is_error=True)
        domain = args.get("domain")
        if domain is not None and not isinstance(domain, str):
            domain = None
        try:
            doc = build_flow(flow_type, domain=domain, config=config)
        except (FlowBuildError, ValueError) as exc:
            return _text_result(f"Error: {exc}", is_error=True)
        return _text_result(_dump_flow_doc(doc))

    valid = ", ".join(sorted(FLOW_TYPES))
    return _text_result(
        "Error: start_flow needs either a flat `steps` graph (with `flow` and "
        f"`entry`) or a `flow_type` preset shorthand (one of: {valid}).",
        is_error=True,
    )


def _dump_flow_doc(doc: dict[str, Any]) -> str:
    """Serialize a flow doc, dropping the internal ``_warnings`` key (soft
    single-branch-reachability notes are build-time guidance, not part of the
    rendered spec). Mirrors ``StartFlowTool._dump``."""
    clean = {k: v for k, v in doc.items() if k != "_warnings"}
    return json.dumps(clean, ensure_ascii=False)


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
            "Scaffold a complete multi-step flow OR interactive mini-app (a "
            "wizard, intake, survey, onboarding sequence, or a 'collect "
            "details then DO something' flow) from a FLAT step-graph. DO NOT "
            "hand-author nested chain / chain_map step trees and DO NOT fake a "
            "flow with a single `set`-stepped spec — both render only the first "
            "step and dead-end. You describe a flat list of steps; this tool "
            "materializes the ENTIRE nested, validated flow tree as a "
            "{version, ui} doc, ready to drop into a ```ui-spec fence. The flow "
            "then advances entirely client-side with no further model calls "
            "between steps.\n\n"
            "AUTHOR A FLAT GRAPH (think in states, not screens):\n"
            "- `flow`: a stable id for the flow (e.g. 'vendor_intake').\n"
            "- `entry`: the id of the first step.\n"
            "- `steps`: a list. Each step has an `id`, a `kind` "
            "(select | form | confirm | info), a `title`, its content "
            "(`options` for select, `fields` for form, `review` for confirm, "
            "`body` for info), and where it goes next:\n"
            '    * `next: "<id>"` — linear next step;\n'
            '    * `branch: { "<optionId>": "<id>" }` — branch on the '
            "picked option.\n"
            "  A step with NEITHER `next` nor `branch` is the TERMINAL step and "
            "carries `complete` (what to do with the answers). A terminal "
            "`complete` uses `action:` (chat | navigate | emit | call_binding | "
            "create_pocket | invoke_tool) — NEVER `type:`/`kind:`:\n"
            "    chat → hand the answers back to you (default); navigate / emit "
            "→ go somewhere / raise an event; call_binding → write to the "
            "backend (works today); create_pocket → materialize a permanent "
            "pocket; invoke_tool → run a named tool (may be unavailable until "
            "the tool registry ships — prefer call_binding to act on data).\n"
            "- Per-step `actions` (optional) are buttons that call a "
            "tool/API/binding MID-FLOW without leaving the step (verb: "
            "call_binding | api | invoke_tool). When the user says approve / "
            "reject / fulfill / take action — that is a `call_binding` ACTION "
            "BUTTON wired to the verb, not a yes/no select.\n"
            "- Reference earlier answers with `{stepId.field}` "
            "(e.g. `{pick_goal.label}`, `{enter_details.company}`) in review "
            "rows and action args — this tool rewrites them correctly; you "
            "NEVER write the raw `{state.…_selection/_formData}` form.\n\n"
            "The builder REPAIRS recoverable slips (a missing terminal "
            "`complete`, a dead-end last step) and REJECTS only genuine "
            "structural bugs (a transition to an undeclared id, a duplicate "
            "step id, a branch key that is not an option id) with a precise "
            "error you can fix and retry.\n\n"
            "PRESET SHORTHAND (optional): instead of `steps`, you may pass a "
            "`flow_type` for one of the two known shapes — 'onboarding_wizard' "
            "or 'due_diligence_intake' — plus an optional `config` for copy "
            "tweaks. The flat `steps` graph is the general path; `flow_type` is "
            "a convenience for those two exact shapes.\n\n"
            "Returns the JSON doc to emit. Wrap it verbatim in a ```ui-spec "
            "fence; do not edit the chain / chain_map structure."
        ),
        {
            "type": "object",
            "properties": {
                "flow": {
                    "type": "string",
                    "description": (
                        "A stable id for the flow (e.g. 'vendor_intake'). "
                        "Required when authoring a flat `steps` graph."
                    ),
                },
                "entry": {
                    "type": "string",
                    "description": (
                        "The id of the FIRST step (must exist in `steps`). "
                        "Required when authoring a flat `steps` graph."
                    ),
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "The flat step-graph: a list of step objects. Each step "
                        "has `id`, `kind` (select | form | confirm | info), "
                        "`title`, its kind-specific content (`options` / "
                        "`fields` / `review` / `body`), and a transition "
                        "(`next` or `branch`) — OR `complete` if it is the "
                        "terminal step. Optional per-step `actions` run a "
                        "tool/API/binding mid-flow. This is the general "
                        "authoring path."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Optional title shown on the flow frame.",
                },
                "complete": {
                    "type": "object",
                    "description": (
                        "Optional flow-level terminal default — the `complete` "
                        "action a terminal step inherits when it declares none "
                        "of its own (e.g. {action:'chat', message:'…'})."
                    ),
                },
                "flow_type": {
                    "type": "string",
                    "enum": list(_flow_types_for_schema()),
                    "description": (
                        "OPTIONAL preset shorthand for the two known shapes "
                        "('onboarding_wizard' | 'due_diligence_intake') instead "
                        "of authoring a flat `steps` graph."
                    ),
                },
                "domain": {
                    "type": "string",
                    "description": (
                        "Optional domain hint (e.g. 'fintech', 'saas') for the "
                        "preset path. Reserved for a future data-fresh path; "
                        "does not change the tree today."
                    ),
                },
                "config": {
                    "type": "object",
                    "description": (
                        "Optional per-preset copy overrides (preset path only). "
                        "Shape-stable: tweaks labels/text, never the chain "
                        "structure."
                    ),
                },
            },
            # Nothing is hard-required at the schema level: the general path
            # needs flow+entry+steps, the preset path needs flow_type. The
            # handler validates the combination and returns an agent-readable
            # error when neither is supplied.
            "required": [],
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
