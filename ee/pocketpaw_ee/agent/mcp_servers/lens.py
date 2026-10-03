# lens.py — in-process MCP server ``pocketpaw_lens``: read-only agent-health
# tools over paw-lens for the chat agent.
#
# Five read tools (overview, runs, one run, issues, monitors), each a thin shim
# over ``pocketpaw_ee.cloud.lens.service`` so the MCP surface and the HTTP proxy
# share one wire contract and ONE privacy rule: the service's ``redact`` strips
# message/tool content unless the CALLER (the human whose chat this is, read
# from the per-stream identity ContextVars) holds ``lens.manage`` in the
# workspace. Any failure resolving that role fails closed (content stripped).
# The workspace always comes from the stream, never from a tool argument, and
# every id argument is checked against the proxy's own patterns before it can
# reach an upstream path.
#
# Surface-scoped, not ambient: ``pocketpaw_lens`` is in core's
# ``SURFACE_SCOPED_MCP_SERVERS``, so the backends register it only on a run
# whose surface grants its tool ids (the ``agent_health`` surface's
# ``allowed_sdk_tools``). Every other chat neither loads the server nor sees
# its tools. Shape follows ask.py: SDK import guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` constants, ``_error_response`` / ``_success_response``.
"""Agent-side MCP surface for paw-lens agent-health reads."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_lens"
LENS_OVERVIEW_TOOL_ID = f"mcp__{SERVER_NAME}__lens_overview"
LENS_RUNS_TOOL_ID = f"mcp__{SERVER_NAME}__lens_runs"
LENS_RUN_TOOL_ID = f"mcp__{SERVER_NAME}__lens_run"
LENS_ISSUES_TOOL_ID = f"mcp__{SERVER_NAME}__lens_issues"
LENS_MONITORS_TOOL_ID = f"mcp__{SERVER_NAME}__lens_monitors"

LENS_TOOL_IDS = (
    LENS_OVERVIEW_TOOL_ID,
    LENS_RUNS_TOOL_ID,
    LENS_RUN_TOOL_ID,
    LENS_ISSUES_TOOL_ID,
    LENS_MONITORS_TOOL_ID,
)


def _error_response(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "is_error": True}


def _success_response(body: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(body, default=str)}]}


async def _load_user(user_id: str) -> Any | None:
    """The caller's ``User`` doc, or None. Module-level so tests can swap it."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User

    try:
        return await User.get(PydanticObjectId(user_id))
    except Exception:  # noqa: BLE001 — bad id / DB error → no user → content stripped
        return None


async def _caller() -> tuple[str | None, bool]:
    """``(workspace_id, full)`` for this stream; ``full`` is the admin check."""
    from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id
    from pocketpaw_ee.guards.deps import has_workspace_action

    workspace_id, user_id = current_workspace_id(), current_user_id()
    if not workspace_id:
        return None, False
    user = await _load_user(user_id) if user_id else None
    if user is None:
        return workspace_id, False
    try:
        return workspace_id, await has_workspace_action(user, workspace_id, "lens.manage")
    except Exception:  # noqa: BLE001 — role lookup failure redacts, never grants
        logger.debug("lens mcp: role check failed", exc_info=True)
        return workspace_id, False


