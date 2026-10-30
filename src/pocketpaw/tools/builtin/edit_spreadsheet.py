# edit_spreadsheet.py — FL-5 structural spreadsheet editing (port of dewani12's #1193).
# Ported prompt-context builder embeds long example-JSON strings (tool op
# examples) that don't wrap without mangling the JSON — suppress line-length.
# ruff: noqa: E501
# Created: 2026-07-03 (FL-5, port of dewani12's origin/feature/files
#   src/pocketpaw/tools/builtin/edit_spreadsheet.py). Credit: dewani12 authored the
#   Univer workbook operation engine, the A1-notation parsers, the prompt-context
#   builder, the module-store transport, and the in-process MCP server — all ported
#   here verbatim (adapted only for imports on current dev).
#
# Two agent-facing surfaces share one workbook operation engine:
#   1. ``EditSpreadsheetTool`` (``BaseTool``, registered in ``_EE_TOOLS``,
#      trust_level "medium") — edits a Library spreadsheet file by id: loads the
#      current workbook snapshot JSON via ``file_versions.service
#      .read_current_content``, applies the ops, and writes the mutated snapshot
#      back through ``update_file_content(editor_kind="agent")`` so every edit
#      lands as a NEW revertable FL-2 version. Follows the FL-3 library-verb
#      pattern (workspace from agent-identity ContextVars; lazy EE imports).
#   2. the in-process MCP server (``build_edit_spreadsheet_mcp_server``) — edits
#      the session snapshot-store synced from the frontend spreadsheet UI (REST
#      spreadsheet-edit path); no version is written (the frontend persists via
#      PUT /files/{id}).
"""edit_spreadsheet tool — structural spreadsheet editing for Univer workbooks."""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# Per-request workbook snapshot (REST ai-edit endpoint).
_edit_session_snapshot: ContextVar[dict | None] = ContextVar("edit_session_snapshot", default=None)

# Per-request selected sheet name for scoped editing.
_selected_sheet: ContextVar[str | None] = ContextVar("selected_sheet", default=None)

# Module-level store keyed by file_id — used when snapshots are synced from
# the frontend spreadsheet UI (chat-initiated editing).
_spreadsheet_snapshot_store: dict[str, dict] = {}

SERVER_NAME = "pocketpaw_edit_spreadsheet"
EDIT_SPREADSHEET_TOOL_ID = f"mcp__{SERVER_NAME}__edit_spreadsheet"

# ---------------------------------------------------------------------------
# Cell reference parsing (A1 notation)
# ---------------------------------------------------------------------------

_A1_RE = re.compile(
    r"^(?:(?P<sheet>[A-Za-z0-9_ ]+)!)?"
    r"(?P<col>[A-Z]+)(?P<row>\d+)$"
)

_RANGE_RE = re.compile(
    r"^(?:(?P<sheet>[A-Za-z0-9_ ]+)!)?"
    r"(?P<col1>[A-Z]+)(?P<row1>\d+)"
    r":"
    r"(?P<col2>[A-Z]+)(?P<row2>\d+)$"
)


def _col_to_index(col: str) -> int:
    """Convert column letter(s) to 0-based index. A=0, B=1, ..., AA=26."""
    idx = 0
    for ch in col.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _index_to_col(idx: int) -> str:
    """Convert 0-based column index to letters. 0='A', 25='Z', 26='AA'."""
    result = ""
    while idx >= 0:
        result = chr(ord("A") + idx % 26) + result
        idx = idx // 26 - 1
    return result


def parse_cell_ref(ref: str, default_sheet: str | None = None) -> tuple[str, int, int]:
    """Parse an A1-style cell reference into (sheet_name, row_0, col_0).

    ``Sheet1!B5`` → ("Sheet1", 4, 1).  ``B5`` → (default_sheet, 4, 1).
    Raises ValueError if the reference is malformed.
    """
    m = _A1_RE.match(ref.strip())
    if not m:
        raise ValueError(
            f"Invalid cell reference '{ref}'. Use A1 notation, e.g. 'B5' or 'Sheet1!B5'."
        )
    sheet = m.group("sheet") or default_sheet or ""
    col_idx = _col_to_index(m.group("col"))
    row_idx = int(m.group("row")) - 1
    if row_idx < 0:
        raise ValueError(f"Invalid row number in '{ref}'")
    return sheet, row_idx, col_idx


