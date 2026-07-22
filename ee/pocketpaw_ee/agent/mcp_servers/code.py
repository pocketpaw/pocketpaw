# code.py — in-process MCP server exposing the ONE tool the main chat agent
# uses to reach the user's code: ``code_mode``. Created: 2026-07-22
# (feat/code-mode-tool, CD-2).
#
# What this is for. The /code surface is being inverted so the MAIN PocketPaw
# agent drives the conversation and Code Mode becomes a sub-agent it delegates
# to. This file is the seam. The main agent gets exactly one coarse tool rather
# than four proxied file verbs, so the sub-agent keeps its own read/write loop
# and the main agent never learns the shape of a file session.
#
# Why the tool does not touch a file. ``codeagent/transport.py`` recorded the
# constraint that shaped this whole design: an MCP server runs in the BACKEND,
# where the user's files are not. That is fatal to a tool that OPENS a file. It
# is not fatal to this one, because ``code_mode`` never opens anything — it asks
# the BROWSER to. The handler is an ``async def``, so it can await a future:
# ``delegate_to_browser`` (CD-1) registers a correlation id, pushes one
# ``code_delegate`` frame down the live SSE stream, and parks until the tab
# POSTs the answer back. The same inversion MCP standardizes as elicitation.
#
# Why this lives in ee/ and not src/pocketpaw/agents/. The build plan said
# ``src/pocketpaw/agents/sdk_mcp_code.py``, next to ``sdk_mcp_widgets.py``. That
# is the wrong side of the OSS/EE line twice over: the channel this wraps
# (``ee.cloud.codeagent.delegates``) is EE cloud code, and the workspace
# identity it needs comes from the per-stream ContextVars in
# ``ee.cloud.chat.agent_service``. An OSS module importing either would invert
# the dependency. So this clones ``belt.py`` instead — the closest sibling in
# kind as well as in shape, since that one is also a single-tool gate that hands
# work to something else rather than doing it. Registration is the standard
# ``pocketpaw.mcp_servers`` entry point (``CloudCodeMcpProvider``), NOT
# ``claude_sdk._get_mcp_servers``, which only builds the OSS-side servers.
#
# On metering, which the PRD left open: a ``code_mode`` call spends nothing
# here. This handler calls no model — it parks on a future. The model spend
# happens when the BROWSER calls ``POST /codeagent/turn``, which meters itself
# (see ``codeagent/router.py``: its workspace check exists because "this route
# spends money"). One delegated task therefore bills exactly once, on the route
# that actually buys the tokens. There is no second meter to reconcile and
# nothing to double-count.
#
# The tool id namespaces as ``mcp__pocketpaw_code__code_mode``. The /code
# SurfaceProfile hardcodes that exact string as a literal (CD-3 shipped before
# this file existed and could not import the constant). Do NOT drift the server
# name or the tool name without changing ``surface_registry._CODE_MODE_TOOL_IDS``
# in the same PR — they are matched by string, so a rename fails silently by
# scoping the surface to a tool id nothing provides.

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_code"

# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
CODE_MODE_TOOL_ID = f"mcp__{SERVER_NAME}__code_mode"

# Exported as a tuple for the provider's ``tool_ids()``, mirroring
# ``BELT_TOOL_IDS``. One entry today; the shape is what the loader expects.
CODE_TOOL_IDS = (CODE_MODE_TOOL_ID,)

# The two permission sets the sub-agent understands. ``ask`` gets the read-only
# verbs; ``edit`` may propose a change.
_MODES = ("ask", "edit")

# The safe default. It is deliberately the READ-ONLY mode, so that every way of
# not-specifying a mode — omitted, null, empty, misspelled, wrong type — lands
# on "cannot edit" rather than "can edit unexpectedly". ``dto.py`` makes the
# same choice on the wire for the same reason.
_DEFAULT_MODE = "ask"

