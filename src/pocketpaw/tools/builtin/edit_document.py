"""edit_document tool — AI-powered block editing for Editor.js documents.

The agent calls ``edit_document`` with an array of operations. Blocks are
stored in a per-request ``ContextVar`` (for the REST ai-edit endpoint) or
a module-level dict keyed by file_id (for chat-initiated editing).
"""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

_edit_session_blocks: ContextVar[list[dict] | None] = ContextVar(
    "edit_session_blocks", default=None
)

# Module-level store keyed by file_id — used when blocks are synced from the
# frontend editor (chat-initiated editing). The MCP handler falls back to this
# when the ContextVar is empty.
_editor_blocks_store: dict[str, list[dict]] = {}

SERVER_NAME = "pocketpaw_edit_document"
EDIT_DOCUMENT_TOOL_ID = f"mcp__{SERVER_NAME}__edit_document"


def set_edit_session(blocks: list[dict]) -> None:
    """Store the current editing session's blocks for MCP handler access."""
    _edit_session_blocks.set(blocks)


def clear_edit_session() -> None:
    """Clear the editing session after the request completes."""
    _edit_session_blocks.set(None)


def get_edit_session() -> list[dict] | None:
    """Get the current editing session's blocks."""
    return _edit_session_blocks.get()


def set_editor_blocks(file_id: str, blocks: list[dict]) -> None:
    """Store blocks from the frontend editor for MCP tool access during chat."""
    _editor_blocks_store[file_id] = blocks


def get_editor_blocks(file_id: str) -> list[dict] | None:
    """Get blocks synced from the frontend editor by file_id."""
    return _editor_blocks_store.get(file_id)


def sync_editor_blocks_from_contextvar() -> None:
    """Copy ContextVar blocks to the module store so GET /files/{id}/editing-context
    returns blocks mutated by the edit_document MCP tool during a chat session."""
    blocks = _edit_session_blocks.get()
    if not blocks or not _editor_blocks_store:
        return
    for fid in _editor_blocks_store:
        _editor_blocks_store[fid] = blocks


def get_active_blocks() -> list[dict] | None:
    """Get whichever blocks are available — ContextVar first, then store.
    Returns None only when no session is active at all. An empty list is
    a valid state (empty document)."""
    blocks = _edit_session_blocks.get()
    if blocks is not None:
        return blocks
    if _editor_blocks_store:
        return next(iter(_editor_blocks_store.values()))
    return None


def _format_block_for_prompt(b: dict, idx: int) -> str:
    """Format a single Editor.js block as a readable line for the system prompt."""
    import json as _json

    bid = b.get("id", "?")
    btype = b.get("type", "?")
    data = b.get("data", {})
    snippet = _json.dumps(data, separators=(",", ":"))
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return f"[{idx}] id={bid} type={btype} data={snippet}"


