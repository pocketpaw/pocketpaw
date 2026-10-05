# craft.py — in-process MCP server ``pocketpaw_craft``: craft studio edits.
#
# One server, one handler, three tools: ``edit_vector``, ``edit_photo`` and
# ``edit_design``, built in one loop over craft_ops.APPS. Each studio surface's
# allowlist exposes only its own tool, so the model sees one clearly named verb
# per editor with that app's op names in its description. (Not one
# ``edit_canvas(app, ops)``: the app is already fixed by the bound surface and
# one description would carry three vocabularies.)
#
#   READ   no read tool. The document lives in the browser (the craft engines run
#          there as WebAssembly), so the page stamps a projection onto every send
#          (``surface_meta.<app>``) and the studio preamble renders it.
#   WRITE  ``edit_<app>(ops)`` validates a BATCH against ``<app>_ops.contract.json``
#          and returns a ``craft_edit`` envelope ``{app, ops}``. run_core promotes
#          it to a ``craft_edit`` SSE frame; the page applies it as one undo step
#          and reports failures on the NEXT send (``last_edit.failures``).
#
# "ok" means validated and DISPATCHED, never applied; the tool text says so.
# Ambient (registered on every surface) but inert elsewhere: the projection
# ContextVar run_core binds is unset off a studio surface, so the validator
# refuses with "No <app> document is open".

from __future__ import annotations

import json
import logging
from typing import Any

from pocketpaw_ee.agent.mcp_servers.craft_ops import APPS, ROUTES, contract

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_craft"
ENVELOPE = "craft_edit"
TOOL_IDS: dict[str, str] = {app: f"mcp__{SERVER_NAME}__edit_{app}" for app in APPS}
CRAFT_TOOL_IDS = tuple(TOOL_IDS.values())

# Per-app wording the contract cannot carry (the vector contract is pinned
# byte-for-byte, so it gains no new keys).
_UNITS = {
    "vector": "Points, y down, origin at the artboard's top-left, 72 pt = 1 inch.",
    "photo": "Pixels, y down, origin at the image's top-left. Opacity is 0..1.",
    "design": (
        "MILLIMETRES from the page's TRIM top-left, y down; `page` is 0-based and "
        "defaults to the page in view. strokeWidth and text size are points."
    ),
}
_NOTES = {
    "vector": (
        "set_paint carries fill, stroke AND strokeWidth. There is no set_fill, "
        "set_stroke or stroke_width op."
    ),
    "photo": (
        'A passport photo is crop {preset: "passport_35x45"} (35x45 mm at 300 ppi). '
        "Layer ops act on the active layer when `layer` is omitted."
    ),
    "design": (
        "set_paint paints a frame's box (fill, stroke AND strokeWidth), never its text; "
        "text colour is set_text_color. set_text with fit: true makes the text fit its frame "
        "(the frame grows into free space, then the text shrinks; overset text is cut off). "
        "The next message lists what the editor found (overset, shrunk, overlap, off page). "
        "Paint CMYK values are 0..1."
    ),
}
_PAINT = 'Paint: {"c","m","y","k"} 0..1 (preferred for print), "#rrggbb", "none".'


def _response(text: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        out["is_error"] = True
    return out


def _current_summary(app: str):
    """The open ``app`` document's projection, from the per-stream ContextVar."""
    from pocketpaw_ee.agent.mcp_servers.craft_ops import CraftSummary

    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_craft
    except ImportError:  # pragma: no cover - cloud chat not installed
        return CraftSummary(has_document=False)
    return CraftSummary.from_meta(current_craft(app))


async def _edit_handler(app: str, args: dict) -> dict:
    """Validate an op batch and hand it to the ``app`` editor tab."""
    from pocketpaw.agents.mcp_arg_coercion import coerce_json_object_args
    from pocketpaw_ee.agent.mcp_servers.craft_ops import validate_ops

    args = coerce_json_object_args(args, ("ops",))
    ops = args.get("ops")
    if isinstance(ops, str):
        try:
            ops = json.loads(ops.strip() or "[]")
        except json.JSONDecodeError:
            return _response("Error: `ops` was a string but not valid JSON.", error=True)

    clean, error = validate_ops(app, ops, _current_summary(app))
    if error is not None:
        return _response(f"Error: {error}", error=True)
    assert clean is not None

    body = {
        "ok": True,
        "dispatched": len(clean),
        ENVELOPE: {"app": app, "ops": clean},
        "note": (
            "Validated and sent to the editor. The browser applies it as one undo "
            "step; the result appears in the document on your next turn. Do not "
            "tell the user it is 'applied' — say what you changed."
        ),
    }
    return _response(json.dumps(body, separators=(",", ":"), ensure_ascii=False, default=str))


def tool_description(app: str) -> str:
    ops = ", ".join(contract(app).ops)
    return (
        f"Change the {app} document the user has open in the {ROUTES[app]} editor.\n\n"
        "Takes a BATCH of operations applied together as ONE undo step: a whole "
        "change is one call, not twenty. Ids come from the document in your "
        "context; copy them exactly. The operation list is closed and an unknown "
        f"op or param is rejected with a hint. The op names are exactly: {ops}. "
        f"{_NOTES[app]}\n\n"
        "Returns once the batch is validated and dispatched, NOT once the browser "
        "has applied it. Report what you changed, never that it is 'done'."
    )


def tool_parameters(app: str) -> dict[str, Any]:
    # A FULL JSON schema on purpose: the SDK's shorthand ({name: {...}}) collapses
    # every param to {"type": "string"} and drops its description, so the model
    # would be told to send `ops` as a string and never see the units.
    ops = {
        "type": "array",
        "items": {"type": "object"},
        "description": (
            f"Operations, in order; each is an object with an `op` field. {_UNITS[app]} "
            f"{_PAINT} The exact params and one valid example per op are in the "
            f"studio-{app}-procedure block of your context. run_command reaches the "
            "engine's other commands; file, export, document, clipboard, preference, "
            "view and app commands are blocked."
        ),
    }
    return {"type": "object", "properties": {"ops": ops}, "required": ["ops"]}


def build_craft_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None if the SDK is unavailable."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_craft MCP disabled")
        return None

    def make(app: str):
        @tool(f"edit_{app}", tool_description(app), tool_parameters(app))
        async def edit(args):  # type: ignore[no-untyped-def]
            return await _edit_handler(app, args)

        return edit

    tools = [make(app) for app in APPS]
    return SERVER_NAME, create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)


__all__ = [
    "CRAFT_TOOL_IDS",
    "ENVELOPE",
    "SERVER_NAME",
    "TOOL_IDS",
    "build_craft_server",
    "tool_description",
    "tool_parameters",
]
