"""Unit tests for the pure spec_ops helpers.

These tests exercise the walk + mutate primitives against fixture trees
— no DB, no events, no MCP. They lock down the contract the service
layer's granular ops rely on.
"""

from __future__ import annotations

import copy

import pytest
from pocketpaw_ee.cloud.pockets import spec_ops


def _tree() -> dict:
    """Canonical fixture: a small dashboard with two rows and a button."""
    return {
        "id": "n_root0000",
        "type": "flex",
        "props": {"direction": "column"},
        "children": [
            {
                "id": "n_header00",
                "type": "heading",
                "props": {"text": "Dashboard"},
            },
            {
                "id": "n_table000",
                "type": "table",
                "props": {"rows": []},
                "children": [
                    {"id": "n_row00001", "type": "row", "props": {"label": "alice"}},
                    {"id": "n_row00002", "type": "row", "props": {"label": "bob"}},
                ],
            },
            {
                "id": "n_button00",
                "type": "button",
                "props": {"label": "Add"},
                "on_click": [{"action": "set", "target": "state.count", "value": 1}],
            },
        ],
    }


# ---------------------------------------------------------------------------
# ID generation / validation
# ---------------------------------------------------------------------------


def test_new_node_id_matches_pattern():
    for _ in range(50):
        nid = spec_ops.new_node_id()
        assert spec_ops.is_valid_id(nid), nid


def test_is_valid_id_rejects_garbage():
    assert not spec_ops.is_valid_id("")
    assert not spec_ops.is_valid_id(None)
    assert not spec_ops.is_valid_id(12)
    assert not spec_ops.is_valid_id("widget-1")
    assert not spec_ops.is_valid_id("n_UPPERCASE")
    assert not spec_ops.is_valid_id("n_short")


def test_ensure_ids_assigns_to_missing():
    tree = {"type": "flex", "children": [{"type": "text"}, {"type": "text"}]}
    changed = spec_ops.ensure_ids(tree)
    assert changed is True
    assert spec_ops.is_valid_id(tree["id"])
    for kid in tree["children"]:
        assert spec_ops.is_valid_id(kid["id"])


def test_ensure_ids_resolves_sibling_collisions():
    # Two children share an id — one must be reassigned.
    tree = {
        "id": "n_root0000",
        "type": "flex",
        "children": [
            {"id": "n_dup00000", "type": "text"},
            {"id": "n_dup00000", "type": "text"},
        ],
    }
    changed = spec_ops.ensure_ids(tree)
    assert changed is True
    ids = [k["id"] for k in tree["children"]]
    assert ids[0] != ids[1]


def test_ensure_ids_idempotent_on_clean_tree():
    tree = _tree()
    snapshot = copy.deepcopy(tree)
    assert spec_ops.ensure_ids(tree) is False
    assert tree == snapshot


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def test_find_by_id_walks_nested():
    tree = _tree()
    found = spec_ops.find_by_id(tree, "n_row00002")
    assert found is not None
    assert found["props"]["label"] == "bob"


def test_find_by_id_missing_returns_none():
    assert spec_ops.find_by_id(_tree(), "n_nope0000") is None


def test_find_parent_root_returns_none():
    assert spec_ops.find_parent(_tree(), "n_root0000") is None


def test_find_parent_locates_child_index():
    tree = _tree()
    parent, key, idx = spec_ops.find_parent(tree, "n_row00002")
    assert parent["id"] == "n_table000"
    assert key == "children"
    assert idx == 1


# ---------------------------------------------------------------------------
# insert_child
# ---------------------------------------------------------------------------


def test_insert_child_appends_by_default():
    tree = _tree()
    parent = spec_ops.find_by_id(tree, "n_table000")
    spec_ops.insert_child(parent, {"id": "n_row00003", "type": "row"})
    assert [c["id"] for c in parent["children"]] == [
        "n_row00001",
        "n_row00002",
        "n_row00003",
    ]


def test_insert_child_after_id():
    tree = _tree()
    parent = spec_ops.find_by_id(tree, "n_table000")
    spec_ops.insert_child(parent, {"id": "n_rowins00", "type": "row"}, after_id="n_row00001")
    assert [c["id"] for c in parent["children"]] == [
        "n_row00001",
        "n_rowins00",
        "n_row00002",
    ]


def test_insert_child_after_unknown_raises():
    tree = _tree()
    parent = spec_ops.find_by_id(tree, "n_table000")
    with pytest.raises(ValueError, match="after_id"):
        spec_ops.insert_child(parent, {"id": "n_x0000000"}, after_id="n_ghost000")


