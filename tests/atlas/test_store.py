# tests/atlas/test_store.py — AtlasStore loader / search / describe (AT-1).
# Created: 2026-07-02 (feat/atlas-core). Proves the packaged seed validates
# against paw.atlas/v1 with all 10 primitive entries, intent search ranks
# the right primitive into the top results ("approve agent actions" →
# Instinct, "build an app dashboard" → Pocket), describe returns the full
# narrative, unknown ids return None, and the seed round-trips the schema.

import json

from pocketpaw.atlas.model import ATLAS_SCHEMA_V1, AtlasModel
from pocketpaw.atlas.store import _DATA_PATH, AtlasStore, get_atlas_store

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


class TestLoader:
    def test_loads_and_validates_seed(self):
        store = AtlasStore.load()
        assert store.model.schema_ == ATLAS_SCHEMA_V1
        assert {e.id for e in store.entries} == EXPECTED_PRIMITIVE_IDS

    def test_all_seed_entries_are_complete_primitives(self):
        """Every v1 entry is a primitive with the load-bearing fields filled:
        a one-line summary, a narrative, and search keywords."""
        for entry in AtlasStore.load().entries:
            assert entry.kind == "primitive"
            assert entry.name
            assert entry.summary and "\n" not in entry.summary.strip()
            assert entry.narrative
            assert entry.keywords, f"{entry.id} must carry search keywords"

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


class TestDescribe:
    def test_describe_instinct_returns_narrative_and_how(self):
        entry = AtlasStore.load().describe("primitive:instinct")
        assert entry is not None
        assert "gate" in entry.narrative.lower()
        assert entry.how, "Instinct must document how it is exercised"

    def test_unknown_id_returns_none(self):
        assert AtlasStore.load().describe("primitive:does-not-exist") is None
