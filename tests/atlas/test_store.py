# tests/atlas/test_store.py — AtlasStore loader / search / describe (AT-1).
# Updated 2026-10-02 (feat/discover-index, review): surface:discover (/discover)
# joins the expected surfaces.
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
# Updated: 2026-07-03 (feat/workspace-admin-tools, WA-3) — the artifact now also
# carries hand-authored ``capability`` cards (the workspace-admin tools, one per
# tool, each role-gated); COMPILED_KINDS widened to seven so the
# seed-completeness check accepts them.
# Updated: 2026-07-05 (fix/atlas-relevance-round2, Finding A) — new coverage for
# the kind-priority bias: a real-store governance-paraphrase pin (Instinct #1,
# not the /agents surface) and a synthetic-model ``TestKindPriorityBias`` proving
# the primitive edges a same-overlap surface but a genuine margin still wins.
# Updated: 2026-08-17 (feat/ast-1-atlas-primitives, AST-1) — two authored
# primitives joined the seed (primitive:source-truth, primitive:verify-loop);
# EXPECTED_PRIMITIVE_IDS pins the twelve.
# Updated: 2026-10-01 (feat/atlas-canonical) — COMPILED_KINDS gains "verb"; the
# two hand-built surface fixtures carry the now-required presentation /
# agent_openable fields. Live tie fix: TestVerbRankingSafety pins the verb
# object-noun damper and the deterministic tie order (surface before verb, then
# name hits, then id). Review pass: five surfaces the composer's slash menu
# offers (/agents/activity, /fabric, /ship, /growth, /browser) join the set.

import json

from pocketpaw.atlas.model import ATLAS_SCHEMA_V1, AtlasEntry, AtlasModel
from pocketpaw.atlas.store import _DATA_PATH, AtlasStore, get_atlas_store

COMPILED_KINDS = (
    "primitive",
    "surface",
    "connector",
    "sense",
    "widget",
    "skill",
    "capability",
    "verb",
)

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
    "primitive:source-truth",
    "primitive:verify-loop",
}

# Surface entries mirror REAL user-facing routes in
# paw-enterprise/src/routes/ — verified against the dir, not invented.
EXPECTED_SURFACE_IDS = {
    "surface:home",
    "surface:chat",
    "surface:pockets",
    "surface:sites",
    "surface:discover",
    "surface:belt",
    "surface:decisions",
    "surface:decisions-graph",
    "surface:mission-control",
    "surface:agents",
    "surface:settings",
    "surface:integrations",
    "surface:workspace-admin",
    "surface:billing",
    "surface:knowledge",
    "surface:files",
    "surface:studio",
    "surface:studio_editor",
    "surface:code",
    "surface:foresight",
    "surface:calendar",
    "surface:meetings",
    "surface:activity",
    "surface:audit",
    "surface:security",
    "surface:agents-activity",
    "surface:agent-health",
    "surface:fabric",
    "surface:ship",
    "surface:growth",
    "surface:browser",
}

