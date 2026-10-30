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
#
# Updated 2026-09-02 (fix/deep-agents-usage-parity): the token_usage event is
# the INVOICE, not just cache telemetry, and it billed $0 on every run. Four new
# suites cover the fix: ``TestAccumulateCacheUsage`` gains the output/inclusive
# keys and the TTL-filed cache write; ``TestTheModelComesOffTheResponse`` covers
# ``_model_name_from_chunk``; ``TestTheUsageEventIsTheInvoice`` pins the payload
# ``metering.resolve_cost`` actually reads; ``TestTheStreamAlwaysBills`` drives
# ``run()`` end to end over a fake graph to prove a COLD turn emits, and that a
# mid-stream error, a hard cancel and a soft stop all keep the usage; and
# ``TestTheMeterPricesADeepAgentsRun`` feeds the real payload to the real meter.
"""Tests for the Anthropic prompt-cache monkey-patch in deep_agents.

The patch wraps ``langchain_anthropic.chat_models._format_messages`` to
inject ``cache_control`` into long system blocks. These tests run the
real (patched) function against canned ``SystemMessage`` inputs and
assert the output shape.

Each test resets the ``_ANTHROPIC_PATCHED`` sentinel + the upstream
function reference so tests don't interfere with each other.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from pocketpaw.agents import deep_agents
from pocketpaw.config import Settings


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


def _fresh_acc():
    """The accumulator ``run()`` seeds, in the shape the fold expects."""

    return {
        "input_tokens": 0,
        "inclusive_input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


class _Chunk:
    """Stand-in for a LangChain ``AIMessageChunk``.

    Only the four attributes the stream loop reads are modelled; ``content`` and
    ``tool_call_chunks`` are here so the same object can be fed to ``run()``.
    """

    def __init__(self, usage_metadata=None, response_metadata=None, content=""):
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata
        self.content = content
        self.tool_call_chunks = []


class TestAccumulateCacheUsage:
    """``_accumulate_cache_usage`` — the LangChain usage_metadata → Anthropic-
    native fold the deep_agents token_usage event consumes."""

    def _fresh_acc(self):
        return _fresh_acc()

    def test_folds_cache_read_and_creation(self):
        """LangChain's input_tokens is the TOTAL (cached+uncached); the fold
        derives the uncached remainder and stores native keys."""
        acc = self._fresh_acc()
        chunk = _Chunk(
            {
                "input_tokens": 5000,  # total incl. cache
                "output_tokens": 700,
                "input_token_details": {"cache_read": 4000, "cache_creation": 500},
            }
        )
        deep_agents._accumulate_cache_usage(chunk, acc)
        assert acc["cache_read_input_tokens"] == 4000
        assert acc["cache_creation_input_tokens"] == 500
        assert acc["input_tokens"] == 500  # 5000 - 4000 - 500 uncached remainder
        assert acc["inclusive_input_tokens"] == 5000
        assert acc["output_tokens"] == 700

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
            chunk = _Chunk(
                {
                    "input_tokens": 1000,
                    "output_tokens": 40,
                    "input_token_details": {"cache_read": 900},
                }
            )
            deep_agents._accumulate_cache_usage(chunk, acc)
        assert acc["cache_read_input_tokens"] == 2700
        assert acc["input_tokens"] == 300  # 3 * (1000 - 900)
        assert acc["inclusive_input_tokens"] == 3000
        assert acc["output_tokens"] == 120

    def test_no_usage_metadata_is_noop(self):
        acc = self._fresh_acc()
        deep_agents._accumulate_cache_usage(_Chunk(None), acc)
        deep_agents._accumulate_cache_usage(object(), acc)  # no attr at all
        assert acc == self._fresh_acc()

    def test_output_tokens_survive_the_fold(self):
        """Output is the half of a turn that costs the most — Anthropic bills it
        at ~5x input — and the fold used to drop it entirely."""
        acc = self._fresh_acc()
        deep_agents._accumulate_cache_usage(
            _Chunk({"input_tokens": 900, "output_tokens": 1500}), acc
        )
        assert acc["output_tokens"] == 1500

    def test_the_inclusive_total_is_kept_beside_the_remainder(self):
        """Both numbers are needed and they are NOT the same number: the meter
        and report_savings read the remainder, price_run reads the inclusive
        total because it subtracts the cached portion itself."""
        acc = self._fresh_acc()
        deep_agents._accumulate_cache_usage(
            _Chunk(
                {
                    "input_tokens": 10_000,
                    "input_token_details": {"cache_read": 8000, "cache_creation": 1500},
                }
            ),
            acc,
        )
        assert acc["input_tokens"] == 500
        assert acc["inclusive_input_tokens"] == 10_000

    @pytest.mark.parametrize("ttl_key", ["ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"])
    def test_a_write_filed_under_a_ttl_key_is_not_lost(self, ttl_key):
        """``langchain_anthropic`` zeroes the generic ``cache_creation`` whenever
        a TTL-specific key carries the write, and this backend marks every prefix
        5m or 1h — so reading only the generic key saw zero writes forever."""
        acc = self._fresh_acc()
        deep_agents._accumulate_cache_usage(
            _Chunk(
                {
                    "input_tokens": 6000,
                    "input_token_details": {
                        "cache_read": 3000,
                        "cache_creation": 0,  # zeroed by langchain, on purpose
                        ttl_key: 2000,
                    },
                }
            ),
            acc,
        )
        assert acc["cache_creation_input_tokens"] == 2000
        assert acc["cache_read_input_tokens"] == 3000
        assert acc["input_tokens"] == 1000  # 6000 - 3000 - 2000

    def test_the_three_write_keys_are_never_double_counted(self):
        """The sum is only safe because at most one key is ever set. Pin that:
        an untagged write still lands exactly once."""
        acc = self._fresh_acc()
        deep_agents._accumulate_cache_usage(
            _Chunk(
                {
                    "input_tokens": 4000,
                    "input_token_details": {
                        "cache_creation": 1200,
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                    },
                }
            ),
            acc,
        )
        assert acc["cache_creation_input_tokens"] == 1200


class TestTheModelComesOffTheResponse:
    """``_model_name_from_chunk`` — the only truthful source of the model that
    priced these tokens. Configuration can be an alias, prefixed, or overridden
    per run."""

    def test_reads_langchain_anthropics_model_name(self):
        chunk = _Chunk(response_metadata={"model_name": "claude-sonnet-4-5-20250929"})
        assert deep_agents._model_name_from_chunk(chunk) == "claude-sonnet-4-5-20250929"

    def test_falls_back_to_the_plain_model_key(self):
        """Not every integration spells it ``model_name``."""
        chunk = _Chunk(response_metadata={"model": "gpt-4.1-mini"})
        assert deep_agents._model_name_from_chunk(chunk) == "gpt-4.1-mini"

    def test_a_chunk_with_no_model_is_none_not_a_guess(self):
        """Most chunks have none — the model rides ``message_start`` and the
        usage rides ``message_delta`` — so the caller keeps its last non-None."""
        assert deep_agents._model_name_from_chunk(_Chunk()) is None
        assert deep_agents._model_name_from_chunk(_Chunk(response_metadata={})) is None
        assert deep_agents._model_name_from_chunk(_Chunk(response_metadata={"model": ""})) is None
        assert deep_agents._model_name_from_chunk(object()) is None


class TestTheUsageEventIsTheInvoice:
    """``_build_usage_event`` — the payload ``metering.resolve_cost`` reads.

    Every run on this backend billed $0 because this payload carried no model,
    no output tokens and no cost, and was skipped outright on a cold turn.
    """

    MODEL = "claude-sonnet-4-5-20250929"

    def _warm_acc(self):
        """One warm turn: 12000 inclusive prompt, 9000 read, 2000 written."""
        acc = _fresh_acc()
        deep_agents._accumulate_cache_usage(
            _Chunk(
                {
                    "input_tokens": 12_000,
                    "output_tokens": 800,
                    "input_token_details": {
                        "cache_read": 9000,
                        "ephemeral_5m_input_tokens": 2000,
                    },
                }
            ),
            acc,
        )
        return acc

    def test_the_payload_carries_what_the_meter_actually_reads(self):
        event = deep_agents._build_usage_event(self._warm_acc(), self.MODEL)
        meta = event.metadata

        assert event.type == "token_usage"
        # The remainder, NOT the inclusive total: metering._prompt_tokens adds
        # the cache lines back for any Anthropic-shaped payload.
        assert meta["input_tokens"] == 1000
        assert meta["output_tokens"] == 800
        assert meta["cached_input_tokens"] == 11_000  # read + write
        assert meta["cache_read_tokens"] == 9000
        assert meta["cache_write_tokens"] == 2000
        assert meta["cache_hit_rate"] == 0.75  # 9000 / 12000
        assert meta["cache_est_tokens_saved"] == 8100.0  # 9000 * 0.90
        assert meta["model"] == self.MODEL
        assert meta["backend"] == "deep_agents"
        assert meta["total_cost_usd"] > 0

    def test_the_price_is_taken_on_the_inclusive_total_not_the_remainder(self):
        """price_run subtracts the cached portion itself. Pricing off the
        already-reduced remainder removes those tokens a SECOND time, which is
        a materially smaller bill that still looks like a real number."""
        from datetime import UTC, datetime

        from pocketpaw.usage_tracker import price_run

        acc = self._warm_acc()
        event = deep_agents._build_usage_event(acc, self.MODEL)

        at = datetime.now(tz=UTC)
        correct = price_run(
            self.MODEL,
            input_tokens=12_000,
            output_tokens=800,
            cache_read_tokens=9000,
            cache_write_tokens=2000,
            at=at,
        )
        double_subtracted = price_run(
            self.MODEL,
            input_tokens=1000,  # the remainder — the bug
            output_tokens=800,
            cache_read_tokens=9000,
            cache_write_tokens=2000,
            at=at,
        )
        assert correct is not None
        assert event.metadata["total_cost_usd"] == pytest.approx(float(correct))
        assert float(double_subtracted) < float(correct)

    def test_a_cold_turn_with_no_cache_activity_is_still_billed(self):
        """The old emit was gated on cache activity, so a first message or any
        prompt under the 4000-char marker threshold billed nothing at all."""
        acc = _fresh_acc()
        deep_agents._accumulate_cache_usage(
            _Chunk({"input_tokens": 500, "output_tokens": 120}), acc
        )
        meta = deep_agents._build_usage_event(acc, self.MODEL).metadata

        assert meta["input_tokens"] == 500
        assert meta["output_tokens"] == 120
        assert meta["cached_input_tokens"] == 0
        assert meta["cache_read_tokens"] == 0
        assert meta["cache_write_tokens"] == 0
        assert meta["cache_hit_rate"] == 0.0
        assert meta["model"] == self.MODEL
        assert meta["total_cost_usd"] > 0

    def test_an_unpriceable_model_degrades_to_zero_instead_of_raising(self):
        """One bad id must not take the turn down with it."""
        meta = deep_agents._build_usage_event(self._warm_acc(), "not-a-real-model-xyz").metadata
        assert meta["total_cost_usd"] == 0.0
        assert meta["model"] == "not-a-real-model-xyz"
        assert meta["input_tokens"] == 1000  # counts still reported

    def test_a_run_with_nothing_to_bill_still_builds_a_payload(self):
        meta = deep_agents._build_usage_event(_fresh_acc(), self.MODEL).metadata
        assert meta["total_cost_usd"] == 0.0
        assert meta["input_tokens"] == 0
        assert meta["output_tokens"] == 0


class _FakeAgent:
    """A stand-in for the compiled Deep Agents graph.

    Yields the given chunks in the ``messages`` shape ``run()`` parses, then
    optionally raises — which is how the error and hard-cancel paths are driven.
    """

    def __init__(self, chunks, raises=None):
        self._chunks = chunks
        self._raises = raises

    def astream(self, *args, **kwargs):
        async def _gen():
            for chunk in self._chunks:
                yield {"type": "messages", "data": (chunk, {})}
            if self._raises is not None:
                raise self._raises

        return _gen()


def _backend_over(chunks, *, raises=None, model="anthropic:claude-sonnet-4-5-20250929"):
    """A DeepAgentsBackend whose graph is ``_FakeAgent``.

    Everything between ``run()`` and the stream is stubbed on the INSTANCE, so
    the test needs neither the deepagents SDK nor a provider key, and the real
    accumulate → capture → emit path still runs.
    """
    from pocketpaw.agents.deep_agents import DeepAgentsBackend

    backend = DeepAgentsBackend(Settings(deep_agents_model=model))
    backend._sdk_available = True
    backend._build_model = lambda: object()

    async def _no_mcp():
        return []

    backend._build_mcp_tools = _no_mcp
    backend._get_or_create_agent = lambda *a, **k: _FakeAgent(chunks, raises=raises)
    return backend


def _usage_events(events):
    return [e for e in events if e.type == "token_usage"]


class TestTheStreamAlwaysBills:
    """``run()`` end to end over a fake graph. The defect was invisible at this
    level: nothing errored, the bill was simply zero."""

    WARM = {
        "input_tokens": 12_000,
        "output_tokens": 800,
        "input_token_details": {"cache_read": 9000, "ephemeral_5m_input_tokens": 2000},
    }
    COLD = {"input_tokens": 500, "output_tokens": 120}

    @pytest.mark.asyncio
    async def test_a_cold_turn_still_emits_the_usage_event(self):
        """THE regression. The emit was gated on cache activity, so a turn that
        never touched the cache persisted ``usage: {}``."""
        backend = _backend_over(
            [
                _Chunk(response_metadata={"model_name": "claude-sonnet-4-5-20250929"}),
                _Chunk(self.COLD),
            ]
        )
        events = [e async for e in backend.run("hi")]

        usage = _usage_events(events)
        assert len(usage) == 1
        assert usage[0].metadata["input_tokens"] == 500
        assert usage[0].metadata["output_tokens"] == 120
        assert usage[0].metadata["total_cost_usd"] > 0
        assert events[-1].type == "done"

    @pytest.mark.asyncio
    async def test_the_model_comes_off_the_response_not_configuration(self):
        """The configured id and the served id differ here on purpose — only the
        response says which model actually priced these tokens."""
        backend = _backend_over(
            [
                _Chunk(response_metadata={"model_name": "claude-haiku-4-5-20251001"}),
                _Chunk(self.WARM),
            ],
            model="anthropic:claude-sonnet-4-5-20250929",
        )
        events = [e async for e in backend.run("hi")]

        assert _usage_events(events)[0].metadata["model"] == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_configuration_is_the_fallback_when_no_chunk_names_a_model(self):
        """A provider that never stamps ``response_metadata`` still bills: a weak
        id prices, and None prices at nothing."""
        backend = _backend_over([_Chunk(self.COLD)], model="anthropic:claude-sonnet-4-5-20250929")
        events = [e async for e in backend.run("hi")]

        meta = _usage_events(events)[0].metadata
        # The provider half is stripped — the price library wants the bare id.
        assert meta["model"] == "claude-sonnet-4-5-20250929"
        assert meta["total_cost_usd"] > 0

    @pytest.mark.asyncio
    async def test_a_warm_turn_reports_the_remainder_and_the_cache_lines(self):
        backend = _backend_over([_Chunk(self.WARM)])
        meta = _usage_events([e async for e in backend.run("hi")])[0].metadata

        assert meta["input_tokens"] == 1000
        assert meta["cache_read_tokens"] == 9000
        assert meta["cache_write_tokens"] == 2000
        assert meta["cached_input_tokens"] == 11_000

    @pytest.mark.asyncio
    async def test_usage_from_several_turns_sums_into_one_event(self):
        backend = _backend_over([_Chunk(self.COLD), _Chunk(self.COLD), _Chunk(self.COLD)])
        usage = _usage_events([e async for e in backend.run("hi")])

        assert len(usage) == 1
        assert usage[0].metadata["input_tokens"] == 1500
        assert usage[0].metadata["output_tokens"] == 360

    @pytest.mark.asyncio
    async def test_a_soft_stop_keeps_the_turns_that_finished(self):
        """``stop()`` breaks the loop ABOVE the emit, so the completed turns are
        still billable — but only now that the cache gate is gone.

        Both chunks carry text so the first one reaches the consumer while the
        stream is still running; with silent chunks the first event a consumer
        sees is already the invoice and nothing is being stopped mid-stream.
        """
        backend = _backend_over(
            [_Chunk(self.COLD, content="one"), _Chunk(self.COLD, content="two")]
        )
        events = []
        async for event in backend.run("hi"):
            events.append(event)
            if event.type == "message":
                await backend.stop()

        assert [e.content for e in events if e.type == "message"] == ["one"]
        usage = _usage_events(events)
        assert len(usage) == 1
        assert usage[0].metadata["input_tokens"] == 500  # the one turn that landed
        assert usage[0].metadata["total_cost_usd"] > 0

    @pytest.mark.asyncio
    async def test_usage_survives_a_mid_stream_failure(self):
        """The turns before the failure consumed real tokens and the provider
        has already billed us for them."""
        backend = _backend_over([_Chunk(self.WARM)], raises=RuntimeError("provider blew up"))
        events = [e async for e in backend.run("hi")]

        types = [e.type for e in events]
        usage = _usage_events(events)
        assert len(usage) == 1
        assert usage[0].metadata["input_tokens"] == 1000
        # Usage lands BEFORE the error, and the error path still completes.
        assert types.index("token_usage") < types.index("error")
        assert types[-1] == "done"

    @pytest.mark.asyncio
    async def test_usage_survives_a_hard_cancel(self):
        """CancelledError is a BaseException, so ``except Exception`` never sees
        it. usage_metadata rides the message_delta at the END of each turn, so
        every turn that finished before the cancel is billable."""
        backend = _backend_over([_Chunk(self.WARM)], raises=asyncio.CancelledError())

        events = []
        with pytest.raises(asyncio.CancelledError):
            async for event in backend.run("hi"):
                events.append(event)

        usage = _usage_events(events)
        assert len(usage) == 1
        assert usage[0].metadata["input_tokens"] == 1000
        assert usage[0].metadata["total_cost_usd"] > 0

    @pytest.mark.asyncio
    async def test_closing_the_stream_early_does_not_break_aclose(self):
        """A yield from the GeneratorExit path raises "async generator ignored
        GeneratorExit" out of the consumer's aclose(). The usage for a
        half-consumed run is unrecoverable; crashing the consumer is worse.

        The break has to land MID-stream, which is why the chunk carries text:
        on a silent stream the first event a consumer sees is already the
        invoice, and closing after that never reaches the un-emitted branch.
        """
        backend = _backend_over(
            [_Chunk(self.WARM, content="one"), _Chunk(self.COLD, content="two")]
        )
        stream = backend.run("hi")

        seen = []
        async for event in stream:
            seen.append(event)
            if event.type == "message":
                break

        # Closed with usage accumulated and NOT yet emitted — the branch that
        # a yield would turn into a RuntimeError.
        assert [e.type for e in seen] == ["message"]
        await stream.aclose()  # must not raise

    @pytest.mark.asyncio
    async def test_exactly_one_usage_event_per_run(self):
        """The emitted-flag exists so the error path cannot double-bill a run
        that already reported."""
        backend = _backend_over([_Chunk(self.WARM)])
        assert len(_usage_events([e async for e in backend.run("hi")])) == 1


class TestTheMeterPricesADeepAgentsRun:
    """The round trip that was actually broken: this backend's payload through
    the real ``metering.resolve_cost``."""

    WARM = TestTheStreamAlwaysBills.WARM

    @pytest.fixture
    def resolve_cost(self):
        service = pytest.importorskip(
            "pocketpaw_ee.cloud.metering.service",
            reason="pocketpaw-ee is not installed in this environment",
        )
        return service.resolve_cost

    async def _payload(self):
        backend = _backend_over(
            [
                _Chunk(response_metadata={"model_name": "claude-sonnet-4-5-20250929"}),
                _Chunk(self.WARM),
            ]
        )
        return _usage_events([e async for e in backend.run("hi")])[0].metadata

    @pytest.mark.asyncio
    async def test_the_meter_bills_a_real_amount(self, resolve_cost):
        from datetime import UTC, datetime

        cost = resolve_cost(await self._payload(), at=datetime.now(tz=UTC))

        assert cost.cost_usd > 0
        assert cost.source == "reported"
        assert cost.model == "claude-sonnet-4-5-20250929"

    @pytest.mark.asyncio
    async def test_the_meter_reconstitutes_the_inclusive_prompt(self, resolve_cost):
        """With the reported cost removed the meter prices it itself, which is
        the path that proves ``input_tokens`` is the remainder: _prompt_tokens
        adds the cache lines back to reach 12000."""
        from datetime import UTC, datetime

        payload = await self._payload()
        payload.pop("total_cost_usd")

        service = pytest.importorskip("pocketpaw_ee.cloud.metering.service")
        assert service._prompt_tokens(payload) == (12_000, 9000, 2000)

        cost = resolve_cost(payload, at=datetime.now(tz=UTC))
        assert cost.cost_usd > 0
        assert cost.source == "estimated"

    @pytest.mark.asyncio
    async def test_the_old_payload_billed_nothing(self, resolve_cost):
        """The shape this backend emitted before the fix — no model, no output,
        no cost. Kept as the regression's own headstone."""
        from datetime import UTC, datetime

        payload = await self._payload()
        old = {
            "input_tokens": payload["input_tokens"],
            "cache_read_tokens": payload["cache_read_tokens"],
            "cache_write_tokens": payload["cache_write_tokens"],
            "cache_hit_rate": payload["cache_hit_rate"],
            "cache_est_tokens_saved": payload["cache_est_tokens_saved"],
            "backend": "deep_agents",
        }
        cost = resolve_cost(old, at=datetime.now(tz=UTC))
        assert cost.cost_usd == 0.0
        assert cost.model is None