def parse_range_ref(ref: str, default_sheet: str | None = None) -> tuple[str, int, int, int, int]:
    """Parse an A1-style range reference into (sheet, row1, col1, row2, col2).

    ``A1:C10`` → (default_sheet, 0, 0, 9, 2). Supports ``Sheet1!A1:C10``.
    """
    m = _RANGE_RE.match(ref.strip())
    if not m:
        raise ValueError(f"Invalid range reference '{ref}'. Use A1 notation, e.g. 'A1:C10'.")
    sheet = m.group("sheet") or default_sheet or ""
    row1 = int(m.group("row1")) - 1
    col1 = _col_to_index(m.group("col1"))
    row2 = int(m.group("row2")) - 1
    col2 = _col_to_index(m.group("col2"))
    if row1 < 0 or row2 < 0:
        raise ValueError(f"Invalid row range in '{ref}'")
    return sheet, row1, col1, row2, col2


# ---------------------------------------------------------------------------
# Session management (ContextVar + module dict — mirrors edit_document.py)
# ---------------------------------------------------------------------------


def set_edit_session(snapshot: dict) -> None:
    _edit_session_snapshot.set(snapshot)


def clear_edit_session() -> None:
    _edit_session_snapshot.set(None)


def get_edit_session() -> dict | None:
    return _edit_session_snapshot.get()


def set_selected_sheet(sheet: str | None) -> None:
    _selected_sheet.set(sheet)


def get_selected_sheet() -> str | None:
    return _selected_sheet.get()


def clear_selected_sheet() -> None:
    _selected_sheet.set(None)


def set_spreadsheet_snapshot(file_id: str, snapshot: dict) -> None:
    _spreadsheet_snapshot_store[file_id] = snapshot


def get_spreadsheet_snapshot(file_id: str) -> dict | None:
    return _spreadsheet_snapshot_store.get(file_id)


def clear_spreadsheet_snapshot(file_id: str) -> None:
    _spreadsheet_snapshot_store.pop(file_id, None)


def get_active_snapshot() -> dict | None:
    """Get whichever snapshot is available — ContextVar first, then store."""
    snap = _edit_session_snapshot.get()
    if snap is not None:
        return snap
    if _spreadsheet_snapshot_store:
        return next(iter(_spreadsheet_snapshot_store.values()))
    return None


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _sheet_names(snapshot: dict) -> list[str]:
    """Return ordered sheet names from the workbook snapshot."""
    return list(snapshot.get("sheetOrder", []) or snapshot.get("sheets", {}).keys())


def _get_sheet(snapshot: dict, name: str) -> dict | None:
    """Get a sheet dict by name from the workbook snapshot."""
    sheets = snapshot.get("sheets", {})
    return sheets.get(name)


def _ensure_sheet(snapshot: dict, name: str) -> dict:
    """Get or create a sheet dict. Initialises cellData if needed."""
    sheets = snapshot.setdefault("sheets", {})
    if name not in sheets:
        sheet = {
            "id": uuid.uuid4().hex[:8],
            "name": name,
            "rowCount": 1000,
            "columnCount": 26,
            "cellData": {},
        }
        sheets[name] = sheet
        snapshot.setdefault("sheetOrder", []).append(name)
        if "id" not in snapshot:
            snapshot["id"] = uuid.uuid4().hex[:8]
        if "name" not in snapshot:
            snapshot["name"] = "Workbook"
    return sheets[name]


def _cell_data(sheet: dict) -> dict:
    """Return the cellData dict for a sheet, initialising if needed."""
    return sheet.setdefault("cellData", {})


def _get_cell(sheet: dict, row: int, col: int) -> dict | None:
    """Get a cell from a sheet by 0-based row/col. Returns None if empty."""
    cd = _cell_data(sheet)
    row_dict = cd.get(str(row))
    if row_dict is None:
        return None
    return row_dict.get(str(col))


def _set_cell(sheet: dict, row: int, col: int, value: Any, /, *, cell_type: int | None = None):
    """Set a cell in a sheet. Auto-bumps rowCount/columnCount if needed."""
    cd = sheet.setdefault("cellData", {})
    row_key = str(row)
    if row_key not in cd:
        cd[row_key] = {}
    col_key = str(col)
    if cell_type is None:
        cell_type = _infer_cell_type(value)
    cd[row_key][col_key] = {"v": value, "t": cell_type}
    if row >= sheet.get("rowCount", 0):
        sheet["rowCount"] = row + 1
    if col >= sheet.get("columnCount", 0):
        sheet["columnCount"] = col + 1


