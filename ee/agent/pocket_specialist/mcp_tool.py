"""In-process SDK MCP binding that exposes the pocket specialist to
MCP-capable agent backends (claude_agent_sdk, deep_agents, etc.).

Mirrors the structure of ``src/pocketpaw/agents/sdk_mcp_pocket.py``. The
single tool ``create`` accepts ``{brief, hints?}`` and hands off to
``runtime.run_specialist``. Workspace / user identity is read from the
per-stream ``ContextVar`` accessors in ``ee.cloud.chat.agent_service``
because the in-process MCP channel doesn't reach the FastAPI request
scope.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistHints,
    run_specialist,
)
from ee.cloud.chat.agent_service import (
    current_user_id,
    current_workspace_id,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_pocket_specialist"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
CREATE_TOOL_ID = f"mcp__{SERVER_NAME}__create"

POCKET_SPECIALIST_TOOL_IDS = (CREATE_TOOL_ID,)


async def _create_handler(args: dict[str, Any]) -> dict[str, Any]:
    """MCP handler for ``pocket_specialist__create``.

    Reads workspace/user identity from the per-stream ContextVars,
    builds the typed input model, and delegates to ``run_specialist``.
    Returns the MCP ``{content: [...], is_error?: bool}`` shape.
    """
    from pocketpaw.config import get_settings

    workspace_id = current_workspace_id()
    user_id = current_user_id()
    if not workspace_id or not user_id:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Error: pocket_specialist__create requires workspace "
                        "and user context (call from a cloud chat session)."
                    ),
                }
            ],
            "is_error": True,
        }

    raw_hints = args.get("hints")
    hints = PocketSpecialistHints(**raw_hints) if raw_hints else None
    payload = PocketSpecialistCreateInput(brief=args.get("brief", ""), hints=hints)

    try:
        out = await run_specialist(
            payload,
            workspace_id=workspace_id,
            user_id=user_id,
            settings=get_settings(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("pocket specialist run failed")
        return {
            "content": [{"type": "text", "text": f"Error: {exc}"}],
            "is_error": True,
        }

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(out.model_dump(), separators=(",", ":")),
            }
        ]
    }


def build_pocket_specialist_server() -> Any:
    """Build the in-process SDK MCP server that exposes the specialist tool."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "create",
        (
            "Create a pocket end-to-end from a natural-language brief. The "
            "specialist lists existing pockets, decides extend-vs-create, "
            "drafts and validates the rippleSpec, and persists. Returns "
            "{ok, action, pocket, warnings, duration_ms, backend_used}. "
            "Always produces a pocket — never noop."
        ),
        {
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
                    "description": (
                        "Optional caller-supplied overrides for fields the user "
                        "named explicitly. Unknown keys are rejected."
                    ),
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "color": {"type": "string"},
                        "icon": {"type": "string"},
                        "target_pocket_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["brief"],
            "additionalProperties": False,
        },
    )
    async def create_pocket_specialist(args: dict[str, Any]) -> dict[str, Any]:
        return await _create_handler(args)

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[create_pocket_specialist],
    )


__all__ = [
    "CREATE_TOOL_ID",
    "POCKET_SPECIALIST_TOOL_IDS",
    "SERVER_NAME",
    "build_pocket_specialist_server",
]
