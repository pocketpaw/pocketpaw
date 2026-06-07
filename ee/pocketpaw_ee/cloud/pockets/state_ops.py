"""Pure walk + mutate helpers for ``rippleSpec.state``.

Companion to ``spec_ops.py`` (which mutates ``rippleSpec.ui``). State is
where the **data** the user sees lives — the values widgets bind to via
``{state.path}``. A todo list's tasks, a filter's current value, a
cursor position: everything that should be reactive belongs in state.

The 3-layer rule for the agent:
- Editing DATA the user sees → ``set_state`` / ``append_state`` / ...
- Editing widget APPEARANCE → ``set_node_prop``
- Editing widget STRUCTURE → ``add_node`` / ``move_node`` / ``remove_node``

These helpers are side-effect-free: they take and return plain ``dict``
structures, never touch Beanie or the SSE bus, and never log. The
service layer wraps them with persistence and event emission.

Path syntax: dotted with bracket indexing. Examples:
    "filter"                  → state["filter"]
    "user.name"               → state["user"]["name"]
    "tasks[0]"                → state["tasks"][0]
    "tasks[0].status"         → state["tasks"][0]["status"]
    "groups[2].members[1].id" → arbitrary nesting

Indices count from 0. Negative indices (``tasks[-1]``) are not
supported; the agent should know the length and pass the explicit index.
"""

from __future__ import annotations

import re
from typing import Any

# Path segment regex: either a bare key, or `key[index]`.
_SEGMENT = re.compile(r"^([^.\[\]]+)((?:\[\d+\])*)$")
_INDEX = re.compile(r"\[(\d+)\]")


def _parse_path(path: str) -> list[str | int]:
    """Tokenise a dotted/bracket path into a flat list of keys + indices.

    ``"tasks[0].status"`` → ``["tasks", 0, "status"]``.

    Raises ``ValueError`` on malformed paths.
    """
    if not path:
        raise ValueError("path is required")
    out: list[str | int] = []
    for part in path.split("."):
        m = _SEGMENT.match(part)
        if not m:
            raise ValueError(f"malformed path segment: {part!r}")
        key, brackets = m.group(1), m.group(2)
        out.append(key)
        for idx_match in _INDEX.finditer(brackets):
            out.append(int(idx_match.group(1)))
    return out


def get_path(state: dict[str, Any], path: str) -> Any:
    """Read the value at ``path``. Returns ``None`` if any segment along
    the way is missing or has the wrong type."""
    tokens = _parse_path(path)
    cursor: Any = state
    for tok in tokens:
        if isinstance(tok, int):
            if not isinstance(cursor, list) or tok >= len(cursor) or tok < 0:
                return None
            cursor = cursor[tok]
        else:
            if not isinstance(cursor, dict) or tok not in cursor:
                return None
            cursor = cursor[tok]
    return cursor


def set_path(state: dict[str, Any], path: str, value: Any) -> Any:
    """Write ``value`` at ``path``, creating intermediate dicts as needed.
    Returns the previous value (``None`` if absent).

    List indices must already exist — we don't auto-grow arrays past
    their current length (writing to ``tasks[5]`` on a 3-item list is
    an error). Use ``append_path`` to grow arrays.
    """
    tokens = _parse_path(path)
    if not tokens:
        raise ValueError("empty path")

    cursor: Any = state
    for tok in tokens[:-1]:
        if isinstance(tok, int):
            if not isinstance(cursor, list):
                raise ValueError(f"expected list at segment {tok!r}, got {type(cursor).__name__}")
            if tok < 0 or tok >= len(cursor):
                raise ValueError(f"list index {tok} out of range")
            cursor = cursor[tok]
        else:
            if not isinstance(cursor, dict):
                raise ValueError(f"expected dict at segment {tok!r}, got {type(cursor).__name__}")
            nxt = cursor.get(tok)
            if not isinstance(nxt, dict | list):
                nxt = {}
                cursor[tok] = nxt
            cursor = nxt

    last = tokens[-1]
    if isinstance(last, int):
        if not isinstance(cursor, list):
            raise ValueError(f"expected list at segment {last!r}, got {type(cursor).__name__}")
        if last < 0 or last >= len(cursor):
            raise ValueError(f"list index {last} out of range")
        old = cursor[last]
        cursor[last] = value
        return old
    if not isinstance(cursor, dict):
        raise ValueError(f"expected dict at segment {last!r}, got {type(cursor).__name__}")
    old = cursor.get(last)
    cursor[last] = value
    return old


