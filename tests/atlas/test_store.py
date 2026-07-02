# tests/atlas/test_store.py — AtlasStore loader / search / describe (AT-1).
# Created: 2026-07-02 (feat/atlas-core). Proves the packaged seed validates
# against paw.atlas/v1 with all 10 primitive entries, intent search ranks
# the right primitive into the top results ("approve agent actions" →
# Instinct, "build an app dashboard" → Pocket), describe returns the full
# narrative, unknown ids return None, and the seed round-trips the schema.
# Updated: 2026-07-02 (feat/atlas-surface, AT-3) — the seed now also carries
# kind="surface" entries (paw-enterprise routes). Loader tests split into
# per-kind completeness checks (every surface entry must carry a route in
# its ``surface`` field); new search/describe tests pin "publish a website"
# → sites with the /sites route populated, and primitives cross-linked to
# their home surfaces.
# Updated: 2026-07-02 (feat/atlas-compiler, AT-4) — the data file is now the
# COMPILED artifact carrying extracted ``connector`` and ``sense`` entries
# next to the authored ones, with ``generated: true``. Loader assertions
# updated: authored ids are a subset (still exact per authored kind), every
# entry kind is one of the four compiled kinds, and search-ranking pins are
# unchanged (they must survive the 41 new entries).
# Updated: 2026-07-02 (feat/atlas-widgets, AT-6) — the artifact now also
# carries extracted ``widget`` (ripple catalog) and ``skill`` (bundled
# skills) entries; COMPILED_KINDS widened to six. Search-ranking pins are
# otherwise unchanged and must survive the ~165 new entries (widget/skill
# specific pins live in tests/atlas/test_widgets_skills.py).

import json

from pocketpaw.atlas.model import ATLAS_SCHEMA_V1, AtlasModel
from pocketpaw.atlas.store import _DATA_PATH, AtlasStore, get_atlas_store

COMPILED_KINDS = ("primitive", "surface", "connector", "sense", "widget", "skill")

EXPECTED_PRIMITIVE_IDS = {
    "primitive:pocket",
    "primitive:instinct",
    "primitive:fabric",
    "primitive:connector",
    "primitive:ripple",
    "primitive:soul",
    "primitive:branch",
    "primitive:workspace-jobs",
    "primitive:sites",
    "primitive:belt",
}

# Surface entries mirror REAL user-facing routes in
# paw-enterprise/src/routes/ — verified against the dir, not invented.
EXPECTED_SURFACE_IDS = {
    "surface:home",
    "surface:chat",
    "surface:pockets",
    "surface:sites",
    "surface:belt",
    "surface:paw-print",
    "surface:decisions-graph",
    "surface:mission-control",
    "surface:agents",
    "surface:settings",
    "surface:integrations",
    "surface:workspace-admin",
    "surface:knowledge",
    "surface:files",
    "surface:studio",
    "surface:code",
    "surface:foresight",
    "surface:calendar",
    "surface:meetings",
    "surface:activity",
    "surface:audit",
}

# primitive id → the home route its ``surface`` field must carry (AT-3
# cross-links, so atlas_describe answers include where to see the result).
EXPECTED_PRIMITIVE_SURFACES = {
    "primitive:pocket": "/pockets",
    "primitive:instinct": "/paw-print",
    "primitive:connector": "/settings/workspace/integrations",
    "primitive:sites": "/sites",
    "primitive:belt": "/belt",
}