def _bad_arg(args: dict, name: str, pattern: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        return f"invalid `{name}`."
    return None


async def _run(args: dict, checks: dict[str, str], call) -> dict:  # type: ignore[no-untyped-def]
    """Validate ``args`` against ``checks`` (name → regex), resolve the caller,
    then ``await call(workspace_id, full)``; CloudErrors become tool errors."""
    from pocketpaw_ee.cloud._core.errors import CloudError

    for name, pattern in checks.items():
        problem = _bad_arg(args, name, pattern)
        if problem:
            return _error_response(problem)
    workspace_id, full = await _caller()
    if not workspace_id:
        return _error_response("no active workspace; lens tools only work inside a cloud chat.")
    try:
        return _success_response(await call(workspace_id, full))
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")


def _since_ok(args: dict) -> str | None:
    since = args.get("since")
    if since is not None and (not isinstance(since, str) or len(since) > 64):
        return "invalid `since`."
    return None


async def _overview_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.lens import service

    if problem := _since_ok(args):
        return _error_response(problem)
    return await _run(
        args,
        {"agent_id": service.AGENT_ID},
        lambda ws, _full: service.overview(ws, args.get("since"), args.get("agent_id")),
    )


async def _runs_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.lens import service

    if problem := _since_ok(args):
        return _error_response(problem)
    status, limit = args.get("status"), args.get("limit")
    if status is not None and status not in ("ok", "error"):
        return _error_response("`status` must be 'ok' or 'error'.")
    if limit is not None and (not isinstance(limit, int) or not 1 <= limit <= 200):
        return _error_response("`limit` must be an integer from 1 to 200.")
    return await _run(
        args,
        {"agent_id": service.AGENT_ID, "automation": service.SAFE_ID},
        lambda ws, full: service.list_runs(
            ws,
            args.get("agent_id"),
            args.get("automation"),
            status,
            args.get("since"),
            limit,
            full=full,
        ),
    )


async def _run_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.lens import service

    if not args.get("trace_id"):
        return _error_response("`trace_id` is required.")
    trace_id, span_id = args["trace_id"], args.get("span_id")

    async def _call(ws: str, full: bool) -> Any:
        if span_id:
            return await service.get_span(ws, trace_id, span_id, full=full)
        return await service.get_run(ws, trace_id, full=full)

    return await _run(args, {"trace_id": service.SAFE_ID, "span_id": service.SAFE_ID}, _call)


async def _issues_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.lens import service

    if problem := _since_ok(args):
        return _error_response(problem)
    status, fingerprint = args.get("status"), args.get("fingerprint")
    if status is not None and status not in ("open", "muted", "resolved"):
        return _error_response("`status` must be 'open', 'muted' or 'resolved'.")

    async def _call(ws: str, full: bool) -> Any:
        if fingerprint:
            return await service.get_issue(ws, fingerprint, args.get("since"), full=full)
        return await service.list_issues(ws, status, args.get("since"), args.get("agent_id"))

    return await _run(args, {"fingerprint": service.SAFE_ID, "agent_id": service.AGENT_ID}, _call)


async def _monitors_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.lens import service

    if problem := _since_ok(args):
        return _error_response(problem)
    slug = args.get("slug")

    async def _call(ws: str, _full: bool) -> Any:
        if slug:
            return await service.get_monitor(ws, slug, args.get("since"))
        return await service.list_monitors(ws, args.get("since"))

    return await _run(args, {"slug": service.SAFE_ID}, _call)


_SINCE = {"type": "string", "description": "Window, e.g. '24h' or '7d' (default 7d)."}
_AGENT = {"type": "string", "description": "Agent id to narrow to one agent."}


def build_lens_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None without claude_agent_sdk."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_lens MCP disabled")
        return None

    def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": props,
            "required": required or [],
            "additionalProperties": False,
        }

    @tool(
        "lens_overview",
        "Agent health headline numbers for this workspace: runs, failing rate, cost, "
        "cache-hit rate, open issues and per-day series.",
        _schema({"since": _SINCE, "agent_id": _AGENT}),
    )
    async def lens_overview(args):  # type: ignore[no-untyped-def]
        return await _overview_handler(args)

    @tool(
        "lens_runs",
        "Recent agent runs, newest first: agent, model, status, duration, tokens, cost, "
        "summary, issue count, trace_id. Filter by agent, automation slug (kind:id) or status.",
        _schema(
            {
                "agent_id": _AGENT,
                "automation": {"type": "string", "description": "Monitor slug, kind:id."},
                "status": {"type": "string", "enum": ["ok", "error"]},
                "since": _SINCE,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
        ),
    )
    async def lens_runs(args):  # type: ignore[no-untyped-def]
        return await _runs_handler(args)

    @tool(
        "lens_run",
        "One run by trace_id: the run, its span tree (kind, status, duration, tool, "
        "error) and findings. Pass span_id for one span's messages, tool call and attributes.",
        _schema(
            {"trace_id": {"type": "string"}, "span_id": {"type": "string"}},
            required=["trace_id"],
        ),
    )
    async def lens_run(args):  # type: ignore[no-untyped-def]
        return await _run_handler(args)

    @tool(
        "lens_issues",
        "Issues (recurring failures grouped by cause) with counts and trend. Pass "
        "fingerprint for one issue with the runs it hit.",
        _schema(
            {
                "status": {"type": "string", "enum": ["open", "muted", "resolved"]},
                "fingerprint": {"type": "string"},
                "since": _SINCE,
                "agent_id": _AGENT,
            }
        ),
    )
    async def lens_issues(args):  # type: ignore[no-untyped-def]
        return await _issues_handler(args)

    @tool(
        "lens_monitors",
        "Scheduled automations (monitors) and their check-in health. Pass slug for one "
        "monitor's recent check-ins.",
        _schema({"slug": {"type": "string"}, "since": _SINCE}),
    )
    async def lens_monitors(args):  # type: ignore[no-untyped-def]
        return await _monitors_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[lens_overview, lens_runs, lens_run, lens_issues, lens_monitors],
    )
    return SERVER_NAME, server