def _infer_cell_type(value: Any) -> int:
    """Infer Univer cell type from Python value. 1=string, 2=number, 3=boolean."""
    if isinstance(value, bool):
        return 3
    if isinstance(value, (int, float)):
        return 2
    return 1


def _clear_cell(sheet: dict, row: int, col: int) -> bool:
    """Remove a cell from cellData. Returns True if a cell was actually removed."""
    cd = _cell_data(sheet)
    row_dict = cd.get(str(row))
    if row_dict is None:
        return False
    col_key = str(col)
    if col_key in row_dict:
        del row_dict[col_key]
        if not row_dict:
            del cd[str(row)]
        return True
    return False


def _shift_rows(sheet: dict, from_row: int, delta: int):
    """Shift cellData rows by delta starting from from_row. Positive = move down."""
    cd = _cell_data(sheet)
    if delta == 0:
        return
    rows_to_move = sorted(
        (int(r) for r in cd if int(r) >= from_row),
        reverse=(delta > 0),
    )
    for r in rows_to_move:
        new_r = r + delta
        if new_r < 0:
            continue
        cd[str(new_r)] = cd.pop(str(r), {})
    sheet["rowCount"] = max(1, sheet.get("rowCount", 0) + delta)


def _shift_columns(sheet: dict, from_col: int, delta: int):
    """Shift cellData columns by delta starting from from_col. Positive = move right."""
    cd = _cell_data(sheet)
    if delta == 0:
        return
    for row_key in list(cd):
        row_dict = cd.get(row_key)
        if not row_dict:
            continue
        new_row: dict[str, dict] = {}
        for col_key, cell in row_dict.items():
            c = int(col_key)
            if c >= from_col:
                new_c = c + delta
                if new_c >= 0:
                    new_row[str(new_c)] = cell
            else:
                new_row[col_key] = cell
        cd[row_key] = new_row
    sheet["columnCount"] = max(1, sheet.get("columnCount", 0) + delta)


def _collect_range(
    sheet: dict, row1: int, col1: int, row2: int, col2: int
) -> list[tuple[int, int, dict | None]]:
    """Collect all cells in a range. Returns list of (row, col, cell_dict_or_none)."""
    result = []
    for r in range(min(row1, row2), max(row1, row2) + 1):
        for c in range(min(col1, col2), max(col1, col2) + 1):
            result.append((r, c, _get_cell(sheet, r, c)))
    return result


