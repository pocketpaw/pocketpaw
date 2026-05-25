# ee/pocketpaw_ee/cloud/pockets/_merge.py
# Created: 2026-05-24 — pure-Python port of the flat-model spike's TS
# `merge()` function, adapted for PocketPaw's NESTED rippleSpec shape
# (``{state, ui: {type, props, children, id}}``). Powers the new
# ``POST /api/v1/pockets/{id}/spec/merge`` endpoint — one server-side
# merge entry point that replaces the 17-tool LangChain edit surface for
# the ``pocket_specialist`` agent.
# Updated: 2026-05-25 (PR #1222 R1 high-priority 1) — added a visited-set
# cycle guard to ``_collect_patch_nodes`` and ``_collect_all_ids`` so an
# adversarial or buggy patch with a self-referential ``children`` graph
# cannot hang the merge walk. The patch comes from untrusted agent
# output, so the guard is required, not optional.
#
# Merge rules — see ``merge_ripple_spec`` docstring for the canonical
# spec. Short version: top-level keys in ``patch`` overwrite the base;
# ``state`` is shallow-merged; ``ui`` is walked for node-id matches and
# any matched node is replaced wholesale at its position in the base
# tree. Patch nodes whose id is not reachable from any base node are
# returned as orphans in the result so the caller can surface them as
# warnings (they do NOT block the merge — the captain's MVP guidance is
# "auto-orphan, document it").
"""Pure merge helpers for nested rippleSpec patches.

No async, no DB, no FastAPI — these functions are unit-testable on
their own. The service layer in ``pockets/service.py`` calls
``merge_ripple_spec`` after loading the base doc, then validates the
merged spec before persisting.
"""

from __future__ import annotations

import copy
from typing import Any