def build_editor_prompt_context(
    blocks: list[dict] | None = None,
    *,
    available_tools: list[str] | None = None,
    selected_block_id: str | None = None,
) -> str | None:
    """Build a system-prompt block describing the current editor document.

    Accepts blocks directly (from the chat request body or ai-edit endpoint).
    Falls back to ``get_active_blocks()`` if no blocks passed.

    Returns None if no blocks are available anywhere.
    """
    resolved = blocks or get_active_blocks()
    if not resolved:
        return None

    block_lines = [_format_block_for_prompt(b, i) for i, b in enumerate(resolved)]
    block_summary = "\n".join(block_lines) if block_lines else "(empty document)"

    selection_hint = ""
    if selected_block_id:
        selection_hint = (
            f"\nThe user's cursor is on block with id={selected_block_id}. "
            f"Focus your edit there unless the prompt says otherwise.\n"
        )

    block_type_ref = (
        "## Block type reference\n\n"
        "You MUST use these EXACT type names. Any other name will cause a rendering error.\n\n"
        "| Type       | Data shape |\n"
        "|------------|------------|\n"
        "| `paragraph` | `{\"text\": \"...\"}` |\n"
        "| `header`    | `{\"text\": \"...\", \"level\": 1\\|2\\|3}` |\n"
        '| `list`      | `{"style": "unordered"\\|"ordered", "meta": {}, "items": [{"content": "item1", "meta": {}, "items": []}]}` |\n'
        "| `code`      | `{\"code\": \"...\"}` |\n"
        '| `quote`     | `{"text": "...", "caption": "..."}` |\n'
        '| `image`     | `{"file": {"url": "..."}, "caption": "..."}` |\n'
        '| `checklist` | `{"items": [{"text": "...", "checked": true\\|false}]}` |\n'
        '| `table`     | `{"content": [["cell", "cell"], ["cell", "cell"]]}` |\n'
        "| `delimiter` | `{}` (no data needed) |\n"
        '| `warning`   | `{"title": "...", "message": "..."}` |\n'
        '| `embed`     | `{"embed": "url", ...}` |\n'
        '| `linkTool`  | `{"link": "url", ...}` |\n'
        '| `raw`       | `{"html": "..."}` |\n'
        "\n"
        "Common mistakes — these are NOT valid types and WILL break rendering:\n"
        "  heading       → use `header` instead\n"
        "  bullet_list_item → use `list` with style=unordered and items array\n"
        "  numbered_list_item → use `list` with style=ordered and items array\n"
        "  divider       → use `delimiter` instead\n"
        "  blockquote     → use `quote` instead\n"
        "  callout        → use `warning` instead\n"
        "  check_list_item → use `checklist` with items array\n"
        "  toggle         → not supported, use `paragraph` instead\n"
        "\n"
        "Lists are a SINGLE block with an `items` array — NOT one block per item.\n"
        "Checklists are a SINGLE block with an `items` array of {text, checked} objects.\n"
        "\n"
    )

    return (
        "You are editing a document in PocketPaw's file editor.\n"
        "The document uses Editor.js block format. Each block has an id, type, and data.\n\n"
        f"{block_type_ref}\n"
        "## Current document\n\n"
        f"{block_summary}\n"
        f"{selection_hint}\n"
        "## How to edit\n\n"
        "Call the `edit_document` tool with an array of operations. "
        "You can call it multiple times — each call returns the updated state.\n\n"
        "Operations:\n"
        '- update: {"op":"update", "id":"<block_id>", "data":{...}}\n'
        '- insert: {"op":"insert", "type":"paragraph", "data":{...}, "index":0}\n'
        '- delete: {"op":"delete", "id":"<block_id>"}\n'
        '- move: {"op":"move", "id":"<block_id>", "toIndex":0}\n'
        '- replaceAll: {"op":"replaceAll", "blocks":[...]}\n\n'
        "After making your edits, briefly tell the user what you changed."
    )


def _normalize_block(block_type: str, data: dict) -> tuple[str, dict]:
    """Normalize block type names and data shapes for Editor.js compatibility.

    Fixes common LLM mistakes: Notion-style type names, wrong data shapes,
    and list/checklist items sent as individual blocks instead of arrays.
    """
    bt = block_type.strip().lower()

    # ── Type name normalization (only the cases that actually break) ──
    if bt in ("heading", "headline", "h1", "h2", "h3"):
        bt = "header"
    elif bt in ("bullet_list_item", "bulleted_list", "unordered_list"):
        bt = "list"
        data = _coerce_list_data(data, style="unordered")
    elif bt in ("numbered_list_item", "numbered_list", "ordered_list"):
        bt = "list"
        data = _coerce_list_data(data, style="ordered")
    elif bt in ("divider", "horizontal_rule", "hr", "separator"):
        bt = "delimiter"
        data = {}
    elif bt in ("blockquote", "block_quote"):
        bt = "quote"
    elif bt in ("callout", "note", "info", "alert"):
        bt = "warning"
        data = _coerce_warning_data(data)
    elif bt in ("check_list_item", "check_list", "todo", "todo_list", "task_list"):
        bt = "checklist"
        data = _coerce_checklist_data(data)
    elif bt in ("toggle", "collapsible", "accordion"):
        bt = "paragraph"

    # ── Data shape fixes for native names that still have wrong shapes ──
    if bt == "list":
        data = _coerce_list_data(data, style=None)
    elif bt == "checklist":
        data = _coerce_checklist_data(data)
    elif bt == "delimiter":
        data = {}
    elif bt == "header" and "level" in data:
        lvl = data["level"]
        if not isinstance(lvl, int) or lvl < 1 or lvl > 3:
            data = {**data, "level": 2}

    return bt, data


