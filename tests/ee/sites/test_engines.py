# tests/ee/sites/test_engines.py — unit tests for the canonical engine-capability
# module (ee/pocketpaw_ee/sites/engines.py).
#
# Created 2026-07-10 (HE-2). Covers all three modeled engines (ripple / svelte /
# html) plus the empty / missing / unknown-engine fallback contract that the
# scattered ``pocket.get("engine") or "ripple"`` call sites relied on.
#
# Edited 2026-08-07 (RX-1 — the react engine): added react to every predicate's
# coverage and to the fallback parametrizations, plus a class for the new fifth
# predicate ``emits_server_worker``. The load-bearing new case is react's
# combination — source-map-backed AND build-requiring AND server-less — because it
# is the one that proves ``needs_node_build`` and ``emits_server_worker`` are not
# the same question. The existing three engines' assertions are untouched, so a
# regression in ripple/svelte/html still fails here.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites import engines


class TestIsSourceEngine:
    def test_svelte_is_source(self) -> None:
        assert engines.is_source_engine("svelte") is True

    def test_html_is_source(self) -> None:
        assert engines.is_source_engine("html") is True

    def test_react_is_source(self) -> None:
        assert engines.is_source_engine("react") is True

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

    def test_react_reads_source(self) -> None:
        assert engines.content_key("react") == "source"

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

    def test_react_needs_build(self) -> None:
        # A react site is an SSG, not a raw tree: `bun install` + a Vite build (client
        # bundle, SSR bundle, prerender pass) must run before there is anything to deploy.
        assert engines.needs_node_build("react") is True

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

    def test_react_output_is_vite_dist(self) -> None:
        assert engines.static_output_rel("react") == "dist"

    def test_react_output_is_not_the_sveltekit_path(self) -> None:
        # Nothing on the react track produces .svelte-kit/cloudflare. Were react to
        # fall through to the ripple default, the deploy would upload an empty dir.
        assert engines.static_output_rel("react") != ".svelte-kit/cloudflare"

    @pytest.mark.parametrize("value", [None, "", "unknown"])
    def test_default_output_matches_ripple(self, value: str | None) -> None:
        assert engines.static_output_rel(value) == ".svelte-kit/cloudflare"


class TestNormalizeEngine:
    @pytest.mark.parametrize("value", ["ripple", "svelte", "html", "react"])
    def test_known_engines_pass_through(self, value: str) -> None:
        assert engines.normalize_engine(value) == value

    @pytest.mark.parametrize(
        "value", [None, "", "  ", "nope", "Svelte", "RIPPLE", "React", "reactjs"]
    )
    def test_empty_missing_unknown_fall_back_to_ripple(self, value: str | None) -> None:
        assert engines.normalize_engine(value) == "ripple"


class TestEmitsServerWorker:
    def test_ripple_emits_a_worker(self) -> None:
        assert engines.emits_server_worker("ripple") is True

    def test_svelte_emits_a_worker(self) -> None:
        assert engines.emits_server_worker("svelte") is True

    def test_html_is_assets_only(self) -> None:
        assert engines.emits_server_worker("html") is False

    def test_react_is_assets_only(self) -> None:
        # The one that matters: react runs a full Node build and STILL has no server
        # entry, because that build prerenders to a static dist/.
        assert engines.emits_server_worker("react") is False

    @pytest.mark.parametrize("value", [None, "", "unknown"])
    def test_default_emits_a_worker(self, value: str | None) -> None:
        # Unknown → ripple → the SvelteKit worker config (the safe, least-capable
        # default: it never skips a guard the real engine would have run).
        assert engines.emits_server_worker(value) is True


def test_source_and_build_are_distinct_capabilities() -> None:
    # The whole point of the module: html is source-map-backed but needs NO build.
    # Guard against a future refactor collapsing the two into one flag.
    assert engines.is_source_engine("html") is True
    assert engines.needs_node_build("html") is False
    # ... while svelte is both source-map-backed AND build-requiring.
    assert engines.is_source_engine("svelte") is True
    assert engines.needs_node_build("svelte") is True


def test_build_and_server_worker_are_distinct_capabilities() -> None:
    # RX-1's reason for a fifth predicate. Before react, "runs a Node build" and
    # "emits a _worker.js" picked out the SAME set of engines, so a caller could use
    # either name and be accidentally right. react separates them: it builds, and it
    # emits no server entry.
    assert engines.needs_node_build("react") is True
    assert engines.emits_server_worker("react") is False
    # html reaches the same assets-only shape from the other direction — no build.
    assert engines.needs_node_build("html") is False
    assert engines.emits_server_worker("html") is False
    # ... while svelte both builds AND emits a worker, which is why the two questions
    # were indistinguishable until now.
    assert engines.needs_node_build("svelte") is True
    assert engines.emits_server_worker("svelte") is True


def test_react_combines_all_three_capabilities_uniquely() -> None:
    # react is the first engine that is source-map-backed AND build-requiring AND
    # server-less. Any future refactor that collapses these predicates into fewer
    # flags has to break one of these three lines.
    assert engines.is_source_engine("react") is True
    assert engines.needs_node_build("react") is True
    assert engines.emits_server_worker("react") is False