# primitive id → the home route its ``surface`` field must carry (AT-3
# cross-links, so atlas_describe answers include where to see the result).
EXPECTED_PRIMITIVE_SURFACES = {
    "primitive:pocket": "/pockets",
    "primitive:instinct": "/decisions",
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

    def test_governance_paraphrase_ranks_instinct_over_agents_surface(self):
        """Finding A (round 2): governance paraphrases must land on the
        governing Instinct primitive, not the /agents management LIST route."""
        store = AtlasStore.load()
        for query in ("gate the agent", "how do I approve what the agent does"):
            top = [e.id for e in store.search(query, limit=3)]
            assert top[0] == "primitive:instinct", f"{query!r}: expected Instinct #1, got {top}"


class TestKindPriorityBias:
    """The kind-priority bias (Finding A, round 2): a primitive is protected
    against a same-overlap surface/capability, but a real margin still wins."""

    def _model(self, entries: list[AtlasEntry]) -> AtlasStore:
        return AtlasStore(AtlasModel(entries=entries))

    def test_primitive_edges_surface_at_equal_overlap(self):
        """Same single keyword hit on both a primitive and a surface: the
        primitive must rank first (kind scale + tiebreak)."""
        store = self._model(
            [
                AtlasEntry(
                    id="primitive:p",
                    kind="primitive",
                    name="P",
                    summary="s",
                    narrative="n",
                    keywords=["widget"],
                ),
                AtlasEntry(
                    id="surface:s",
                    kind="surface",
                    name="S",
                    summary="s",
                    narrative="n",
                    keywords=["widget"],
                    presentation="window",
                    agent_openable=False,
                ),
            ]
        )
        ranked = store.search("widget", limit=2)
        assert ranked[0].id == "primitive:p"
        assert ranked[1].id == "surface:s"

    def test_real_margin_still_beats_the_primitive(self):
        """A surface that genuinely out-scores the primitive (name hit vs a
        single keyword hit) is NOT flipped — the bias is near-tie only."""
        store = self._model(
            [
                AtlasEntry(
                    id="primitive:p",
                    kind="primitive",
                    name="P",
                    summary="s",
                    narrative="n",
                    keywords=["kanban"],
                ),
                AtlasEntry(
                    id="surface:kanban",
                    kind="surface",
                    name="kanban",  # name hit >> a single keyword hit
                    summary="s",
                    narrative="n",
                    keywords=[],
                    presentation="window",
                    agent_openable=False,
                ),
            ]
        )
        ranked = store.search("kanban", limit=2)
        assert ranked[0].id == "surface:kanban"


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


class TestVerbRankingSafety:
    """Live bug (2026-10-01): "show me my files" tied surface:files with
    verb:file-delete and verb:file-download, and seed order picked the winner."""

    @staticmethod
    def _store(entries):
        return AtlasStore(AtlasModel(entries=entries))

    @staticmethod
    def _surface(eid, name, keywords=()):
        return AtlasEntry(
            id=eid,
            kind="surface",
            name=name,
            summary="s",
            narrative="n",
            keywords=list(keywords),
            presentation="window",
            agent_openable=False,
        )

    @staticmethod
    def _verb(eid, name, keywords=(), applies_to=("file",)):
        return AtlasEntry(
            id=eid,
            kind="verb",
            name=name,
            summary="s",
            narrative="n",
            keywords=list(keywords),
            applies_to=list(applies_to),
            triggers=["verb"],
            risk="risky",
            undo=False,
        )

    def test_object_noun_alone_does_not_tie_a_verb_with_the_surface(self):
        store = self._store(
            [self._verb("verb:file-delete", "Delete file"), self._surface("surface:files", "Files")]
        )
        (top_score, top), (verb_score, verb) = store.search_scored("show me my files")
        assert top.id == "surface:files" and verb.id == "verb:file-delete"
        assert verb_score <= 0.5 * top_score

    def test_action_word_lifts_the_damping(self):
        store = self._store(
            [self._verb("verb:file-delete", "Delete file"), self._surface("surface:files", "Files")]
        )
        assert store.search("delete this file", limit=1)[0].id == "verb:file-delete"

    def test_exact_tie_puts_surface_before_verb_then_name_hits_then_id_order(self):
        # Same keyword-only overlap, no damping (the token is not an object
        # noun). The verb comes FIRST in seed order and still ranks last.
        store = self._store(
            [
                self._verb("verb:a", "A", keywords=["zap"], applies_to=["task"]),
                self._surface("surface:b", "B", keywords=["zap"]),
                self._surface("surface:z", "Z", keywords=["zap"]),
            ]
        )
        ranked = store.search_scored("zap")
        assert len({round(score, 9) for score, _ in ranked}) == 1, "fixture must tie"
        assert [e.id for _, e in ranked] == ["surface:b", "surface:z", "verb:a"]
