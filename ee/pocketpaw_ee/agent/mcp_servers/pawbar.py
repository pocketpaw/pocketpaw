# ee/agent/mcp_servers/pawbar.py — in-process MCP server: Paw Bar action tools.
# Created: 2026-07-16 (Paw Bar action registry, C1) — exposes ONE tool per verb a
#   CONCIERGE widget declares (e.g. ``pawbar_add_to_cart``) so the public concierge
#   agent can drive the visitor commerce loop through the SAME shared executor the
#   POST /paw-bar/action endpoint uses. The tool set is PER-RUN and PER-WIDGET: it
#   is built from the ``current_pawbar_run()`` ContextVar that ``run_core`` binds
#   for a concierge run whose widget declares actions. When that context is absent
#   (every non-concierge run, or a concierge widget with no actions) the builder
#   returns None — no tools register, so the concierge tool surface stays deny-all
#   exactly as before this slice.
#
#   SS-2 safety: each handler RE-LOADS the live widget by id (workspace-scoped) and
#   calls ``execute_action``, which re-validates the verb is declared, coerces the
#   args, and enforces the auto/gated policy. So even if a warm subprocess reuses a
#   stale tool set, no undeclared/unauthorized effect can fire — the executor, not
#   the tool surface, is the authority. A gated verb still only raises an Instinct
#   proposal; the agent never executes a tenant-scoped effect.
#
#   Registered via the ``pocketpaw.mcp_servers`` entry point (``pawbar`` →
#   ``CloudPawBarActionsMcpProvider``); its tool ids join the SDK allowlist through
#   the provider's ``tool_ids()`` and are kept past the concierge restrictive
#   allow-list because ``_concierge_profile`` names exactly this widget's verb ids.

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pawbar_actions"

# type-name (as declared in the spec's flat arg map) → the Python type the SDK
# ``@tool`` schema uses for that arg.
_ARG_PYTYPE: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool}


def pawbar_tool_name(verb: str) -> str:
    """The tool name for a declared verb (``add_to_cart`` → ``pawbar_add_to_cart``)."""
    return f"pawbar_{verb}"


def pawbar_tool_id(verb: str) -> str:
    """The fully-namespaced MCP tool id the SDK allowlist + profile use."""
    return f"mcp__{SERVER_NAME}__{pawbar_tool_name(verb)}"


def _run_context() -> dict[str, Any] | None:
    """Resolve the active concierge run's Paw Bar action context, or None."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_pawbar_run

        return current_pawbar_run()
    except Exception:
        return None


def pawbar_tool_ids() -> tuple[str, ...]:
    """Tool ids for the CURRENT run's declared verbs — for the SDK allowlist.

    Empty when there is no active concierge action context (so the allowlist gains
    nothing on any other run)."""
    run = _run_context()
    if not run:
        return ()
    ids: list[str] = []
    for action in run.get("actions", []) or []:
        verb = action.get("verb") if isinstance(action, dict) else None
        if verb:
            ids.append(pawbar_tool_id(verb))
    return tuple(ids)


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}]}


async def _run_verb(verb: str, args: dict[str, Any]) -> dict[str, Any]:
    """Shared tool handler: re-load the live widget and run the shared executor."""
    from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

    run = _run_context()
    if not run:
        return _error("no active Paw Bar action context for this run")
    widget_id = str(run.get("widget_id") or "")
    if not widget_id:
        return _error("no widget bound to this run")
    workspace_id = current_workspace_id() or ""
    customer_ref = current_user_id() or ""

    from pocketpaw.stores import get_paw_bar_store
    from pocketpaw_ee.paw_bar.actions import execute_action

    store = get_paw_bar_store()
    # Scope the load to the run's workspace so a tool can only ever touch its own
    # tenant's widget (defense-in-depth beside the endpoint binding check).
    widget = await store.get_widget(widget_id, workspace_id=workspace_id or None)
    if widget is None:
        return _error("widget not found")

    if not isinstance(args, dict):
        args = {}
    outcome = await execute_action(widget, workspace_id, customer_ref, verb, args, store=store)
    if not outcome.ok:
        return _error(outcome.error or "action_failed")
    return _ok({"ok": True, "result": outcome.result, "cart": outcome.cart})


def _arg_schema(declared_args: dict[str, Any]) -> dict[str, type]:
    """Map an action's declared flat arg types to the SDK ``@tool`` schema shape."""
    schema: dict[str, type] = {}
    for name, type_name in (declared_args or {}).items():
        schema[name] = _ARG_PYTYPE.get(str(type_name), str)
    return schema


def build_pawbar_actions_server() -> tuple[str, Any] | None:
    """Build the in-process server with one tool per declared verb, or None.

    Reads ``current_pawbar_run()``: no context / no actions → None (no server, so
    the concierge tool surface stays deny-all). Otherwise builds one tool per
    declared action, each wired to the shared executor for that verb."""
    run = _run_context()
    if not run:
        return None
    actions = [a for a in (run.get("actions") or []) if isinstance(a, dict) and a.get("verb")]
    if not actions:
        return None

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pawbar_actions MCP disabled")
        return None

    def _make_tool(action: dict[str, Any]) -> Any:
        verb = str(action["verb"])
        policy = str(action.get("policy") or "gated")
        label = str(action.get("label") or "") or verb
        if policy == "auto":
            effect = "Runs immediately and returns the updated cart."
        else:
            effect = (
                "Does NOT run the action — it sends the request to the business for "
                "a human to approve. Tell the visitor it was submitted for review."
            )
        description = (
            f"Paw Bar action '{verb}' ({label}). {effect} Call it only when the "
            "visitor clearly wants this action; pass the declared args."
        )

        @tool(pawbar_tool_name(verb), description, _arg_schema(action.get("args", {})))
        async def _handler(args: dict[str, Any], _verb: str = verb) -> dict[str, Any]:  # type: ignore[no-untyped-def]
            return await _run_verb(_verb, args)

        return _handler

    tools = [_make_tool(a) for a in actions]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
    return SERVER_NAME, server


__all__ = [
    "SERVER_NAME",
    "build_pawbar_actions_server",
    "pawbar_tool_id",
    "pawbar_tool_ids",
    "pawbar_tool_name",
]
