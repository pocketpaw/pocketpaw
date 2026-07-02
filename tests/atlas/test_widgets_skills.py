# tests/atlas/test_widgets_skills.py — widget + skill extraction and the
# suffix-normalizer ranking pass (AT-6). Created: 2026-07-02
# (feat/atlas-widgets). Proves:
#   * widget extraction: one valid kind="widget" entry per ripple
#     WIDGET_CATALOG type (control-flow if/each excluded), offline from the
#     bundled design-language module — no network, no CDN manifest;
#   * intent-phrase search reaches widgets without exact names
#     ("show tasks as a board" → widget:kanban in the top 3);
#   * describe routes the agent to the real prop contract: every widget's
#     `how` (and narrative) names the existing get_widget_spec tool;
#   * skill extraction: one valid kind="skill" entry per BUNDLED skill
#     (workspace-installed skills are deliberately absent), and a
#     capability phrase (not the skill's name) surfaces a bundled skill;
#   * the store's suffix normalizer (_stem): plural/inflected query tokens
#     now match singular index keywords ("competitors" → "competitor"),
#     with the exact strip rules pinned; field weights unchanged.

from pocketpaw.atlas.compile import compile_atlas
from pocketpaw.atlas.model import AtlasEntry, AtlasModel
from pocketpaw.atlas.store import (
    _KEYWORD_WEIGHT,
    _NAME_WEIGHT,
    _NARRATIVE_WEIGHT,
    _SUMMARY_WEIGHT,
    AtlasStore,
    _stem,
)


def _store() -> AtlasStore:
    return AtlasStore.load()


class TestWidgetExtraction:
    def test_every_catalog_type_has_a_valid_entry(self):
        """One kind='widget' entry per WIDGET_CATALOG type, complete fields."""
        from pocketpaw.atlas.compile import _parse_widget_catalog

        catalog = _parse_widget_catalog()
        assert len(catalog) >= 100, "the bundled catalog lists ~150 widget types"
        store = _store()
        for wtype in catalog:
            entry = store.describe(f"widget:{wtype}")
            assert entry is not None, f"widget:{wtype} missing — run `pocketpaw atlas build`"
            assert entry.kind == "widget"
            assert entry.name == wtype
            assert entry.summary and "\n" not in entry.summary.strip()
            assert entry.narrative
            assert entry.keywords
            assert entry.requires == ["primitive:ripple"]

    def test_control_flow_types_are_not_widgets(self):
        """`if` / `each` are spec grammar, not renderable catalog widgets."""
        store = _store()
        assert store.describe("widget:if") is None
        assert store.describe("widget:each") is None

    def test_intent_phrase_search_hits_kanban(self):
        """Fuzzy intent → widget without the exact name: a board of tasks
        is the kanban widget (its USE_THE_WIDGET_RULE vocabulary)."""
        results = _store().search("show tasks as a board")
        top_ids = [e.id for e in results[:3]]
        assert "widget:kanban" in top_ids, f"expected widget:kanban in top-3, got {top_ids}"

    def test_describe_widget_routes_to_get_widget_spec(self):
        """The card is a discovery pointer — `how` must name the existing
        get_widget_spec tool for the full prop schema."""
        entry = _store().describe("widget:kanban")
        assert entry is not None
        assert "get_widget_spec" in entry.how
        assert "get_widget_spec" in entry.narrative

    def test_kanban_narrative_names_key_props(self):
        """Key prop NAMES (from the bundled WIDGET_SHAPES doc) ride in the
        narrative — brief pointers, never the full schema."""
        entry = _store().describe("widget:kanban")
        assert entry is not None
        assert "columns" in entry.narrative
        assert "columnKey" in entry.narrative

    def test_widget_keywords_carry_category_and_intent_vocabulary(self):
        entry = _store().describe("widget:kanban")
        assert entry is not None
        assert "data" in entry.keywords, "category rides in keywords"
        assert "board" in entry.keywords, "intent vocabulary rides in keywords"