def _summarize_sheet(snapshot: dict, sheet_name: str) -> str:
    """Build a compact summary of a sheet: dimensions, headers, sample data."""
    sheet = _get_sheet(snapshot, sheet_name)
    if not sheet:
        return f"Sheet '{sheet_name}': (empty)"

    cd = _cell_data(sheet)
    rows = sorted({int(r) for r in cd} | {0})
    cols = sorted({int(c) for row_dict in cd.values() for c in row_dict} | {0, 1, 2, 3, 4})

    num_rows = sheet.get("rowCount", 0)
    num_cols = sheet.get("columnCount", 0)
    lines = [
        f"Sheet '{sheet_name}': {num_rows} rows x {num_cols} columns, "
        f"{sum(len(row_dict) for row_dict in cd.values())} cells with data",
    ]

    if rows:
        top_rows = rows[:15]
        top_cols = sorted(cols)[:10]
        if top_cols:
            header = "    | " + " | ".join(_index_to_col(c).rjust(5) for c in top_cols) + " |"
            lines.append(header)
            lines.append("    |-" + "-|-".join("-" * 5 for _ in top_cols) + "-|")
            for r in top_rows:
                row_parts = []
                for c in top_cols:
                    cell = _get_cell(sheet, r, c)
                    if cell is None:
                        row_parts.append("".rjust(5))
                    else:
                        val = str(cell.get("v", ""))[:4]
                        row_parts.append(val.rjust(5))
                lines.append(f"  {r + 1:2d} | " + " | ".join(row_parts) + " |")

        if len(rows) > 15:
            lines.append(f"  ... ({len(rows) - 15} more rows)")

        if len(cols) > 10:
            lines.append(f"  ... ({len(cols) - 10} more columns)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt context builder
# ---------------------------------------------------------------------------


def build_spreadsheet_prompt_context(
    snapshot: dict | None = None,
    *,
    selected_sheet: str | None = None,
) -> str | None:
    """Build a system-prompt block describing the current workbook state.

    Returns None if no snapshot is available.
    """
    resolved = snapshot or get_active_snapshot()
    if not resolved:
        return None

    sheets = _sheet_names(resolved)
    active = selected_sheet or (sheets[0] if sheets else "Sheet1")

    if not sheets:
        return (
            "You are editing a spreadsheet in PocketPaw. "
            "The workbook is currently empty (no sheets). "
            "Use the edit_spreadsheet tool to create the first sheet."
        )

    parts = [
        "You are editing a spreadsheet in PocketPaw's spreadsheet editor.\n",
        f"Workbook: {resolved.get('name', 'Untitled')}",
        f"Sheets: {', '.join(sheets)}",
        f"Active sheet: {active}\n",
        "## Workbook structure\n",
    ]

    for sn in sheets:
        parts.append(_summarize_sheet(resolved, sn))
        parts.append("")

    parts.extend(
        [
            "## How to edit\n",
            "Call the `edit_spreadsheet` tool with an array of operations. "
            "You can call it multiple times — each call returns the updated state.\n",
            "Operations:",
            '- setCell: {"op":"setCell", "cell":"A1", "value":"hello"}',
            '- setRange: {"op":"setRange", "range":"A1:C3", "values":[["a","b","c"],["d","e","f"],["g","h","i"]]}',
            '- setFormula: {"op":"setFormula", "cell":"D1", "formula":"=SUM(A1:C1)"}',
            '- clearRange: {"op":"clearRange", "range":"A1:C3"}',
            '- insertRows: {"op":"insertRows", "sheet":"Sheet1", "atRow":2, "count":1}',
            '- deleteRows: {"op":"deleteRows", "sheet":"Sheet1", "atRow":2, "count":1}',
            '- insertColumns: {"op":"insertColumns", "sheet":"Sheet1", "atCol":"B", "count":1}',
            '- deleteColumns: {"op":"deleteColumns", "sheet":"Sheet1", "atCol":"B", "count":1}',
            '- insertSheet: {"op":"insertSheet", "name":"NewSheet"}',
            '- deleteSheet: {"op":"deleteSheet", "name":"Sheet2"}',
            '- renameSheet: {"op":"renameSheet", "name":"Sheet1", "newName":"Data"}',
            '- mergeCells: {"op":"mergeCells", "range":"A1:B2"}',
            '- replaceAll: {"op":"replaceAll", "snapshot":{...}}',
            "\nCell references use A1 notation. Use 'SheetName!A1' for cross-sheet references.\n",
            "After making your edits, briefly tell the user what you changed.",
        ]
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Operations engine
# ---------------------------------------------------------------------------


def apply_spreadsheet_ops(
    snapshot: dict,
    operations: list[dict],
    selected_sheet: str | None = None,
) -> tuple[dict, str]:
    """Apply spreadsheet operations to a workbook snapshot. Mutates in place.

    Returns (snapshot, summary) — snapshot is the same dict reference, mutated.
    """
    summaries: list[str] = []
    active_sheet = selected_sheet or (
        _sheet_names(snapshot)[0] if _sheet_names(snapshot) else "Sheet1"
    )

    for op in operations:
        op_type = op.get("op")
        try:
            if op_type == "setCell":
                _op_set_cell(snapshot, op, active_sheet, summaries)
            elif op_type == "setRange":
                _op_set_range(snapshot, op, active_sheet, summaries)
            elif op_type == "setFormula":
                _op_set_formula(snapshot, op, active_sheet, summaries)
            elif op_type == "clearRange":
                _op_clear_range(snapshot, op, active_sheet, summaries)
            elif op_type == "insertRows":
                _op_insert_rows(snapshot, op, summaries)
            elif op_type == "deleteRows":
                _op_delete_rows(snapshot, op, summaries)
            elif op_type == "insertColumns":
                _op_insert_columns(snapshot, op, summaries)
            elif op_type == "deleteColumns":
                _op_delete_columns(snapshot, op, summaries)
            elif op_type == "insertSheet":
                _op_insert_sheet(snapshot, op, summaries)
            elif op_type == "deleteSheet":
                _op_delete_sheet(snapshot, op, summaries)
            elif op_type == "renameSheet":
                _op_rename_sheet(snapshot, op, summaries)
            elif op_type == "mergeCells":
                _op_merge_cells(snapshot, op, active_sheet, summaries)
            elif op_type == "replaceAll":
                _op_replace_all(snapshot, op, summaries)
            else:
                summaries.append(f"Unknown operation '{op_type}' — skipped")
        except ValueError as exc:
            summaries.append(f"ERROR in '{op_type}': {exc}")

    summary = "; ".join(summaries) if summaries else "No operations applied"
    return snapshot, summary


def _resolve_sheet(snapshot: dict, op: dict, active: str) -> str:
    """Get sheet name from op['sheet'] or the active sheet."""
    return op.get("sheet") or active


# ── Individual operation handlers ──


def _op_set_cell(snapshot: dict, op: dict, active: str, summaries: list):
    cell_ref = op.get("cell", "")
    sheet, row, col = parse_cell_ref(cell_ref, active)
    s = _ensure_sheet(snapshot, sheet)
    _set_cell(s, row, col, op["value"])
    summaries.append(f"Set {cell_ref} = {_fmt_val(op['value'])}")


def _op_set_range(snapshot: dict, op: dict, active: str, summaries: list):
    range_ref = op.get("range", "")
    sheet, row1, col1, row2, col2 = parse_range_ref(range_ref, active)
    values = op.get("values", [])
    s = _ensure_sheet(snapshot, sheet)
    count = 0
    for ri, row_vals in enumerate(values):
        if not isinstance(row_vals, list):
            row_vals = [row_vals]
        for ci, val in enumerate(row_vals):
            _set_cell(s, row1 + ri, col1 + ci, val)
            count += 1
    summaries.append(f"Set {count} cells in {range_ref}")


def _op_set_formula(snapshot: dict, op: dict, active: str, summaries: list):
    cell_ref = op.get("cell", "")
    sheet, row, col = parse_cell_ref(cell_ref, active)
    s = _ensure_sheet(snapshot, sheet)
    cd = _cell_data(s)
    cd.setdefault(str(row), {})[str(col)] = {
        "v": op.get("formula", ""),
        "f": op.get("formula", ""),
        "t": 2,
    }
    summaries.append(f"Set formula in {cell_ref}: {op.get('formula', '')}")


def _op_clear_range(snapshot: dict, op: dict, active: str, summaries: list):
    range_ref = op.get("range", "")
    sheet, row1, col1, row2, col2 = parse_range_ref(range_ref, active)
    s = _get_sheet(snapshot, sheet)
    if s is None:
        summaries.append(f"Sheet '{sheet}' not found — skipped clearRange")
        return
    count = 0
    for r in range(min(row1, row2), max(row1, row2) + 1):
        for c in range(min(col1, col2), max(col1, col2) + 1):
            if _clear_cell(s, r, c):
                count += 1
    summaries.append(f"Cleared {count} cells in {range_ref}")


def _op_insert_rows(snapshot: dict, op: dict, summaries: list):
    sheet_name = op.get("sheet", "")
    s = _get_sheet(snapshot, sheet_name)
    if s is None:
        summaries.append(f"Sheet '{sheet_name}' not found — skipped insertRows")
        return
    at_row = op.get("atRow", 0)
    count = op.get("count", 1)
    _shift_rows(s, at_row, count)
    summaries.append(f"Inserted {count} row(s) at row {at_row + 1} in '{sheet_name}'")


def _op_delete_rows(snapshot: dict, op: dict, summaries: list):
    sheet_name = op.get("sheet", "")
    s = _get_sheet(snapshot, sheet_name)
    if s is None:
        summaries.append(f"Sheet '{sheet_name}' not found — skipped deleteRows")
        return
    at_row = op.get("atRow", 0)
    count = op.get("count", 1)
    _shift_rows(s, at_row + count, -count)
    summaries.append(f"Deleted {count} row(s) at row {at_row + 1} in '{sheet_name}'")


def _op_insert_columns(snapshot: dict, op: dict, summaries: list):
    sheet_name = op.get("sheet", "")
    s = _get_sheet(snapshot, sheet_name)
    if s is None:
        summaries.append(f"Sheet '{sheet_name}' not found — skipped insertColumns")
        return
    at_col_str = op.get("atCol", "A")
    at_col = _col_to_index(at_col_str)
    count = op.get("count", 1)
    _shift_columns(s, at_col, count)
    summaries.append(f"Inserted {count} column(s) at column {at_col_str} in '{sheet_name}'")


def _op_delete_columns(snapshot: dict, op: dict, summaries: list):
    sheet_name = op.get("sheet", "")
    s = _get_sheet(snapshot, sheet_name)
    if s is None:
        summaries.append(f"Sheet '{sheet_name}' not found — skipped deleteColumns")
        return
    at_col_str = op.get("atCol", "A")
    at_col = _col_to_index(at_col_str)
    count = op.get("count", 1)
    _shift_columns(s, at_col + count, -count)
    summaries.append(f"Deleted {count} column(s) at column {at_col_str} in '{sheet_name}'")


def _op_insert_sheet(snapshot: dict, op: dict, summaries: list):
    name = op.get("name", f"Sheet{len(_sheet_names(snapshot)) + 1}")
    _ensure_sheet(snapshot, name)
    summaries.append(f"Inserted sheet '{name}'")


def _op_delete_sheet(snapshot: dict, op: dict, summaries: list):
    name = op.get("name", "")
    sheets = snapshot.get("sheets", {})
    if name not in sheets:
        summaries.append(f"Sheet '{name}' not found — skipped deleteSheet")
        return
    del sheets[name]
    order = snapshot.get("sheetOrder", [])
    if name in order:
        order.remove(name)
    summaries.append(f"Deleted sheet '{name}'")


def _op_rename_sheet(snapshot: dict, op: dict, summaries: list):
    name = op.get("name", "")
    new_name = op.get("newName", "")
    sheets = snapshot.get("sheets", {})
    if name not in sheets:
        summaries.append(f"Sheet '{name}' not found — skipped renameSheet")
        return
    sheets[new_name] = sheets.pop(name)
    sheets[new_name]["name"] = new_name
    order = snapshot.get("sheetOrder", [])
    if name in order:
        order[order.index(name)] = new_name
    summaries.append(f"Renamed sheet '{name}' → '{new_name}'")


def _op_merge_cells(snapshot: dict, op: dict, active: str, summaries: list):
    range_ref = op.get("range", "")
    sheet, row1, col1, row2, col2 = parse_range_ref(range_ref, active)
    s = _ensure_sheet(snapshot, sheet)
    merges = s.setdefault("mergeData", [])
    merge = {
        "startRow": min(row1, row2),
        "endRow": max(row1, row2),
        "startColumn": min(col1, col2),
        "endColumn": max(col1, col2),
    }
    merges.append(merge)
    summaries.append(f"Merged cells {range_ref}")


def _op_replace_all(snapshot: dict, op: dict, summaries: list):
    new_snapshot = op.get("snapshot", {})
    snapshot.clear()
    snapshot.update(new_snapshot)
    summaries.append(f"Replaced entire workbook ({len(_sheet_names(snapshot))} sheets)")


def _fmt_val(val: Any) -> str:
    s = str(val)
    return s[:40] + "..." if len(s) > 40 else s


# ---------------------------------------------------------------------------
# FL-5 registered tool — workspace-scoped, wired onto the FL-2 versioned service.
# ---------------------------------------------------------------------------


def _current_workspace() -> str | None:
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id

        return current_workspace_id()
    except ImportError:
        return None


def _current_user() -> str | None:
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id

        return current_user_id()
    except ImportError:
        return None


def _request_ctx(workspace_id: str, user_id: str | None):
    """Build a minimal RequestContext for the FL-2 file_versions service."""
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id or "agent",
        workspace_id=workspace_id,
        request_id="",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _parse_snapshot(content: str) -> dict:
    """Parse a workbook snapshot JSON string into a mutable snapshot dict.

    Tolerates a blank file or malformed JSON (treated as an empty workbook so
    the agent can build from scratch). Deep-copied so mutations don't corrupt
    any shared reference.
    """
    try:
        parsed = json.loads(content) if content and content.strip() else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return json.loads(json.dumps(parsed))


class EditSpreadsheetTool(BaseTool):
    """Edit a Univer workbook in the Library, writing a revertable version.

    Loads the file's current workbook snapshot JSON, applies cell/range/row/
    column/sheet operations, and writes the mutated snapshot back through the
    FL-2 service so the change is a new archived version. Operates only on
    files in the current workspace.
    """

    @property
    def name(self) -> str:
        return "edit_spreadsheet"

    @property
    def description(self) -> str:
        return (
            "Structurally edit a spreadsheet file (Univer workbook) in the "
            "workspace Library. Provide the file's id and an array of operations "
            "to set cell values or formulas, set/clear ranges, insert/delete rows "
            "and columns, manage sheets, merge cells, or replace the workbook. "
            "This writes a new file version, so the edit is fully revertable. Cell "
            "references use A1 notation. Operates only on files in the current "
            "workspace."
        )

    @property
    def trust_level(self) -> str:
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "ID of the Library spreadsheet file to edit.",
                },
                "operations": {
                    "type": "array",
                    "description": "Ordered list of spreadsheet operations to apply.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": [
                                    "setCell",
                                    "setRange",
                                    "setFormula",
                                    "clearRange",
                                    "insertRows",
                                    "deleteRows",
                                    "insertColumns",
                                    "deleteColumns",
                                    "insertSheet",
                                    "deleteSheet",
                                    "renameSheet",
                                    "mergeCells",
                                    "replaceAll",
                                ],
                            },
                            "cell": {
                                "type": "string",
                                "description": "Cell ref in A1 notation, e.g. 'B5' or 'Sheet1!B5'",
                            },
                            "value": {"description": "Cell value (string, number, or boolean)"},
                            "range": {
                                "type": "string",
                                "description": "Range in A1 notation, e.g. 'A1:C10'",
                            },
                            "values": {
                                "type": "array",
                                "description": "2D array of values for setRange",
                            },
                            "formula": {
                                "type": "string",
                                "description": "Formula string, e.g. '=SUM(A1:A10)'",
                            },
                            "sheet": {"type": "string", "description": "Target sheet name"},
                            "name": {
                                "type": "string",
                                "description": "Sheet name for insert/delete/rename",
                            },
                            "newName": {
                                "type": "string",
                                "description": "New sheet name for renameSheet",
                            },
                            "atRow": {
                                "type": "integer",
                                "description": "0-based row index for insertRows/deleteRows",
                            },
                            "atCol": {
                                "type": "string",
                                "description": "Column letter for insertColumns/deleteColumns, e.g. 'B'",
                            },
                            "count": {
                                "type": "integer",
                                "description": "Number of rows/columns to insert or delete (default 1)",
                            },
                            "snapshot": {
                                "type": "object",
                                "description": "Complete workbook snapshot for replaceAll",
                            },
                        },
                        "required": ["op"],
                    },
                },
            },
            "required": ["file_id", "operations"],
        }

    async def execute(self, file_id: str, operations: list[dict]) -> str:
        workspace = _current_workspace()
        if not workspace:
            return self._error("Edit tools require a workspace context (cloud chat session).")

        if not isinstance(operations, list) or not operations:
            return self._error("Provide a non-empty `operations` array.")

        try:
            from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
            from pocketpaw_ee.cloud.file_versions import service as fv_service
            from pocketpaw_ee.cloud.file_versions.dto import UpdateFileContentRequest
        except ImportError:
            return self._error("Spreadsheet editing is not available (enterprise feature).")

        ctx = _request_ctx(workspace, _current_user())

        try:
            content = await fv_service.read_current_content(ctx, file_id)
        except NotFound:
            return self._error(f"File {file_id!r} not found in this workspace.")
        except CloudError as e:
            return self._error(str(e))

        snapshot = _parse_snapshot(content)
        try:
            updated, summary = apply_spreadsheet_ops(snapshot, operations)
        except Exception as exc:
            return self._error(f"Edit failed: {exc}")

        new_content = json.dumps(updated, separators=(",", ":"))
        try:
            result = await fv_service.update_file_content(
                ctx,
                file_id,
                UpdateFileContentRequest(content=new_content),
                editor_kind="agent",
            )
        except NotFound:
            return self._error(f"File {file_id!r} not found in this workspace.")
        except CloudError as e:
            return self._error(str(e))

        return self._success(
            f"{summary}. Saved {file_id} — new version {result.new_version} (revertable)."
        )


