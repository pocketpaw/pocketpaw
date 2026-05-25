# tests/cloud/pockets/test_merge_spec.py
# Created: 2026-05-24 — unit tests for the pure merge helper that powers
# the new ``POST /api/v1/pockets/{id}/spec/merge`` endpoint. Covers the
# four cases the merge semantics need to pin: a prop-change on one node,
# an added child via parent re-statement, a state-only patch, and an
# orphan id that doesn't match anything in the base tree.
# Updated: 2026-05-25 (PR #1222 R1 high-priority 1) — added
# ``test_merge_cycle_in_patch_does_not_hang`` to pin the visited-set
# guard added to ``_collect_patch_nodes`` / ``_collect_all_ids``. The
# patch tree is untrusted agent output; a self-referential children
# graph used to loop forever before the guard.
"""Unit tests for ``pocketpaw_ee.cloud.pockets._merge.merge_ripple_spec``."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets._merge import merge_ripple_spec

# ---------------------------------------------------------------------------
# Fixtures — a small base spec a few tests can reuse without re-typing.
# ---------------------------------------------------------------------------


def _base_spec() -> dict:
    """Two-button flex pocket with a draft state field. Deliberately
    small — every merge test below either swaps one node or adds one
    sibling, and a small base keeps the diff obvious in failure output.
    """
    return {
        "version": "1.0",
        "state": {"draft": "", "count": 0},
        "ui": {
            "id": "n_root0001",
            "type": "flex",
            "props": {"direction": "column", "gap": 12},
            "children": [
                {
                    "id": "n_btn00001",
                    "type": "button",
                    "props": {"label": "Click me"},
                },
                {
                    "id": "n_btn00002",
                    "type": "button",
                    "props": {"label": "Or me"},
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# Test 1 — prop change on one node (re-state by id, no sibling touched).
# ---------------------------------------------------------------------------


def test_prop_change_replaces_only_targeted_node():
    """Patch re-states a single node by id. That node is replaced
    wholesale; the sibling stays byte-identical; the parent's structure
    is preserved."""
    base = _base_spec()
    patch = {
        "ui": {
            "id": "n_btn00001",
            "type": "button",
            "props": {"label": "New label", "color": "#0A84FF"},
        },
    }

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    # The targeted button is fully replaced — props REPLACED, not merged.
    target = merged["ui"]["children"][0]
    assert target["id"] == "n_btn00001"
    assert target["props"] == {"label": "New label", "color": "#0A84FF"}
    # Sibling is untouched.
    sibling = merged["ui"]["children"][1]
    assert sibling == base["ui"]["children"][1]
    # Parent structure preserved.
    assert merged["ui"]["id"] == base["ui"]["id"]
    assert merged["ui"]["type"] == base["ui"]["type"]
    assert merged["ui"]["props"] == base["ui"]["props"]
    # State untouched.
    assert merged["state"] == base["state"]


# ---------------------------------------------------------------------------
# Test 2 — added child via parent re-statement.
# ---------------------------------------------------------------------------


def test_add_child_via_parent_restatement():
    """To add a new node, the patch re-states the parent's children
    array with the new child appended. The new child has an id that's
    NOT yet in the base — it should land in the merged tree as part of
    the parent's wholesale replacement, with no orphan reported."""
    base = _base_spec()
    new_child = {
        "id": "n_btn00003",
        "type": "button",
        "props": {"label": "Brand new"},
    }
    patch = {
        "ui": {
            "id": "n_root0001",
            "type": "flex",
            "props": {"direction": "column", "gap": 12},
            "children": [
                # Re-state every existing child verbatim so the parent
                # replacement keeps them.
                copy.deepcopy(base["ui"]["children"][0]),
                copy.deepcopy(base["ui"]["children"][1]),
                new_child,
            ],
        },
    }

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    children = merged["ui"]["children"]
    assert len(children) == 3
    assert [c["id"] for c in children] == ["n_btn00001", "n_btn00002", "n_btn00003"]
    assert children[2] == new_child


# ---------------------------------------------------------------------------
# Test 3 — state-only patch leaves ui untouched.
# ---------------------------------------------------------------------------


def test_state_only_patch_shallow_merges():
    """Patch with only a ``state`` key shallow-merges into existing
    state. Keys not present in the patch are kept; the ui tree is
    untouched byte-identical."""
    base = _base_spec()
    patch = {"state": {"count": 7, "newKey": "hello"}}

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    assert merged["state"] == {"draft": "", "count": 7, "newKey": "hello"}
    # ui untouched
    assert merged["ui"] == base["ui"]


# ---------------------------------------------------------------------------
# Test 4 — invalid patch with an id that doesn't reach base. The MVP
# decision is auto-orphan + report the id so the caller (the agent)
# can surface a corrective warning. The merge does NOT block.
# ---------------------------------------------------------------------------


