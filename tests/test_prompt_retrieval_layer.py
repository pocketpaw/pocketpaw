# tests/test_prompt_retrieval_layer.py
# Created: 2026-08-02 (PA-3, feat/prompt-assembler-seam) — pins the retrieval
# layer: the per-message soul recall, extracted out of the legacy tail and made
# to DECLARE itself volatile (``cache_key=None``) rather than be recovered as
# volatile by string surgery downstream.
#
# The properties held here:
#   1. Two turns differing ONLY in the user's message produce different prompt
#      TEXT and one ``stable_digest``. That is the whole task: a backend keying
#      its agent cache on the digest reuses the warm agent across turns even
#      though the recall block changed underneath it. Give this layer a real
#      cache key and this test fails — which is what makes it load-bearing
#      rather than decorative.
#   2. The inverse, stated so the trade-off is on the record: recall present
#      and recall absent hash ALIKE. A volatile layer contributes nothing to the
#      digest, so a soul that starts (or stops) answering does not invalidate a
#      cached agent. That is the deliberate cost of (1), not an oversight.
#   3. Volatility does not swallow real change: a different agent identity still
#      moves the digest with the recall block in the prompt.
#   4. The recall block's GUARDS are unchanged from ``passthrough.py``. This
#      layer runs per turn against a soul doing I/O; a layer that RAISES where
#      the old code logged at debug turns a degraded prompt into a failed turn.
#   5. ``ClaudeSDKBackend._behavior_prefix`` is unchanged by the reorder — the
#      warm-client cache key must not move, and this task has no business
#      moving it. Held against the pre-reorder text captured verbatim.
#
# THE ORDER MOVED, AND THAT IS INTENDED — read this before "fixing" a golden.
#   old: identity → surface → instructions → RECALL → knowledge
#   new: identity → surface → instructions → knowledge → RECALL
# Extracting recall into its own layer forces the choice, because layer order
# IS tuple order and this task does not split ``instructions`` out of the tail
# (PA-4 does). Volatile-last is the module's stated ordering principle, both
# blocks are per-message volatile so their relative order cannot affect prompt
# caching either way, and on the U-curve the end position goes to the block
# chosen for THIS question rather than to a KB dump. See ``_LEGACY_ORDER_TEXT``
# below for the exact bytes this replaced.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from pocketpaw.agents.pool import _SYSTEM_PROMPT_LAYERS, AgentPool
from pocketpaw.prompt import (
    AssembledPrompt,
    LayerOutput,
    PromptContext,
    RetrievalLayer,
    prompt_layer_registry,
)

pytestmark = pytest.mark.asyncio

_STAMP = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class _FakeSoul:
    """Answers ``context_for`` with a fixed block that ECHOES the query.

    Echoing is what makes the recall genuinely per-turn: without it two turns
    would render identical text and every digest test below would pass for the
    wrong reason. ``kwargs`` are recorded so the call's shape can be pinned.
    """

    def __init__(self, recall: str = "- talked about tea yesterday") -> None:
        self._recall = recall
        self.calls: list[tuple[str, dict]] = []

    async def context_for(self, message: str, **kwargs) -> str:
        self.calls.append((message, kwargs))
        return f"{self._recall} (asked: {message})" if self._recall else ""


class _RaisingSoul:
    async def context_for(self, message: str, **kwargs):  # noqa: ARG002
        raise RuntimeError("soul store unreachable")


def _instance(*, soul=None, persona: str = "WHO I AM"):
    """A minimal instance. No bootstrap provider, so identity resolves through
    the config branch and nothing but the recall varies between turns."""
    return SimpleNamespace(
        backend=None,
        soul_manager=SimpleNamespace(bootstrap_provider=None, soul=soul) if soul else None,
        config={"soul_persona": persona, "system_prompt": ""},
        created_from_updated_at=_STAMP,
        last_active=datetime.now(UTC),
        active_runs=0,
    )