# ---------------------------------------------------------------------------
# In-process MCP server (Claude Agent SDK) — session-store editing.
# Ported verbatim from #1193. Mutates the in-memory snapshot store (frontend
# sync); does NOT write an FL-2 version (frontend persists via PUT /files/{id}).
# ---------------------------------------------------------------------------


def _spreadsheet_error(message: str) -> dict:
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


async def _edit_spreadsheet_handler(args: dict) -> dict:
    """MCP handler for the edit_spreadsheet tool."""
    snapshot = get_active_snapshot()
    if snapshot is None:
        return _spreadsheet_error(
            "No spreadsheet is currently open for editing. "
            "Open a spreadsheet file in the editor first."
        )

    operations = args.get("operations")
    if not operations:
        if args.get("operation"):
            return _spreadsheet_error(
                "Use `operations` (plural) as the key, not `operation`. "
                'Example: {"operations": [{"op": "setCell", ...}]}'
            )
        if any(k in args for k in ("setCell", "setRange", "cell", "range", "value")):
            return _spreadsheet_error(
                "Wrap operations inside an `operations` array. "
                'Example: {"operations": [{"op": "setCell", "cell": "A1", "value": "hello"}]}'
            )
        return _spreadsheet_error(
            "Missing `operations` array. "
            'Format: {"operations": [{"op": "setCell|setRange|...", ...}]}'
        )

    if not isinstance(operations, list):
        return _spreadsheet_error("`operations` must be an array (list), not a single object.")

    try:
        selected = get_selected_sheet()
        updated, summary = apply_spreadsheet_ops(snapshot, operations, selected_sheet=selected)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "summary": summary,
                            "sheetCount": len(_sheet_names(updated)),
                            "snapshot": updated,
                        },
                        separators=(",", ":"),
                    ),
                }
            ]
        }
    except Exception as exc:
        logger.exception("edit_spreadsheet handler failed")
        return _spreadsheet_error(str(exc))


