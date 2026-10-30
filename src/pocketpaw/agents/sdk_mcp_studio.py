# agents/sdk_mcp_studio.py — in-process SDK MCP server for /studio flow building.
#
# Created: 2026-08-21 (feat/studio-real-backend, agent-drawn studio flows).
#
# The default ``claude_agent_sdk`` backend reaches tools via MCP servers, not
# the function-calling bridge, so a runtime BaseTool alone would never surface
# to the agent most deployments run. This server exposes the SAME
# ``build_studio_flow`` contract as ``tools/builtin/studio_flow_tool.py``
# (description / schema / validator shared from there — the SPLIT-BRAIN lesson
# of ``flow_tool.py``: one contract, two surfaces), so the SDK agent can build
# a /studio Flow node graph from a natural-language goal.
#
# Mirrors ``sdk_mcp_widgets.py`` / ``sdk_mcp_atlas.py``: same server/tool
# registration shape, ``mcp__<server>__<tool>`` id convention, text-block
# result envelope with ``is_error`` on failures, and the same wiring points in
# ``claude_sdk.py`` (``_get_mcp_servers`` + ``_collect_mcp_tool_ids`` + the
# mode-scope grant).

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_studio"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
BUILD_STUDIO_FLOW_TOOL_ID = f"mcp__{SERVER_NAME}__build_studio_flow"

STUDIO_TOOL_IDS = (BUILD_STUDIO_FLOW_TOOL_ID,)


def _text_result(text: str, *, is_error: bool = False) -> dict:
    """Shape an SDK MCP tool result from a single text block."""
    out: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


def _coerce_json_arg(value: Any, name: str) -> Any:
    """Coerce a JSON-string arg into a structure; pass through if already one.

    Mirrors ``StudioFlowTool.execute``: SDK callers that can't pass a nested
    object through a flat signature send ``nodes`` / ``edges`` as a JSON
    string. Returns an ``Error: …`` string on bad JSON so the handler can
    relay it verbatim.
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


async def _build_studio_flow_handler(args: dict) -> dict:
    """Validate a node/edge graph and return the canonical flow spec envelope.

    Backs the ``build_studio_flow`` MCP tool on the default ``claude_agent_sdk``
    backend. MIRRORS ``tools/builtin/studio_flow_tool.py::StudioFlowTool.execute``
    (shared description / schema / validator) so the SDK chat agent and the
    runtime builtin registry scaffold identical graphs from identical input.
    """
    from pocketpaw.tools.builtin.studio_flow_tool import (
        dump_flow_spec,
        persist_flow_spec,
        validate_flow_spec,
    )

    args = coerce_json_object_args(args, ("nodes", "edges"))
    nodes = args.get("nodes")
    edges = args.get("edges")
    goal = args.get("goal") or ""
    flow_id = args.get("flow_id") or args.get("flowId") or ""

    if isinstance(nodes, str):
        nodes = _coerce_json_arg(nodes, "nodes")
        if isinstance(nodes, str):
            return _text_result(nodes, is_error=True)
    if isinstance(edges, str):
        edges = _coerce_json_arg(edges, "edges")
        if isinstance(edges, str):
            return _text_result(edges, is_error=True)

    spec, error = validate_flow_spec(nodes, edges, goal, flow_id)
    if error is not None:
        # Precise, agent-readable: the model fixes the graph and retries.
        return _text_result(f"Error: {error}", is_error=True)
    # Persist server-side the moment the agent produces the graph — the same
    # store the frontend's PUT uses, so the build lands in the DB even if the
    # SSE canvas event is late or dropped. Non-fatal on a save miss.
    persist_flow_spec(spec)
    return _text_result(dump_flow_spec(spec))


def build_studio_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None if the SDK is unavailable."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_studio MCP disabled")
        return None

    from pocketpaw.tools.builtin.studio_flow_tool import (
        STUDIO_FLOW_DESCRIPTION,
        studio_flow_parameters,
    )

    @tool(
        "build_studio_flow",
        STUDIO_FLOW_DESCRIPTION,
        studio_flow_parameters(),
    )
    async def build_studio_flow(args):  # type: ignore[no-untyped-def]
        return await _build_studio_flow_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[build_studio_flow],
    )
    return SERVER_NAME, server


__all__ = [
    "BUILD_STUDIO_FLOW_TOOL_ID",
    "SERVER_NAME",
    "STUDIO_TOOL_IDS",
    "build_studio_context_server",
]
