"""Pure walk + mutate helpers for ``rippleSpec.ui`` trees.

These helpers are intentionally side-effect-free: they take and return
plain ``dict``/``list`` structures, never touch Beanie or the SSE bus,
and never log. The service layer wraps them with persistence and event
emission.

Why a separate module: the granular mutation tools
(``add_node`` / ``replace_node`` / ``set_node_prop`` / ``move_node`` /
``remove_node``) all reduce to the same set of tree primitives. Keeping
the primitives pure makes them trivial to unit-test against fixture
trees, and keeps the per-op service handlers small.

ID format: ``n_<8 chars from [a-z0-9]>``. 40 bits of randomness — at the
pocket sizes we see (max ~1k nodes) collision probability is
vanishingly small, and the format is short enough for the agent to
copy/paste over the wire without errors.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_ID_LEN = 8
_ID_PATTERN = re.compile(r"^n_[a-z0-9]{8}$")


def new_node_id() -> str:
    """Return a new ``n_xxxxxxxx`` identifier."""
    return "n_" + "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LEN))


def is_valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_PATTERN.match(value))


def ensure_ids(node: dict[str, Any]) -> bool:
    """Walk ``node`` and its children, assigning ``id`` to any node that
    lacks one. Returns ``True`` if any id was assigned (caller persists
    when so).

    Sibling ID collisions are detected and the duplicates re-assigned —
    if the agent or a legacy writer produced colliding ids, the tree
    still ends up uniquely keyed before we hand it back.
    """
    if not isinstance(node, dict):
        return False

    seen: set[str] = set()
    changed = _ensure_ids_walk(node, seen)
    return changed


def _ensure_ids_walk(node: dict[str, Any], seen: set[str]) -> bool:
    changed = False
    nid = node.get("id")
    if not is_valid_id(nid) or (isinstance(nid, str) and nid in seen):
        new_id = new_node_id()
        while new_id in seen:
            new_id = new_node_id()
        node["id"] = new_id
        changed = True
    seen.add(node["id"])

    for child_key in ("children", "else_children"):
        kids = node.get(child_key)
        if isinstance(kids, list):
            for kid in kids:
                if isinstance(kid, dict):
                    if _ensure_ids_walk(kid, seen):
                        changed = True
    return changed


# ---------------------------------------------------------------------------
# Tree walks
# ---------------------------------------------------------------------------

# Container child-list keys recognised when walking. Order matters for
# move semantics — ``children`` is the default container. ``else_children``
# is the if-widget's false-branch slot.
_CHILD_KEYS = ("children", "else_children")


def find_by_id(root: dict[str, Any], target_id: str) -> dict[str, Any] | None:
    """Return the dict node with the given id, or ``None``. Walks
    ``children`` and ``else_children``."""
    if not isinstance(root, dict) or not target_id:
        return None
    if root.get("id") == target_id:
        return root
    for key in _CHILD_KEYS:
        kids = root.get(key)
        if isinstance(kids, list):
            for kid in kids:
                if isinstance(kid, dict):
                    found = find_by_id(kid, target_id)
                    if found is not None:
                        return found
    return None


def find_parent(root: dict[str, Any], target_id: str) -> tuple[dict[str, Any], str, int] | None:
    """Return ``(parent_node, child_key, index)`` for the child with
    ``target_id``, or ``None`` if not found (including when ``target_id``
    refers to the root itself — the root has no parent)."""
    if not isinstance(root, dict) or not target_id:
        return None
    for key in _CHILD_KEYS:
        kids = root.get(key)
        if isinstance(kids, list):
            for idx, kid in enumerate(kids):
                if isinstance(kid, dict) and kid.get("id") == target_id:
                    return root, key, idx
                if isinstance(kid, dict):
                    inner = find_parent(kid, target_id)
                    if inner is not None:
                        return inner
    return None


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def insert_child(
    parent: dict[str, Any],
    child: dict[str, Any],
    *,
    after_id: str | None = None,
    index: int | None = None,
    child_key: str = "children",
) -> None:
    """Insert ``child`` into ``parent[child_key]``. If ``index`` is given,
    insert at that 0-based position (clamped to the list bounds). Else if
    ``after_id`` is given, insert immediately after that sibling.
    Otherwise append.

    Raises ``ValueError`` if ``after_id`` is given but not found among
    siblings, or if ``parent`` is not a container (i.e. its existing
    children slot is non-list)."""
    existing = parent.get(child_key)
    if existing is None:
        parent[child_key] = []
        existing = parent[child_key]
    if not isinstance(existing, list):
        raise ValueError(f"parent {parent.get('id', '<?>')} has non-list '{child_key}'")

    if index is not None:
        # Clamp — an out-of-range index from the agent lands at the
        # nearest valid slot rather than raising.
        existing.insert(max(0, min(index, len(existing))), child)
        return

    if after_id is None:
        existing.append(child)
        return

    for idx, sibling in enumerate(existing):
        if isinstance(sibling, dict) and sibling.get("id") == after_id:
            existing.insert(idx + 1, child)
            return
    raise ValueError(f"after_id {after_id!r} is not a child of {parent.get('id', '<?>')}")


def replace_node(
    root: dict[str, Any], target_id: str, replacement: dict[str, Any]
) -> dict[str, Any]:
    """Replace the subtree rooted at ``target_id`` with ``replacement``.
    Preserves ``replacement['id']`` if set, otherwise keeps the original
    id. Returns the OLD subtree (the caller stores it as the inverse for
    undo).

    When ``target_id`` is the root node, the whole tree is swapped:
    ``root`` is mutated in place to become ``replacement``. This is how a
    single-widget pocket gains a container at the root — e.g. wrapping a
    bare ``project-dashboard`` root in a ``flex`` so a sibling section can
    be added next to it.

    Raises ``ValueError`` only if ``target_id`` isn't found.
    """
    if root.get("id") == target_id:
        # Root replacement — there is no parent. Mutate the root dict in
        # place so callers holding a reference to it (doc.rippleSpec.ui)
        # see the swap. Preserve the root id when the replacement omits one.
        old = dict(root)
        if not is_valid_id(replacement.get("id")):
            replacement["id"] = old.get("id") or new_node_id()
        root.clear()
        root.update(replacement)
        return old
    loc = find_parent(root, target_id)
    if loc is None:
        raise ValueError(f"no node with id {target_id!r}")
    parent, key, idx = loc
    old = parent[key][idx]
    # Preserve the existing id when the replacement omits one, so callers
    # don't have to round-trip the id back through the agent.
    if not is_valid_id(replacement.get("id")):
        replacement["id"] = old.get("id") or new_node_id()
    parent[key][idx] = replacement
    return old


def remove_node(
    root: dict[str, Any], target_id: str
) -> tuple[dict[str, Any], str, int, dict[str, Any]]:
    """Remove the subtree rooted at ``target_id``. Returns
    ``(parent, child_key, index, removed)`` — the parent ref plus
    enough info to reconstruct the insertion point for undo.

    Raises ``ValueError`` on missing target or root removal.
    """
    if root.get("id") == target_id:
        raise ValueError("cannot remove the root node")
    loc = find_parent(root, target_id)
    if loc is None:
        raise ValueError(f"no node with id {target_id!r}")
    parent, key, idx = loc
    removed = parent[key].pop(idx)
    return parent, key, idx, removed


def set_prop(node: dict[str, Any], prop: str, value: Any) -> Any:
    """Set ``node['props'][prop]`` (with dot-path support inside
    ``props``). Returns the previous value (``None`` if absent), which
    the caller stores as the inverse for undo.

    Top-level node fields (``show``, ``class``, ``style``, ``bind``,
    ``items``, ``item_as``, ``index_as``, ``condition``, event handlers
    ``on_*``, and ``slot``) are addressable as ``prop="show"`` etc. —
    set them directly without traversing ``props``.

    Raises ``ValueError`` on empty ``prop``.
    """
    if not prop:
        raise ValueError("prop name is required")

    if "." in prop:
        # Dot-path inside props.
        if not isinstance(node.get("props"), dict):
            node["props"] = {}
        return _set_dotted(node["props"], prop, value)

    if prop in _TOP_LEVEL_PROP_KEYS:
        old = node.get(prop)
        node[prop] = value
        return old

    # Default: write into props.
    if not isinstance(node.get("props"), dict):
        node["props"] = {}
    old = node["props"].get(prop)
    node["props"][prop] = value
    return old


_TOP_LEVEL_PROP_KEYS = frozenset(
    {
        "show",
        "class",
        "style",
        "bind",
        "items",
        "item_as",
        "index_as",
        "condition",
        "slot",
        "on_click",
        "on_change",
        "on_input",
        "on_submit",
        "on_focus",
        "on_blur",
    }
)


def _set_dotted(container: dict[str, Any], path: str, value: Any) -> Any:
    parts = path.split(".")
    last = parts[-1]
    cursor = container
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    old = cursor.get(last)
    cursor[last] = value
    return old


def move_node(
    root: dict[str, Any],
    node_id: str,
    new_parent_id: str,
    *,
    after_id: str | None = None,
) -> tuple[str, int]:
    """Move the subtree rooted at ``node_id`` under ``new_parent_id``.

    Returns ``(old_parent_id, old_index)`` so the caller can build the
    inverse op. Raises ``ValueError`` on missing source/target, root
    moves, or an attempt to move a node into its own descendant.
    """
    if root.get("id") == node_id:
        raise ValueError("cannot move the root node")

    src = find_parent(root, node_id)
    if src is None:
        raise ValueError(f"no node with id {node_id!r}")
    new_parent = find_by_id(root, new_parent_id)
    if new_parent is None:
        raise ValueError(f"no parent with id {new_parent_id!r}")

    src_parent, src_key, src_idx = src
    subtree = src_parent[src_key][src_idx]

    # Guard: moving into self/descendant would create a cycle.
    if find_by_id(subtree, new_parent_id) is not None:
        raise ValueError("cannot move a node into itself or its descendants")

    # Detach.
    src_parent[src_key].pop(src_idx)
    old_parent_id = str(src_parent.get("id", ""))

    # Re-attach. ``after_id`` is resolved against the destination siblings
    # AFTER detachment, so a same-parent reorder past the removed slot
    # still works.
    try:
        insert_child(new_parent, subtree, after_id=after_id)
    except ValueError:
        # Rollback the detach so the tree stays consistent on error.
        src_parent[src_key].insert(src_idx, subtree)
        raise
    return old_parent_id, src_idx


# ---------------------------------------------------------------------------
# Prop-array item matching
#
# Used by the Tier-2 array-element ops (set_prop_array_item etc.) to locate
# a single item inside a node's prop-array (chart.data, table.rows, …)
# without forcing the agent to copy-paste the entire array.
# ---------------------------------------------------------------------------


def _match_form(match: dict[str, Any]) -> str:
    if not isinstance(match, dict) or not match:
        raise ValueError("empty match")
    if "index" in match:
        return "index"
    if "id" in match:
        return "id"
    if "by_key" in match:
        return "by_key"
    if "by_field" in match and "equals" in match:
        return "by_field"
    raise ValueError(f"unknown match form: {sorted(match.keys())}")


def _item_matches(item: Any, form: str, match: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    if form == "id":
        return item.get("id") == match["id"]
    if form == "by_key":
        pairs = match["by_key"]
        if not isinstance(pairs, dict) or not pairs:
            raise ValueError("by_key must be a non-empty mapping")
        return all(item.get(k) == v for k, v in pairs.items())
    if form == "by_field":
        return item.get(match["by_field"]) == match["equals"]
    return False


def match_array_item(arr: list[Any], match: dict[str, Any]) -> int | None:
    """Return the index of the FIRST item in ``arr`` matching ``match``,
    or ``None`` if no item matches. Raises ``ValueError`` on a malformed
    or out-of-range ``index`` match form.

    Match forms: see module docstring / design doc. Use
    ``match_array_item_candidates`` to detect ambiguity (multiple matches).
    """
    form = _match_form(match)
    if form == "index":
        idx = match["index"]
        if not isinstance(idx, int) or idx < 0 or idx >= len(arr):
            raise ValueError(f"index {idx!r} out of range [0,{len(arr)})")
        return idx
    for i, item in enumerate(arr):
        if _item_matches(item, form, match):
            return i
    return None


def match_array_item_candidates(arr: list[Any], match: dict[str, Any]) -> list[int]:
    """Return ALL indices in ``arr`` whose item matches ``match``. Used by
    the service layer to render `ambiguous` errors with candidates."""
    form = _match_form(match)
    if form == "index":
        # Positional match is never ambiguous.
        idx = match_array_item(arr, match)
        return [idx] if idx is not None else []
    return [i for i, item in enumerate(arr) if _item_matches(item, form, match)]


__all__ = [
    "ensure_ids",
    "find_by_id",
    "find_parent",
    "insert_child",
    "is_valid_id",
    "match_array_item",
    "match_array_item_candidates",
    "move_node",
    "new_node_id",
    "remove_node",
    "replace_node",
    "set_prop",
]