def build_edit_spreadsheet_mcp_server() -> tuple[str, Any] | None:
    """Build the in-process MCP server for the Claude Agent SDK.

    Returns (server_name, server_config) or None if the SDK is unavailable.
    Always registered — the handler returns an error when no editing session
    is active, so the agent won't use it outside the spreadsheet editing context.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; edit_spreadsheet MCP disabled")
        return None

    @tool(
        "edit_spreadsheet",
        (
            "Edit the current spreadsheet by performing operations on cells, "
            "ranges, rows, columns, and sheets. "
            "Use this when the user asks you to edit, populate, format, or "
            "restructure a spreadsheet they have open in the editor.\n\n"
            "You MUST call it with an `operations` array. Each item must have "
            "an `op` field set to one of: setCell, setRange, setFormula, "
            "clearRange, insertRows, deleteRows, insertColumns, deleteColumns, "
            "insertSheet, deleteSheet, renameSheet, mergeCells, replaceAll.\n\n"
            "EXACT format (copy this pattern):\n"
            '{"operations": [\n'
            '  {"op": "setCell", "cell": "A1", "value": "Revenue"},\n'
            '  {"op": "setRange", "range": "B1:D1", "values": [["Q1","Q2","Q3"]]},\n'
            '  {"op": "setFormula", "cell": "B6", "formula": "=SUM(B2:B5)"},\n'
            '  {"op": "clearRange", "range": "E2:F10"},\n'
            '  {"op": "insertRows", "sheet": "Sheet1", "atRow": 2, "count": 1},\n'
            '  {"op": "deleteColumns", "sheet": "Sheet1", "atCol": "C", "count": 1},\n'
            '  {"op": "insertSheet", "name": "Summary"},\n'
            '  {"op": "renameSheet", "name": "Sheet1", "newName": "Data"},\n'
            '  {"op": "mergeCells", "range": "A1:B1"},\n'
            '  {"op": "replaceAll", "snapshot": {...}}\n'
            "]}\n\n"
            "RULES:\n"
            "- Cell references use A1 notation: 'B5', 'A1', 'Sheet1!D10'\n"
            "- Ranges use A1 notation: 'A1:C10', 'B2:D2'\n"
            "- Row indices are 0-based (row 0 = row 1 in the UI)\n"
            "- Column reference 'atCol' uses letters: 'A', 'B', 'C', ...\n"
            "- Values can be strings, numbers, or booleans\n"
            "- For cross-sheet references in formulas, use 'SheetName!A1'\n"
            "- The `sheet` field in operations is optional; defaults to the "
            "active sheet if omitted\n"
            "- Sparse is fine — you don't need to fill every cell. Only set "
            "cells that have meaningful data.\n\n"
            "IMPORTANT: The field is `op` (not operation or action). "
            "Everything goes inside the `operations` array — not at top level."
        ),
        {
            "operations": list,
        },
    )
    async def edit_spreadsheet(args):
        return await _edit_spreadsheet_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[edit_spreadsheet],
    )
    return SERVER_NAME, server
