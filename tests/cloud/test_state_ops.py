"""Unit tests for the pure state_ops helpers — get/set/append/remove/patch
against a plain dict-of-state. No DB, no events."""

from __future__ import annotations

import copy

import pytest
from pocketpaw_ee.cloud.pockets import state_ops


def _state() -> dict:
    return {
        "filter": "all",
        "user": {"name": "alice", "age": 30},
        "tasks": [
            {"id": "t1", "label": "buy milk", "status": "todo"},
            {"id": "t2", "label": "walk dog", "status": "done"},
            {"id": "t3", "label": "pay bills", "status": "todo"},
        ],
    }


# ---------------------------------------------------------------------------
# get_path
# ---------------------------------------------------------------------------


def test_get_path_top_level():
    assert state_ops.get_path(_state(), "filter") == "all"


def test_get_path_nested_dict():
    assert state_ops.get_path(_state(), "user.name") == "alice"


def test_get_path_array_index():
    assert state_ops.get_path(_state(), "tasks[0].label") == "buy milk"


def test_get_path_deep_array():
    s = {"groups": [{"members": [{"id": "a"}, {"id": "b"}]}]}
    assert state_ops.get_path(s, "groups[0].members[1].id") == "b"


def test_get_path_missing_returns_none():
    assert state_ops.get_path(_state(), "ghost") is None
    assert state_ops.get_path(_state(), "user.email") is None
    assert state_ops.get_path(_state(), "tasks[99].label") is None


def test_get_path_empty_raises():
    with pytest.raises(ValueError):
        state_ops.get_path(_state(), "")


# ---------------------------------------------------------------------------
# set_path
# ---------------------------------------------------------------------------


def test_set_path_writes_top_level_and_returns_old():
    s = _state()
    old = state_ops.set_path(s, "filter", "todo")
    assert old == "all"
    assert s["filter"] == "todo"


def test_set_path_writes_nested_creating_intermediates():
    s = {}
    state_ops.set_path(s, "user.profile.color", "blue")
    assert s == {"user": {"profile": {"color": "blue"}}}


def test_set_path_array_index_in_range():
    s = _state()
    old = state_ops.set_path(s, "tasks[1].status", "todo")
    assert old == "done"
    assert s["tasks"][1]["status"] == "todo"


def test_set_path_array_index_out_of_range_raises():
    s = _state()
    with pytest.raises(ValueError, match="out of range"):
        state_ops.set_path(s, "tasks[99].label", "x")


def test_set_path_through_non_dict_raises():
    s = {"x": "string-not-dict"}
    with pytest.raises(ValueError):
        state_ops.set_path(s, "x[0]", 1)


# ---------------------------------------------------------------------------
# append_path
# ---------------------------------------------------------------------------


def test_append_path_to_existing_list():
    s = _state()
    n = state_ops.append_path(s, "tasks", {"id": "t4", "label": "new", "status": "todo"})
    assert n == 4
    assert s["tasks"][-1]["id"] == "t4"


def test_append_path_creates_list_when_absent():
    s: dict = {}
    state_ops.append_path(s, "tags", "first")
    state_ops.append_path(s, "tags", "second")
    assert s == {"tags": ["first", "second"]}


def test_append_path_to_non_list_raises():
    s = {"filter": "all"}
    with pytest.raises(ValueError, match="non-list"):
        state_ops.append_path(s, "filter", "x")


def test_append_path_with_index_target_raises():
    s = _state()
    with pytest.raises(ValueError, match="must be a key"):
        state_ops.append_path(s, "tasks[0]", "x")


# ---------------------------------------------------------------------------
# remove_path
# ---------------------------------------------------------------------------


def test_remove_path_deletes_dict_key():
    s = _state()
    removed = state_ops.remove_path(s, "filter")
    assert removed == "all"
    assert "filter" not in s


def test_remove_path_pops_list_element():
    s = _state()
    removed = state_ops.remove_path(s, "tasks[1]")
    assert removed["id"] == "t2"
    assert [t["id"] for t in s["tasks"]] == ["t1", "t3"]


def test_remove_path_missing_raises():
    with pytest.raises(ValueError):
        state_ops.remove_path(_state(), "ghost")


def test_remove_path_out_of_range_raises():
    with pytest.raises(ValueError, match="out of range"):
        state_ops.remove_path(_state(), "tasks[99]")


# ---------------------------------------------------------------------------
# patch (shallow top-level merge)
# ---------------------------------------------------------------------------


def test_patch_overwrites_top_level_returns_previous():
    s = _state()
    prev = state_ops.patch(s, {"filter": "done", "newkey": 42})
    assert prev == {"filter": "all", "newkey": None}
    assert s["filter"] == "done"
    assert s["newkey"] == 42
    # Untouched keys preserved.
    assert s["user"]["name"] == "alice"


def test_patch_does_not_deep_merge():
    """patch is intentionally shallow — nested dicts get replaced, not
    merged. Callers wanting deep merge should use multiple set_path
    calls instead."""
    s = _state()
    state_ops.patch(s, {"user": {"name": "bob"}})  # No age field
    assert s["user"] == {"name": "bob"}  # age dropped, not merged


def test_patch_non_dict_raises():
    with pytest.raises(ValueError):
        state_ops.patch(_state(), [1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Path parser edge cases
# ---------------------------------------------------------------------------


def test_path_with_multiple_indices_in_one_segment():
    s = {"grid": [[1, 2], [3, 4]]}
    assert state_ops.get_path(s, "grid[1][0]") == 3
    state_ops.set_path(s, "grid[1][0]", 99)
    assert s["grid"][1][0] == 99


def test_malformed_path_raises():
    with pytest.raises(ValueError):
        state_ops.get_path({}, "foo[]")
    with pytest.raises(ValueError):
        state_ops.get_path({}, "[0]")


# ---------------------------------------------------------------------------
# Immutability check — operations are in-place; copies stay untouched
# ---------------------------------------------------------------------------


def test_set_path_does_not_affect_deep_copy():
    s = _state()
    snapshot = copy.deepcopy(s)
    state_ops.set_path(s, "tasks[0].status", "done")
    assert snapshot["tasks"][0]["status"] == "todo"
