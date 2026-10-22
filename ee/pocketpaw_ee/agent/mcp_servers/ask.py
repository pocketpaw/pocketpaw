# ask.py — in-process MCP server exposing an ``ask_user`` tool to the
# claude_agent_sdk cloud chat backend.
#
# WHY this exists: the cloud chat agent runs on the claude_agent_sdk backend,
# whose built-in tool set is only Bash/Read/Write/Edit/Glob/Grep/WebSearch/
# WebFetch (+Skill); the harness ``AskUserQuestion`` tool is NOT exposed to it.
# So on surfaces where inline Ripple is OFF (notably /sites svelte-create,
# ``ripple_mode="off"``) the agent has no other way to render an interactive
# question. This tool restores the "ask the user a choice" UI there WITHOUT
# re-enabling inline-Ripple ui-spec authoring.
#
# HOW it renders: the tool itself is a near no-op that returns an ack. The real
# effect is downstream: ``ee/pocketpaw_ee/cloud/chat/runs/run_core.py`` detects
# a ``tool_use`` for ``ASK_USER_TOOL_ID`` and emits an ``ask_user_question``
# stream frame ({question, options}) instead of a generic ``tool_start``. Both
# sides validate through ``ask_user_payload``, so run_core sends a frame only
# for a call this handler accepts, with the same trimmed, capped options. The
# client renders the frame as option chips and sends the clicked label as the
# next user message. Rendering depends on the client handling the frame on its
# active chat path: the legacy chatStore path handles it; the ChatSession path
# is wired by a separate paw-enterprise change.
#
# Clones the icons.py / palette.py shape: a single ``create_sdk_mcp_server`` with
# an SDK import-guard, ``SERVER_NAME`` / ``*_TOOL_ID`` allowlist constants, and
# the ``_error_response`` / ``_success_response`` helpers. PURE LOCAL: no
# network, no identity, persists nothing. Surfaced to core via the
# ``CloudAskMcpProvider`` ``pocketpaw.mcp_servers`` entry-point (extensions.py)
# and added to ``sites_allow`` in surface_registry.py so it is callable on
# /sites. Tool id namespaces as ``mcp__pocketpaw_ask__ask_user``.
"""Agent-side MCP surface for the interactive ``ask_user`` question tool."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_ask"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
ASK_USER_TOOL_ID = f"mcp__{SERVER_NAME}__ask_user"

ASK_TOOL_IDS = (ASK_USER_TOOL_ID,)

# Bounds on the option list — enough for a real choice, few enough to render as
# a tidy chip row. A single question per call (the client renders the latest
# ask_user_question on the turn's assistant message; multiple calls in one turn
# would overwrite each other).
_MIN_OPTIONS = 2
_MAX_OPTIONS = 6


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: Any) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON text."""
    return {"content": [{"type": "text", "text": json.dumps(body)}]}


def _normalize_options(raw: Any) -> list[str] | None:
    """Coerce the ``options`` arg into a clean list of non-empty label strings,
    or return ``None`` if it isn't a usable list. Accepts plain strings or
    ``{label|text|value}`` dicts (the client maps both), flattening to labels."""
    if not isinstance(raw, list):
        return None
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            label = item.strip()
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("text") or item.get("value") or "").strip()
        else:
            label = ""
        if label:
            out.append(label)
    return out


def ask_user_payload(args: Any) -> dict[str, Any] | None:
    """Return the ``{question, options}`` a call renders, or ``None`` if the
    call is invalid. The single validation rule for ask_user: the handler acks
    exactly this, and run_core emits it as the ``ask_user_question`` frame.
    Valid means a non-empty question and at least ``_MIN_OPTIONS`` non-empty
    labels; the question is trimmed and options are capped at ``_MAX_OPTIONS``.
    """
    if not isinstance(args, dict):
        return None
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    options = _normalize_options(args.get("options"))
    if options is None or len(options) < _MIN_OPTIONS:
        return None
    return {"question": question.strip(), "options": options[:_MAX_OPTIONS]}


async def _ask_user_handler(args: dict) -> dict:
    """MCP handler for ``ask__ask_user``.

    Validates a single multiple-choice question + its options. Returns an ack
    that tells the agent to END its turn and wait — the interactive UI is
    rendered by run_core from the tool CALL, not by anything this handler does.
    Fail-soft: a bad question/options returns an ``_error_response`` and never
    raises into the agent.
    """
    payload = ask_user_payload(args)
    if payload is None:
        question = args.get("question") if isinstance(args, dict) else None
        if not isinstance(question, str) or not question.strip():
            return _error_response("ask_user requires a non-empty `question` string.")
        return _error_response(
            f"ask_user requires an `options` array of at least {_MIN_OPTIONS} short "
            "answer labels the user can click."
        )

    # The ack is the agent-facing contract: the question is now on screen as
    # clickable chips; the agent must stop and let the user answer.
    return _success_response(
        {
            "ok": True,
            "shown": True,
            **payload,
            "note": (
                "The question is shown to the user as clickable options. END your turn "
                "now and wait for their reply — do NOT continue, ask another question, "
                "or assume an answer. The user's choice arrives as the next message."
            ),
        }
    )


def build_ask_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for ``ask_user``, or return ``None``
    if the Claude Agent SDK isn't installed. Matches the ``(name, server)`` /
    ``None`` shape of ``build_palette_server`` so the backend's MCP registration
    loop treats it identically."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_ask MCP disabled")
        return None

    @tool(
        "ask_user",
        (
            "Ask the user ONE multiple-choice question and render it as clickable "
            "option chips in the chat, instead of writing the question as plain "
            "text. Use this at a genuine decision point when a small set of "
            "concrete choices captures the answer — e.g. the site's vibe / design "
            "direction, tone, or which of a few layouts to use. Args: `question` "
            "(required — the question text, one sentence) and `options` (required "
            "— an array of 2-6 SHORT, self-contained answer labels the user can "
            "click, e.g. ['Clean & modern','Warm & friendly','Bold & confident']; "
            "each label is sent verbatim as the user's reply when clicked). Ask "
            "ONE question per call and per turn. After calling it, STOP and end "
            "your turn — the user's click comes back as the next message; do not "
            "continue or assume an answer. For open-ended answers (a business "
            "name, an address) just ask in plain text instead — this tool is for "
            "CHOICES, not free text."
        ),
        {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The question to ask, as a single clear sentence.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": _MIN_OPTIONS,
                    "maxItems": _MAX_OPTIONS,
                    "description": (
                        "2-6 short answer labels the user picks from; each is sent "
                        "verbatim as the reply when clicked."
                    ),
                },
            },
            "required": ["question", "options"],
            "additionalProperties": False,
        },
    )
    async def ask_user_tool(args):  # type: ignore[no-untyped-def]
        return await _ask_user_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[ask_user_tool],
    )
    return SERVER_NAME, server
