# agents/sdk_mcp_atlas.py — in-process SDK MCP server exposing the atlas
# OS self-model to backends (AT-1). Created: 2026-07-02 (feat/atlas-core).
#
# atlas is the runtime OS self-model: hand-authored capability cards for
# the OS's own primitives (Pocket, Instinct, Fabric, Connector, Ripple,
# Soul, Branch, workspace-jobs, Sites, Belt) in PAW meanings, not
# LLM-default meanings. The ``atlas_search`` / ``atlas_describe`` tools
# let an agent look up what the OS is and can do BEFORE guessing a
# capability from its priors. Pure core: the seed ships as packaged data
# (``pocketpaw.atlas``), no cloud dependency.
#
# Mirrors ``sdk_mcp_widgets.py`` — same server/tool registration shape,
# ``mcp__<server>__<tool>`` id convention, text-block result envelope
# with ``is_error`` on failures, and the same wiring points in
# ``claude_sdk.py`` (``_get_mcp_servers`` + ``_collect_mcp_tool_ids`` +
# the mode-scope grant).

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_atlas"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
ATLAS_SEARCH_TOOL_ID = f"mcp__{SERVER_NAME}__atlas_search"
ATLAS_DESCRIBE_TOOL_ID = f"mcp__{SERVER_NAME}__atlas_describe"

ATLAS_TOOL_IDS = (
    ATLAS_SEARCH_TOOL_ID,
    ATLAS_DESCRIBE_TOOL_ID,
)


def _text_result(text: str, *, is_error: bool = False) -> dict:
    """Shape an SDK MCP tool result from a single text block."""
    out: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


async def _atlas_search_handler(args: dict) -> dict:
    """Rank atlas entries for an intent and return capability cards.

    Backs the ``atlas_search`` MCP tool. Cards are intentionally thin
    (id, kind, name, summary, surface when set) — the agent follows up
    with ``atlas_describe`` on the id it picks.
    """
    from pocketpaw.atlas.store import get_atlas_store

    intent = args.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return _text_result(
            "Error: pass `intent` as a non-empty string describing what you "
            "are trying to do (e.g. 'approve agent actions').",
            is_error=True,
        )

    entries = get_atlas_store().search(intent, limit=5)
    if not entries:
        return _text_result(f"No atlas entries matched intent: {intent!r}. Try broader wording.")

    cards: list[dict[str, Any]] = []
    for entry in entries:
        card: dict[str, Any] = {
            "id": entry.id,
            "kind": entry.kind,
            "name": entry.name,
            "summary": entry.summary,
        }
        if entry.surface:
            card["surface"] = entry.surface
        cards.append(card)
    return _text_result(json.dumps({"results": cards}, ensure_ascii=False))


async def _atlas_describe_handler(args: dict) -> dict:
    """Return the full atlas entry for a stable id.

    Backs the ``atlas_describe`` MCP tool. Unknown ids return an error
    envelope listing the known ids so the agent can self-correct.
    """
    from pocketpaw.atlas.store import get_atlas_store

    entry_id = args.get("id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        return _text_result(
            "Error: pass `id` as a non-empty string (e.g. 'primitive:instinct').",
            is_error=True,
        )

    store = get_atlas_store()
    entry = store.describe(entry_id.strip())
    if entry is None:
        known = ", ".join(sorted(e.id for e in store.entries))
        return _text_result(
            f"Error: unknown atlas id {entry_id!r}. Known ids: {known}",
            is_error=True,
        )
    return _text_result(entry.model_dump_json(by_alias=True))


def build_atlas_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None if the SDK is unavailable."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_atlas MCP disabled")
        return None

    @tool(
        "atlas_search",
        (
            "Search the OS self-model (atlas) for the capability that matches "
            "an intent. Call this BEFORE guessing whether the OS can do "
            "something or which primitive to reach for — atlas terms carry "
            "paw-specific meanings (Pocket = workspace app container, "
            "Instinct = human approval gate, Fabric = typed knowledge graph, "
            "Belt = code assembly line...) that differ from their everyday "
            "meanings. Pass `intent` as what you're trying to accomplish "
            "(e.g. 'approve agent actions', 'publish a website'). Returns "
            "ranked capability cards (id, kind, name, summary, surface if "
            "set); follow up with atlas_describe on the best id. Cheap, "
            "in-process, single round-trip."
        ),
        {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": (
                        "What you are trying to do, in plain words "
                        "(e.g. 'let a human review agent changes')."
                    ),
                }
            },
            "required": ["intent"],
        },
    )
    async def atlas_search(args):  # type: ignore[no-untyped-def]
        return await _atlas_search_handler(args)

    @tool(
        "atlas_describe",
        (
            "Fetch the full atlas entry for one capability by stable id "
            "(e.g. 'primitive:instinct'). Returns the narrative (WHEN to "
            "reach for it and what it pairs with), `how` (the tool / verb / "
            "API that exercises it), `requires`, and `surface`. Use after "
            "atlas_search picks a candidate, or whenever you're about to "
            "explain or exercise an OS primitive and want ground truth "
            "instead of guessing from the name."
        ),
        {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Stable atlas entry id, e.g. 'primitive:pocket'.",
                }
            },
            "required": ["id"],
        },
    )
    async def atlas_describe(args):  # type: ignore[no-untyped-def]
        return await _atlas_describe_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[atlas_search, atlas_describe],
    )
    return SERVER_NAME, server


__all__ = [
    "ATLAS_DESCRIBE_TOOL_ID",
    "ATLAS_SEARCH_TOOL_ID",
    "ATLAS_TOOL_IDS",
    "SERVER_NAME",
    "build_atlas_context_server",
]
