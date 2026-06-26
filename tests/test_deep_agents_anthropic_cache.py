# tests/test_deep_agents_anthropic_cache.py
# Created: 2026-05-13 (fix/pocket-specialist-speed) — covers the
# _patch_anthropic_message_serializer monkey-patch that adds Anthropic's
# ``cache_control: ephemeral`` markup to long system messages. Without
# this, the pocket specialist's ~12k-token design-rules prompt is
# re-tokenized on every spec generation; the patch unlocks Anthropic's
# prompt cache so warm calls reuse the prefix at ~10% of the cost.
#
# Updated 2026-06-26 (integration/model-catalog-v2, MCG-11): added
# ``TestSpecialistLivePathTTL`` — exercises the patch on the ACTUAL
# ``POCKET_SPECIALIST_PROMPT`` (the string the live site/pocket-gen run passes
# as system_prompt) and asserts the stable prefix is byte-stable across two
# different briefs AND carries the **1h** cache_control marker. Also covers
# ``_cache_ttl_for_system`` (1h for the ``<pocket-scope>`` prefix, 5m otherwise)
# and ``_accumulate_cache_usage`` (the LangChain→native usage fold the
# token_usage telemetry consumes).
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


# ---------------------------------------------------------------------------
# MCG-11 — the LIVE path. The pocket/site generator passes a STRING system
# prompt (POCKET_SPECIALIST_PROMPT + appended hints) to backend.run; the patch
# below is what actually places the cache marker on production traffic. These
# tests assert the margin lands at 1h on the real prefix, across briefs.
# ---------------------------------------------------------------------------


class TestSpecialistTTLSelector:
    """``_cache_ttl_for_system`` — the byte-stable content gate."""

    def test_specialist_prefix_gets_1h(self):
        from pocketpaw.ripple import POCKET_SPECIALIST_PROMPT

        assert deep_agents._cache_ttl_for_system(POCKET_SPECIALIST_PROMPT) == "1h"

    def test_pocket_scope_sentinel_triggers_1h(self):
        assert deep_agents._cache_ttl_for_system("...<pocket-scope>...") == "1h"

    def test_generic_prompt_stays_5m(self):
        """A long non-specialist prompt (chat / home / one-shot) keeps the 5m
        default so its first-call write cost isn't doubled."""
        assert deep_agents._cache_ttl_for_system("You are a helpful assistant. " * 50) == "5m"


class TestSpecialistLivePathTTL:
    """Exercise the real patch on the real specialist prompt — the string the
    live run actually hands to backend.run."""

    def _make_specialist_system(self, brief: str) -> str:
        """Mirror the live assembly: stable POCKET_SPECIALIST_PROMPT prefix +
        a per-brief variable suffix appended AFTER it (as
        runtime._build_system_prompt does)."""
        from pocketpaw.ripple import POCKET_SPECIALIST_PROMPT

        return POCKET_SPECIALIST_PROMPT + "\n\nCALLER METADATA:\n  brief: " + brief

    def test_specialist_prefix_marked_at_1h(self, fresh_patch):
        """The live specialist system string is wrapped into one block carrying
        the 1h cache_control marker (not 5m)."""
        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages

        sys_str = self._make_specialist_system("a dentist landing page")
        system, _ = _format_messages([SystemMessage(content=sys_str), HumanMessage(content="go")])
        assert isinstance(system, list)
        assert len(system) == 1
        assert system[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_prefix_byte_stable_across_two_briefs(self, fresh_patch):
        """THE live margin property: two DIFFERENT briefs produce system blocks
        whose cached PREFIX (POCKET_SPECIALIST_PROMPT) is byte-identical, so the
        provider cache hits on the 2nd brief. The marker is the same 1h block."""
        from pocketpaw.ripple import POCKET_SPECIALIST_PROMPT

        deep_agents._patch_anthropic_message_serializer()
        from langchain_anthropic.chat_models import _format_messages

        a_str = self._make_specialist_system("a dentist in Austin, blue theme")
        b_str = self._make_specialist_system("a law firm in NYC, dark theme, 4 services")
        sys_a, _ = _format_messages([SystemMessage(content=a_str), HumanMessage(content="go")])
        sys_b, _ = _format_messages([SystemMessage(content=b_str), HumanMessage(content="go")])

        # Both wrap to a single block; the marker is identical (1h).
        assert (
            sys_a[0]["cache_control"]
            == sys_b[0]["cache_control"]
            == {
                "type": "ephemeral",
                "ttl": "1h",
            }
        )
        # The cached prefix — the stable specialist prompt — is a byte-identical
        # leading slice of both system texts (only the trailing brief differs).
        text_a = sys_a[0]["text"]
        text_b = sys_b[0]["text"]
        assert text_a.startswith(POCKET_SPECIALIST_PROMPT)
        assert text_b.startswith(POCKET_SPECIALIST_PROMPT)
        assert text_a[: len(POCKET_SPECIALIST_PROMPT)] == text_b[: len(POCKET_SPECIALIST_PROMPT)]
        # ... and the variable tails legitimately differ.
        assert text_a != text_b


class TestAccumulateCacheUsage:
    """``_accumulate_cache_usage`` — the LangChain usage_metadata → Anthropic-
    native fold the deep_agents token_usage telemetry consumes."""

    class _Chunk:
        def __init__(self, usage_metadata):
            self.usage_metadata = usage_metadata

    def _fresh_acc(self):
        return {
            "input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    def test_folds_cache_read_and_creation(self):
        """LangChain's input_tokens is the TOTAL (cached+uncached); the fold
        derives the uncached remainder and stores native keys."""
        acc = self._fresh_acc()
        chunk = self._Chunk(
            {
                "input_tokens": 5000,  # total incl. cache
                "input_token_details": {"cache_read": 4000, "cache_creation": 500},
            }
        )
        deep_agents._accumulate_cache_usage(chunk, acc)
        assert acc["cache_read_input_tokens"] == 4000
        assert acc["cache_creation_input_tokens"] == 500
        assert acc["input_tokens"] == 500  # 5000 - 4000 - 500 uncached remainder

        # And report_savings reads the accumulated native dict correctly:
        from pocketpaw.llm.caching import report_savings

        s = report_savings(acc)
        assert s.provider == "anthropic"
        assert s.cache_read_tokens == 4000
        assert s.prompt_tokens == 5000  # uncached(500) + read(4000) + write(500)

    def test_sums_across_multiple_turns(self):
        """A tool loop yields several AI turns; usage SUMS."""
        acc = self._fresh_acc()
        for _ in range(3):
            chunk = self._Chunk({"input_tokens": 1000, "input_token_details": {"cache_read": 900}})
            deep_agents._accumulate_cache_usage(chunk, acc)
        assert acc["cache_read_input_tokens"] == 2700
        assert acc["input_tokens"] == 300  # 3 * (1000 - 900)

    def test_no_usage_metadata_is_noop(self):
        acc = self._fresh_acc()
        deep_agents._accumulate_cache_usage(self._Chunk(None), acc)
        deep_agents._accumulate_cache_usage(object(), acc)  # no attr at all
        assert acc == self._fresh_acc()
