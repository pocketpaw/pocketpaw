# ee/pocketpaw_ee/cloud/foresight/agent_context.py
# Created: 2026-05-28 — agent-facing Foresight wrappers for the in-process
# MCP tools that back the ``/foresight`` chat agent. Each function looks
# up workspace + user identity from the per-stream ``ContextVar``s in
# ``ee.cloud.chat.agent_service``, builds a ``RequestContext`` the cloud
# service layer expects, and translates the response into the
# ``{ok, ...}`` envelope the MCP layer in ``ee/pocketpaw_ee/agent/
# mcp_servers/foresight.py`` consumes. CloudError subclasses (NotFound,
# ValidationError, Forbidden) collapse to ``{ok: False, error, message}``;
# any unexpected exception bubbles for the SDK to surface as a tool
# failure.
#
# This sits parallel to ``ee.cloud.pockets.agent_context`` and exists for
# the same reason: the in-process MCP tool channel doesn't reach the
# FastAPI request scope, so the agent can't go through the loopback REST
# path the SKILL teaches today (``$WORKSPACE_ID`` / ``$USER_ID`` env vars
# the ``claude_agent_sdk`` backend never sets). Typed MCP tools close
# over the chat session's workspace id and remove the entire class of
# "agent saved to the wrong workspace" bugs.
#
# Scope rules:
#   - The run wrapper only supports the saved-scenario path
#     (``custom_scenario_id`` required). Inline-personas runs are still
#     reachable via the REST surface; we don't expose two run shapes on
#     the chat surface because mixing them is the bug we're fixing.
#   - The list-runs wrapper carries ``offset`` for parity with the brief
#     signature, then slices client-side because
#     ``list_scenario_runs`` itself doesn't yet take an offset arg.
#     Cheap on small workspaces; document the limitation.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

NO_WORKSPACE_ERROR = "no_workspace_context"
NO_WORKSPACE_MESSAGE = (
    "no active workspace/user — foresight tools can only be called from "
    "inside a cloud SSE chat stream"
)


