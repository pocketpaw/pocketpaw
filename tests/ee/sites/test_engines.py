# tests/ee/sites/test_engines.py — unit tests for the canonical engine-capability
# module (ee/pocketpaw_ee/sites/engines.py).
#
# Created 2026-07-10 (HE-2). Covers all three modeled engines (ripple / svelte /
# html) plus the empty / missing / unknown-engine fallback contract that the
# scattered ``pocket.get("engine") or "ripple"`` call sites relied on.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites import engines


class TestIsSourceEngine:
    def test_svelte_is_source(self) -> None:
        assert engines.is_source_engine("svelte") is True

    def test_html_is_source(self) -> None:
        assert engines.is_source_engine("html") is True

    def test_ripple_is_not_source(self) -> None:
        assert engines.is_source_engine("ripple") is False

    @pytest.mark.parametrize("value", [None, "", "  ", "nope", "SVELTE"])
    def test_empty_missing_unknown_default_to_ripple_semantics(self, value: str | None) -> None:
        # Empty / missing / unknown (incl. wrong case) → treated as ripple → not a
        # source engine. Preserves the historical ``engine or "ripple"`` behaviour.
        assert engines.is_source_engine(value) is False


class TestContentKey:
    def test_svelte_reads_source(self) -> None:
        assert engines.content_key("svelte") == "source"

    def test_html_reads_source(self) -> None:
        assert engines.content_key("html") == "source"

    def test_ripple_reads_ripplespec(self) -> None:
        assert engines.content_key("ripple") == "rippleSpec"

    @pytest.mark.parametrize("value", [None, "", "unknown"])
    def test_default_reads_ripplespec(self, value: str | None) -> None:
        assert engines.content_key(value) == "rippleSpec"


class TestNeedsNodeBuild:
    def test_ripple_needs_build(self) -> None:
        assert engines.needs_node_build("ripple") is True

    def test_svelte_needs_build(self) -> None:
        assert engines.needs_node_build("svelte") is True

    def test_html_skips_build(self) -> None:
        assert engines.needs_node_build("html") is False

    @pytest.mark.parametrize("value", [None, "", "unknown"])
    def test_default_needs_build(self, value: str | None) -> None:
        # Unknown → ripple → runs the full build (the safe, least-capable default).
        assert engines.needs_node_build(value) is True


class TestStaticOutputRel:
    def test_ripple_output(self) -> None:
        assert engines.static_output_rel("ripple") == ".svelte-kit/cloudflare"

    def test_svelte_output(self) -> None:
        assert engines.static_output_rel("svelte") == ".svelte-kit/cloudflare"

    def test_html_output_is_project_root(self) -> None:
        assert engines.static_output_rel("html") == "."

    @pytest.mark.parametrize("value", [None, "", "unknown"])
    def test_default_output_matches_ripple(self, value: str | None) -> None:
        assert engines.static_output_rel(value) == ".svelte-kit/cloudflare"


class TestNormalizeEngine:
    @pytest.mark.parametrize("value", ["ripple", "svelte", "html"])
    def test_known_engines_pass_through(self, value: str) -> None:
        assert engines.normalize_engine(value) == value

    @pytest.mark.parametrize("value", [None, "", "  ", "nope", "Svelte", "RIPPLE"])
    def test_empty_missing_unknown_fall_back_to_ripple(self, value: str | None) -> None:
        assert engines.normalize_engine(value) == "ripple"


def test_source_and_build_are_distinct_capabilities() -> None:
    # The whole point of the module: html is source-map-backed but needs NO build.
    # Guard against a future refactor collapsing the two into one flag.
    assert engines.is_source_engine("html") is True
    assert engines.needs_node_build("html") is False
    # ... while svelte is both source-map-backed AND build-requiring.
    assert engines.is_source_engine("svelte") is True
    assert engines.needs_node_build("svelte") is True
