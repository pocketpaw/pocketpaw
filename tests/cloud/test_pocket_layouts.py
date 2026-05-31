# tests/cloud/test_pocket_layouts.py — Cluster B Sub-PR #3.
# Created: 2026-04-19 — Unit coverage for the YAML export/parse helpers
# and the in-process user-template store. These tests are pure — no
# Beanie, no MongoDB, no FastAPI. The router-level tests in
# test_pocket_layout_routes.py exercise the HTTP surface against the
# same store with ``pockets_service.get`` monkeypatched.
#
# What this pins:
#   1. YAML export is deterministic (same input → byte-identical output).
#   2. YAML export carries the required metadata block + spec.
#   3. parse_layout_yaml rejects malformed YAML with a ValueError that
#      is safe to surface to the caller.
#   4. parse_layout_yaml rejects a missing/invalid `spec` block.
#   5. Round-trip: export → parse_layout_yaml gives back the original
#      ripple_spec dict.
#   6. UserTemplateStore scoping: one workspace's templates do not
#      leak into another's list.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud.pockets.layouts import (
    UserPocketTemplate,
    UserTemplateStore,
    export_layout_yaml,
    parse_layout_yaml,
)


def _base_spec() -> dict[str, Any]:
    return {
        "widgets": [
            {"id": "w1", "type": "pipeline", "title": "Pipeline"},
            {"id": "w2", "type": "leads", "title": "Leads"},
        ],
        "layout": "2-col",
    }


# ---------------------------------------------------------------------------
# 1. Determinism.
# ---------------------------------------------------------------------------


class TestExportDeterminism:
    def test_same_input_yields_byte_identical_yaml_excluding_timestamp(self) -> None:
        """``exportedAt`` changes every call, but every other key is
        sorted and stable. Strip the timestamp line, then expect
        equality.
        """

        y1 = export_layout_yaml(
            pocket_id="p-1",
            name="Dashboard",
            description="",
            category="custom",
            ripple_spec=_base_spec(),
            widgets=[],
        )
        y2 = export_layout_yaml(
            pocket_id="p-1",
            name="Dashboard",
            description="",
            category="custom",
            ripple_spec=_base_spec(),
            widgets=[],
        )

        def strip_ts(s: str) -> list[str]:
            return [ln for ln in s.splitlines() if "exportedAt" not in ln]

        assert strip_ts(y1) == strip_ts(y2)


# ---------------------------------------------------------------------------
# 2. Required metadata + spec presence.
# ---------------------------------------------------------------------------


class TestExportShape:
    def test_yaml_carries_required_top_level_keys(self) -> None:
        y = export_layout_yaml(
            pocket_id="p-1",
            name="Dashboard",
            description="desc",
            category="custom",
            ripple_spec=_base_spec(),
            widgets=[],
        )
        assert "apiVersion: pocketpaw.io/v1" in y
        assert "kind: PocketLayout" in y
        assert "name: Dashboard" in y
        assert "sourcePocketId: p-1" in y

    def test_empty_ripple_spec_falls_back_to_widgets_mirror(self) -> None:
        y = export_layout_yaml(
            pocket_id="p-2",
            name="Blank",
            description="",
            category="custom",
            ripple_spec=None,
            widgets=[{"id": "w-legacy", "type": "metric"}],
        )
        parsed = parse_layout_yaml(y)
        assert parsed["widgets"][0]["id"] == "w-legacy"


# ---------------------------------------------------------------------------
# 3 + 4. parse_layout_yaml validation.
# ---------------------------------------------------------------------------


class TestParseValidation:
    def test_malformed_yaml_raises_valueerror(self) -> None:
        with pytest.raises(ValueError) as exc:
            parse_layout_yaml("name: : : nope")
        assert "Invalid YAML" in str(exc.value)

    def test_missing_spec_raises_valueerror(self) -> None:
        with pytest.raises(ValueError) as exc:
            parse_layout_yaml("kind: PocketLayout\nname: no-spec")
        assert "spec" in str(exc.value)

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            parse_layout_yaml("kind: OtherThing\nspec: {}")
        assert "Unsupported template kind" in str(exc.value)

    def test_list_root_is_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            parse_layout_yaml("- one\n- two\n")
        assert "mapping" in str(exc.value)


# ---------------------------------------------------------------------------
# 5. Round-trip.
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_export_then_parse_recovers_the_spec(self) -> None:
        original = _base_spec()
        y = export_layout_yaml(
            pocket_id="p-1",
            name="Dashboard",
            description="",
            category="custom",
            ripple_spec=original,
            widgets=[],
        )
        recovered = parse_layout_yaml(y)
        assert recovered == original


# ---------------------------------------------------------------------------
# 6. Store scoping.
# ---------------------------------------------------------------------------


class TestStoreScoping:
    def test_templates_do_not_leak_between_workspaces(self) -> None:
        store = UserTemplateStore()
        store.save(
            UserPocketTemplate(
                id="tpl-a",
                workspace_id="ws-alpha",
                owner_id="alice",
                name="Alpha dashboard",
                description="",
                category="custom",
                spec=_base_spec(),
            ),
        )
        store.save(
            UserPocketTemplate(
                id="tpl-b",
                workspace_id="ws-beta",
                owner_id="carol",
                name="Beta dashboard",
                description="",
                category="custom",
                spec=_base_spec(),
            ),
        )

        alpha = store.list_for_workspace("ws-alpha")
        beta = store.list_for_workspace("ws-beta")

        assert [t.id for t in alpha] == ["tpl-a"]
        assert [t.id for t in beta] == ["tpl-b"]