def _build_request_context(workspace_id: str, user_id: str) -> Any:
    """Synthesise a ``RequestContext`` for service calls reaching us
    from the MCP tool channel. Mirrors the surface handler's shape
    (``surface/handlers/foresight.py`` lines 212-218)."""
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id=f"mcp-{uuid4().hex[:8]}",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _resolve_identity() -> tuple[str | None, str | None]:
    """Read the per-stream workspace + user from the chat ContextVars.
    Returns ``(None, None)`` if either is unset — the caller surfaces a
    clean error instead of fabricating a workspace."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        logger.debug("foresight agent_context: identity resolution failed", exc_info=True)
        return None, None


def _no_workspace_error() -> dict[str, Any]:
    """Common error payload when the chat ContextVars aren't set. The
    agent reads ``error`` and ``message`` and surfaces them to the user.
    """
    return {
        "ok": False,
        "error": NO_WORKSPACE_ERROR,
        "message": NO_WORKSPACE_MESSAGE,
    }


def _cloud_error_payload(exc: Any) -> dict[str, Any]:
    """Collapse a ``CloudError`` into the agent-facing error envelope."""
    return {
        "ok": False,
        "error": getattr(exc, "code", "cloud_error"),
        "message": getattr(exc, "message", str(exc)),
    }


# ---------------------------------------------------------------------------
# Custom scenario CRUD
# ---------------------------------------------------------------------------


async def list_scenarios_for_agent(
    limit: int = 20, offset: int = 0, sub_type: str | None = None
) -> dict[str, Any]:
    """List the workspace's saved custom scenarios for the active stream.

    Shape on success: ``{"ok": True, "items": [...], "total": N,
    "limit": int, "offset": int, "has_more": bool}``.
    """
    workspace_id, user_id = _resolve_identity()
    if not workspace_id or not user_id:
        return _no_workspace_error()

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios

    ctx = _build_request_context(workspace_id, user_id)
    try:
        response = await foresight_scenarios.list_custom_scenarios(
            ctx, sub_type=sub_type, limit=limit, offset=offset
        )
    except CloudError as exc:
        return _cloud_error_payload(exc)

    payload = response.model_dump()
    payload["ok"] = True
    return payload


async def get_scenario_for_agent(scenario_id: str) -> dict[str, Any]:
    """Fetch one saved scenario by id (full yaml_body + parsed_meta).

    Shape on success: ``{"ok": True, ...scenario fields}``.
    """
    workspace_id, user_id = _resolve_identity()
    if not workspace_id or not user_id:
        return _no_workspace_error()

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios

    ctx = _build_request_context(workspace_id, user_id)
    try:
        response = await foresight_scenarios.get_custom_scenario(ctx, scenario_id)
    except CloudError as exc:
        return _cloud_error_payload(exc)

    payload = response.model_dump()
    payload["ok"] = True
    return payload


async def save_scenario_for_agent(
    name: str,
    sub_type: str,
    yaml_body: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Persist a new custom scenario in the active workspace.

    Shape on success: ``{"ok": True, "id": ..., "name": ..., ...}``.
    """
    workspace_id, user_id = _resolve_identity()
    if not workspace_id or not user_id:
        return _no_workspace_error()

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios
    from pocketpaw_ee.cloud.foresight.dto import CreateCustomScenarioRequest

    ctx = _build_request_context(workspace_id, user_id)
    try:
        body = CreateCustomScenarioRequest(
            name=name,
            sub_type=sub_type,  # type: ignore[arg-type]
            description=description or "",
            yaml_body=yaml_body,
        )
    except Exception as exc:  # noqa: BLE001 — pydantic validation surfaces here
        return {
            "ok": False,
            "error": "foresight.invalid_request",
            "message": str(exc),
        }

    try:
        response = await foresight_scenarios.create_custom_scenario(ctx, body)
    except CloudError as exc:
        return _cloud_error_payload(exc)

    payload = response.model_dump()
    payload["ok"] = True
    return payload


async def update_scenario_for_agent(
    scenario_id: str,
    name: str,
    sub_type: str,
    yaml_body: str,
    description: str | None = None,
) -> dict[str, Any]:
    """Full-replace a saved scenario (PUT semantics — every field
    overwrites). The agent should GET first, mutate only what the user
    named, then call this.

    Shape on success: ``{"ok": True, "id": ..., ...}``.
    """
    workspace_id, user_id = _resolve_identity()
    if not workspace_id or not user_id:
        return _no_workspace_error()

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios
    from pocketpaw_ee.cloud.foresight.dto import CreateCustomScenarioRequest

    ctx = _build_request_context(workspace_id, user_id)
    try:
        body = CreateCustomScenarioRequest(
            name=name,
            sub_type=sub_type,  # type: ignore[arg-type]
            description=description or "",
            yaml_body=yaml_body,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "foresight.invalid_request",
            "message": str(exc),
        }

    try:
        response = await foresight_scenarios.update_custom_scenario(ctx, scenario_id, body)
    except CloudError as exc:
        return _cloud_error_payload(exc)

    payload = response.model_dump()
    payload["ok"] = True
    return payload


async def delete_scenario_for_agent(scenario_id: str) -> dict[str, Any]:
    """Remove a saved scenario. Idempotency: a second delete on the same
    id returns ``{ok: False, error: 'foresight_custom_scenario.not_found'}``.

    Shape on success: ``{"ok": True, "scenario_id": "..."}``.
    """
    workspace_id, user_id = _resolve_identity()
    if not workspace_id or not user_id:
        return _no_workspace_error()

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios

    ctx = _build_request_context(workspace_id, user_id)
    try:
        await foresight_scenarios.delete_custom_scenario(ctx, scenario_id)
    except CloudError as exc:
        return _cloud_error_payload(exc)

    return {"ok": True, "scenario_id": scenario_id}


