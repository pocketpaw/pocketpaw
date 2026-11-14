# ee/pocketpaw_ee/cloud/belt/architecture_diff.py — what changed between two C4
# models (``docs/c4/model.json``'s ``model`` block), for the architecture
# viewer's Compare (``GET /belt/architecture?diff=<sha>``) and the history's
# per-commit ``+n −m ~k`` counts.
#
# Pure: no git, no I/O. Elements are matched by ``id`` across ``people`` and
# ``systems`` and their nested ``containers`` / ``components`` / ``children``
# (the first occurrence of an id wins). An element is changed when one of its
# own fields differs (nested lists are not its fields) or it moved under another
# parent (``parent``). Relationships have no id: they match by ``(source, target)``
# (``from`` / ``to`` accepted) counting copies, so a second one between the same
# ends is added or removed on its own and a relabelled one is neither.
#
# ``removed_elements`` carries what a viewer needs to draw what is gone: each
# topmost removed element, its ``parent`` id (``None`` at the top level) and the
# list it sat in (``in``), with its subtree minus any descendant that still
# exists (a moved element must not be drawn twice).

from __future__ import annotations

from typing import Any

_NESTED = ("containers", "components", "children")
_TOP = ("people", "systems")


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _index(model: dict[str, Any]) -> dict[str, tuple[dict[str, Any], str | None, str]]:
    """``id -> (element, parent id, the list it sits in)`` over the whole tree."""
    out: dict[str, tuple[dict[str, Any], str | None, str]] = {}

    def walk(el: Any, parent: str | None, key: str) -> None:
        if not isinstance(el, dict) or not isinstance(el.get("id"), str):
            return
        out.setdefault(el["id"], (el, parent, key))
        for k in _NESTED:
            for kid in _list(el.get(k)):
                walk(kid, el["id"], k)

    for key in _TOP:
        for el in _list(model.get(key)):
            walk(el, None, key)
    return out


def _own(el: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in el.items() if k not in _NESTED}


def _rels(model: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in _list(model.get("relationships")):
        if isinstance(r, dict):
            ends = (str(r.get("source", r.get("from"))), str(r.get("target", r.get("to"))))
            out.setdefault(ends, []).append(r)
    return out


def _extra(rels: dict[tuple[str, str], list[Any]], other: dict[tuple[str, str], list[Any]]):
    """Copies in ``rels`` with no partner in ``other`` (identical pairs first, then relabels)."""
    out = []
    for ends, mine in rels.items():
        theirs, alone = list(other.get(ends, [])), []
        for r in mine:
            if r in theirs:
                theirs.remove(r)
            else:
                alone.append(r)
        out += alone[len(theirs) :]
    return out


def _pruned(el: dict[str, Any], keep: set[str]) -> dict[str, Any]:
    """``el`` with every descendant whose id is in ``keep`` (still present) dropped."""
    out = _own(el)
    for k in _NESTED:
        kids = [_pruned(c, keep) for c in _list(el.get(k)) if isinstance(c, dict)]
        kids = [c for c in kids if c.get("id") not in keep]
        if kids:
            out[k] = kids
    return out


def diff_models(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """``new`` against ``old`` (both a model's ``model`` block); see the header."""
    before, after = _index(old), _index(new)
    removed = [i for i in before if i not in after]
    changed = []
    for i, (el, parent, _key) in after.items():
        if i not in before:
            continue
        was, was_parent, _ = before[i]
        a, b = _own(was), _own(el)
        fields = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
        if parent != was_parent:
            fields.append("parent")
        if fields:
            changed.append({"id": i, "fields": fields})
    gone, present = set(removed), set(after)
    top = [i for i in removed if before[i][1] not in gone]
    r_old, r_new = _rels(old), _rels(new)
    return {
        "added": [i for i in after if i not in before],
        "removed": removed,
        "changed": changed,
        "removed_elements": [
            {"parent": before[i][1], "in": before[i][2], "element": _pruned(before[i][0], present)}
            for i in top
        ],
        "relationships": {
            "added": _extra(r_new, r_old),
            "removed": _extra(r_old, r_new),
        },
    }


def diff_counts(diff: dict[str, Any]) -> dict[str, int]:
    """``{added, removed, changed}`` for a history row: elements plus relationships."""
    rels = diff["relationships"]
    return {
        "added": len(diff["added"]) + len(rels["added"]),
        "removed": len(diff["removed"]) + len(rels["removed"]),
        "changed": len(diff["changed"]),
    }
