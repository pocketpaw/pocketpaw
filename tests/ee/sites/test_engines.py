# tests/ee/sites/test_engines.py — unit tests for the canonical engine-capability
# module (ee/pocketpaw_ee/sites/engines.py). Also pins the per-engine
# ``engine_capabilities`` wire shape, ``site_has_database``, the unknown-engine
# warning, and an AST scan that fails on any engine literal in the backend source
# that is not in ``engines.KNOWN_ENGINES``.
#
# Created 2026-07-10 (HE-2). Covers all three modeled engines (ripple / svelte /
# html) plus the empty / missing / unknown-engine fallback contract that the
# scattered ``pocket.get("engine") or "ripple"`` call sites relied on.
#
# Edited 2026-08-10 (SL-1 — the static svelte landing lane): added
# TestResolveStaticOutputRel + TestResolveEmitsServerWorker for the two new
# artifact-resolving predicates. These use REAL tmp_path dirs rather than mocks,
# because reading the filesystem is the entire behaviour under test — a mocked
# filesystem here would test the mock.
#
# Two cases carry most of the weight. `test_build_wins_when_both_exist` pins the probe
# ORDER, so a project dir built both before and after SL-1 cannot have its stale
# adapter-cloudflare tree shadow the current build. `test_a_worker_DIRECTORY_still_counts`
# pins existence-over-is_file, because adapter-cloudflare emits `_worker.js` as a
# DIRECTORY once an app is large enough — an is_file() check would report "no worker"
# for a big dynamic site and deploy it assets-only, replacing a working site with a
# broken one.
#
# Mutation-verified: tests/mutations/sites_sl1_resolvers.json, 5/5 caught. This suite
# is also the deterministic gate for SL-1, because tests/ee/sites at large is ~89 red
# and order-dependent on dev independently of this change.
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


class TestResolveStaticOutputRel:
    """SL-1 — the svelte track spans two adapters, so the output dir is a fact about
    the ARTIFACT rather than about the engine string. These are deliberately real
    temp dirs: the whole point of the resolver is that it reads the filesystem, so a
    mocked one would test the mock."""

    def test_static_svelte_resolves_to_build(self, tmp_path) -> None:
        (tmp_path / "build").mkdir()
        assert engines.resolve_static_output_rel(tmp_path, "svelte") == "build"

    def test_dynamic_svelte_resolves_to_the_adapter_cloudflare_tree(self, tmp_path) -> None:
        (tmp_path / ".svelte-kit" / "cloudflare").mkdir(parents=True)
        assert engines.resolve_static_output_rel(tmp_path, "svelte") == ".svelte-kit/cloudflare"

    def test_build_wins_when_both_exist(self, tmp_path) -> None:
        # THE ORDER TEST. A project dir that has been built before AND after SL-1
        # carries both trees; the stale adapter-cloudflare one must not shadow the
        # current build. Reversing the probe order silently serves the old artifact,
        # which is the failure this asserts against.
        (tmp_path / "build").mkdir()
        (tmp_path / ".svelte-kit" / "cloudflare").mkdir(parents=True)
        assert engines.resolve_static_output_rel(tmp_path, "svelte") == "build"

    def test_neither_present_falls_back_to_the_nominal_value(self, tmp_path) -> None:
        # Total, never raising — the caller then reports a missing build against a
        # concrete path, which is a truer error than the predicate refusing to decide.
        assert engines.resolve_static_output_rel(tmp_path, "svelte") == ".svelte-kit/cloudflare"

    @pytest.mark.parametrize(
        ("engine", "expected"),
        [("ripple", ".svelte-kit/cloudflare"), ("html", "."), ("react", "dist")],
    )
    def test_non_svelte_engines_ignore_the_filesystem(
        self, tmp_path, engine: str, expected: str
    ) -> None:
        # A decoy `build/` dir is present for every one of them. Only svelte probes,
        # so ripple/html/react must return their nominal value regardless — otherwise
        # this change would silently repoint react and html deploys too.
        (tmp_path / "build").mkdir()
        assert engines.resolve_static_output_rel(tmp_path, engine) == expected

    def test_accepts_a_str_path(self, tmp_path) -> None:
        # Call sites pass both str and Path (``build.project_dir`` is a str).
        (tmp_path / "build").mkdir()
        assert engines.resolve_static_output_rel(str(tmp_path), "svelte") == "build"


