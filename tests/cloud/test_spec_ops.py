"""Unit tests for the pure spec_ops helpers.

These tests exercise the walk + mutate primitives against fixture trees
— no DB, no events, no MCP. They lock down the contract the service
layer's granular ops rely on.

Changes: 2026-06-12 (fix/pocket-anchored-chat-context) — added the
``summarize_ripple_spec`` suite: composed template-style spec, list-root
ui, the 20-key state cap, empty/None/malformed degradation, and the
legacy ``widgets_count`` passthrough. Locks the lightweight-metadata
contract both the chat ``<pocket-summary>`` block and the get_pocket
agent view ``_summary`` lead are built from.
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


def test_replace_root_swaps_the_whole_tree():
    """Root replacement is allowed: replace_node on the root id swaps the
    entire ui tree in place (the wrap-the-root path — e.g. wrapping a
    bare project-dashboard root in a flex). The root id is preserved and
    the old root is returned for undo."""
    tree = _tree()
    old = spec_ops.replace_node(tree, "n_root0000", {"type": "flex", "props": {"gap": 8}})
    assert tree["type"] == "flex"
    assert tree["props"] == {"gap": 8}
    assert tree["id"] == "n_root0000"  # root id preserved
    assert old["id"] == "n_root0000"  # old root returned for undo


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


# ---------------------------------------------------------------------------
# Prop-array item matching
# ---------------------------------------------------------------------------


def _sample_array() -> list[dict]:
    return [
        {"id": "b1", "label": "Online Store", "value": 62},
        {"id": "b2", "label": "POS", "value": 18},
        {"id": "b3", "label": "Social", "value": 12},
        {"id": "b4", "label": "Other", "value": 8},
    ]


def test_match_by_index_returns_position():
    idx = spec_ops.match_array_item(_sample_array(), {"index": 0})
    assert idx == 0


def test_match_by_index_negative_rejected():
    with pytest.raises(ValueError, match="index"):
        spec_ops.match_array_item(_sample_array(), {"index": -1})


def test_match_by_index_out_of_range_rejected():
    with pytest.raises(ValueError, match="index"):
        spec_ops.match_array_item(_sample_array(), {"index": 99})


def test_match_by_field_equals_finds_first():
    idx = spec_ops.match_array_item(_sample_array(), {"by_field": "label", "equals": "Other"})
    assert idx == 3


def test_match_by_field_missing_returns_none():
    idx = spec_ops.match_array_item(_sample_array(), {"by_field": "label", "equals": "Nope"})
    assert idx is None


def test_match_by_key_requires_all_pairs():
    arr = [
        {"orderId": "#1039", "channel": "Online"},
        {"orderId": "#1039", "channel": "POS"},
    ]
    idx = spec_ops.match_array_item(arr, {"by_key": {"orderId": "#1039", "channel": "POS"}})
    assert idx == 1


def test_match_id_shortcut_equivalent_to_by_key_id():
    idx = spec_ops.match_array_item(_sample_array(), {"id": "b3"})
    assert idx == 2


def test_match_ambiguous_returns_candidates_indices():
    arr = [
        {"label": "Other", "value": 1},
        {"label": "Other", "value": 2},
    ]
    candidates = spec_ops.match_array_item_candidates(arr, {"by_field": "label", "equals": "Other"})
    assert candidates == [0, 1]


def test_match_rejects_unknown_match_form():
    with pytest.raises(ValueError, match="unknown match form"):
        spec_ops.match_array_item(_sample_array(), {"weird": "shape"})


def test_match_rejects_empty_match():
    with pytest.raises(ValueError, match="empty"):
        spec_ops.match_array_item(_sample_array(), {})


# ---------------------------------------------------------------------------
# summarize_ripple_spec — lightweight metadata for agent orientation
# ---------------------------------------------------------------------------


def _composed_spec() -> dict:
    """Template-style spec: dict ui root with children, state, sources,
    actions — the applications-triage shape from the bug transcript."""
    return {
        "version": "1.0",
        "ui": {
            "id": "n_root0000",
            "type": "flex",
            "children": [
                {"id": "n_header00", "type": "page-header"},
                {"id": "n_grid0001", "type": "grid"},
                {"id": "n_grid0002", "type": "grid"},
            ],
        },
        "state": {
            "selected_id": None,
            "pending_proposal": None,
            "status_counts": {},
            "queue_total": 0,
            "oldest_waiting": "",
            "applications": [],
            "selected": None,
        },
        "sources": {
            "applications": {
                "method": "GET",
                "path": "/applications?status=open",
                "bind": "state.applications",
                "refresh": ["pocket_open", "manual"],
            },
        },
        "actions": {"approve": {"method": "POST"}, "reject": {"method": "POST"}},
    }


def test_summarize_composed_spec():
    out = spec_ops.summarize_ripple_spec(_composed_spec(), widgets_count=0)
    assert out["has_ripple_spec"] is True
    # Top-level nodes = the dict root's children (the root is just the
    # layout container; its children are what the user sees).
    assert out["ui_node_count"] == 3
    assert out["ui_node_types"] == ["page-header", "grid", "grid"]
    assert out["state_keys"] == [
        "selected_id",
        "pending_proposal",
        "status_counts",
        "queue_total",
        "oldest_waiting",
        "applications",
        "selected",
    ]
    assert out["state_keys_omitted"] == 0
    assert out["sources"] == [
        {
            "key": "applications",
            "method": "GET",
            "path": "/applications?status=open",
            "bind": "state.applications",
        }
    ]
    assert out["action_keys"] == ["approve", "reject"]
    assert out["widgets_count"] == 0


def test_summarize_list_ui_root():
    spec = {"ui": [{"type": "heading"}, {"type": "table"}, "garbage-entry"]}
    out = spec_ops.summarize_ripple_spec(spec)
    assert out["ui_node_count"] == 2
    assert out["ui_node_types"] == ["heading", "table"]


def test_summarize_dict_root_without_children_counts_the_root():
    spec = {"ui": {"id": "n_root0000", "type": "markdown"}}
    out = spec_ops.summarize_ripple_spec(spec)
    assert out["ui_node_count"] == 1
    assert out["ui_node_types"] == ["markdown"]


def test_summarize_caps_state_keys_at_20_with_omitted_count():
    spec = {"state": {f"key_{i:02d}": i for i in range(25)}}
    out = spec_ops.summarize_ripple_spec(spec)
    assert len(out["state_keys"]) == 20
    assert out["state_keys"][0] == "key_00"
    assert out["state_keys_omitted"] == 5


def test_summarize_empty_spec():
    out = spec_ops.summarize_ripple_spec({})
    assert out["has_ripple_spec"] is False
    assert out["ui_node_count"] == 0
    assert out["ui_node_types"] == []
    assert out["state_keys"] == []
    assert out["state_keys_omitted"] == 0
    assert out["sources"] == []
    assert out["action_keys"] == []
    assert out["widgets_count"] == 0


def test_summarize_none_spec():
    out = spec_ops.summarize_ripple_spec(None, widgets_count=4)
    assert out["has_ripple_spec"] is False
    assert out["ui_node_count"] == 0
    assert out["widgets_count"] == 4


def test_summarize_malformed_spec_degrades_gracefully():
    spec = {
        "ui": "not-a-tree",
        "state": ["not", "a", "dict"],
        "sources": ["not-a-dict"],
        "actions": "nope",
    }
    out = spec_ops.summarize_ripple_spec(spec)
    assert out["has_ripple_spec"] is True  # a dict spec exists, even if junk
    assert out["ui_node_count"] == 0
    assert out["ui_node_types"] == []
    assert out["state_keys"] == []
    assert out["sources"] == []
    assert out["action_keys"] == []
    # Not even a dict at the top → same shape as None.
    assert spec_ops.summarize_ripple_spec("garbage")["has_ripple_spec"] is False


def test_summarize_source_row_survives_non_dict_binding():
    spec = {"sources": {"weird": "just-a-string"}}
    out = spec_ops.summarize_ripple_spec(spec)
    assert out["sources"] == [{"key": "weird", "method": None, "path": None, "bind": None}]


def test_summarize_node_without_type_reports_unknown():
    spec = {"ui": {"children": [{"id": "n_notype00"}]}}
    out = spec_ops.summarize_ripple_spec(spec)
    assert out["ui_node_types"] == ["unknown"]