class TestSkillExtraction:
    def test_every_bundled_skill_has_a_valid_entry(self):
        """One kind='skill' entry per bundled skill dir, summary from the
        frontmatter description."""
        from pocketpaw.bundled_skills.installer import _SKILLS_DIR

        store = _store()
        slugs = sorted(
            p.name for p in _SKILLS_DIR.iterdir() if p.is_dir() and (p / "SKILL.md").exists()
        )
        assert slugs, "bundled skills must exist in the package"
        for slug in slugs:
            entry = store.describe(f"skill:{slug}")
            assert entry is not None, f"skill:{slug} missing — run `pocketpaw atlas build`"
            assert entry.kind == "skill"
            assert entry.summary and "\n" not in entry.summary.strip()
            assert entry.narrative
            assert entry.keywords
            assert "Skill tool" in entry.how, "how must say how the skill is invoked"

    def test_only_bundled_skills_are_compiled(self):
        """Workspace-installed skills vary per machine — every skill:* id in
        the artifact must correspond to a bundled skill dir."""
        from pocketpaw.bundled_skills.installer import _SKILLS_DIR

        bundled = {p.name for p in _SKILLS_DIR.iterdir() if p.is_dir()}
        for entry in _store().entries:
            if entry.kind == "skill":
                assert entry.id.split(":", 1)[1] in bundled

    def test_capability_phrase_search_hits_a_bundled_skill(self):
        """A capability phrase (no skill named) must surface the right
        bundled skill — 'rehearse a decision' is foresight vocabulary."""
        results = _store().search("rehearse a decision before announcing it")
        ids = [e.id for e in results]
        assert "skill:foresight-create-sim" in ids, f"expected the foresight skill, got {ids}"


class TestStemming:
    def test_exact_strip_rules(self):
        """Pin the documented normalizer rules: ing/ed/es → s → trailing e,
        3+ char stems only, ss never stripped."""
        assert _stem("competitors") == "competitor"
        assert _stem("competitor") == "competitor"
        assert _stem("matches") == _stem("matching") == "match"
        assert _stem("approved") == _stem("approve") == "approv"
        assert _stem("sites") == _stem("site")
        assert _stem("boards") == "board"
        # Guards: short stems and double-s stay put.
        assert _stem("as") == "as"
        assert _stem("less") == "less"
        assert _stem("using") == "using"  # 2-char stem — not stripped

    def test_plural_query_matches_singular_keyword(self):
        """The eval-documented miss class: a plural query token must now hit
        a singular keyword at KEYWORD weight (no field-weight rebalance)."""
        model = AtlasModel(
            entries=[
                AtlasEntry(
                    id="primitive:test",
                    kind="primitive",
                    name="Test",
                    summary="A test entry.",
                    narrative="Nothing else matches here.",
                    keywords=["competitor"],
                )
            ]
        )
        store = AtlasStore(model)
        scored = store.search_scored("competitors")
        assert scored, "plural query must match the singular keyword"
        assert scored[0][0] == _KEYWORD_WEIGHT

    def test_field_weights_unchanged(self):
        """AT-6 adds stemming only — the AT-1 field weights are untouched."""
        assert (_NAME_WEIGHT, _KEYWORD_WEIGHT, _SUMMARY_WEIGHT, _NARRATIVE_WEIGHT) == (
            5.0,
            3.0,
            1.5,
            1.0,
        )


class TestDeterminism:
    def test_widget_and_skill_entries_are_deterministic(self):
        """Two in-memory compiles yield identical widget/skill entries —
        the twice-compile byte pin lives in test_compile.py."""
        first = [e for e in compile_atlas().entries if e.kind in ("widget", "skill")]
        second = [e for e in compile_atlas().entries if e.kind in ("widget", "skill")]
        assert first == second
        assert first, "compiled artifact must carry widget + skill entries"