class TestResolveEmitsServerWorker:
    """The deploy-shape half. Getting this wrong points ``main`` at a worker that does
    not exist, which fails the deploy outright."""

    def test_static_svelte_emits_no_worker(self, tmp_path) -> None:
        (tmp_path / "build").mkdir()
        assert engines.resolve_emits_server_worker(tmp_path, "svelte") is False

    def test_dynamic_svelte_emits_a_worker(self, tmp_path) -> None:
        out = tmp_path / ".svelte-kit" / "cloudflare"
        out.mkdir(parents=True)
        (out / "_worker.js").write_text("export default {}")
        assert engines.resolve_emits_server_worker(tmp_path, "svelte") is True

    def test_a_worker_DIRECTORY_still_counts(self, tmp_path) -> None:
        # adapter-cloudflare emits _worker.js as a DIRECTORY once an app is large
        # enough (_worker.js/chunks/0.js). An is_file() check would report "no worker"
        # for a big dynamic site and deploy it assets-only — replacing a working site
        # with a broken one. This is the mutation that must not escape.
        out = tmp_path / ".svelte-kit" / "cloudflare"
        (out / "_worker.js" / "chunks").mkdir(parents=True)
        (out / "_worker.js" / "chunks" / "0.js").write_text("//")
        assert engines.resolve_emits_server_worker(tmp_path, "svelte") is True

    def test_ripple_with_a_worker_is_unchanged(self, tmp_path) -> None:
        out = tmp_path / ".svelte-kit" / "cloudflare"
        out.mkdir(parents=True)
        (out / "_worker.js").write_text("export default {}")
        assert engines.resolve_emits_server_worker(tmp_path, "ripple") is True

    @pytest.mark.parametrize("engine", ["html", "react"])
    def test_serverless_engines_short_circuit(self, tmp_path, engine: str) -> None:
        # Never a worker on these tracks, even if a stray _worker.js is lying around —
        # the engine-level answer is authoritative for them and must not be overridden
        # by a file that cannot be theirs.
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "_worker.js").write_text("stray")
        (tmp_path / "_worker.js").write_text("stray")
        assert engines.resolve_emits_server_worker(tmp_path, engine) is False

    def test_missing_output_dir_reports_no_worker(self, tmp_path) -> None:
        # Nothing built yet: no worker is the honest answer, and the caller's own
        # missing-build check is what turns this into an error.
        assert engines.resolve_emits_server_worker(tmp_path, "svelte") is False


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


class TestExpectsServerWorker:
    """SL-2 slice 2's sixth predicate: is a worker's ABSENCE a problem?

    Not the same question as ``emits_server_worker``, and the difference is svelte. That
    one answers the DEPLOY-SHAPE question from the engine name and still says svelte emits
    a worker; this one answers "should a finished artifact have one" and says the name
    cannot tell. Both are right about their own question, which is why this is a new
    predicate rather than an edit to that one.
    """

    def test_ripple_must_have_a_worker(self) -> None:
        assert engines.expects_server_worker("ripple") is True

    @pytest.mark.parametrize("engine", ["react", "html"])
    def test_a_static_engine_must_not(self, engine: str) -> None:
        assert engines.expects_server_worker(engine) is False

    def test_svelte_is_unanswerable_from_the_name(self) -> None:
        """The load-bearing case. Since SL-1 a static landing site builds on
        adapter-static (no worker) and a dynamic one on adapter-cloudflare (a worker), and
        which ran is a property of the SITE. ``None``, not False: False would make a
        dynamic site's worker read as an anomaly instead."""
        assert engines.expects_server_worker("svelte") is None

    @pytest.mark.parametrize("value", [None, "", "unknown"])
    def test_the_default_expects_a_worker(self, value: str | None) -> None:
        """Unknown → ripple, the least-capable shape, same as every other predicate."""
        assert engines.expects_server_worker(value) is True

    def test_it_disagrees_with_the_name_only_predicate_on_exactly_one_engine(self) -> None:
        """Pins the scope of the divergence. If these two ever agree everywhere again,
        either svelte stopped spanning two adapters (then this predicate can go) or someone
        collapsed the tri-state back into a bool (then static svelte builds are warning
        again)."""
        differs = {
            engine
            for engine in ("ripple", "svelte", "react", "html")
            if engines.expects_server_worker(engine) is not engines.emits_server_worker(engine)
        }
        assert differs == {"svelte"}


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


