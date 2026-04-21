# tests/test_api_soul_memories.py — Unit tests for GET /soul/memories.
# Created: 2026-04-19 (feat/cluster-d-agent-reasoning-viewer-plus-soul-memory)
# Covers the pager semantics (tier filter, limit clamp), the soul-off
# fall-through, and the serialisation of MemoryEntry-like objects to plain
# dicts. Uses the in-memory ``_collect_tier_entries`` helper directly so we
# don't spin a FastAPI client — the endpoint body is a thin wrapper.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from pocketpaw.api.v1.soul import (
    _ALLOWED_TIERS,
    _collect_tier_entries,
    list_soul_memories,
)


class FakeMemoryEntry(BaseModel):
    content: str
    importance: int = 5


def _soul_with_memories(
    episodic: list | None = None,
    semantic: list | None = None,
    procedural: list | None = None,
):
    """Build a minimal soul-shaped object good enough for
    ``_collect_tier_entries``."""
    episodic = episodic or []
    semantic = semantic or []
    procedural = procedural or []

    episodic_store = SimpleNamespace(entries=lambda: list(episodic))
    semantic_store = SimpleNamespace(facts=lambda: list(semantic))
    procedural_store = SimpleNamespace(entries=lambda: list(procedural))
    mm = SimpleNamespace(
        _episodic=episodic_store,
        _semantic=semantic_store,
        _procedural=procedural_store,
    )
    return SimpleNamespace(_memory=mm)


class TestCollectTierEntries:
    def test_episodic_returns_most_recent_first_slice(self):
        soul = _soul_with_memories(
            episodic=[
                FakeMemoryEntry(content="a"),
                FakeMemoryEntry(content="b"),
                FakeMemoryEntry(content="c"),
            ]
        )
        out = _collect_tier_entries(soul, "episodic", limit=2)
        assert [m["content"] for m in out] == ["a", "b"]
        # Entries are serialised to dicts, not raw BaseModels.
        assert all(isinstance(m, dict) for m in out)

    def test_semantic_uses_facts_iterator(self):
        soul = _soul_with_memories(
            semantic=[FakeMemoryEntry(content="fact-1"), FakeMemoryEntry(content="fact-2")]
        )
        out = _collect_tier_entries(soul, "semantic", limit=10)
        assert len(out) == 2
        assert out[0]["content"] == "fact-1"

    def test_procedural_path(self):
        soul = _soul_with_memories(procedural=[FakeMemoryEntry(content="skill")])
        out = _collect_tier_entries(soul, "procedural", limit=10)
        assert out == [{"content": "skill", "importance": 5}]

    def test_missing_memory_returns_empty(self):
        empty_soul = SimpleNamespace(_memory=None)
        assert _collect_tier_entries(empty_soul, "episodic", limit=10) == []

    def test_missing_tier_store_returns_empty(self):
        mm = SimpleNamespace()  # no _episodic attr
        soul = SimpleNamespace(_memory=mm)
        assert _collect_tier_entries(soul, "episodic", limit=5) == []

    def test_plain_string_entries_normalised_to_content_dict(self):
        soul = _soul_with_memories(episodic=["raw-string-memory"])
        out = _collect_tier_entries(soul, "episodic", limit=1)
        assert out == [{"content": "raw-string-memory"}]

    def test_dict_entries_passed_through(self):
        soul = _soul_with_memories(episodic=[{"content": "d", "extra": "ok"}])
        out = _collect_tier_entries(soul, "episodic", limit=1)
        assert out == [{"content": "d", "extra": "ok"}]


class TestListSoulMemoriesEndpoint:
    @pytest.mark.asyncio
    async def test_unknown_tier_returns_error(self):
        resp = await list_soul_memories(tier="bogus")
        assert "error" in resp
        assert "Unknown tier" in resp["error"]

    @pytest.mark.asyncio
    async def test_limit_clamped_to_upper_bound(self, monkeypatch):
        # Force the soul manager to return a soul with a tonne of entries,
        # then verify the endpoint hands back at most 200.
        big = _soul_with_memories(
            episodic=[FakeMemoryEntry(content=str(i)) for i in range(300)]
        )

        class FakeMgr:
            soul = big

        monkeypatch.setattr("pocketpaw.soul.manager.get_soul_manager", lambda: FakeMgr())

        resp = await list_soul_memories(tier="episodic", limit=9999)
        assert resp["total"] == 200
        assert len(resp["memories"]) == 200

    @pytest.mark.asyncio
    async def test_limit_clamped_to_lower_bound(self, monkeypatch):
        small = _soul_with_memories(
            episodic=[FakeMemoryEntry(content="only")] * 5
        )

        class FakeMgr:
            soul = small

        monkeypatch.setattr("pocketpaw.soul.manager.get_soul_manager", lambda: FakeMgr())

        resp = await list_soul_memories(tier="episodic", limit=-1)
        assert resp["total"] == 1

    @pytest.mark.asyncio
    async def test_no_soul_returns_empty_not_error(self, monkeypatch):
        monkeypatch.setattr("pocketpaw.soul.manager.get_soul_manager", lambda: None)

        resp = await list_soul_memories(tier="episodic", limit=20)
        assert resp == {"tier": "episodic", "memories": [], "total": 0}

    @pytest.mark.asyncio
    async def test_tier_filter_applied(self, monkeypatch):
        soul = _soul_with_memories(
            episodic=[FakeMemoryEntry(content="ep")],
            semantic=[FakeMemoryEntry(content="sem")],
        )

        class FakeMgr:
            def __init__(self, soul):
                self.soul = soul

        monkeypatch.setattr(
            "pocketpaw.soul.manager.get_soul_manager", lambda: FakeMgr(soul)
        )

        ep = await list_soul_memories(tier="episodic")
        sem = await list_soul_memories(tier="semantic")

        assert ep["memories"][0]["content"] == "ep"
        assert sem["memories"][0]["content"] == "sem"
        assert ep["tier"] == "episodic"
        assert sem["tier"] == "semantic"


def test_allowed_tiers_set_is_frozen():
    # Belt-and-braces: if someone adds a tier, they have to update both the
    # set and at least one test. That's the whole point.
    assert _ALLOWED_TIERS == frozenset({"episodic", "semantic", "procedural"})