def append_path(state: dict[str, Any], path: str, item: Any) -> int:
    """Append ``item`` to the list at ``path``. If the path is absent,
    creates an empty list and appends. Returns the new length.

    Raises ``ValueError`` if the existing value at ``path`` is non-list.
    """
    tokens = _parse_path(path)
    if not tokens:
        raise ValueError("empty path")

    # Walk to the parent, creating intermediates.
    cursor: Any = state
    for tok in tokens[:-1]:
        if isinstance(tok, int):
            if not isinstance(cursor, list) or tok < 0 or tok >= len(cursor):
                raise ValueError(f"cannot walk through index {tok!r}")
            cursor = cursor[tok]
        else:
            if not isinstance(cursor, dict):
                raise ValueError(f"cannot walk through {tok!r}")
            nxt = cursor.get(tok)
            if not isinstance(nxt, dict | list):
                nxt = {}
                cursor[tok] = nxt
            cursor = nxt

    last = tokens[-1]
    if isinstance(last, int):
        raise ValueError("append_path target must be a key, not an index")
    if not isinstance(cursor, dict):
        raise ValueError(f"expected dict at parent of {last!r}")
    target = cursor.get(last)
    if target is None:
        target = []
        cursor[last] = target
    if not isinstance(target, list):
        raise ValueError(f"cannot append to non-list at {path!r} (got {type(target).__name__})")
    target.append(item)
    return len(target)


def remove_path(state: dict[str, Any], path: str) -> Any:
    """Remove the value at ``path``. Returns the removed value.

    For dict keys, deletes the key. For list indices, removes the
    element (shifting subsequent indices down). Raises ``ValueError`` if
    the path is missing.
    """
    tokens = _parse_path(path)
    if not tokens:
        raise ValueError("empty path")

    cursor: Any = state
    for tok in tokens[:-1]:
        if isinstance(tok, int):
            if not isinstance(cursor, list) or tok < 0 or tok >= len(cursor):
                raise ValueError(f"cannot walk through index {tok}")
            cursor = cursor[tok]
        else:
            if not isinstance(cursor, dict) or tok not in cursor:
                raise ValueError(f"path not found at segment {tok!r}")
            cursor = cursor[tok]

    last = tokens[-1]
    if isinstance(last, int):
        if not isinstance(cursor, list) or last < 0 or last >= len(cursor):
            raise ValueError(f"list index {last} out of range")
        return cursor.pop(last)
    if not isinstance(cursor, dict) or last not in cursor:
        raise ValueError(f"key {last!r} not in state")
    return cursor.pop(last)


def patch(state: dict[str, Any], partial: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``partial`` into ``state`` at the top level. Returns
    the previous values of overwritten keys (the inverse for undo).

    Use this for batched writes when the agent has several independent
    keys to set in one round-trip. For nested updates, prefer
    ``set_path`` — patch only merges at the top level on purpose, so
    callers don't accidentally clobber nested dicts.
    """
    if not isinstance(partial, dict):
        raise ValueError("patch target must be a dict")
    prev: dict[str, Any] = {}
    for k, v in partial.items():
        prev[k] = state.get(k)
        state[k] = v
    return prev


__all__ = [
    "append_path",
    "get_path",
    "patch",
    "remove_path",
    "set_path",
]