# --------------------------------------------------------- site capabilities (P0-c)

_EXPECTED_CAPABILITIES = {
    "ripple": {
        "select": True, "text": False, "code": False, "build_log": False,
        "design": False, "sections": True,
    },
    "svelte": {
        "select": True, "text": True, "code": True, "build_log": False,
        "design": True, "sections": True,
    },
    "html": {
        "select": True, "text": True, "code": True, "build_log": False,
        "design": False, "sections": False,
    },
    "react": {
        "select": True, "text": False, "code": True, "build_log": False,
        "design": False, "sections": True,
    },
    "project": {
        "select": False, "text": False, "code": True, "build_log": True,
        "design": False, "sections": False,
    },
}  # fmt: skip


class TestEngineCapabilities:
    def test_every_known_engine_has_a_pinned_shape(self) -> None:
        assert set(_EXPECTED_CAPABILITIES) == engines.KNOWN_ENGINES

    @pytest.mark.parametrize("engine", sorted(_EXPECTED_CAPABILITIES))
    def test_shape_per_engine(self, engine: str) -> None:
        assert engines.engine_capabilities(engine) == _EXPECTED_CAPABILITIES[engine]

    def test_project_keeps_its_original_four_flags(self) -> None:
        caps = engines.engine_capabilities("project")
        assert {k: caps[k] for k in ("select", "text", "code", "build_log")} == {
            "select": False,
            "text": False,
            "code": True,
            "build_log": True,
        }

    def test_design_matches_the_frontend_inspector_rule(self) -> None:
        """paw-enterprise ``hasInspectorRail`` is svelte only."""
        designers = {e for e in engines.KNOWN_ENGINES if engines.engine_capabilities(e)["design"]}
        assert designers == {"svelte"}

    @pytest.mark.parametrize("value", [None, ""])
    def test_legacy_empty_engine_reads_as_ripple(self, value: str | None) -> None:
        assert engines.engine_capabilities(value) == _EXPECTED_CAPABILITIES["ripple"]

    @pytest.mark.parametrize("has_database", [True, False])
    def test_site_capabilities_adds_has_database(self, has_database: bool) -> None:
        caps = engines.site_capabilities("project", has_database=has_database)
        assert caps == {**_EXPECTED_CAPABILITIES["project"], "has_database": has_database}


class TestSiteHasDatabase:
    def test_nothing_means_no_database(self) -> None:
        assert engines.site_has_database() is False

    def test_a_bound_d1_id(self) -> None:
        assert engines.site_has_database(d1_database_id="db-1") is True

    def test_the_d1_recipe(self) -> None:
        meta = {"project": {"template": "astro", "recipes": ["kv", "d1-drizzle"]}}
        assert engines.site_has_database(site_meta=meta) is True

    def test_other_recipes_are_not_a_database(self) -> None:
        meta = {"project": {"template": "astro", "recipes": ["kv", "r2", 3]}}
        assert engines.site_has_database(site_meta=meta) is False

    def test_a_dynamic_pattern(self) -> None:
        assert engines.site_has_database(pattern="dynamic") is True

    @pytest.mark.parametrize("key", ["sources", "actions", "auth"])
    def test_a_spec_that_declares_data(self, key: str) -> None:
        assert engines.site_has_database(pattern="landing", ripple_spec={key: {"x": 1}}) is True

    def test_a_static_landing_spec(self) -> None:
        assert engines.site_has_database(pattern="landing", ripple_spec={"ui": {}}) is False

    @pytest.mark.parametrize("meta", [None, {}, {"project": None}, {"project": {"recipes": "d1"}}])
    def test_malformed_site_meta_is_no_database(self, meta: object) -> None:
        assert engines.site_has_database(site_meta=meta) is False  # type: ignore[arg-type]

    def test_publish_and_capabilities_share_the_dynamic_rule(self) -> None:
        from pocketpaw_ee.sites import service as sites_service

        for pattern, spec in [("dynamic", None), ("landing", {"actions": [1]}), (None, {})]:
            assert sites_service._is_dynamic(pattern, spec) is engines.spec_is_dynamic(
                pattern, spec
            )


