# ee/agent/mcp_servers/pawbar.py — in-process MCP server: Paw Bar action tools.
# Updated: 2026-07-31 (owner inbox, slice 3 — the escape hatch) — the server now
#   also carries ONE built-in tool, ``pawbar_request_human``, which is NOT a
#   spec-declared verb: it is present on every concierge run bound to a widget,
#   including the overwhelming majority that declare no actions at all (the
#   builder used to return None for those). It raises a human handoff through
#   ``paw_bar.handoff.raise_handoff`` — escalate the conversation, write the
#   ``_paw_handoffs`` record, notify the owner — and executes nothing else, so
#   CONCIERGE stays zero-authority: an agent reaching this tool can ask for a
#   person and cannot touch tenant state. A widget that has (oddly) declared its
#   own ``request_human`` verb keeps its declared semantics and the built-in is
#   skipped, so one tool name never registers twice; the visitor's always-
#   available ``POST /paw-bar/request-human`` covers that site regardless.
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

# The built-in escape hatch's verb. Imported from the producer so the tool name,
# the audit marker and the writer can't drift apart. Stdlib-only at import time.
from pocketpaw_ee.paw_bar.handoff import HANDOFF_VERB

logger = logging.getLogger(__name__)

SERVER_NAME = "pawbar_actions"

# type-name (as declared in the spec's flat arg map) → the Python type the SDK
# ``@tool`` schema uses for that arg.
_ARG_PYTYPE: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool}

_HANDOFF_DESCRIPTION = (
    "Ask a real person from the business to take over this conversation. Call it "
    "whenever the visitor asks to talk to a human, a person, someone real, "
    "support, the owner, or the team — that request is ALWAYS honored, never "
    "deflected — and also when you have failed to answer the same question twice "
    "and have nothing new to offer. It notifies the business and marks this "
    "conversation as waiting for a person; it takes no other action. After "
    "calling it, tell the visitor plainly that a human has been notified."
)

# The one arg: why they want a person. Free text the visitor effectively authored,
# sanitized by the producer before it reaches any owner-facing surface.
_HANDOFF_ARG_SCHEMA: dict[str, type] = {"reason": str}


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


def handoff_tool_id() -> str:
    """The escape hatch's fully-namespaced tool id (``…__pawbar_request_human``)."""
    return pawbar_tool_id(HANDOFF_VERB)


def _declared_verbs(run: dict[str, Any]) -> list[str]:
    """The run's declared action verbs, in order, skipping malformed entries."""
    verbs: list[str] = []
    for action in run.get("actions", []) or []:
        verb = action.get("verb") if isinstance(action, dict) else None
        if verb:
            verbs.append(str(verb))
    return verbs


def pawbar_tool_ids() -> tuple[str, ...]:
    """Tool ids for the CURRENT run's tools — for the SDK allowlist.

    The run's declared verbs, plus the built-in escape hatch when the run carries
    a handoff context (every concierge run bound to a widget). Empty when there is
    no active concierge context at all, so the allowlist gains nothing on any
    other run."""
    run = _run_context()
    if not run:
        return ()
    verbs = _declared_verbs(run)
    if run.get("handoff") and HANDOFF_VERB not in verbs:
        verbs.append(HANDOFF_VERB)
    return tuple(pawbar_tool_id(v) for v in verbs)


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


async def _run_handoff(args: dict[str, Any]) -> dict[str, Any]:
    """The escape-hatch handler: raise a handoff for THIS run's conversation.

    Same shape as ``_run_verb`` — re-resolve the widget workspace-scoped from the
    live store rather than trusting anything the tool call carried — but it runs
    the handoff producer instead of the action executor. Nothing here can execute
    a declared verb, so a concierge run reaching this tool escalates itself to a
    human and touches no tenant state.
    """
    from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

    run = _run_context()
    if not run:
        return _error("no active Paw Bar context for this run")
    widget_id = str(run.get("widget_id") or "")
    if not widget_id:
        return _error("no widget bound to this run")
    workspace_id = current_workspace_id() or ""
    customer_ref = current_user_id() or ""

    from pocketpaw.stores import get_paw_bar_store
    from pocketpaw_ee.paw_bar.handoff import raise_handoff

    store = get_paw_bar_store()
    widget = await store.get_widget(widget_id, workspace_id=workspace_id or None)
    if widget is None:
        return _error("widget not found")

    reason = str((args or {}).get("reason", "") or "") if isinstance(args, dict) else ""
    outcome = await raise_handoff(
        widget=widget,
        workspace_id=workspace_id,
        customer_ref=customer_ref,
        question=reason,
        # The agent never supplies a contact address: it would be repeating
        # something out of an untrusted transcript, and the visitor's own
        # request-human call is where a first-party address belongs.
        contact="",
        source="agent",
        store=store,
    )
    if not outcome.ok:
        return _error(outcome.error or "handoff_failed")
    return _ok(
        {
            "ok": True,
            "result": {
                "status": "notified",
                "message": (
                    "A person from the business has been notified and will pick up "
                    "this conversation. Tell the visitor so plainly."
                ),
            },
        }
    )


def _arg_schema(declared_args: dict[str, Any]) -> dict[str, type]:
    """Map an action's declared flat arg types to the SDK ``@tool`` schema shape."""
    schema: dict[str, type] = {}
    for name, type_name in (declared_args or {}).items():
        schema[name] = _ARG_PYTYPE.get(str(type_name), str)
    return schema


def build_pawbar_actions_server() -> tuple[str, Any] | None:
    """Build the in-process server: one tool per declared verb + the escape hatch.

    Reads ``current_pawbar_run()``: no context → None (no server, so the tool
    surface stays deny-all on every non-concierge run). With a context, builds one
    tool per declared action, plus the built-in ``pawbar_request_human`` whenever
    the run carries a handoff context — which is why a concierge widget with NO
    declared actions now yields a server where it used to yield None. A widget
    that declared its own ``request_human`` verb keeps that verb's semantics and
    the built-in is skipped, so the server never registers one name twice."""
    run = _run_context()
    if not run:
        return None
    actions = [a for a in (run.get("actions") or []) if isinstance(a, dict) and a.get("verb")]
    wants_handoff = bool(run.get("handoff")) and not any(
        str(a.get("verb")) == HANDOFF_VERB for a in actions
    )
    if not actions and not wants_handoff:
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
    if wants_handoff:

        @tool(pawbar_tool_name(HANDOFF_VERB), _HANDOFF_DESCRIPTION, _HANDOFF_ARG_SCHEMA)
        async def _handoff_handler(args: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
            return await _run_handoff(args)

        tools.append(_handoff_handler)
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
    return SERVER_NAME, server


__all__ = [
    "HANDOFF_VERB",
    "SERVER_NAME",
    "build_pawbar_actions_server",
    "handoff_tool_id",
    "pawbar_tool_id",
    "pawbar_tool_ids",
    "pawbar_tool_name",
]