def _collect_patch_nodes(patch_ui: Any) -> dict[str, dict[str, Any]]:
    """Walk a patch UI subtree and return ``{id: node_dict}`` for every
    node that carries an ``id``. Children of a node ARE included in the
    map AND remain part of the parent's recorded subtree — the merge
    rule below stops descending once a parent is matched, so nesting is
    preserved naturally. Nodes without an ``id`` field are skipped (only
    id-bearing nodes can act as merge points).

    Cycle guard (PR #1222 R1 high-priority 1): the patch comes from
    untrusted agent output. A buggy or adversarial patch with a
    self-referential ``children`` list (``a.children = [b]``,
    ``b.children = [a]``) would loop forever without a visited-set
    guard. ``visited_ids`` skips id-bearing nodes we've already walked
    — that is sufficient because cycles can only form through the
    id-keyed graph the merge rule cares about.
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(patch_ui, dict):
        return out
    visited_ids: set[str] = set()
    stack: list[Any] = [patch_ui]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        nid = node.get("id")
        if isinstance(nid, str) and nid:
            if nid in visited_ids:
                # Cycle (or duplicate id-bearing subtree): stop
                # descending. The first occurrence of this id is
                # already in ``out`` — first-write-wins, so we keep it.
                continue
            visited_ids.add(nid)
            # First-write wins — defensive against a patch that
            # accidentally re-states the same id twice. Either is
            # technically a bug in the caller; pinning to the first
            # occurrence gives a stable, debuggable outcome.
            out.setdefault(nid, node)
        for kid_key in ("children", "else_children"):
            kids = node.get(kid_key)
            if isinstance(kids, list):
                stack.extend(kids)
    return out


def _collect_all_ids(node: Any) -> list[str]:
    """Walk a node and return every ``id`` reachable from it (including
    its own). Helper for marking patch descendants as ``matched`` when
    their parent is the merge point — they ride along inside the
    wholesale replacement and should NOT be reported as orphans.

    Cycle guard (PR #1222 R1 high-priority 1): mirrors
    ``_collect_patch_nodes`` — an adversarial patch with a
    self-referential children graph would loop forever otherwise. The
    visited-set is keyed on node id, the only invariant we can rely on
    in untrusted agent output.
    """
    out: list[str] = []
    if not isinstance(node, dict):
        return out
    visited_ids: set[str] = set()
    stack: list[Any] = [node]
    while stack:
        cur = stack.pop()
        if not isinstance(cur, dict):
            continue
        nid = cur.get("id")
        if isinstance(nid, str) and nid:
            if nid in visited_ids:
                continue
            visited_ids.add(nid)
            out.append(nid)
        for kid_key in ("children", "else_children"):
            kids = cur.get(kid_key)
            if isinstance(kids, list):
                stack.extend(kids)
    return out


def _replace_in_tree(
    base_node: Any,
    by_id: dict[str, dict[str, Any]],
    matched: set[str],
) -> Any:
    """Walk ``base_node`` top-down. If the current node's id is in
    ``by_id``, return that patch subtree verbatim (deep-copied) and stop
    descending. Otherwise descend into ``children`` / ``else_children``
    and rebuild the node with merged child arrays.

    ``matched`` accumulates the patch ids actually consumed by the
    walk so the caller can detect orphans. When a node is replaced
    wholesale, EVERY id in the patch's replacement subtree is marked
    matched — those descendants ride along inside the wholesale swap
    and are NOT orphans even though no base node carried their id.
    """
    if not isinstance(base_node, dict):
        return base_node
    nid = base_node.get("id")
    if isinstance(nid, str) and nid in by_id:
        replacement = by_id[nid]
        # Mark the replacement's id AND every id-bearing descendant —
        # they're all placed in the merged tree implicitly via this
        # wholesale swap.
        for placed_id in _collect_all_ids(replacement):
            matched.add(placed_id)
        # Wholesale replacement — return the patch's node copy. We do
        # NOT keep walking inside it: the patch fully owns its subtree.
        return copy.deepcopy(replacement)
    # No id match at this node — descend.
    out: dict[str, Any] = dict(base_node)
    for kid_key in ("children", "else_children"):
        kids = base_node.get(kid_key)
        if isinstance(kids, list):
            out[kid_key] = [_replace_in_tree(k, by_id, matched) for k in kids]
    return out


def merge_ripple_spec(
    base: dict[str, Any], patch: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Merge a partial ``rippleSpec`` onto a base ``rippleSpec``.

    Returns ``(new_spec, orphan_ids)``. ``orphan_ids`` is the list of
    node ids that appeared in ``patch.ui`` but did not match any node
    id in ``base.ui`` — the captain's MVP decision is auto-orphan +
    warn, so these are reported back to the caller and NOT applied.

    Rules:

    - Every top-level key on ``patch`` overwrites the same key on the
      base — EXCEPT ``state`` and ``ui`` which have their own rules.
    - ``state``: shallow-merged. Patch's top-level state keys overwrite
      the base's keys of the same name; keys absent from the patch stay.
      Nested values are NOT recursively merged — the patch must include
      whole subtrees if it wants to swap, say, a nested object.
    - ``ui``: walked. A flat ``{id: subtree}`` map is built from every
      id-bearing node in ``patch.ui`` (including descendants). Then the
      base ``ui`` tree is walked top-down — at each node, if its id is
      in the patch map, the whole node (and its subtree) is replaced
      verbatim from the patch. Otherwise the walk descends into that
      node's ``children`` / ``else_children`` arrays.

    Adding a NEW node = patch re-states the PARENT with the new child
    appended in the parent's ``children`` array. The new child rides
    along inside the wholesale parent replacement.

    Orphans (ids present in patch.ui but not reachable from base.ui)
    are silently dropped from the merged spec. The list is returned so
    the caller can surface them as warnings — they usually indicate the
    agent mis-typed an id or forgot to include the parent re-statement.
    """
    if not isinstance(base, dict):
        raise TypeError("merge base must be a dict")
    if not isinstance(patch, dict):
        raise TypeError("merge patch must be a dict")

    out: dict[str, Any] = copy.deepcopy(base)

    # ---- top-level scalar/dict fields (skip state + ui, handle below)
    for key, value in patch.items():
        if key in ("state", "ui"):
            continue
        out[key] = copy.deepcopy(value)

    # ---- state: shallow-merge top-level keys
    patch_state = patch.get("state")
    if isinstance(patch_state, dict):
        merged_state = dict(out.get("state") or {})
        for sk, sv in patch_state.items():
            merged_state[sk] = copy.deepcopy(sv)
        out["state"] = merged_state

    # ---- ui: walk + replace-by-id
    patch_ui = patch.get("ui")
    if isinstance(patch_ui, dict):
        by_id = _collect_patch_nodes(patch_ui)
        matched: set[str] = set()
        base_ui = out.get("ui")
        if isinstance(base_ui, dict):
            new_ui = _replace_in_tree(base_ui, by_id, matched)
            out["ui"] = new_ui
        else:
            # No base ui at all — treat the entire patch ui as the new
            # root. This is technically also covered by ``replace`` at
            # the endpoint level but supporting it here keeps the
            # helper composable.
            out["ui"] = copy.deepcopy(patch_ui)
            matched.update(by_id.keys())
        orphans = sorted(set(by_id.keys()) - matched)
    else:
        orphans = []

    return out, orphans


__all__ = ["merge_ripple_spec"]
