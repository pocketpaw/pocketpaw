# game.py — in-process MCP server exposing the DETERMINISTIC game-world create
# action to agent backends (claude_agent_sdk). Created: 2026-07-02
# (feat/game-surface, PW-2). Mirrors the layout of the sibling mcp_servers
# (sites.py / sites_create.py): a single ``create_sdk_mcp_server`` with an SDK
# import-guard, the ``SERVER_NAME`` / ``*_TOOL_ID`` allowlist constants,
# ContextVar-sourced identity (the same ``current_workspace_id`` /
# ``current_user_id`` accessors in ``ee.cloud.chat.agent_service`` the sites +
# pocket-specialist servers read), and the same validation posture as the
# nearest agent-write surface (``create_landing_site``): identity check →
# record_tool_call → input validation (fail closed with an actionable message)
# → plan gate (``game.service.require_game_plan``, mirroring the Sites gate) →
# persist through the shared service. Tool ids namespace as
# ``mcp__pocketpaw_game__<tool>`` so the Claude Code allowlist machinery
# matches them (the /game SurfaceProfile's ``allow_mcp_tool_ids`` names them).
"""Agent-side MCP surface for DETERMINISTIC game-world creation.

A living world is composed from an LLM-authored ``world_spec`` (a small cast
of Souls, zones, optional feel dials). This tool:

  1. validates the spec in code (``game.service.validate_world_spec``) and
     fills missing dials from the v0 vibe→dials preset table — the LLM never
     decides the persistence shape;
  2. persists the pocket DIRECTLY via ``game.service.create_game_world`` →
     ``pockets.service.agent_create`` stamped ``type="game"`` +
     ``pattern="living-world"`` — bypassing the pocket_specialist adapter, its
     draft/redraft loop, and any subagent delegation;
  3. binds the active chat session + pushes the ``pocket_created`` SSE event
     (the same post-create side effects the sites create tools run) so the
     canvas auto-opens.

Returns ``{ok, pocket_id, pocket}``. ``is_error`` is set when identity is
missing, the inputs are malformed, the plan lacks the Game feature, or the
persist fails — the chat agent surfaces the reason instead of fabricating a
created world.

Workspace / user identity comes from the per-stream ``ContextVar``s in
``ee.cloud.chat.agent_service``. When run outside an SSE chat stream the tool
returns a clear error rather than silently mis-tenanting the pocket.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ._audit import record_tool_call

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_game"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form. The /game SurfaceProfile's
# allow-list (surface_registry._game_profile) names this id — do NOT drift it.
CREATE_GAME_WORLD_TOOL_ID = f"mcp__{SERVER_NAME}__create_game_world"

GAME_TOOL_IDS = (CREATE_GAME_WORLD_TOOL_ID,)


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


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user id from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


async def _require_game_plan_or_error(workspace_id: str) -> dict | None:
    """Gate the create on the workspace's plan. Returns an MCP ``_error_response``
    when the plan lacks the Game ("game") feature (so the agent surfaces the
    upgrade message instead of a phantom-created world), or ``None`` when the
    plan is allowed.

    Delegates to the shared service gate (``game.service.require_game_plan``) —
    the same posture as ``sites_create._require_sites_plan_or_error``: an
    in-process create tool reaches persistence directly, so it must run the
    same plan check a future HTTP surface would."""
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.game.service import require_game_plan

    try:
        await require_game_plan(workspace_id)
    except CloudError as exc:
        # Forbidden('plan.feature_denied') / NotFound('workspace'). Relay the
        # code + message so the agent tells the user to upgrade / switch
        # workspace, not "world created".
        return _error_response(f"{exc.code}: {exc.message}")
    return None


async def _bind_session_and_emit(pocket_id: str, view: dict[str, Any], user_id: str) -> None:
    """Bind the active chat session to the new pocket and push the
    ``pocket_created`` SSE event so the canvas auto-opens — the same atomic
    post-create side effects the sites create tools run. Best-effort: a
    bind / SSE failure must never undo a successful create (the pocket already
    exists in Mongo, which is the primary contract)."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_session_mongo_id,
            push_sse_event,
        )
        from pocketpaw_ee.cloud.sessions import service as sessions_service

        session_mongo_id = current_session_mongo_id()
        if session_mongo_id:
            await sessions_service.attach_pocket_to_session_doc(
                session_mongo_id, user_id, pocket_id
            )
        push_sse_event(
            "pocket_created",
            {"pocket_id": pocket_id, "pocket": view, "session_id": session_mongo_id},
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "create_game_world: post-create side effects failed (non-fatal)",
            exc_info=True,
        )