# ------------------------------------------------------------- strict engine guard


class TestUnknownEngineWarning:
    def test_project_is_known(self) -> None:
        assert engines.normalize_engine("project") == "project"

    def test_unknown_value_warns_once_and_still_reads_as_ripple(self, caplog) -> None:
        engines._WARNED_UNKNOWN.discard("vue-test")
        with caplog.at_level("WARNING", logger="pocketpaw_ee.sites.engines"):
            assert engines.normalize_engine("vue-test") == "ripple"
            assert engines.normalize_engine("vue-test") == "ripple"
        warnings = [r for r in caplog.records if "vue-test" in r.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.parametrize("value", [None, ""])
    def test_legacy_empty_engine_does_not_warn(self, value: str | None, caplog) -> None:
        with caplog.at_level("WARNING", logger="pocketpaw_ee.sites.engines"):
            assert engines.normalize_engine(value) == "ripple"
        assert not caplog.records


def _engine_literals() -> dict[str, list[str]]:
    """Every engine string literal in the backend source, found by AST: a comparison
    of a name/attribute/``.get("engine")`` called ``engine`` against a string (or a
    tuple/set/list of strings), an ``engine=`` keyword, an ``"engine": ...`` dict
    entry and a ``... .get("engine") or "x"`` default. Strings inside prompts and
    docstrings are not code and are not matched."""
    import ast
    from pathlib import Path

    import pocketpaw_ee

    import pocketpaw

    roots = [Path(pocketpaw_ee.__file__).parent, Path(pocketpaw.__file__).parent]

    def is_engine(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id == "engine"
        if isinstance(node, ast.Attribute):
            return node.attr == "engine"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return (
                node.func.attr == "get"
                and bool(node.args)
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "engine"
            )
        if isinstance(node, ast.BoolOp):
            return any(is_engine(v) for v in node.values)
        return False

    def strings(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Tuple | ast.Set | ast.List):
            return [s for elt in node.elts for s in strings(elt)]
        return []

    found: dict[str, list[str]] = {}
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                values: list[str] = []
                if isinstance(node, ast.Compare):
                    sides = [node.left, *node.comparators]
                    if any(is_engine(s) for s in sides):
                        values = [v for s in sides if not is_engine(s) for v in strings(s)]
                elif isinstance(node, ast.keyword) and node.arg == "engine":
                    values = strings(node.value)
                elif isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values, strict=True):
                        if isinstance(k, ast.Constant) and k.value == "engine":
                            values += strings(v)
                elif isinstance(node, ast.BoolOp) and is_engine(node):
                    values = [v for s in node.values for v in strings(s)]
                line = getattr(node, "lineno", 0)
                for value in values:
                    found.setdefault(value, []).append(f"{path.name}:{line}")
    return found


def test_every_engine_literal_in_the_codebase_is_known() -> None:
    """A new engine must be registered in ``engines.KNOWN_ENGINES``; until it is, every
    predicate quietly treats it as ripple. ``""`` is the legacy "unset" value."""
    found = _engine_literals()
    assert {"ripple", "svelte", "html", "react", "project"} <= set(found), sorted(found)
    unknown = {v: where[:3] for v, where in found.items() if v and v not in engines.KNOWN_ENGINES}
    assert not unknown, f"engine literals not in engines.KNOWN_ENGINES: {unknown}"