# A task that is empty or absent is a bug in the caller, not a request. Bounded
# because it crosses the wire into the browser and ends up in a prompt.
_MAX_TASK_CHARS = 8000


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _workspace_id() -> str | None:
    """Resolve the active workspace from the per-stream ContextVars set by the
    cloud chat agent runtime.

    Only the workspace is read. The delegate is addressed to a workspace's live
    stream, and ``delegates.resolve_pending`` scopes the return leg the same
    way; the user id is not an authorization input there, so asking for one
    here would imply a check that is not made.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id

        return current_workspace_id()
    except Exception:  # noqa: BLE001 — outside a cloud stream this is simply absent
        return None


def _normalize_mode(raw: Any) -> str:
    """Coerce whatever the model sent into one of ``_MODES``.

    Anything unrecognized becomes ``ask``. This is the one place the fail-safe
    direction matters: a model that hallucinates ``mode="write"`` or sends an
    integer must lose the ability to edit, not gain it. Logged rather than
    rejected, because turning a garbled enum into a hard tool error would cost
    the user a turn for something that has a correct, safe reading.
    """
    if isinstance(raw, str):
        mode = raw.strip().lower()
        if mode in _MODES:
            return mode
        if mode:
            logger.warning("code_mode: unrecognized mode %r, falling back to %s", raw, _DEFAULT_MODE)
    elif raw is not None:
        logger.warning("code_mode: non-string mode %r, falling back to %s", type(raw), _DEFAULT_MODE)
    return _DEFAULT_MODE


async def _code_mode_handler(args: dict) -> dict[str, Any]:
    """Hand one coding task to the browser's Code Mode sub-agent and wait.

    Returns the sub-agent's payload on success. On EVERY failure path it returns
    an MCP error rather than raising: a raise inside an in-process tool handler
    surfaces to the model as a broken tool, whereas an error response gives it a
    true sentence to say to the user. That distinction is the whole reason
    ``delegate_to_browser`` returns an outcome on timeout instead of raising.
    """
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return _error_response("`task` is required — describe the coding task in words.")
    task = task.strip()
    if len(task) > _MAX_TASK_CHARS:
        return _error_response(
            f"`task` is too long ({len(task)} chars, limit {_MAX_TASK_CHARS}). "
            "Describe the change rather than pasting the code — the sub-agent "
            "reads the files itself."
        )

    mode = _normalize_mode(args.get("mode"))

    workspace_id = _workspace_id()
    if not workspace_id:
        # No cloud stream in scope: a CLI run, a background job, a test. The
        # delegate has nowhere to go and no tenant to scope the return leg to.
        return _error_response(
            "There is no active workspace session, so the code tool cannot reach "
            "the user's project from here."
        )

    from pocketpaw_ee.cloud.codeagent.delegates import delegate_to_browser

    outcome = await delegate_to_browser(workspace_id, task, mode)

    if not outcome.ok:
        logger.info("code_mode delegate failed ws=%s error=%s", workspace_id, outcome.error)
        # Relay the channel's own message. It already distinguishes "no browser
        # attached" from "the browser was slow", and the model needs that
        # difference to say something true about what happened.
        return _error_response(outcome.message or "The coding task could not be completed.")

    return _success_response(outcome.result or {})


def build_code_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for ``code_mode``, or return ``None``
    if the Claude Agent SDK isn't installed.

    Matches the ``(name, server) | None`` shape of ``build_belt_server`` so the
    backend's MCP registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_code MCP disabled")
        return None

    @tool(
        "code_mode",
        (
            "Read, search, or change the code in the user's project. This is the "
            "ONLY way to reach their code — the project does not live on your "
            "machine, so the built-in file and shell tools do not address it. "
            "Describe the task in words, the way the user gave it to you; this "
            "tool locates the relevant code itself, so do NOT try to find files "
            "first. Args: `task` (what to do, in plain language — required), "
            "`mode` ('ask' to read, search, and answer questions about the code; "
            "'edit' to change it — defaults to 'ask'). Use `mode='edit'` only "
            "when the user actually wants the code changed. Returns the "
            "sub-agent's result. On an error, relay the reason to the user — do "
            "NOT claim the code was read or changed when this returned an error."
        ),
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The coding task in plain language, e.g. 'add a loading "
                        "state to the submit button' or 'where is the retry "
                        "logic'. Describe the change, don't paste the code."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": list(_MODES),
                    "default": _DEFAULT_MODE,
                    "description": (
                        "'ask' reads and answers (read-only); 'edit' proposes a "
                        "change. Defaults to 'ask'."
                    ),
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    )
    async def code_mode(args):  # type: ignore[no-untyped-def]
        return await _code_mode_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[code_mode],
    )
    return SERVER_NAME, server


__all__ = [
    "CODE_MODE_TOOL_ID",
    "CODE_TOOL_IDS",
    "SERVER_NAME",
    "build_code_server",
]
