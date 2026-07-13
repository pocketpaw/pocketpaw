# tests/atlas/test_sdk_mcp_atlas.py — pocketpaw_atlas MCP tool handlers (AT-1).
# Created: 2026-07-02 (feat/atlas-core). Mirrors tests/test_sdk_mcp_inline_help.py
# (the pocketpaw_widgets handler tests): proves atlas_search returns ranked
# capability cards with the Instinct card for an approval intent, atlas_describe
# returns the full entry (narrative + how), unknown / missing args come back as
# agent-readable error envelopes, and the exported tool ids follow the
# mcp__<server>__<tool> allowlist convention.

import json

import pytest

from pocketpaw.agents.sdk_mcp_atlas import (
    ATLAS_TOOL_IDS,
    SERVER_NAME,
    _atlas_describe_handler,
    _atlas_search_handler,
)


def _text_of(result: dict) -> str:
    block = next((c for c in result.get("content", []) if c.get("type") == "text"), None)
    assert block is not None, "handler must return a text content block"
    return block["text"]


class TestAtlasSearchHandler:
    @pytest.mark.asyncio
    async def test_approve_agent_actions_returns_instinct_card(self):
        out = await _atlas_search_handler({"intent": "approve agent actions"})
        assert not out.get("is_error")
        cards = json.loads(_text_of(out))["results"]
        top_ids = [c["id"] for c in cards[:3]]
        assert "primitive:instinct" in top_ids, f"expected Instinct in top-3, got {top_ids}"
        # Cards are the thin shape: id/kind/name/summary (+surface when set).
        instinct = next(c for c in cards if c["id"] == "primitive:instinct")
        assert instinct["kind"] == "primitive"
        assert instinct["name"] == "Instinct"
        assert instinct["summary"]
        assert "narrative" not in instinct, "search cards stay thin; describe carries narrative"

    @pytest.mark.asyncio
    async def test_missing_intent_returns_error_envelope(self):
        out = await _atlas_search_handler({})
        assert out.get("is_error") is True
        assert "intent" in _text_of(out)

    @pytest.mark.asyncio
    async def test_no_match_returns_agent_readable_text(self):
        out = await _atlas_search_handler({"intent": "zzzz qqqq xyzzy"})
        assert not out.get("is_error"), "an empty result set is not a tool error"
        assert "No atlas entries matched" in _text_of(out)


class TestAtlasDescribeHandler:
    @pytest.mark.asyncio
    async def test_describe_instinct_returns_narrative_and_how(self):
        out = await _atlas_describe_handler({"id": "primitive:instinct"})
        assert not out.get("is_error")
        entry = json.loads(_text_of(out))
        assert entry["id"] == "primitive:instinct"
        assert "gate" in entry["narrative"].lower()
        assert entry["how"]
        assert "requires" in entry and "surface" in entry

    @pytest.mark.asyncio
    async def test_unknown_id_returns_error_with_known_ids(self):
        out = await _atlas_describe_handler({"id": "primitive:nope"})
        assert out.get("is_error") is True
        # The error names the valid ids so the model can self-correct.
        assert "primitive:pocket" in _text_of(out)

    @pytest.mark.asyncio
    async def test_missing_id_returns_error_envelope(self):
        out = await _atlas_describe_handler({})
        assert out.get("is_error") is True
        assert "id" in _text_of(out)


class TestToolIds:
    def test_tool_ids_follow_allowlist_convention(self):
        assert SERVER_NAME == "pocketpaw_atlas"
        assert ATLAS_TOOL_IDS == (
            f"mcp__{SERVER_NAME}__atlas_search",
            f"mcp__{SERVER_NAME}__atlas_describe",
        )
