# tests/test_ripple_required_props.py
# Created: 2026-05-31 (constraint-zone enforcer) — OSS-core tests for the
# required-prop "HARD constraint" walker in `pocketpaw.ripple.manifest`:
# `required_props_from_manifest` (the manifest reader) and
# `validate_required_props` (the spec walker). The rippleSpec analogue of
# the genesis "Constraint Zones" 🔒 HARD `required_fields` zone — a node
# that omits a prop the manifest marks `required: true` renders empty even
# though every existing gate (catalog, action-verb) passes.
#
# No EE imports, so this file runs in the `Test (OSS-only)` CI scope. The
# EE strict/logged wiring is covered in
# `tests/cloud/test_ripple_required_props_gate.py`.
"""Tests for the Ripple required-prop gate (constraint-zone HARD enforcement)."""

from __future__ import annotations

from pocketpaw.ripple.manifest import (
    required_props_from_manifest,
    validate_required_props,
)

# A representative manifest fragment: `chart` requires `data`, `stat` requires
# `value`, `table` requires both `columns` and `rows`, `input` requires `value`
# (the bindable case), and `text` requires nothing (creative-zone widget).
MANIFEST = {
    "schema": "ripple.manifest/v1",
    "widgets": [
        {
            "type": "chart",
            "props": {
                "data": {"type": "array", "required": True},
                "title": {"type": "string"},
            },
        },
        {
            "type": "stat",
            "props": {
                "value": {"type": "number", "required": True},
                "label": {"type": "string"},
            },
        },
        {
            "type": "table",
            "props": {
                "columns": {"type": "array", "required": True},
                "rows": {"type": "array", "required": True},
            },
        },
        {
            "type": "input",
            "props": {"value": {"type": "string", "required": True}},
        },
        {
            "type": "text",
            "props": {"content": {"type": "string"}},
        },
        {"type": "flex", "props": {}},
    ],
}

# The extracted required-prop map, for tests that pass the map form directly.
REQUIRED = required_props_from_manifest(MANIFEST)


# ---------------------------------------------------------------------------
# required_props_from_manifest
# ---------------------------------------------------------------------------


class TestRequiredPropsFromManifest:
    def test_extracts_required_props_per_type(self) -> None:
        out = required_props_from_manifest(MANIFEST)
        assert out["chart"] == ["data"]
        assert out["stat"] == ["value"]
        assert out["input"] == ["value"]

    def test_multiple_required_props_preserve_declaration_order(self) -> None:
        assert required_props_from_manifest(MANIFEST)["table"] == ["columns", "rows"]

    def test_widgets_without_required_props_are_omitted(self) -> None:
        out = required_props_from_manifest(MANIFEST)
        # `text` and `flex` have no required props → not keys at all, so a
        # membership test doubles as "does this type have anything to enforce".
        assert "text" not in out
        assert "flex" not in out

    def test_empty_manifest_returns_empty_map(self) -> None:
        assert required_props_from_manifest({}) == {}
        assert required_props_from_manifest({"widgets": []}) == {}

    def test_malformed_widget_entries_skipped(self) -> None:
        manifest = {
            "widgets": [
                {"type": "ok", "props": {"x": {"required": True}}},
                {"props": {"y": {"required": True}}},  # no type
                {"type": "noprops"},  # no props
                {"type": "bad-props", "props": "not-a-dict"},
                "not-a-dict",
            ]
        }
        out = required_props_from_manifest(manifest)
        assert out == {"ok": ["x"]}


# ---------------------------------------------------------------------------
# validate_required_props — happy paths
# ---------------------------------------------------------------------------


class TestValidateRequiredPropsClean:
    def test_non_dict_spec_returns_empty(self) -> None:
        assert validate_required_props(None, MANIFEST) == []
        assert validate_required_props("nope", MANIFEST) == []  # type: ignore[arg-type]
        assert validate_required_props([], MANIFEST) == []  # type: ignore[arg-type]

    def test_all_required_props_present_passes(self) -> None:
        spec = {
            "ui": {
                "type": "flex",
                "children": [
                    {"type": "stat", "props": {"label": "Revenue", "value": 42}},
                    {"type": "chart", "props": {"data": [1, 2, 3]}},
                ],
            }
        }
        assert validate_required_props(spec, MANIFEST) == []

    def test_bound_expression_counts_as_present(self) -> None:
        # A required prop fed by a `{...}` expression is present — the gate
        # checks key-presence, not value-resolution.
        spec = {"ui": {"type": "chart", "props": {"data": "{state.series}"}}}
        assert validate_required_props(spec, MANIFEST) == []

    def test_falsy_but_present_values_pass(self) -> None:
        # Empty list / 0 / "" / False are deliberate choices, not omissions.
        spec = {
            "ui": {
                "type": "flex",
                "children": [
                    {"type": "chart", "props": {"data": []}},
                    {"type": "stat", "props": {"value": 0}},
                ],
            }
        }
        assert validate_required_props(spec, MANIFEST) == []

    def test_unknown_widget_type_not_flagged_here(self) -> None:
        # An unknown `type` is the catalog gate's concern — the required-prop
        # walker only checks types that declare required props, so it never
        # double-flags an unknown widget.
        spec = {"ui": {"type": "revenue-card", "props": {}}}
        assert validate_required_props(spec, MANIFEST) == []

    def test_widget_with_no_required_props_never_flagged(self) -> None:
        spec = {"ui": {"type": "text", "props": {}}}
        assert validate_required_props(spec, MANIFEST) == []

    def test_empty_required_map_short_circuits(self) -> None:
        spec = {"ui": {"type": "chart", "props": {}}}
        assert validate_required_props(spec, {}) == []
        assert validate_required_props(spec, {"widgets": []}) == []


