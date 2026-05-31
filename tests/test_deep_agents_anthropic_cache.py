# tests/test_deep_agents_anthropic_cache.py
# Created: 2026-05-13 (fix/pocket-specialist-speed) — covers the
# _patch_anthropic_message_serializer monkey-patch that adds Anthropic's
# ``cache_control: ephemeral`` markup to long system messages. Without
# this, the pocket specialist's ~12k-token design-rules prompt is
# re-tokenized on every spec generation; the patch unlocks Anthropic's
# prompt cache so warm calls reuse the prefix at ~10% of the cost.
"""Tests for the Anthropic prompt-cache monkey-patch in deep_agents.

The patch wraps ``langchain_anthropic.chat_models._format_messages`` to
inject ``cache_control`` into long system blocks. These tests run the
real (patched) function against canned ``SystemMessage`` inputs and
assert the output shape.

Each test resets the ``_ANTHROPIC_PATCHED`` sentinel + the upstream
function reference so tests don't interfere with each other.
"""

from __future__ import annotations

import importlib

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from pocketpaw.agents import deep_agents


@pytest.fixture
def fresh_patch(monkeypatch):
    """Reset the patch sentinel + restore the original ``_format_messages``
    around each test so we exercise the wrapping logic deterministically."""

    from langchain_anthropic import chat_models as _ac

    # Re-import to recover the pristine implementation in case a prior
    # test session left the module in a patched state.
    importlib.reload(_ac)
    monkeypatch.setattr(deep_agents, "_ANTHROPIC_PATCHED", False)
    yield
    # Reload again to leave the next test with a pristine module.
    importlib.reload(_ac)


@pytest.fixture
def long_prompt() -> str:
    """A system prompt comfortably above the cache threshold (4000 chars)."""

    return "Design rule. " * 400  # ~5200 chars


@pytest.fixture
def short_prompt() -> str:
    """A system prompt comfortably below the threshold."""

    return "You are a helpful assistant."


class TestAnthropicCachePatch:
    def test_long_string_system_gets_cache_control(self, fresh_patch, long_prompt):
        """A long string-typed system message lifts into a single-block
        list carrying cache_control."""
        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages

        system, _ = _format_messages(
            [SystemMessage(content=long_prompt), HumanMessage(content="hi")]
        )
        assert isinstance(system, list)
        assert len(system) == 1
        block = system[0]
        assert block["type"] == "text"
        assert block["text"] == long_prompt
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_short_string_system_left_alone(self, fresh_patch, short_prompt):
        """A short system message stays a plain string — caching overhead
        outweighs savings on small prompts."""
        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages

        system, _ = _format_messages(
            [SystemMessage(content=short_prompt), HumanMessage(content="hi")]
        )
        assert system == short_prompt

    def test_long_block_list_tags_last_text_block(self, fresh_patch):
        """A pre-blocked system whose total text exceeds the threshold
        gets cache_control on its LAST text block (longest cacheable
        prefix). Earlier blocks remain untagged so the cache breakpoint
        sits at the very tail of the stable prefix."""
        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages

        blocks = [
            {"type": "text", "text": "Header block A. " * 100},
            {"type": "text", "text": "Header block B. " * 200},
        ]
        system, _ = _format_messages([SystemMessage(content=blocks), HumanMessage(content="hi")])
        assert isinstance(system, list)
        assert len(system) == 2
        # First block untagged, last block carries cache_control.
        assert "cache_control" not in system[0]
        assert system[1]["cache_control"] == {"type": "ephemeral"}

    def test_already_cached_blocks_not_double_tagged(self, fresh_patch):
        """If the caller pre-tagged a block, the patch leaves the list
        alone — no shifting of the cache breakpoint."""
        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages

        blocks = [
            {
                "type": "text",
                "text": "Pre-tagged. " * 300,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": "Trailing. " * 200},
        ]
        system, _ = _format_messages([SystemMessage(content=blocks), HumanMessage(content="hi")])
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        # Patch must not add a second cache breakpoint.
        assert "cache_control" not in system[1]

    def test_short_block_list_left_alone(self, fresh_patch):
        """Pre-blocked systems below the threshold are passed through
        unchanged — no cache_control added."""
        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages

        blocks = [{"type": "text", "text": "Small system."}]
        system, _ = _format_messages([SystemMessage(content=blocks), HumanMessage(content="hi")])
        assert isinstance(system, list)
        assert "cache_control" not in system[0]

    def test_idempotent(self, fresh_patch, long_prompt):
        """Calling the patch twice does not stack — the second invocation
        is a no-op."""
        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages as first

        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages as second

        assert first is second  # same function object after the second call

        system, _ = first([SystemMessage(content=long_prompt), HumanMessage(content="hi")])
        # Exactly one cache breakpoint after two patch installs.
        assert isinstance(system, list)
        assert len(system) == 1
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_formatted_messages_passthrough(self, fresh_patch, long_prompt):
        """The non-system half of the conversation is forwarded verbatim
        — the patch only touches the system slot."""
        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages

        _, formatted = _format_messages(
            [
                SystemMessage(content=long_prompt),
                HumanMessage(content="user message"),
            ]
        )
        # Exactly one user message in the conversation.
        assert len(formatted) == 1
        assert formatted[0]["role"] == "user"