# ---------------------------------------------------------------------------
# Scenario runs — only the saved-scenario path is exposed on chat
# ---------------------------------------------------------------------------


async def run_scenario_for_agent(
    name: str,
    custom_scenario_id: str,
    route_to_instinct: bool = False,
    precedent_seed: str | None = None,
) -> dict[str, Any]:
    """Execute a saved scenario. ``custom_scenario_id`` is REQUIRED — the
    chat surface only supports the saved-scenario path because the bug
    we're fixing is "agent saved nothing, then claimed it ran something
    that didn't exist". Inline-personas runs stay reachable via the REST
    surface for power users.

    Shape on success: ``{"ok": True, "id": run_id, "status": ...,
    "result": ..., "scenario_name": ...}``.
    """
    workspace_id, user_id = _resolve_identity()
    if not workspace_id or not user_id:
        return _no_workspace_error()

    if not custom_scenario_id:
        return {
            "ok": False,
            "error": "foresight.missing_scenario_id",
            "message": (
                "run_scenario requires a custom_scenario_id — save the "
                "scenario via save_scenario first, then run it by id"
            ),
        }

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.foresight import service as foresight_service
    from pocketpaw_ee.cloud.foresight.dto import CreateScenarioRequest

    ctx = _build_request_context(workspace_id, user_id)
    try:
        body = CreateScenarioRequest(
            name=name,
            custom_scenario_id=custom_scenario_id,
            route_to_instinct=route_to_instinct,
            precedent_seed=precedent_seed,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": "foresight.invalid_request",
            "message": str(exc),
        }

    try:
        response = await foresight_service.create_scenario_run(ctx, body)
    except CloudError as exc:
        return _cloud_error_payload(exc)

    payload = response.model_dump()
    payload["ok"] = True
    return payload


async def list_runs_for_agent(limit: int = 10, offset: int = 0) -> dict[str, Any]:
    """List recent scenario runs in the active workspace, newest first.

    Note: ``ee.cloud.foresight.service.list_scenario_runs`` doesn't yet
    accept an ``offset`` kwarg, so we fetch ``limit + offset`` and slice
    client-side. Adequate for the chat surface (the agent rarely scrolls
    back; sessions average single-digit runs) — a server-side offset is
    a separate follow-up.

    Shape on success: ``{"ok": True, "items": [...], "limit": int,
    "offset": int}``.
    """
    workspace_id, user_id = _resolve_identity()
    if not workspace_id or not user_id:
        return _no_workspace_error()

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.foresight import service as foresight_service

    ctx = _build_request_context(workspace_id, user_id)
    try:
        runs = await foresight_service.list_scenario_runs(ctx, limit=limit + offset)
    except CloudError as exc:
        return _cloud_error_payload(exc)

    items = [run.model_dump() for run in runs[offset : offset + limit]]
    return {"ok": True, "items": items, "limit": limit, "offset": offset}


async def get_run_for_agent(run_id: str) -> dict[str, Any]:
    """Fetch a single scenario run by id (full result blob).

    Shape on success: ``{"ok": True, "id": ..., "status": ...,
    "result": ..., ...}``.
    """
    workspace_id, user_id = _resolve_identity()
    if not workspace_id or not user_id:
        return _no_workspace_error()

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.foresight import service as foresight_service

    ctx = _build_request_context(workspace_id, user_id)
    try:
        response = await foresight_service.get_scenario_run(ctx, run_id)
    except CloudError as exc:
        return _cloud_error_payload(exc)

    payload = response.model_dump()
    payload["ok"] = True
    return payload


__all__ = [
    "NO_WORKSPACE_ERROR",
    "NO_WORKSPACE_MESSAGE",
    "delete_scenario_for_agent",
    "get_run_for_agent",
    "get_scenario_for_agent",
    "list_runs_for_agent",
    "list_scenarios_for_agent",
    "run_scenario_for_agent",
    "save_scenario_for_agent",
    "update_scenario_for_agent",
]
