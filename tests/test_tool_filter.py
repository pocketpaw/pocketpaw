# Tests for tool pre-filtering (tools/filter.py).
#
# Created: 2026-04-01
#
# Covers: under-limit passthrough, max_tools cap, always-include pinning,
# relevance scoring, empty message handling, pinned slot accounting,
# tokenizer behavior, and custom always_include sets.

from __future__ import annotations

from typing import Any

from pocketpaw.tools.filter import ALWAYS_INCLUDE, _tokenize, filter_tools
from pocketpaw.tools.protocol import BaseTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeTool(BaseTool):
    """Minimal BaseTool stub for testing."""

    def __init__(self, name: str, description: str = "") -> None:
        self._name = name
        self._description = description

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    async def execute(self, **params: Any) -> str:  # pragma: no cover
        return ""


def _make_tools(n: int, prefix: str = "tool") -> list[_FakeTool]:
    """Generate *n* fake tools with sequential names."""
    return [_FakeTool(f"{prefix}_{i}", f"Description for {prefix} {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFilterTools:
    """Unit tests for filter_tools()."""

    def test_returns_all_tools_when_under_limit(self) -> None:
        """When tool count <= max_tools, return everything unchanged."""
        tools = _make_tools(10)
        result = filter_tools("anything", tools, max_tools=15)
        assert result == tools

    def test_returns_all_tools_when_exactly_at_limit(self) -> None:
        tools = _make_tools(15)
        result = filter_tools("anything", tools, max_tools=15)
        assert len(result) == 15

    def test_filters_to_max_tools(self) -> None:
        """40 tools should be trimmed to 15."""
        tools = _make_tools(40)
        result = filter_tools("hello world", tools, max_tools=15, always_include=frozenset())
        assert len(result) == 15

    def test_always_include_tools_present(self) -> None:
        """Pinned tools appear even when they have zero token overlap."""
        # Build 30 generic tools + 1 pinned tool with an unrelated description
        tools = _make_tools(30)
        pinned = _FakeTool("web_search", "Find pages on the internet")
        tools.append(pinned)

        result = filter_tools("unrelated quantum physics topic", tools, max_tools=10)
        result_names = {t.name for t in result}
        assert "web_search" in result_names

    def test_relevant_tools_scored_higher(self) -> None:
        """A message about 'email' should rank an email tool above unrelated ones."""
        email_tool = _FakeTool("send_email", "Send an email message to a recipient")
        weather_tool = _FakeTool("get_weather", "Get current weather forecast")
        calc_tool = _FakeTool("calculator", "Perform math calculations")
        filler = _make_tools(20, prefix="filler")

        all_tools = filler + [weather_tool, email_tool, calc_tool]
        result = filter_tools(
            "send an email to Alice",
            all_tools,
            max_tools=5,
            always_include=frozenset(),
        )
        result_names = [t.name for t in result]
        assert "send_email" in result_names

    def test_empty_message_returns_max(self) -> None:
        """Empty string input should return the first max_tools tools."""
        tools = _make_tools(25)
        result = filter_tools("", tools, max_tools=10)
        assert len(result) == 10
        # Should be the first 10 in order
        assert result == tools[:10]

    def test_pinned_dont_count_against_scored(self) -> None:
        """If 5 tools are pinned, only 10 scored slots remain (max=15)."""
        pinned_names = list(ALWAYS_INCLUDE)[:5]
        pinned_tools = [_FakeTool(n, f"Always-on {n}") for n in pinned_names]
        scored_tools = _make_tools(30, prefix="scored")
        all_tools = pinned_tools + scored_tools

        result = filter_tools("some query", all_tools, max_tools=15)
        assert len(result) == 15

        result_names = {t.name for t in result}
        for pn in pinned_names:
            assert pn in result_names

    def test_custom_always_include(self) -> None:
        """Caller can override the always_include set."""
        special = _FakeTool("my_special_tool", "Very special")
        filler = _make_tools(25)
        all_tools = filler + [special]

        result = filter_tools(
            "nothing relevant",
            all_tools,
            max_tools=5,
            always_include=frozenset({"my_special_tool"}),
        )
        result_names = {t.name for t in result}
        assert "my_special_tool" in result_names


class TestTokenize:
    """Unit tests for the _tokenize helper."""

    def test_strips_punctuation(self) -> None:
        tokens = _tokenize("Hello, world! How's it going?")
        assert "hello" in tokens
        assert "world" in tokens
        assert "how" in tokens
        assert "s" in tokens
        # No punctuation tokens
        assert "," not in tokens
        assert "!" not in tokens

    def test_lowercases(self) -> None:
        tokens = _tokenize("UPPER lower MiXeD")
        assert tokens == {"upper", "lower", "mixed"}

    def test_empty_string(self) -> None:
        assert _tokenize("") == set()

    def test_numbers_included(self) -> None:
        tokens = _tokenize("GPT4 model version 3")
        assert "gpt4" in tokens
        assert "3" in tokens