# ---------------------------------------------------------------------------
# replace_node
# ---------------------------------------------------------------------------


def test_replace_node_returns_old_and_preserves_id():
    tree = _tree()
    old = spec_ops.replace_node(tree, "n_header00", {"type": "heading", "props": {"text": "Hello"}})
    assert old["props"]["text"] == "Dashboard"
    replaced = spec_ops.find_by_id(tree, "n_header00")
    assert replaced is not None
    assert replaced["props"]["text"] == "Hello"


def test_replace_node_keeps_explicit_id():
    tree = _tree()
    spec_ops.replace_node(
        tree,
        "n_header00",
        {"id": "n_newhead0", "type": "heading", "props": {"text": "Hello"}},
    )
    assert spec_ops.find_by_id(tree, "n_header00") is None
    assert spec_ops.find_by_id(tree, "n_newhead0") is not None


def test_replace_root_raises():
    tree = _tree()
    with pytest.raises(ValueError, match="root"):
        spec_ops.replace_node(tree, "n_root0000", {"type": "flex"})


# ---------------------------------------------------------------------------
# remove_node
# ---------------------------------------------------------------------------


def test_remove_node_returns_position_and_subtree():
    tree = _tree()
    parent, key, idx, removed = spec_ops.remove_node(tree, "n_row00001")
    assert parent["id"] == "n_table000"
    assert key == "children"
    assert idx == 0
    assert removed["props"]["label"] == "alice"
    assert [c["id"] for c in parent["children"]] == ["n_row00002"]


def test_remove_root_raises():
    with pytest.raises(ValueError, match="root"):
        spec_ops.remove_node(_tree(), "n_root0000")


def test_remove_missing_raises():
    with pytest.raises(ValueError, match="no node"):
        spec_ops.remove_node(_tree(), "n_ghost000")


# ---------------------------------------------------------------------------
# set_prop
# ---------------------------------------------------------------------------


def test_set_prop_writes_into_props_and_returns_old():
    node = {"type": "text", "props": {"label": "old"}}
    old = spec_ops.set_prop(node, "label", "new")
    assert old == "old"
    assert node["props"]["label"] == "new"


def test_set_prop_creates_props_when_missing():
    node = {"type": "text"}
    spec_ops.set_prop(node, "label", "x")
    assert node["props"]["label"] == "x"


def test_set_prop_top_level_fields():
    node = {"type": "button", "props": {}}
    spec_ops.set_prop(node, "show", "{state.flag}")
    assert node["show"] == "{state.flag}"
    # Doesn't bleed into props.
    assert "show" not in node["props"]


def test_set_prop_dotted_path_walks_props():
    node = {"type": "table", "props": {"data": {"rows": []}}}
    spec_ops.set_prop(node, "data.rows", [{"a": 1}])
    assert node["props"]["data"]["rows"] == [{"a": 1}]


def test_set_prop_empty_name_raises():
    with pytest.raises(ValueError):
        spec_ops.set_prop({"type": "text"}, "", "x")


# ---------------------------------------------------------------------------
# move_node
# ---------------------------------------------------------------------------


def test_move_node_cross_parent():
    tree = _tree()
    # Move the button under the table.
    old_parent_id, old_idx = spec_ops.move_node(tree, "n_button00", "n_table000", after_id=None)
    assert old_parent_id == "n_root0000"
    assert old_idx == 2
    table = spec_ops.find_by_id(tree, "n_table000")
    assert [c["id"] for c in table["children"]] == [
        "n_row00001",
        "n_row00002",
        "n_button00",
    ]
    # And gone from the root.
    assert [c["id"] for c in tree["children"]] == ["n_header00", "n_table000"]


def test_move_node_within_same_parent():
    tree = _tree()
    spec_ops.move_node(tree, "n_button00", "n_root0000", after_id="n_header00")
    assert [c["id"] for c in tree["children"]] == [
        "n_header00",
        "n_button00",
        "n_table000",
    ]


def test_move_node_into_self_raises():
    tree = _tree()
    with pytest.raises(ValueError, match="descendant"):
        spec_ops.move_node(tree, "n_table000", "n_row00001")


def test_move_root_raises():
    tree = _tree()
    with pytest.raises(ValueError, match="root"):
        spec_ops.move_node(tree, "n_root0000", "n_table000")
