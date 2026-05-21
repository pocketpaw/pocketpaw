# Created: 2026-05-17 — pocketpaw#1118 P1. In-process MCP server exposing
#   the cloud planner as a single ``plan_project`` tool the running agent
#   can invoke from inside an SSE chat stream. Mirrors the pattern in
#   ``sdk_mcp_tasks.py`` and ``sdk_mcp_pocket.py``: the agent identity
#   comes from the per-stream ContextVars in
#   ``ee.cloud.chat.agent_service``; outside an SSE stream the tool
#   returns a clear MCP error rather than silently mis-tenanting.
"""Agent-side MCP surface for the cloud Planner entity.

Tools registered:

  - ``plan_project(project_id, goal, deep_research=False)`` — invokes
    ``ee.cloud.planner.service.agent_plan_project`` so the agent can
    drive the full deep_work planner against a workspace Project and
    receive the materialized cloud primitives back as JSON.

The agent identity is resolved through the same chokepoint the pocket
and tasks MCP servers use — see ``sdk_mcp_tasks._identity`` for the
contract. ``mcp__pocketpaw_planner__plan_project`` is the canonical
allowlist id.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_planner"
PLAN_PROJECT_TOOL_ID = f"mcp__{SERVER_NAME}__plan_project"

PLANNER_TOOL_IDS = (PLAN_PROJECT_TOOL_ID,)


def _error_response(message: str) -> dict[str, Any]:
    """MCP error envelope. Matches the shape Claude's SDK expects."""

    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """MCP success envelope. Body is JSON-encoded into a text block."""

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve workspace + agent (user) id from per-stream ContextVars.

    Mirrors ``sdk_mcp_tasks._identity`` so when the planner tool is
    invoked from outside an SSE chat stream, both values come back
    ``None`` and the handler returns a clear error instead of
    silently mis-tenanting.
    """

    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:
        return None, None


def _build_ctx(workspace_id: str, user_id: str):
    """Build a ``RequestContext`` for service calls from the MCP tool
    channel. Same approach the tasks MCP server uses."""

    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="mcp",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _plan_project_handler(args: dict) -> dict:
    workspace_id, agent_id = _identity()
    if not workspace_id or not agent_id:
        return _error_response(
            "no active workspace/agent — plan_project can only be called "
            "from inside a cloud SSE chat stream"
        )

    project_id = (args or {}).get("project_id") or ""
    goal = (args or {}).get("goal") or ""
    deep_research = bool((args or {}).get("deep_research", False))

    if not project_id:
        return _error_response("project_id is required")
    if not goal:
        return _error_response("goal is required")

    try:
        from pocketpaw_ee.cloud._core.errors import CloudError
        from pocketpaw_ee.cloud.planner import service as planner_service
        from pocketpaw_ee.cloud.planner.dto import PlanProjectRequest
    except ImportError as exc:  # pragma: no cover — defensive
        return _error_response(f"planner module not installed: {exc}")

    ctx = _build_ctx(workspace_id, agent_id)
    try:
        response = await planner_service.agent_plan_project(
            ctx,
            PlanProjectRequest(
                project_id=project_id,
                goal=goal,
                deep_research=deep_research,
            ),
        )
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan_project failed", exc_info=True)
        return _error_response(f"plan_project failed: {exc}")

    return _success_response({"ok": True, "plan": response.model_dump()})


def build_planner_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the planner, or ``None``
    when the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_tasks_context_server`` so the
    backend's MCP registration loop in ``claude_sdk.py`` treats both
    identically.
    """

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_planner MCP disabled")
        return None

    @tool(
        "plan_project",
        (
            "Plan a cloud Project end-to-end: research the domain, draft a "
            "PRD, decompose the work into Mission Control Tasks, and "
            "recommend a team. Wraps PocketPaw's deep_work planner. "
            "Returns ``{ok: True, plan}`` where ``plan`` carries the "
            "materialized ``prd_file_id``, ``task_ids``, and any "
            "``agent_gaps`` (planner-recommended agents missing from the "
            "workspace). The operator should re-route human tasks from the "
            "Mission Control tray and decide whether each agent_gap is "
            "worth creating a new cloud Agent for. Long-running — make a "
            "single call per project and let the FE show progress."
        ),
        {"project_id": str, "goal": str, "deep_research": bool},
    )
    async def plan_project(args):  # type: ignore[no-untyped-def]
        return await _plan_project_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[plan_project],
    )
    return SERVER_NAME, server


__all__ = [
    "PLANNER_TOOL_IDS",
    "PLAN_PROJECT_TOOL_ID",
    "SERVER_NAME",
    "build_planner_context_server",
]