def _coerce_list_data(data: dict, *, style: str | None = None) -> dict:
    """Normalize data for a ``list`` block into Editor.js list v2 format.

    v2 format: {style, meta: {}, items: [{content, meta: {}, items: []}, ...]}
    Also accepts v1 flat strings and Notion-style {text} items.
    """
    items = data.get("items", [])
    # AI sent {text: "..."} instead of {items: [...]}
    if not items and data.get("text"):
        items = [str(data["text"])]
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        items = []

    normalized_items = []
    for item in items:
        if isinstance(item, str):
            # v1 flat string → v2
            normalized_items.append({"content": item, "meta": {}, "items": []})
        elif isinstance(item, dict):
            # Already v2 format (has "content") or Notion format (has "text")
            content = str(item.get("content") or item.get("text") or "")
            # Preserve nested items if present, otherwise empty list
            nested = item.get("items", [])
            if isinstance(nested, list):
                nested = [
                    {"content": str(s) if isinstance(s, str) else s.get("content", s.get("text", "")), "meta": s.get("meta", {}) if isinstance(s, dict) else {}, "items": s.get("items", []) if isinstance(s, dict) else []}
                    if isinstance(s, (str, dict)) else {"content": str(s), "meta": {}, "items": []}
                    for s in nested
                ]
            else:
                nested = []
            normalized_items.append({
                "content": content,
                "meta": item.get("meta", {}),
                "items": nested,
            })

    resolved_style = style or data.get("style", "unordered")
    if resolved_style not in ("unordered", "ordered"):
        resolved_style = "unordered"

    return {"style": resolved_style, "meta": {}, "items": normalized_items}


def _coerce_checklist_data(data: dict) -> dict:
    """Normalize data for a ``checklist`` block into {items: [{text, checked}, ...]}."""
    items = data.get("items", [])
    # AI sent {text: "...", checked: true} as a single item
    if not items and data.get("text"):
        items = [{"text": str(data["text"]), "checked": bool(data.get("checked", False))}]
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    normalized = []
    for item in items:
        if isinstance(item, str):
            normalized.append({"text": item, "checked": False})
        elif isinstance(item, dict):
            normalized.append({
                "text": str(item.get("text", "")),
                "checked": bool(item.get("checked", False)),
            })
    return {"items": normalized}


def _coerce_warning_data(data: dict) -> dict:
    """Normalize data for a ``warning`` block into {title, message}."""
    result: dict[str, str] = {}
    result["title"] = str(data.get("title") or data.get("text") or "")
    result["message"] = str(data.get("message") or data.get("text") or "")
    if not result["title"] and not result["message"]:
        result["title"] = "Note"
    return result


