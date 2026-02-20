"""Tests for Claude SDK model alias resolution."""

import pytest

from pocketpaw.agents.claude_sdk import (
    _CLAUDE_MODEL_IDS,
    _LONG_CONTEXT_ALIASES,
    resolve_claude_model,
)


class TestResolveClaude:
    """resolve_claude_model() — alias → (model_id, extended_context)."""

    def test_sonnet_alias(self):
        model, ext = resolve_claude_model("sonnet")
        assert model == "claude-sonnet-4-6"
        assert ext is False

    def test_opus_alias(self):
        model, ext = resolve_claude_model("opus")
        assert model == "claude-opus-4-6"
        assert ext is False

    def test_haiku_alias(self):
        model, ext = resolve_claude_model("haiku")
        assert model == "claude-haiku-4-5-20251001"
        assert ext is False

    def test_sonnet_1m_alias(self):
        model, ext = resolve_claude_model("sonnet[1m]")
        assert model == "claude-sonnet-4-6"
        assert ext is True

    def test_opusplan_plan_mode_true(self):
        model, ext = resolve_claude_model("opusplan", plan_mode=True)
        assert model == "claude-opus-4-6"
        assert ext is False

    def test_opusplan_plan_mode_false(self):
        model, ext = resolve_claude_model("opusplan", plan_mode=False)
        assert model == "claude-sonnet-4-6"
        assert ext is False

    def test_case_insensitive(self):
        for variant in ("Sonnet", "SONNET", "sOnNeT"):
            model, _ = resolve_claude_model(variant)
            assert model == "claude-sonnet-4-6", f"Failed for {variant!r}"

    def test_raw_model_id_passthrough(self):
        raw = "claude-sonnet-4-6"
        model, ext = resolve_claude_model(raw)
        assert model == raw
        assert ext is False

    def test_unknown_alias_passthrough(self):
        model, ext = resolve_claude_model("my-custom-model-v2")
        assert model == "my-custom-model-v2"
        assert ext is False

    def test_empty_string(self):
        model, ext = resolve_claude_model("")
        assert model == ""
        assert ext is False

    def test_whitespace_only(self):
        model, ext = resolve_claude_model("   ")
        assert model == ""
        assert ext is False

    def test_whitespace_stripped(self):
        model, _ = resolve_claude_model("  sonnet  ")
        assert model == "claude-sonnet-4-6"

    def test_long_context_aliases_frozenset(self):
        assert isinstance(_LONG_CONTEXT_ALIASES, frozenset)
        assert "sonnet[1m]" in _LONG_CONTEXT_ALIASES

    def test_all_aliases_present(self):
        expected = {"sonnet", "opus", "haiku", "sonnet[1m]", "opusplan"}
        assert expected == set(_CLAUDE_MODEL_IDS.keys())