class TestLoader:
    def test_loads_and_validates_seed(self):
        store = AtlasStore.load()
        assert store.model.schema_ == ATLAS_SCHEMA_V1
        assert store.model.generated is True, "the data file is the compiled artifact"
        ids = {e.id for e in store.entries}
        # Authored entries are exact per kind; extracted connector/sense
        # entries ride alongside (their exact set is pinned by the compiler
        # tests against the repo's connector YAMLs).
        assert {i for i in ids if i.startswith("primitive:")} == EXPECTED_PRIMITIVE_IDS
        assert {i for i in ids if i.startswith("surface:")} == EXPECTED_SURFACE_IDS
        assert any(i.startswith("connector:") for i in ids)
        assert any(i.startswith("sense:") for i in ids)

    def test_all_seed_entries_are_complete(self):
        """Every entry has the load-bearing fields filled: a one-line
        summary, a narrative, and search keywords."""
        for entry in AtlasStore.load().entries:
            assert entry.kind in COMPILED_KINDS
            assert entry.name
            assert entry.summary and "\n" not in entry.summary.strip()
            assert entry.narrative
            assert entry.keywords, f"{entry.id} must carry search keywords"

    def test_surface_entries_carry_a_route(self):
        """Every kind='surface' entry points at a real client route: the
        ``surface`` field is a rooted path and the id is 'surface:<slug>'."""
        surfaces = [e for e in AtlasStore.load().entries if e.kind == "surface"]
        assert {e.id for e in surfaces} == EXPECTED_SURFACE_IDS
        for entry in surfaces:
            assert entry.id.startswith("surface:")
            assert entry.surface.startswith("/"), f"{entry.id} must carry a rooted route"

    def test_primitives_cross_link_home_surfaces(self):
        """Primitives with a natural home route carry it in ``surface`` so
        atlas_describe answers include where to see the result."""
        store = AtlasStore.load()
        for primitive_id, route in EXPECTED_PRIMITIVE_SURFACES.items():
            entry = store.describe(primitive_id)
            assert entry is not None
            assert entry.surface == route

    def test_singleton_getter_returns_same_instance(self):
        assert get_atlas_store() is get_atlas_store()

    def test_seed_round_trips_the_schema(self):
        """Raw seed → model → dump (by alias) → model again, byte-stable."""
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        model = AtlasModel.model_validate(raw)
        dumped = model.model_dump(by_alias=True)
        assert dumped["schema"] == ATLAS_SCHEMA_V1
        assert AtlasModel.model_validate(dumped) == model


class TestSearch:
    def test_approve_agent_actions_hits_instinct(self):
        results = AtlasStore.load().search("approve agent actions")
        top_ids = [e.id for e in results[:3]]
        assert "primitive:instinct" in top_ids, f"expected Instinct in top-3, got {top_ids}"

    def test_build_an_app_dashboard_top_hits_pocket(self):
        results = AtlasStore.load().search("build an app dashboard")
        assert results, "query must match at least one entry"
        assert results[0].id == "primitive:pocket"

    def test_limit_is_respected(self):
        results = AtlasStore.load().search("workspace data agents", limit=2)
        assert len(results) <= 2

    def test_no_overlap_returns_empty(self):
        assert AtlasStore.load().search("zzzz qqqq xyzzy") == []

    def test_empty_query_returns_empty(self):
        assert AtlasStore.load().search("   ") == []

    def test_publish_a_website_surfaces_sites_with_route(self):
        """'publish a website' must rank a sites entry (surface:sites or
        primitive:sites) into the results, with the /sites route populated."""
        results = AtlasStore.load().search("publish a website")
        sites_hits = [e for e in results if e.id in ("surface:sites", "primitive:sites")]
        assert sites_hits, f"expected a sites entry, got {[e.id for e in results]}"
        assert all(e.surface == "/sites" for e in sites_hits)


class TestDescribe:
    def test_describe_instinct_returns_narrative_and_how(self):
        entry = AtlasStore.load().describe("primitive:instinct")
        assert entry is not None
        assert "gate" in entry.narrative.lower()
        assert entry.how, "Instinct must document how it is exercised"

    def test_unknown_id_returns_none(self):
        assert AtlasStore.load().describe("primitive:does-not-exist") is None

    def test_describe_primitive_sites_includes_surface_route(self):
        entry = AtlasStore.load().describe("primitive:sites")
        assert entry is not None
        assert entry.surface == "/sites"

    def test_describe_surface_entry_returns_route(self):
        entry = AtlasStore.load().describe("surface:sites")
        assert entry is not None
        assert entry.kind == "surface"
        assert entry.surface == "/sites"
