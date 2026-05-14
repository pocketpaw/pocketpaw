"""In-process SDK MCP binding that exposes kb-go recipe retrieval to
MCP-capable agent backends.

Mirrors ``sdk_mcp_pocket.py`` / ``sdk_mcp_tasks.py``. Single tool
``find_recipe`` runs ``kb search <query> --scope <scope> --context``
against PocketPaw's bundled kb-go scopes (default
``ripple-recipes``) so the chat agent can EXPLICITLY query the
recipe library at pocket-creation time instead of relying on the
silent system-prompt injection in ``bootstrap.context_builder``
(which is invisible from the agent's perspective — it can't tell
whether retrieval happened).

The tool exists because the captain's first pocket-creation test
revealed the agent claimed "no kb-go fetch happened" — even though
the auto-injection in context_builder may have fired, the agent
had no observable handle on the retrieval and didn't know to look
for recipe content in its system prompt. An explicit MCP tool fixes
that: the agent makes the call, sees the result in its tool-use
stream, and can anchor its draft on the returned recipe.

Created: 2026-05-14 (feat/ripple-recipes-poc) — explicit retrieval
companion for the bundled kb-go scopes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_kb"
FIND_RECIPE_TOOL_ID = f"mcp__{SERVER_NAME}__find_recipe"

POCKET_KB_TOOL_IDS = (FIND_RECIPE_TOOL_ID,)


async def _run_kb_search(
    *, query: str, scope: str, limit: int, binary: str
) -> dict[str, Any]:
    """Subprocess wrapper around ``kb search --json``.

    Returns the parsed JSON payload or an error dict. Never raises —
    the MCP tool surface should surface errors to the agent as
    structured text, not as exceptions that crash the chat.
    """

    binary_path = shutil.which(binary)
    if binary_path is None:
        return {
            "error": (
                f"kb binary {binary!r} not found on PATH. Install kb-go "
                f"(https://github.com/qbtrix/kb-go) or set "
                f"``POCKETPAW_KB_BINARY`` to its absolute path."
            ),
        }

    args = [
        binary_path,
        "search",
        query,
        "--scope",
        scope,
        "--limit",
        str(limit),
        "--json",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        return {"error": f"kb search timed out (10s) for scope {scope!r}"}
    except OSError as exc:
        return {"error": f"kb search subprocess failed: {exc}"}

    if proc.returncode != 0:
        return {
            "error": f"kb search exited {proc.returncode}",
            "stderr": stderr.decode("utf-8", errors="replace")[:500],
        }

    try:
        return json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return {
            "error": f"kb search output not JSON: {exc}",
            "raw": stdout.decode("utf-8", errors="replace")[:500],
        }


async def _find_recipe_handler(args: dict[str, Any]) -> dict[str, Any]:
    """MCP handler for ``find_recipe``.

    Resolves the kb binary path + scope, runs ``kb search``, and
    formats the result as MCP content. The agent gets back either a
    list of matching recipes (with titles, summaries, and the first
    chunk of body content) or a clear "no matches" / error message.
    """

    from pocketpaw.config import get_settings

    settings = get_settings()
    query = args.get("query", "").strip()
    scope = args.get("scope", "ripple-recipes").strip()
    limit = args.get("limit", 1)

    if not query:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Error: find_recipe requires a non-empty ``query`` "
                        "describing the user's intent (e.g., "
                        "\"sales pipeline dashboard\", \"customer support "
                        "ticket triage app\", \"how-to recipe viewer\")."
                    ),
                }
            ],
            "is_error": True,
        }

    binary = settings.kb_binary or "kb"
    result = await _run_kb_search(
        query=query, scope=scope, limit=int(limit), binary=binary
    )

    if "error" in result:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"kb search failed for scope {scope!r}: "
                        f"{result['error']}. Fall back to drafting from "
                        "first principles using the pocket-creator skill's "
                        "guidance."
                    ),
                }
            ],
            "is_error": True,
        }

    results = result.get("results", []) if isinstance(result, dict) else []
    if not results:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"No recipes matched in scope {scope!r}. Draft "
                        "from first principles using the pocket-creator "
                        "skill's pattern-first guidance + widget catalog."
                    ),
                }
            ],
        }

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"scope": scope, "query": query, "results": results},
                    separators=(",", ":"),
                ),
            }
        ]
    }


def build_kb_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server that exposes the
    ``find_recipe`` tool. Returns ``None`` (silently) when the
    claude_agent_sdk package is not installed — keeps the chat agent
    boot path working in environments without the SDK.
    """

    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        return None

    @tool(
        "find_recipe",
        (
            "Query PocketPaw's bundled pattern-recipe library for a "
            "polished rippleSpec example matching the user's intent. "
            "Use BEFORE calling ``pocket_specialist__create`` for a "
            "richer first-draft anchor. Returns one or more matching "
            "recipes with their composition, anti-patterns, and "
            "domain variations. Falls back to first-principles drafting "
            "if no recipe matches."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The user's intent in natural language — e.g. "
                        "\"sales pipeline dashboard\", \"customer support "
                        "ticket triage app\", \"how-to recipe viewer\", "
                        "\"order tracking with map\". The kb-go BM25 "
                        "search matches keywords + concepts."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Which kb-go scope to search. Default "
                        "``ripple-recipes`` — the bundled pattern "
                        "recipe library auto-installed by PocketPaw. "
                        "Operators may add other scopes."
                    ),
                    "default": "ripple-recipes",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "How many recipes to return. Default 1 (single "
                        "best match). Increase to 2-3 only when you "
                        "want to compare candidates before drafting."
                    ),
                    "default": 1,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def find_recipe(args: dict[str, Any]) -> dict[str, Any]:
        return await _find_recipe_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="0.1.0",
        tools=[find_recipe],
    )
    return SERVER_NAME, server


__all__ = [
    "FIND_RECIPE_TOOL_ID",
    "POCKET_KB_TOOL_IDS",
    "SERVER_NAME",
    "build_kb_server",
]