# ---------------------------------------------------------------------------
# validate_required_props — violation paths
# ---------------------------------------------------------------------------


class TestValidateRequiredPropsViolations:
    def test_missing_required_prop_flagged(self) -> None:
        spec = {"ui": {"type": "chart", "props": {"title": "Sales"}}}
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 1
        assert issues[0]["type"] == "chart"
        assert issues[0]["missing"] == ["data"]
        assert issues[0]["required"] == ["data"]
        assert issues[0]["path"] == "ui"

    def test_missing_props_entirely_flagged(self) -> None:
        # No `props` key at all → the single required prop is missing.
        spec = {"ui": {"type": "stat"}}
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 1
        assert issues[0]["missing"] == ["value"]

    def test_explicit_null_is_missing(self) -> None:
        spec = {"ui": {"type": "chart", "props": {"data": None}}}
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 1
        assert issues[0]["missing"] == ["data"]

    def test_partial_multi_required_reports_only_missing(self) -> None:
        # `table` requires columns + rows; supply only columns.
        spec = {"ui": {"type": "table", "props": {"columns": ["a", "b"]}}}
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 1
        assert issues[0]["missing"] == ["rows"]
        assert issues[0]["required"] == ["columns", "rows"]

    def test_nested_path_is_reported(self) -> None:
        spec = {
            "ui": {
                "type": "flex",
                "children": [
                    {"type": "text", "props": {"content": "ok"}},
                    {"type": "chart", "props": {}},
                ],
            }
        }
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 1
        assert issues[0]["path"] == "ui.children[1]"
        assert issues[0]["type"] == "chart"

    def test_multiple_violations_collected(self) -> None:
        spec = {
            "ui": {
                "type": "flex",
                "children": [
                    {"type": "chart", "props": {}},
                    {"type": "stat", "props": {}},
                    {"type": "table", "props": {}},
                ],
            }
        }
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 3
        by_type = {i["type"]: i["missing"] for i in issues}
        assert by_type["chart"] == ["data"]
        assert by_type["stat"] == ["value"]
        assert by_type["table"] == ["columns", "rows"]

    def test_walks_else_children_branch(self) -> None:
        # `if` nodes carry an `else_children` collection — the walker must
        # descend it too (mirrors the catalog walk).
        spec = {
            "ui": {
                "type": "if",
                "condition": "{state.ok}",
                "children": [{"type": "stat", "props": {"value": 1}}],
                "else_children": [{"type": "chart", "props": {}}],
            }
        }
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 1
        assert issues[0]["path"] == "ui.else_children[0]"
        assert issues[0]["type"] == "chart"

    def test_accepts_unwrapped_subtree(self) -> None:
        # The walker tolerates a bare node (no `ui` wrapper) the same way the
        # catalog walk does — used by the per-widget `spec` gate.
        spec = {"type": "chart", "props": {}}
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 1
        assert issues[0]["type"] == "chart"


# ---------------------------------------------------------------------------
# Bind-rescue: a single-required-prop input fed by node-level `bind`
# ---------------------------------------------------------------------------


class TestBindRescue:
    def test_node_level_bind_satisfies_single_required_prop(self) -> None:
        # `input` requires `value`; a node-level `bind` populates it at render
        # time (the widget's bind contract target), so it must not be flagged.
        spec = {"ui": {"type": "input", "bind": "state.email"}}
        assert validate_required_props(spec, MANIFEST) == []

    def test_bind_does_not_rescue_multi_required_widget(self) -> None:
        # `table` has two required props — a single `bind` can't supply both,
        # so the rescue does NOT apply and the omission is still flagged.
        spec = {"ui": {"type": "table", "bind": "state.sel", "props": {"columns": ["a"]}}}
        issues = validate_required_props(spec, MANIFEST)
        assert len(issues) == 1
        assert issues[0]["missing"] == ["rows"]


# ---------------------------------------------------------------------------
# Map form vs manifest form — both inputs produce identical results
# ---------------------------------------------------------------------------


class TestInputForms:
    def test_map_form_matches_manifest_form(self) -> None:
        spec = {"ui": {"type": "chart", "props": {}}}
        from_manifest = validate_required_props(spec, MANIFEST)
        from_map = validate_required_props(spec, REQUIRED)
        assert from_manifest == from_map
        assert from_map[0]["missing"] == ["data"]
