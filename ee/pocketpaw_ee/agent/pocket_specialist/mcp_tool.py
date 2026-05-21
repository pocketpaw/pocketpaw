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

from pocketpaw_ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistEditInput,
    PocketSpecialistHints,
    run_edit_specialist,
    run_specialist,
)
from pocketpaw_ee.cloud.chat.agent_service import (
    current_user_id,
    current_workspace_id,
)

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_pocket_specialist"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
CREATE_TOOL_ID = f"mcp__{SERVER_NAME}__create"
EDIT_TOOL_ID = f"mcp__{SERVER_NAME}__edit"

POCKET_SPECIALIST_TOOL_IDS = (CREATE_TOOL_ID, EDIT_TOOL_ID)


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
    raw_spec = args.get("spec")
    payload = PocketSpecialistCreateInput(
        brief=args.get("brief", ""),
        hints=hints,
        spec=raw_spec if isinstance(raw_spec, dict) else None,
    )

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


async def _edit_handler(args: dict[str, Any]) -> dict[str, Any]:
    """MCP handler for ``pocket_specialist__edit``."""
    from pocketpaw.config import get_settings

    workspace_id = current_workspace_id()
    user_id = current_user_id()
    if not workspace_id or not user_id:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Error: pocket_specialist__edit requires workspace "
                        "and user context (call from a cloud chat session)."
                    ),
                }
            ],
            "is_error": True,
        }

    payload = PocketSpecialistEditInput(
        pocket_id=args.get("pocket_id", ""),
        intent=args.get("intent", ""),
        pocket=args.get("pocket"),
        target_node_ids=args.get("target_node_ids"),
    )

    try:
        out = await run_edit_specialist(
            payload,
            workspace_id=workspace_id,
            user_id=user_id,
            settings=get_settings(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("pocket specialist edit run failed")
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
                        "Caller-supplied metadata + structural plan. "
                        "Surface fields (name/description/color/icon/"
                        "target_pocket_id) override what the user named "
                        "explicitly. Plan fields (purpose/layout/"
                        "focal_widget/data_shape/key_interactions) shift "
                        "design thinking onto the parent agent — when set, "
                        "the specialist FOLLOWS them rather than "
                        "re-deciding. Unknown keys are rejected."
                    ),
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "color": {"type": "string"},
                        "icon": {"type": "string"},
                        "target_pocket_id": {"type": "string"},
                        "purpose": {
                            "type": "string",
                            "description": (
                                "One-sentence statement of what this pocket "
                                "should ACCOMPLISH for the user. Drives "
                                "focal-widget + layout selection."
                            ),
                        },
                        "layout": {
                            "type": "string",
                            "enum": [
                                "hero+grid",
                                "single-pane",
                                "sidebar+main",
                                "tabs",
                                "master-detail",
                                "stacked",
                                "wizard",
                            ],
                            "description": (
                                "High-level layout shape. Pick the one "
                                "that fits the user's intent best."
                            ),
                        },
                        "focal_widget": {
                            "type": "string",
                            "description": (
                                "The ONE widget that IS this pocket "
                                "(calendar, kanban, data-grid, tree-table, "
                                "funnel, heatmap, treemap, timeline, "
                                "pricing-table, comparison-layout, "
                                "entity-detail, form-layout, report-layout, "
                                "etc.)."
                            ),
                        },
                        "data_shape": {
                            "type": "object",
                            "description": (
                                "Sketch of the state schema to seed. Keys "
                                "are state field names, values describe "
                                "shape. Example: "
                                '{"tasks":"[{id,label,status,due}]",'
                                '"filter":"string"}'
                            ),
                        },
                        "key_interactions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "What the user should be able to DO with "
                                "this pocket. e.g. ['add task', 'mark "
                                "done', 'filter by status']."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
                "spec": {
                    "type": "object",
                    "description": (
                        "Agent-mode second call: a pre-drafted rippleSpec "
                        "from the chat agent. The specialist validates it "
                        "against the widget manifest and persists. Omit on "
                        "the first call (in agent mode you'll get back "
                        "``action='draft_kit'`` with instructions). In "
                        "subagent mode this argument is ignored — the "
                        "spawned specialist drafts its own spec."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["brief"],
            "additionalProperties": False,
        },
    )
    async def create_pocket_specialist(args: dict[str, Any]) -> dict[str, Any]:
        return await _create_handler(args)

    @tool(
        "edit",
        (
            "Edit an existing pocket from a natural-language intent. The "
            "specialist reads the current pocket, picks the smallest set "
            "of granular ops (set_state for data, set_node_prop for "
            "widget appearance, add/move/remove_node for structure), and "
            "applies them. Each op persists and pushes its own SSE event "
            "so the canvas updates in place. Returns "
            "{ok, pocket_id, ops, duration_ms}."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Id of the pocket to edit.",
                },
                "intent": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 4000,
                    "description": (
                        "What the user wants changed. Be specific: 'mark "
                        "task 3 as done', 'add a status badge to the "
                        "header', 'rename the chart to Revenue Q4'."
                    ),
                },
                "pocket": {
                    "type": "object",
                    "description": (
                        "OPTIONAL handoff. The current pocket view "
                        "(rippleSpec + metadata) you already fetched. "
                        "When passed, the specialist skips its own "
                        "get_pocket call. Pass this when you read the "
                        "pocket to disambiguate or confirm the edit."
                    ),
                },
                "target_node_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "OPTIONAL handoff. Node ids you identified as "
                        "edit targets after reading the pocket. When "
                        "set, the specialist works ONLY on these nodes "
                        "and does not search. Best practice for any "
                        "edit that needs disambiguation (the user said "
                        "'the chart' and there are three)."
                    ),
                },
            },
            "required": ["pocket_id", "intent"],
            "additionalProperties": False,
        },
    )
    async def edit_pocket_specialist(args: dict[str, Any]) -> dict[str, Any]:
        return await _edit_handler(args)

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[create_pocket_specialist, edit_pocket_specialist],
    )


__all__ = [
    "CREATE_TOOL_ID",
    "EDIT_TOOL_ID",
    "POCKET_SPECIALIST_TOOL_IDS",
    "SERVER_NAME",
    "build_pocket_specialist_server",
]