async def _assemble(**kwargs) -> AssembledPrompt:
    """Assemble through the REAL seam, so the layer order under test is the one
    the cloud path uses rather than one this test made up — and so a change to
    ``RetrievalLayer``'s cache key is visible here."""
    return await AgentPool()._assemble_system_prompt(
        kwargs.pop("instance", None) or _instance(),
        agent_id=kwargs.pop("agent_id", "agent-1"),
        message=kwargs.pop("message", ""),
        instructions=kwargs.pop("instructions", ""),
        knowledge_context=kwargs.pop("knowledge_context", ""),
        system_message_override=kwargs.pop("system_message_override", None),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1 — the point of the task
# ---------------------------------------------------------------------------


async def test_two_turns_differing_only_in_the_message_share_one_digest():
    """Different prompt text, one digest — so the warm agent is REUSED.

    The recall block is rebuilt per turn against the user's message, so turn 2's
    prompt genuinely differs from turn 1's. Nothing about WHO the agent is or
    WHERE the user is has changed, so a backend caching an agent object keyed on
    ``stable_digest`` must hand back the same agent. A retrieval layer with a
    real cache key breaks this and rebuilds the agent on every single turn —
    the cost PR #1842 refused to pay.
    """
    instance = _instance(soul=_FakeSoul())

    turn_1 = await _assemble(instance=instance, message="what time do you open?")
    turn_2 = await _assemble(instance=instance, message="and on sundays?")

    assert "what time do you open?" in turn_1.text
    assert "and on sundays?" in turn_2.text
    assert turn_1.text != turn_2.text, "the fixture is not exercising the recall at all"
    assert turn_1.stable_digest == turn_2.stable_digest, (
        "a per-message layer must declare itself volatile, or every turn rebuilds the agent"
    )


async def test_the_retrieval_layer_declares_itself_volatile():
    """The mechanism behind the test above, held directly.

    ``cache_key=None`` is the layer's answer to the one question ``LayerOutput``
    exists to force. Held on the layer rather than only through the seam so the
    claim is legible at the place it is made.
    """
    out = await RetrievalLayer().render(_ctx(_instance(soul=_FakeSoul()), message="hi"))
    assert out.text.startswith("## Relevant Past Memories")
    assert out.cache_key is None


async def test_the_retrieval_layer_is_the_last_layer_assembled():
    """Order is behaviour. Stable first, volatile last — and last is also the
    end of the prompt, the position the model attends best, which the block
    chosen for THIS question earns over a knowledge-base dump.

    Held as the PROPERTY this test names rather than as the whole tuple. PA-4
    inserted ``instructions`` and PA-5 inserts ``atlas`` and ``user``; none of
    them touch what this file is about, and a test that has to be rewritten on
    every insertion is a test nobody reads before rewriting. The full order
    contract lives in ``tests/test_prompt_instructions_layer.py``.
    """
    assert _SYSTEM_PROMPT_LAYERS[-1] == "retrieval"
    assert "retrieval" in prompt_layer_registry.list()


# ---------------------------------------------------------------------------
# 2 — the inverse: what volatility COSTS, stated rather than discovered
# ---------------------------------------------------------------------------


async def test_a_turn_with_recall_and_one_without_hash_alike():
    """The must-not-collide half, pointed the honest way.

    A volatile layer contributes NOTHING to the digest, so "the soul answered"
    and "the soul had nothing" are one identity. Different text, one digest —
    deliberately, because the alternative is invalidating a cached agent every
    time a recall lands. Pinned so a future keyed-retrieval change has to come
    here and argue with it rather than silently pass.
    """
    with_recall = await _assemble(instance=_instance(soul=_FakeSoul()), message="tea?")
    without = await _assemble(instance=_instance(soul=_FakeSoul(recall="")), message="tea?")

    assert "## Relevant Past Memories" in with_recall.text
    assert "## Relevant Past Memories" not in without.text
    assert with_recall.stable_digest == without.stable_digest


async def test_volatility_does_not_swallow_a_real_identity_change():
    """The other must-not-collide half, and the one that would actually hurt.

    Excluding the recall from the digest must not make the digest deaf. Same
    message, same recall, DIFFERENT agent — a backend keying on the digest must
    still refuse to serve one agent's cached object to the other.
    """
    soul = _FakeSoul()
    one = await _assemble(instance=_instance(soul=soul), agent_id="agent-1", message="tea?")
    two = await _assemble(instance=_instance(soul=soul), agent_id="agent-2", message="tea?")

    assert one.stable_digest != two.stable_digest


# ---------------------------------------------------------------------------
# The bytes — the order that moved, and the order it moved from
# ---------------------------------------------------------------------------

# Captured VERBATIM from the pre-PA-3 assembled prompt (commit f58b20fe, the
# ``_GOLDEN_SOUL_PATH`` constant in tests/test_prompt_assembler.py). Kept here
# as the anchor for the ``_behavior_prefix`` equality below: the prefix must be
# byte-identical across this reorder, or the warm-client cache key moved and
# every live session pays a reconnect.
_LEGACY_ORDER_TEXT = (
    "WHO I AM\n"
    "\n"
    "RIPPLE LAW: narrate before every tool call.\n"
    "\n"
    "## Relevant Past Memories\n"
    "Below are memories from previous conversations that are relevant to the "
    "current question. Use them to provide continuity and a personalized "
    "response.\n"
    "\n"
    "- talked about tea yesterday (asked: what time do you open?)\n"
    "\n"
    "## Your Knowledge Base\n"
    "Use the following information from your knowledge base to answer questions. "
    "Always reference this data when relevant instead of making things up or "
    "using tools to search.\n"
    "\n"
    "Acme Dental opens at 9am."
)

_NEW_ORDER_TEXT = (
    "WHO I AM\n"
    "\n"
    "RIPPLE LAW: narrate before every tool call.\n"
    "\n"
    "## Your Knowledge Base\n"
    "Use the following information from your knowledge base to answer questions. "
    "Always reference this data when relevant instead of making things up or "
    "using tools to search.\n"
    "\n"
    "Acme Dental opens at 9am.\n"
    "\n"
    "## Relevant Past Memories\n"
    "Below are memories from previous conversations that are relevant to the "
    "current question. Use them to provide continuity and a personalized "
    "response.\n"
    "\n"
    "- talked about tea yesterday (asked: what time do you open?)"
)


async def _full_prompt() -> AssembledPrompt:
    return await _assemble(
        instance=_instance(soul=_FakeSoul()),
        message="what time do you open?",
        instructions="RIPPLE LAW: narrate before every tool call.",
        knowledge_context="Acme Dental opens at 9am.",
    )


async def test_the_recall_block_now_renders_after_the_knowledge_wrapper():
    """The golden for the new order, written out in full so the move is a diff
    someone can read rather than a hash that changed."""
    assembled = await _full_prompt()
    assert assembled.text == _NEW_ORDER_TEXT
    assert assembled.text != _LEGACY_ORDER_TEXT, "the reorder did not happen"


# ---------------------------------------------------------------------------
# 5 — the warm-client cache key did NOT move
# ---------------------------------------------------------------------------


async def test_the_behaviour_prefix_survives_the_reorder():
    """``_behavior_prefix`` cuts at the EARLIEST volatile marker, taking
    ``min()`` across them. Both orders put a marker immediately after the
    authoritative ``instructions``, so which of the two blocks comes first is
    invisible to the cut — only WHERE the volatile region starts matters, and
    that did not move.

    This is the assertion that says the reorder cost nothing: a moved prefix
    would change every live session's warm-client key and force a reconnect.
    """
    assert ClaudeSDKBackend._behavior_prefix(_NEW_ORDER_TEXT) == ClaudeSDKBackend._behavior_prefix(
        _LEGACY_ORDER_TEXT
    )


async def test_the_behaviour_prefix_keeps_the_stable_blocks_and_drops_both_volatile_ones():
    """The absolute form, so the equality above cannot pass by both sides being
    empty (or both being the whole prompt).

    The prefix is exactly identity + instructions; NEITHER volatile block is in
    it. Assembled through the real seam rather than from the constant, so a
    layer that renders somewhere unexpected is caught here.
    """
    assembled = await _full_prompt()
    prefix = ClaudeSDKBackend._behavior_prefix(assembled.text)

    assert prefix == "WHO I AM\n\nRIPPLE LAW: narrate before every tool call."
    assert "## Relevant Past Memories" not in prefix
    assert "## Your Knowledge Base" not in prefix


_SURFACE = '<current-pocket id="A" widgets="2" />'


async def test_the_full_prompt_keeps_every_stable_block_in_the_behaviour_prefix():
    """The whole prompt at once — identity, surface, instructions, AND both
    volatile blocks — because that is the shape a real cloud turn has and the
    one the warm-client key is computed over.

    Asserted as an EQUALITY, not a containment: the prefix is exactly the three
    stable blocks, so this catches the cut landing too early (a stable block
    lost, and every warm client invalidated) as well as too late (a volatile
    block hashed, and every turn rebuilding). Containment would catch neither.

    It also re-proves PA-2's property with the recall extracted: the surface
    preamble stays ABOVE the volatile region, or navigating between pockets
    stops invalidating the warm client.
    """
    assembled = await _assemble(
        instance=_instance(soul=_FakeSoul()),
        message="what time do you open?",
        instructions="RIPPLE LAW: narrate.",
        knowledge_context="Acme Dental opens at 9am.",
        surface_preamble=_SURFACE,
        surface_cache_key="pocket:A:rev1",
    )

    assert "## Your Knowledge Base" in assembled.text, "the fixture must carry BOTH volatile blocks"
    assert "## Relevant Past Memories" in assembled.text
    assert (
        ClaudeSDKBackend._behavior_prefix(assembled.text)
        == f"WHO I AM\n\n{_SURFACE}\n\nRIPPLE LAW: narrate."
    )


async def test_the_full_prompts_behaviour_prefix_is_what_the_legacy_order_produced():
    """The same full prompt, cut both ways. This is the assertion that says the
    reorder did not move a single live session's warm-client key: the legacy
    byte order and the shipped one reduce to identical bytes."""
    assembled = await _assemble(
        instance=_instance(soul=_FakeSoul()),
        message="what time do you open?",
        instructions="RIPPLE LAW: narrate.",
        knowledge_context="Acme Dental opens at 9am.",
        surface_preamble=_SURFACE,
        surface_cache_key="pocket:A:rev1",
    )
    head, _, tail = assembled.text.partition("\n\n## Your Knowledge Base")
    knowledge_block, _, recall_block = f"## Your Knowledge Base{tail}".partition(
        "\n\n## Relevant Past Memories"
    )
    legacy_order = f"{head}\n\n## Relevant Past Memories{recall_block}\n\n{knowledge_block}"

    assert legacy_order != assembled.text, "the reconstruction must actually differ"
    assert ClaudeSDKBackend._behavior_prefix(legacy_order) == ClaudeSDKBackend._behavior_prefix(
        assembled.text
    )


async def test_prewarm_and_turn_one_hash_the_same_behaviour_prefix():
    """Criterion 5: ``prewarm`` passes ``message=""``, so the recall layer must
    render EMPTY there — the same reason it could be passed empty when it lived
    inside the tail. If extraction had changed that, the prewarmed client would
    hash differently from turn 1 and turn 1 would evict the client the prewarm
    just paid ~12s to build.
    """
    instance = _instance(soul=_FakeSoul())
    prewarmed = await _assemble(
        instance=instance,
        message="",
        knowledge_context="",
        instructions="RIPPLE LAW: narrate before every tool call.",
    )
    turn_one = await _assemble(
        instance=instance,
        message="what time do you open?",
        knowledge_context="Acme Dental opens at 9am.",
        instructions="RIPPLE LAW: narrate before every tool call.",
    )

    assert "## Relevant Past Memories" not in prewarmed.text, "prewarm must not run the recall"
    assert prewarmed.text != turn_one.text, "the volatile tail must differ, or this proves nothing"
    assert ClaudeSDKBackend._behavior_prefix(prewarmed.text) == ClaudeSDKBackend._behavior_prefix(
        turn_one.text
    )


# ---------------------------------------------------------------------------
# 4 — the guards, unchanged from passthrough.py
# ---------------------------------------------------------------------------


def _ctx(instance, *, message: str = "") -> PromptContext:
    return PromptContext(
        instance=instance,
        agent_id="agent-1",
        message=message,
        instructions="",
        knowledge_context="",
        system_message_override=None,
    )


async def test_no_soul_manager_renders_nothing():
    out = await RetrievalLayer().render(_ctx(_instance(), message="tea?"))
    assert out == LayerOutput(text="", cache_key=None)


async def test_a_soul_manager_with_no_soul_renders_nothing():
    instance = SimpleNamespace(soul_manager=SimpleNamespace(bootstrap_provider=None, soul=None))
    out = await RetrievalLayer().render(_ctx(instance, message="tea?"))
    assert out == LayerOutput(text="", cache_key=None)


@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
async def test_a_blank_message_does_not_query_the_soul(message):
    """``ctx.message.strip()``, not ``ctx.message`` — a whitespace-only message
    is the prewarm case and must not pay a soul query."""
    soul = _FakeSoul()
    out = await RetrievalLayer().render(_ctx(_instance(soul=soul), message=message))
    assert out == LayerOutput(text="", cache_key=None)
    assert soul.calls == [], "the soul was queried on a blank message"


async def test_an_empty_recall_renders_nothing():
    """A soul that answers with nothing contributes no header — an empty
    ``## Relevant Past Memories`` section is noise the model has to read."""
    out = await RetrievalLayer().render(_ctx(_instance(soul=_FakeSoul(recall="")), message="tea?"))
    assert out == LayerOutput(text="", cache_key=None)


async def test_a_failing_soul_degrades_the_prompt_instead_of_failing_the_turn():
    """Criterion 3, and the one that would hurt in production.

    ``passthrough.py`` swallowed a bare ``Exception`` at ``logger.debug``. The
    assembler's guard would catch a raise here too — but it would also record a
    ``DroppedLayer`` and contribute a FAILURE key, which for a KEYED layer is
    the right answer and for this one is noise on a path the old code treated as
    routine. The layer keeps its own guard, so a flaky soul store is invisible.
    """
    layer = RetrievalLayer()
    out = await layer.render(_ctx(_instance(soul=_RaisingSoul()), message="tea?"))
    assert out == LayerOutput(text="", cache_key=None)


async def test_a_failing_soul_does_not_show_up_as_a_dropped_layer():
    """The same guard, observed through the seam: no drop is reported, and the
    rest of the prompt is intact."""
    assembled = await _assemble(
        instance=_instance(soul=_RaisingSoul()),
        message="tea?",
        instructions="RIPPLE LAW: narrate.",
    )
    assert assembled.dropped == []
    assert assembled.text == "WHO I AM\n\nRIPPLE LAW: narrate."


async def test_the_soul_is_queried_with_the_same_arguments_as_before():
    """The call's shape is behaviour: ``max_memories=5`` bounds the block's
    size, and both ``include_*`` flags are False because that material already
    reaches the prompt through the identity layer's soul bootstrap — turning
    either on would duplicate it under a header claiming it is recall."""
    soul = _FakeSoul()
    await RetrievalLayer().render(_ctx(_instance(soul=soul), message="what time do you open?"))

    assert soul.calls == [
        (
            "what time do you open?",
            {"max_memories": 5, "include_state": False, "include_self_model": False},
        )
    ]


async def test_the_layer_is_queried_with_the_raw_message_not_the_stripped_one():
    """The guard strips to DECIDE; the query passes the message through. Pinned
    because ``passthrough.py`` did exactly this and a tidy-up would not notice
    it had changed what the soul is asked."""
    soul = _FakeSoul()
    await RetrievalLayer().render(_ctx(_instance(soul=soul), message="  tea?  "))
    assert soul.calls[0][0] == "  tea?  "
