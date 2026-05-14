# sdk_mcp_tasks.py — in-process MCP server exposing Mission Control Tasks.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend. Mirrors
#   the read-only ``sdk_mcp_pocket.py`` pattern but adds the
#   state-mutating ``claim_task`` and ``complete_task`` verbs that the
#   agent runtime needs to pick up work routed to it from Mission
#   Control's create form. Tool ids namespace as
#   ``mcp__pocketpaw_tasks__*`` so the Claude Code allowlist machinery
#   matches them.
"""Agent-side MCP surface for the Mission Control Tasks entity.

Tools registered:

  - ``list_my_tasks(status='proposed')`` — list Tasks where
    ``assignee.id == this agent`` in the active workspace. Cheap; the
    agent loop calls this on its polling cycle to find new work even
    when SSE is unavailable.
  - ``claim_task(task_id)`` — atomic ``proposed → in_progress`` flip.
    Returns ``{ok, task}`` on success or ``{ok: False, reason}`` when
    another agent (or the agent's own retry) lost the race.
  - ``complete_task(task_id, next_action='archive')`` — finish.
    ``next_action='request_approval'`` routes the task back to the
    creator as a Nudge for sign-off.

The agent's identity comes from the per-stream ``ContextVar``s in
``ee.cloud.chat.agent_service`` (same chokepoint the pocket MCP server
uses). When run outside an SSE chat stream the tools return a clear
error rather than silently mis-tenant.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_tasks"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
LIST_MY_TASKS_TOOL_ID = f"mcp__{SERVER_NAME}__list_my_tasks"
CLAIM_TASK_TOOL_ID = f"mcp__{SERVER_NAME}__claim_task"
COMPLETE_TASK_TOOL_ID = f"mcp__{SERVER_NAME}__complete_task"

TASK_TOOL_IDS = (
    LIST_MY_TASKS_TOOL_ID,
    CLAIM_TASK_TOOL_ID,
    COMPLETE_TASK_TOOL_ID,
)


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects."""

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


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + agent id from the per-stream
    ContextVars set by ``agent_router._run_agent_stream``.

    Returns ``(workspace_id, user_id)`` where ``user_id`` is treated as
    the agent's identity from the runtime's perspective (the agent's
    runtime authenticates as itself when calling into the cloud).
    """

    try:
        from ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:
        return None, None


def _build_ctx(workspace_id: str, user_id: str):
    """Construct a ``RequestContext`` for service calls from the MCP
    tool channel. The chat stream doesn't have a FastAPI request scope,
    so we synthesise one. Same approach the pocket-specialist subagent
    uses."""

    from datetime import UTC, datetime

    from ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="mcp",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _list_my_tasks_handler(args: dict) -> dict:
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — list_my_tasks can only be called "
            "from inside a cloud SSE chat stream"
        )

    from ee.cloud.tasks import service as tasks_service

    status = args.get("status") or "proposed"
    limit_raw = args.get("limit") or 50
    try:
        limit = max(1, min(int(limit_raw), 200))
    except (TypeError, ValueError):
        limit = 50

    try:
        tasks = await tasks_service.list_for_agent_runtime(
            workspace_id=workspace_id,
            agent_id=agent_id,
            status=str(status),
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_my_tasks failed", exc_info=True)
        return _error_response(f"list_my_tasks failed: {exc}")

    return _success_response({"tasks": [t.model_dump() for t in tasks], "count": len(tasks)})


async def _claim_task_handler(args: dict) -> dict:
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — claim_task can only be called "
            "from inside a cloud SSE chat stream"
        )

    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return _error_response("task_id is required (string)")

    from ee.cloud.tasks import service as tasks_service
    from ee.cloud.tasks.dto import ClaimTaskRequest

    ctx = _build_ctx(workspace_id, agent_id)
    try:
        result = await tasks_service.agent_claim_task(
            ctx, task_id, ClaimTaskRequest(agent_id=agent_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("claim_task failed", exc_info=True)
        return _error_response(f"claim_task failed: {exc}")

    return _success_response(result)


async def _complete_task_handler(args: dict) -> dict:
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — complete_task can only be called "
            "from inside a cloud SSE chat stream"
        )

    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return _error_response("task_id is required (string)")

    next_action = args.get("next_action") or "archive"
    if next_action not in {"archive", "request_approval"}:
        return _error_response("next_action must be 'archive' or 'request_approval'")
    result_summary = args.get("result_summary") or ""

    from ee.cloud._core.errors import CloudError
    from ee.cloud.tasks import service as tasks_service
    from ee.cloud.tasks.dto import CompleteTaskRequest

    ctx = _build_ctx(workspace_id, agent_id)
    try:
        response = await tasks_service.agent_complete_task(
            ctx,
            task_id,
            CompleteTaskRequest(
                next_action=next_action,  # type: ignore[arg-type]
                result_summary=str(result_summary),
            ),
        )
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("complete_task failed", exc_info=True)
        return _error_response(f"complete_task failed: {exc}")

    return _success_response({"ok": True, "task": response.model_dump()})


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_tasks_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for Tasks, or return ``None``
    if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_pocket_context_server`` so the
    backend's MCP registration loop in ``claude_sdk.py`` treats both
    identically.
    """

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_tasks MCP disabled")
        return None

    @tool(
        "list_my_tasks",
        (
            "List Mission Control Tasks routed to the currently-running "
            "agent in its active workspace. Defaults to ``status='proposed'`` "
            "(tasks waiting to be claimed). Pass ``status='in_progress'`` to "
            "see tasks already in flight. Cheap, in-process, no rippleSpec or "
            "heavy joins. Use this on every polling cycle to find new work."
        ),
        {"status": str, "limit": int},
    )
    async def list_my_tasks(args):  # type: ignore[no-untyped-def]
        return await _list_my_tasks_handler(args)

    @tool(
        "claim_task",
        (
            "Atomically claim a proposed Mission Control Task for this "
            "agent. The claim only succeeds if the task is still in "
            "``proposed`` state AND assigned to this agent. Returns "
            "``{ok: True, task}`` on success, or ``{ok: False, reason}`` "
            "when another writer beat you (``already_claimed``), the task "
            "doesn't exist (``not_found``), or you aren't the assignee "
            "(``not_assigned_to_agent``). Treat ``ok: False`` as a race, "
            "not an error — move on to the next task in your queue."
        ),
        {"task_id": str},
    )
    async def claim_task(args):  # type: ignore[no-untyped-def]
        return await _claim_task_handler(args)

    @tool(
        "complete_task",
        (
            "Finish a Mission Control Task this agent is working on. "
            "``next_action='archive'`` (default) flips the task to "
            "``done`` and removes it from the live feed. "
            "``next_action='request_approval'`` routes the task back to "
            "the creator as a Nudge in their Tray for sign-off — use this "
            "when the agent has produced work product (a draft, a "
            "proposal, a query) the human should approve before it ships. "
            "Optional ``result_summary`` is appended to the task's "
            "summary so the detail panel shows the agent's hand-off note."
        ),
        {"task_id": str, "next_action": str, "result_summary": str},
    )
    async def complete_task(args):  # type: ignore[no-untyped-def]
        return await _complete_task_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[list_my_tasks, claim_task, complete_task],
    )
    return SERVER_NAME, server


__all__ = [
    "CLAIM_TASK_TOOL_ID",
    "COMPLETE_TASK_TOOL_ID",
    "LIST_MY_TASKS_TOOL_ID",
    "SERVER_NAME",
    "TASK_TOOL_IDS",
    "build_tasks_context_server",
]
