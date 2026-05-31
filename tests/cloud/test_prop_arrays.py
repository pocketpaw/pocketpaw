# test_prop_arrays.py — unit tests for the closed prop-array allowlist.
# Created: 2026-05-14. Reworked onto the pocketpaw_ee layout from PR #1106.
"""Tests for the closed (widget_type, prop_name) prop-array allowlist."""

from __future__ import annotations

from pocketpaw_ee.cloud.pockets import prop_arrays


def test_chart_data_is_allowed():
    assert prop_arrays.is_allowed("chart", "data")


def test_table_rows_and_columns_allowed():
    assert prop_arrays.is_allowed("table", "rows")
    assert prop_arrays.is_allowed("table", "columns")


def test_kanban_columns_allowed():
    assert prop_arrays.is_allowed("kanban", "columns")


def test_calendar_events_allowed():
    assert prop_arrays.is_allowed("calendar", "events")


def test_checklist_layout_items_allowed():
    assert prop_arrays.is_allowed("checklist-layout", "items")


def test_select_options_allowed():
    assert prop_arrays.is_allowed("select", "options")


def test_form_layout_sections_allowed():
    assert prop_arrays.is_allowed("form-layout", "sections")


def test_random_prop_rejected():
    assert not prop_arrays.is_allowed("chart", "title")
    assert not prop_arrays.is_allowed("stat", "value")
    assert not prop_arrays.is_allowed("chart", "series")


def test_unknown_widget_rejected():
    assert not prop_arrays.is_allowed("frobnicator", "data")


def test_allowed_props_for_known_type():
    assert sorted(prop_arrays.allowed_props_for("table")) == ["columns", "rows"]


def test_allowed_props_for_unknown_returns_empty():
    assert prop_arrays.allowed_props_for("frobnicator") == ()