def apply_operations(
    blocks: list[dict],
    operations: list[dict],
) -> tuple[list[dict], str]:
    """Apply edit operations to a blocks list. Mutates in place.

    Returns (blocks, summary) — blocks is the same list reference, mutated.
    """
    summaries: list[str] = []

    for op in operations:
        op_type = op.get("op")

        if op_type == "update":
            block_id = op["id"]
            new_data = op.get("data", {})
            for b in blocks:
                if b.get("id") == block_id:
                    b["data"] = {**b.get("data", {}), **new_data}
                    summaries.append(f"Updated block {block_id} ({b.get('type', '?')})")
                    break
            else:
                summaries.append(f"Block {block_id} not found — skipped update")

        elif op_type == "insert":
            raw_type = op.get("type", "paragraph")
            raw_data = op.get("data", {})
            norm_type, norm_data = _normalize_block(raw_type, raw_data)
            new_block: dict[str, Any] = {
                "id": uuid.uuid4().hex[:6],
                "type": norm_type,
                "data": norm_data,
            }
            idx = op.get("index", len(blocks))
            idx = max(0, min(idx, len(blocks)))
            blocks.insert(idx, new_block)
            normalized = " (normalized)" if raw_type != norm_type else ""
            summaries.append(f"Inserted {norm_type} at index {idx}{normalized}")

        elif op_type == "delete":
            block_id = op["id"]
            for i, b in enumerate(blocks):
                if b.get("id") == block_id:
                    blocks.pop(i)
                    summaries.append(
                        f"Deleted block {block_id} ({b.get('type', '?')}) at index {i}"
                    )
                    break
            else:
                summaries.append(f"Block {block_id} not found — skipped delete")

        elif op_type == "move":
            block_id = op["id"]
            to_idx = op["toIndex"]
            for i, b in enumerate(blocks):
                if b.get("id") == block_id:
                    moved = blocks.pop(i)
                    to_idx = max(0, min(to_idx, len(blocks)))
                    blocks.insert(to_idx, moved)
                    summaries.append(
                        f"Moved block {block_id} ({b.get('type', '?')}) "
                        f"from {i} to {to_idx}"
                    )
                    break
            else:
                summaries.append(f"Block {block_id} not found — skipped move")

        elif op_type == "replaceAll":
            new_blocks = op.get("blocks", [])
            old_count = len(blocks)
            blocks.clear()
            for b in new_blocks:
                if "id" not in b:
                    b["id"] = uuid.uuid4().hex[:6]
                raw_type = b.get("type", "paragraph")
                raw_data = b.get("data", {})
                norm_type, norm_data = _normalize_block(raw_type, raw_data)
                b["type"] = norm_type
                b["data"] = norm_data
            blocks.extend(new_blocks)
            summaries.append(f"Replaced all {old_count} blocks with {len(new_blocks)} blocks")

        else:
            summaries.append(f"Unknown operation '{op_type}' — skipped")

    summary = "; ".join(summaries) if summaries else "No operations applied"
    return blocks, summary


class EditDocumentTool(BaseTool):
    """Tool the agent can call to edit Editor.js document blocks.

    Holds a reference to the current request's blocks. Operations are
    applied in order, mutating the blocks list in place.
    """

    def __init__(self, blocks: list[dict]) -> None:
        self._blocks = blocks

    @property
    def name(self) -> str:
        return "edit_document"

    @property
    def description(self) -> str:
        return (
            "Edit the document by performing operations on blocks. "
            "You can update, insert, delete, move, or replace all blocks. "
            "Each operation targets a block by its id (for update/delete/move) "
            "or specifies a position index (for insert). "
            "Call this tool multiple times to build up your edit — each call "
            "returns the full updated block list so you can verify your changes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "Ordered list of edit operations to apply.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["update", "insert", "delete", "move", "replaceAll"],
                                "description": "The operation to perform.",
                            },
                            "id": {
                                "type": "string",
                                "description": "Block ID to update, delete, or move.",
                            },
                            "type": {
                                "type": "string",
                                "description": (
                            "Block type name for insert "
                            "(paragraph, header, list, etc.)."
                        ),
                            },
                            "data": {
                                "type": "object",
                                "description": "Block data for update or insert.",
                            },
                            "index": {
                                "type": "integer",
                                "description": "Insert position (defaults to end).",
                            },
                            "toIndex": {
                                "type": "integer",
                                "description": "Destination index for move.",
                            },
                            "blocks": {
                                "type": "array",
                                "description": "Complete new blocks array for replaceAll.",
                            },
                        },
                        "required": ["op"],
                    },
                }
            },
            "required": ["operations"],
        }

    async def execute(self, operations: list[dict]) -> str:
        """Apply operations and return the updated blocks as JSON."""
        if not self._blocks:
            return self._error("No document blocks loaded. Start an editing session first.")

        try:
            updated, summary = apply_operations(self._blocks, operations)
            result = json.dumps(updated, separators=(",", ":"))
            return f"{summary}\n\nUpdated blocks:\n{result}"
        except Exception as exc:
            return self._error(f"Edit failed: {exc}")