async def _create_game_world_handler(args: dict) -> dict:
    """MCP handler for ``pocketpaw_game__create_game_world``.

    Reads workspace/user identity from the per-stream ContextVars, validates
    the ``vibe`` / ``world_spec`` inputs, runs the plan gate, and delegates to
    ``game.service.create_game_world`` (validate → fill dials from the vibe
    preset → persist via ``agent_create`` stamped type="game" +
    pattern="living-world"). Returns ``{ok, pocket_id, pocket}`` on success;
    sets ``is_error`` when identity is missing, the inputs are malformed, the
    plan lacks the feature, or the persist fails.
    """
    workspace_id, user_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "create_game_world requires workspace and user context (call from a "
            "cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_game",
        tool_name="_create_game_world",
        status="ok",
        ok=True,
    )

    vibe = args.get("vibe")
    if not isinstance(vibe, str) or not vibe.strip():
        return _error_response(
            "create_game_world requires a `vibe` — the one-line feel of the "
            "world (e.g. 'a cozy cliffside tea town where the fog gossips'). "
            "It picks the dial preset when world_spec omits dials."
        )

    world_spec = args.get("world_spec")
    if not isinstance(world_spec, dict) or not world_spec:
        return _error_response(
            "create_game_world requires a `world_spec` object — the living "
            "world: `cast` (3-6 Souls: {name, archetype, persona, ocean}), "
            "`zones` (the places), and optionally `dials` (the seven feel "
            "dials; omit them and the vibe preset fills them). See the game "
            "skill for the shape."
        )

    # Fail closed on a malformed world BEFORE the plan gate, with the same
    # actionable phrasing the dynamic-sites create uses, so the agent fixes the
    # spec instead of persisting a world the runtime can't wake up.
    from pocketpaw_ee.game.service import validate_world_spec

    problems = validate_world_spec(world_spec)
    if problems:
        return _error_response(
            "create_game_world `world_spec` is not a valid living world — it "
            "needs " + "; ".join(problems) + ". See the game skill for the shape."
        )

    # Plan gate (Game = "game"): reject a plan without the feature here so the
    # in-process create can't bypass plan enforcement — mirrors the Sites gate.
    if (gate := await _require_game_plan_or_error(workspace_id)) is not None:
        return gate

    name_raw = args.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else "Game world"

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.game.service import create_game_world

    try:
        view = await create_game_world(
            workspace_id=workspace_id,
            user_id=user_id,
            name=name,
            vibe=vibe.strip(),
            world_spec=world_spec,
        )
    except CloudError as exc:
        # ValidationError / Internal from the game service — relay the code +
        # message so the agent surfaces the reason, not a phantom world.
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_game_world: persist raised", exc_info=True)
        return _error_response(f"create failed: {exc}")

    pocket_id = str(view.get("_id") or view.get("id") or "")
    if not pocket_id:
        return _error_response("create failed: created pocket returned no id")

    await _bind_session_and_emit(pocket_id, view, user_id)

    return _success_response(
        {
            "ok": True,
            "pocket_id": pocket_id,
            "pocket": {
                "id": pocket_id,
                "name": view.get("name"),
                "type": view.get("type"),
                "pattern": view.get("pattern"),
            },
        }
    )


def build_game_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for game worlds, or return ``None``
    if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_sites_manager_server`` /
    ``build_media_server`` (``(name, server)`` or ``None``) so the backend's
    MCP registration loop in ``claude_sdk.py`` treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_game MCP disabled")
        return None

    @tool(
        "create_game_world",
        (
            "Create a LIVING GAME WORLD deterministically as a Pocket stamped "
            "type='game' + pattern='living-world'. You provide the WORLD SPEC "
            "only — a small cast of Souls, the zones, and optionally the seven "
            "feel dials; the tool validates it, fills missing dials from the "
            "vibe preset table (cozy / tense / mystery / sandbox → dial "
            "settings; unknown vibe → balanced default), and persists it. You "
            "do NOT compose a rippleSpec, do NOT hand-build widgets, and do "
            "NOT call pocket_specialist. Use this when the user describes a "
            "world they want to exist ('a cozy cliffside tea town', 'a "
            "rain-slick noir city where everyone lies'). Args: `vibe` "
            "(required — the one-line feel of the world), `world_spec` "
            "(required — {cast: [{name, archetype, persona, ocean:{}}...] "
            "(keep it 3-6 Souls — the foreground-cast rule), zones: [str...], "
            "dials?: {challenge, progress, choice, bonds, mark, pulse, spark "
            "— each 0-1; JUICE is platform-provided, not a dial}, vibe?: "
            "str}), and optional `name` (the world's name; defaults to 'Game "
            "world'). NPCs are Souls — they carry persistent memory, "
            "relationships, and grudges. Returns {ok, pocket_id, pocket}. "
            "ok=false with an error means relay the reason, do NOT report a "
            "created world."
        ),
        {
            "type": "object",
            "properties": {
                "vibe": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The one-line feel of the world — picks the dial preset "
                        "(cozy / tense / mystery / sandbox; unknown → balanced) "
                        "when world_spec omits dials."
                    ),
                },
                "world_spec": {
                    "type": "object",
                    "description": (
                        "The living world you authored: `cast` (3-6 Souls: "
                        "{name, archetype, persona, ocean}), `zones` (the "
                        "places), optional `dials` (the seven feel dials, each "
                        "0-1) and optional `vibe`. See the tool description / "
                        "game skill for the shape."
                    ),
                    "additionalProperties": True,
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Optional pocket/world name. Defaults to 'Game world' when omitted."
                    ),
                },
            },
            "required": ["vibe", "world_spec"],
            "additionalProperties": False,
        },
    )
    async def create_game_world(args):  # type: ignore[no-untyped-def]
        return await _create_game_world_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[create_game_world],
    )
    return SERVER_NAME, server


__all__ = [
    "CREATE_GAME_WORLD_TOOL_ID",
    "GAME_TOOL_IDS",
    "SERVER_NAME",
    "build_game_server",
]
