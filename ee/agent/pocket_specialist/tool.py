"""``PocketSpecialistTool`` — a ``BaseTool`` exposing the pocket specialist.

Plugs into the existing ``pocketpaw.agents.tool_bridge`` adapter pattern.
Any MCP-capable backend that goes through ``build_*_function_tools``
(``deep_agents``, ``google_adk``, ``openai_agents``) picks this up
automatically; the tool name is the canonical ``pocket_specialist__create``
so the delegation prompt resolves uniformly across all backends.

The claude_agent_sdk path stays on its in-process MCP server (see
``mcp_tool.build_pocket_specialist_server``) because that backend doesn't
consume PocketPaw ``BaseTool``s — it uses native SDK tools + MCP only.

Identity (workspace_id, user_id) is read from the per-stream ContextVars
in ``ee.cloud.chat.agent_service``. Outside of a cloud chat session the
tool returns a clear error envelope rather than running.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

TOOL_NAME = "pocket_specialist__create"
TOOL_DESCRIPTION = (
    "Create a pocket end-to-end from a natural-language brief. The "
    "specialist lists existing pockets, decides extend-vs-create, drafts "
    "and validates the rippleSpec, and persists. Returns "
    "{ok, action, pocket, warnings, duration_ms, backend_used}. "
    "Always produces a pocket — never noop."
)


class PocketSpecialistHintsModel(BaseModel):
    """Pydantic mirror of ``PocketSpecialistHints`` for richer arg schemas."""

    name: str | None = None
    description: str | None = None
    color: str | None = None
    icon: str | None = None
    target_pocket_id: str | None = None


class PocketSpecialistArgs(BaseModel):
    brief: str = Field(..., min_length=10, max_length=4000)
    hints: PocketSpecialistHintsModel | None = None


_PARAMS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "brief": {
            "type": "string",
            "minLength": 10,
            "maxLength": 4000,
            "description": (
                "Natural-language description of what the user wants. "
                "Include any research/context already gathered."
            ),
        },
        "hints": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "color": {"type": "string"},
                "icon": {"type": "string"},
                "target_pocket_id": {"type": "string"},
            },
            "description": (
                "Optional caller-supplied overrides for fields the user named explicitly."
            ),
        },
    },
    "required": ["brief"],
    "additionalProperties": False,
}


class PocketSpecialistTool(BaseTool):
    """Single-tool surface for end-to-end pocket creation."""

    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return TOOL_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return _PARAMS_JSON_SCHEMA

    @property
    def args_schema(self) -> type:
        return PocketSpecialistArgs

    async def execute(self, **params: Any) -> str:
        brief = params.get("brief", "")
        hints = params.get("hints")
        normalized = _normalize_hints(hints)
        return await _run_handler(brief, normalized)


def _normalize_hints(hints: Any) -> dict[str, Any] | None:
    """Accept hints as dict, Pydantic model, JSON string, or None.

    Different adapters surface ``hints`` in different shapes:
      * OpenAI Agents:    real dict (params_json_schema preserved)
      * LangChain:        Pydantic ``PocketSpecialistHintsModel`` instance
                          (via ``args_schema``)
      * ADK / fallback:   JSON string (default str-only signature)
    Normalize all three to a plain dict (or None) before handing off.
    """
    if hints is None:
        return None
    if hasattr(hints, "model_dump"):
        return hints.model_dump(exclude_none=True)
    if isinstance(hints, dict):
        return hints
    if isinstance(hints, str):
        text = hints.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("pocket_specialist: dropped unparseable hints string: %r", hints)
            return None
    logger.warning("pocket_specialist: dropped hints of unsupported type %s", type(hints).__name__)
    return None


async def _run_handler(brief: str, hints: dict[str, Any] | None) -> str:
    """Dispatch to ``run_specialist`` and serialize the result as JSON.

    Reads workspace_id / user_id from the per-stream ContextVars. Returns
    an ``{"ok": False, "error": ...}`` envelope (JSON-encoded) when
    identity is missing or the run raises — the calling agent surfaces the
    string back to the user.
    """
    from ee.agent.pocket_specialist.runtime import (
        PocketSpecialistCreateInput,
        PocketSpecialistHints,
        run_specialist,
    )
    from ee.cloud.chat.agent_service import current_user_id, current_workspace_id
    from pocketpaw.config import get_settings

    workspace_id = current_workspace_id()
    user_id = current_user_id()
    if not workspace_id or not user_id:
        return json.dumps(
            {
                "ok": False,
                "error": (
                    "pocket_specialist__create requires workspace and user "
                    "context (call from a cloud chat session)."
                ),
            }
        )

    parsed_hints = PocketSpecialistHints(**hints) if hints else None
    try:
        payload = PocketSpecialistCreateInput(brief=brief, hints=parsed_hints)
    except Exception as exc:  # pydantic ValidationError lands here
        return json.dumps({"ok": False, "error": f"invalid input: {exc}"})

    try:
        out = await run_specialist(
            payload,
            workspace_id=workspace_id,
            user_id=user_id,
            settings=get_settings(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("pocket specialist run failed (BaseTool surface)")
        return json.dumps({"ok": False, "error": str(exc)})

    return json.dumps(out.model_dump(), separators=(",", ":"))


__all__ = [
    "PocketSpecialistArgs",
    "PocketSpecialistHintsModel",
    "PocketSpecialistTool",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
]
