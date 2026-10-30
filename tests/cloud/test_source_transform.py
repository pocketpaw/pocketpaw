# tests/cloud/test_source_transform.py — feat/connector-as-pocket-backend.
# Created: 2026-06-12 — unit coverage for the pure source-transform function
# (`_source_transform.apply_transform`). The transform shapes a raw fetch
# result (http / sense / connector) into the widget's display shape before it
# binds to pocket state. No network, no eval — this is a pure function, so it
# is exhaustively unit-testable here.
#
# What this pins:
#   - No transform (None / {}) -> the raw value, unchanged (back-compat).
#   - select drills a dotted path (incl. list-index segments); missing -> None.
#   - map reshapes a list row-by-row: copy-by-path, values-lookup (+default),
#     const, and missing `from` -> None.
#   - select + map compose.
#   - map over a non-list -> [] (no crash).
#   - Anything outside the v1 grammar raises TransformError.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets._source_transform import TransformError, apply_transform

# ---------------------------------------------------------------------------
# No transform — raw passthrough (today's behavior)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", [None, {}])
def test_no_transform_returns_raw_unchanged(spec):
    raw = [{"id": 1}, {"id": 2}]
    assert apply_transform(raw, spec) is raw


def test_no_transform_passes_scalars_and_dicts():
    assert apply_transform({"a": 1}, None) == {"a": 1}
    assert apply_transform(42, {}) == 42


# ---------------------------------------------------------------------------
# select — drill a dotted path
# ---------------------------------------------------------------------------


def test_select_drills_to_nested_list():
    raw = {"data": {"applications": [{"id": "a1"}, {"id": "a2"}]}}
    assert apply_transform(raw, {"select": "data.applications"}) == [
        {"id": "a1"},
        {"id": "a2"},
    ]


def test_select_missing_path_is_none():
    assert apply_transform({"data": {}}, {"select": "data.applications"}) is None
    assert apply_transform({"a": 1}, {"select": "x.y.z"}) is None


def test_select_indexes_a_list_segment():
    raw = {"items": [{"name": "first"}, {"name": "second"}]}
    assert apply_transform(raw, {"select": "items.1.name"}) == "second"


def test_select_empty_path_returns_raw():
    raw = {"a": 1}
    assert apply_transform(raw, {"select": ""}) == {"a": 1}


# ---------------------------------------------------------------------------
# map — reshape a list row by row
# ---------------------------------------------------------------------------


def test_map_copy_by_path():
    raw = [{"fullName": "Ada"}, {"fullName": "Bo"}]
    out = apply_transform(raw, {"map": [{"to": "applicant", "from": "fullName"}]})
    assert out == [{"applicant": "Ada"}, {"applicant": "Bo"}]


def test_map_copy_by_dotted_path():
    raw = [{"profile": {"name": "Ada"}}]
    out = apply_transform(raw, {"map": [{"to": "name", "from": "profile.name"}]})
    assert out == [{"name": "Ada"}]


def test_map_values_lookup_with_default():
    raw = [{"status": "new"}, {"status": "rejected"}, {"status": "weird"}]
    spec = {
        "map": [
            {
                "to": "variant",
                "from": "status",
                "values": {"new": "warning", "rejected": "destructive"},
                "default": "muted",
            }
        ]
    }
    out = apply_transform(raw, spec)
    assert out == [{"variant": "warning"}, {"variant": "destructive"}, {"variant": "muted"}]


def test_map_values_lookup_no_default_is_none():
    raw = [{"status": "unknown"}]
    out = apply_transform(
        raw, {"map": [{"to": "v", "from": "status", "values": {"new": "warning"}}]}
    )
    assert out == [{"v": None}]


def test_map_const():
    raw = [{"id": "x1"}, {"id": "x2"}]
    out = apply_transform(raw, {"map": [{"to": "source", "const": "snctm-admin"}]})
    assert out == [{"source": "snctm-admin"}, {"source": "snctm-admin"}]


def test_map_missing_from_is_none_not_crash():
    raw = [{"id": "x1"}]
    out = apply_transform(raw, {"map": [{"to": "name", "from": "nope"}]})
    assert out == [{"name": None}]


def test_map_multiple_fields_compose_per_row():
    raw = [{"fullName": "Ada", "status": "new", "id": "x1"}]
    spec = {
        "map": [
            {"to": "applicant", "from": "fullName"},
            {
                "to": "status_variant",
                "from": "status",
                "values": {"new": "warning", "rejected": "destructive"},
            },
            {"to": "id", "from": "id"},
            {"to": "source", "const": "snctm-admin"},
        ]
    }
    out = apply_transform(raw, spec)
    assert out == [
        {
            "applicant": "Ada",
            "status_variant": "warning",
            "id": "x1",
            "source": "snctm-admin",
        }
    ]


def test_map_over_non_list_is_empty():
    # The post-select value is a dict, not a list — map yields [] (no crash).
    assert apply_transform({"x": 1}, {"map": [{"to": "a", "from": "b"}]}) == []
    assert apply_transform(None, {"map": [{"to": "a", "from": "b"}]}) == []


# ---------------------------------------------------------------------------
# select + map compose
# ---------------------------------------------------------------------------


def test_select_then_map_compose():
    raw = {
        "data": {
            "applications": [
                {"fullName": "Ada", "status": "new", "id": "x1"},
                {"fullName": "Bo", "status": "rejected", "id": "x2"},
            ]
        }
    }
    spec = {
        "select": "data.applications",
        "map": [
            {"to": "applicant", "from": "fullName"},
            {
                "to": "status_variant",
                "from": "status",
                "values": {"new": "warning", "rejected": "destructive"},
                "default": "muted",
            },
            {"to": "id", "from": "id"},
            {"to": "source", "const": "snctm-admin"},
        ],
    }
    out = apply_transform(raw, spec)
    assert out == [
        {"applicant": "Ada", "status_variant": "warning", "id": "x1", "source": "snctm-admin"},
        {
            "applicant": "Bo",
            "status_variant": "destructive",
            "id": "x2",
            "source": "snctm-admin",
        },
    ]


# ---------------------------------------------------------------------------
# Grammar rejection — anything outside the v1 spec raises TransformError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_spec",
    [
        {"unknown_key": 1},  # unknown top-level key
        {"select": 123},  # select must be a string
        {"map": "not-a-list"},  # map must be a list
        {"map": [{"from": "b"}]},  # field needs a 'to'
        {"map": [{"to": ""}]},  # empty 'to'
        {"map": [{"to": "a"}]},  # field needs 'from' or 'const'
        {"map": [{"to": "a", "from": "b", "const": 1}]},  # not both
        {"map": [{"to": "a", "const": 1, "values": {}}]},  # values only on 'from'
        {"map": [{"to": "a", "from": 5}]},  # 'from' must be a string
        {"map": [{"to": "a", "from": "b", "values": "x"}]},  # values must be an object
        {"map": [{"to": "a", "from": "b", "bogus": 1}]},  # unknown field key
        {"map": ["not-an-object"]},  # field must be an object
    ],
)
def test_invalid_spec_raises_transform_error(bad_spec):
    with pytest.raises(TransformError):
        apply_transform([{"b": 1}], bad_spec)


def test_non_dict_spec_raises():
    with pytest.raises(TransformError):
        apply_transform([], "not-a-dict")