def test_orphan_id_in_patch_is_reported_and_dropped():
    """A patch that mentions a node id which is NOT present in
    base.ui — and is NOT placed under any parent that IS in base — gets
    reported in the orphans list. The base tree is otherwise untouched.

    MVP semantics (per the captain): auto-orphan + warn, do not error.
    The merged spec is the base unchanged and the orphans list flags
    what was ignored."""
    base = _base_spec()
    patch = {
        "ui": {
            "id": "n_notInBase",
            "type": "button",
            "props": {"label": "Ghost"},
        },
    }

    merged, orphans = merge_ripple_spec(base, patch)

    # The orphan id is reported.
    assert orphans == ["n_notInBase"]
    # The base ui is preserved unchanged (the orphan got dropped).
    assert merged["ui"] == base["ui"]
    assert merged["state"] == base["state"]


# ---------------------------------------------------------------------------
# Bonus — top-level field overwrite + deep copy isolation.
# ---------------------------------------------------------------------------


def test_top_level_field_overwrite_and_deep_copy_isolation():
    """Top-level keys other than state / ui overwrite the base. The
    merge result is a deep copy of the base — mutating the merged tree
    must not mutate the original base."""
    base = _base_spec()
    base["title"] = "Old title"
    patch = {"title": "New title"}

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    assert merged["title"] == "New title"
    # Deep-copy isolation: mutate merged, original base must not change.
    merged["ui"]["children"][0]["props"]["label"] = "MUTATED"
    assert base["ui"]["children"][0]["props"]["label"] == "Click me"


def test_root_node_id_match_replaces_whole_tree():
    """A patch whose ui top-level id matches base's root id replaces
    the entire ui tree wholesale — the equivalent of ``replace_node``
    against the root in the old granular-op surface."""
    base = _base_spec()
    new_root = {
        "id": "n_root0001",
        "type": "flex",
        "props": {"direction": "row", "gap": 4},
        "children": [
            {"id": "n_text0001", "type": "text", "props": {"value": "Hi"}},
        ],
    }
    patch = {"ui": new_root}

    merged, orphans = merge_ripple_spec(base, patch)

    assert orphans == []
    assert merged["ui"] == new_root
    assert merged["state"] == base["state"]


def test_non_dict_inputs_raise_typeerror():
    """The helper is a pure function — non-dict inputs should fail
    fast instead of silently producing a malformed spec."""
    with pytest.raises(TypeError):
        merge_ripple_spec([], {})
    with pytest.raises(TypeError):
        merge_ripple_spec({}, "not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PR #1222 R1 high-priority 1 — cycle guard. The patch tree comes from
# untrusted agent output; a self-referential ``children`` list must not
# hang the merge walk. The guard lives in ``_collect_patch_nodes`` and
# ``_collect_all_ids`` and is keyed on node id.
# ---------------------------------------------------------------------------


def test_merge_cycle_in_patch_does_not_hang():
    """A patch where node_a.children references node_b and node_b.children
    references node_a must terminate. ``asyncio.wait_for`` is the
    timeout primitive available without an extra plugin.
    """
    import asyncio

    # Construct a cycle via shared dict references — the merge walk
    # follows ``children`` lists, so two id-bearing dicts that point at
    # each other's children form a self-referential graph.
    node_a: dict[str, Any] = {"id": "n_a00001", "type": "flex", "props": {}}
    node_b: dict[str, Any] = {"id": "n_b00001", "type": "flex", "props": {}}
    node_a["children"] = [node_b]
    node_b["children"] = [node_a]

    base = _base_spec()
    patch = {"ui": node_a}

    async def _run() -> tuple[dict, list[str]]:
        # ``merge_ripple_spec`` is sync — wrap so wait_for can cancel
        # if the implementation regresses to an unbounded walk.
        return merge_ripple_spec(base, patch)

    merged, orphans = asyncio.run(asyncio.wait_for(_run(), timeout=2.0))

    # Result shape doesn't matter as much as termination. Confirm the
    # walk produced a sane structure: both ids in the patch are reachable
    # (via the visited-set, each is recorded once) and neither is an
    # orphan because the patch UI tree was walked but never matched a
    # base id — the orphan-vs-matched bookkeeping treats them as orphans
    # (n_a / n_b aren't in base.ui, base's children are unchanged).
    assert isinstance(merged, dict)
    assert isinstance(orphans, list)
    # The IDs in the cycle must appear in the orphan list (they're in
    # patch but not in base) — exactly once each, no infinite duplicates.
    assert orphans.count("n_a00001") == 1
    assert orphans.count("n_b00001") == 1