def _edit_error(message: str) -> dict:
    """Return a structured error with a hint to guide the agent to the correct format."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


async def _edit_document_handler(args: dict) -> dict:
    """MCP handler for the edit_document tool. Reads blocks from ContextVar
    first, falls back to the module-level editor store (chat-initiated editing)."""
    blocks = get_active_blocks()
    if blocks is None:
        return _edit_error(
            "No document is currently open for editing. "
            "Open a file in the editor first."
        )

    operations = args.get("operations")
    if not operations:
        # Detect common mistakes and give targeted help.
        if args.get("operation"):  # wrong field name
            return _edit_error(
                "Use `operations` (plural) as the key, not `operation`. "
                'Example: {"operations": [{"op": "update", ...}]}'
            )
        if any(k in args for k in ("update", "insert", "delete", "move", "replaceAll")):
            return _edit_error(
                "Wrap operations inside an `operations` array. "
                'Example: {"operations": [{"op": "update", "id": "x", "data": {...}}]}'
            )
        if args.get("blocks"):  # replaceAll blocks at top level
            return _edit_error(
                'Wrap in operations array: {"operations": [{"op": "replaceAll", "blocks": [...]}]}'
            )
        return _edit_error(
            "Missing `operations` array. "
            'Format: {"operations": [{"op": "update|insert|delete|move|replaceAll", ...}]}'
        )

    if not isinstance(operations, list):
        return _edit_error("`operations` must be an array (list), not a single object.")

    try:
        updated, summary = apply_operations(blocks, operations)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"summary": summary, "blockCount": len(updated), "blocks": updated},
                        separators=(",", ":"),
                    ),
                }
            ]
        }
    except Exception as exc:
        logger.exception("edit_document handler failed")
        return _edit_error(str(exc))


def build_edit_document_mcp_server() -> tuple[str, Any] | None:
    """Build the in-process MCP server for the Claude Agent SDK.

    Returns (server_name, server_config) or None if the SDK is unavailable.
    Always registered — the handler returns an error when no editing session
    is active, so the agent won't use it outside the file editing context.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; edit_document MCP disabled")
        return None

    @tool(
        "edit_document",
        (
            "Edit the current document by performing operations on blocks. "
            "Use this when the user asks you to edit, rewrite, fix, expand, "
            "or restructure a document they have open in the editor.\n\n"
            "You MUST call it with an `operations` array. Each item must have "
            'an `op` field set to one of: update, insert, delete, move, replaceAll.\n\n'
            "EXACT format (copy this pattern):\n"
            '{"operations": [\n'
            '  {"op": "update", "id": "<block_id>", "data": {"text": "new text"}},\n'
            '  {"op": "insert", "type": "paragraph", "data": {"text": "new"}, "index": 1},\n'
            '  {"op": "delete", "id": "<block_id>"},\n'
            '  {"op": "move", "id": "<block_id>", "toIndex": 0},\n'
            '  {"op": "replaceAll", "blocks": [{"type":"paragraph","data":{"text":"..."}}]}\n'
            "]}\n\n"
            "VALID BLOCK TYPES — use these exact names, anything else will break:\n"
            "paragraph, header, list, code, quote, image, checklist, table,\n"
            "delimiter, warning, embed, linkTool, raw\n\n"
            "Data shape for each type:\n"
            '- paragraph: {"text": "..."}\n'
            '- header: {"text": "...", "level": 1|2|3}\n'
            '- list: {"style": "unordered"|"ordered", "meta": {}, "items": [{"content": "item1", "meta": {}, "items": []}]}\n'
            '- code: {"code": "..."}\n'
            '- quote: {"text": "...", "caption": "..."}\n'
            '- checklist: {"items": [{"text": "...", "checked": true|false}]}\n'
            '- table: {"content": [["cell", "cell"], ["cell", "cell"]]}\n'
            '- delimiter: {} (empty object)\n'
            '- warning: {"title": "...", "message": "..."}\n'
            '- embed: {"embed": "url", ...}\n'
            '- linkTool: {"link": "url", ...}\n'
            '- raw: {"html": "..."}\n\n'
            "NEVER use these (they are NOT valid): heading, bullet_list_item,\n"
            "numbered_list_item, divider, blockquote, callout, check_list_item, toggle.\n\n"
            "Lists are ONE block with an items array — NOT one block per item.\n"
            "Checklists are ONE block with an items array — NOT one block per item.\n\n"
            "IMPORTANT: The field is `op` (not operation or action). "
            "Everything goes inside the `operations` array — not at top level."
        ),
        {
            "operations": list,
        },
    )
    async def edit_document(args):  
        return await _edit_document_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[edit_document],
    )
    return SERVER_NAME, server
