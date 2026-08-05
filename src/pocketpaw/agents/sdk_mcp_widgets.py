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
  - 2026-08-04 (fix/prompt-tells-the-truth — ONE CONTRACT, TWO SURFACES): the
    ``start_flow`` description and JSON schema were a second hand-written copy
    of the runtime builtin's (``tools/builtin/flow_tool.py``), 82.7% similar,
    and had drifted — this copy carried four rules the runtime one lacked, so
    the backend most deployments run got the weaker briefing. Both now read
    ``_flows.START_FLOW_DESCRIPTION`` / ``_flows.start_flow_parameters()``,
    beside the builder that enforces them. Note the entry below: that was the
    FIRST split of this same pair. Twice is a pattern, and the pattern is two
    copies.
  - 2026-07-03 (fix/mcp-tool-json-string-args): ``get_widget_spec`` and
    ``get_inline_widget_help`` now run their ``types`` array through the shared
    ``coerce_json_object_args`` helper, so a ``types`` value the model sent as a
    JSON string (``'["chart","stat"]'``) is decoded instead of mistaken for a
    single type name. Same class of fix as start_flow's ``_coerce_json_arg``,
    now shared across every in-process MCP handler.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args

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

    args = coerce_json_object_args(args, ("types",))
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

    args = coerce_json_object_args(args, ("types",))
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
        # Shared with the runtime builtin (``tools/builtin/flow_tool.py``).
        # These were two hand-written copies at 82.7% similarity, and they had
        # drifted — this one carried four rules the runtime copy lacked, so the
        # backend most deployments run got the weaker briefing. Second split of
        # this exact pair; the 2026-06-15 SPLIT-BRAIN FIX was the first.
        _start_flow_description(),
        _start_flow_parameters(),
    )
    async def start_flow(args):  # type: ignore[no-untyped-def]
        return await _start_flow_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[get_widget_spec, get_inline_widget_help, start_flow],
    )
    return SERVER_NAME, server


def _start_flow_description() -> str:
    """The canonical ``start_flow`` description, from the flow builder.

    Lazy for the same reason ``_flow_types_for_schema`` was: this module must
    stay importable without the ripple flow builder loaded at definition time.
    The fallback is deliberately a POINTER rather than a shortened copy of the
    contract — a second abbreviated description is exactly the drift this
    change removes, and a model told to call the tool and read its error is in
    better shape than one working from stale prose.
    """
    try:
        from pocketpaw.ripple._flows import START_FLOW_DESCRIPTION

        return START_FLOW_DESCRIPTION
    except Exception:  # pragma: no cover - defensive
        return (
            "Scaffold a multi-step flow or interactive mini-app from a FLAT "
            "step-graph. The flow builder is unavailable in this process, so "
            "the full authoring contract cannot be shown; call the tool with a "
            "`flow`, an `entry` and a `steps` list and it will name whatever "
            "is wrong."
        )


def _start_flow_parameters() -> dict[str, Any]:
    """The canonical ``start_flow`` JSON schema, from the flow builder.

    Same lazy-import contract as the description. The fallback keeps the tool
    callable with a permissive object schema rather than asserting a shape this
    process cannot validate.
    """
    try:
        from pocketpaw.ripple._flows import start_flow_parameters

        return start_flow_parameters()
    except Exception:  # pragma: no cover - defensive
        return {"type": "object", "properties": {}, "required": []}


__all__ = [
    "GET_INLINE_WIDGET_HELP_TOOL_ID",
    "GET_WIDGET_SPEC_TOOL_ID",
    "SERVER_NAME",
    "START_FLOW_TOOL_ID",
    "WIDGET_TOOL_IDS",
    "build_widgets_context_server",
]
