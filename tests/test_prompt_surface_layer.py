# tests/test_prompt_surface_layer.py
# Created: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — pins the surface
# layer: the block that says WHERE the user is, and the key that says what it
# was built from.
#
# The properties held here are the ones a user would feel if they broke:
#   1. Navigating from pocket A to pocket B moves ``stable_digest``, so a
#      backend caching an agent object rebuilds instead of describing pocket A
#      in a chat about pocket B.
#   2. Two turns on the SAME unchanged surface hold one digest. This is the
#      half a careless fix to (1) destroys — key on anything per-turn and every
#      turn rebuilds the agent, which is the cost PR #1842 refused to pay.
#   3. Same text, different key must NOT collide, and same key, different text
#      must NOT move the digest. Together they pin that this layer hashes the
#      KEY it was handed and never the text — which is what leaves each EE
#      handler free to decide what its key tracks. (Today they all track the
#      rendered text, having weighed it per handler; the layer must not bake
#      that in, or the decision stops being theirs.)
#
# The second half is PA-1's ``test_the_digest_is_not_a_hash_of_the_text`` at
# the assembler. It is re-proven through the surface layer specifically,
# because that is the layer where someone would be most tempted to hash the
# text here and drop the threading.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.pool import _SYSTEM_PROMPT_LAYERS, AgentPool
from pocketpaw.prompt import AssembledPrompt, PromptContext, SurfaceContextLayer

pytestmark = pytest.mark.asyncio

_STAMP = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

# Two preambles for the same pocket, before and after an edit the render shows.
_POCKET_A = '<surface kind="pocket" route="/pockets/A" />\n<current-pocket id="A" widgets="2" />'
_POCKET_B = '<surface kind="pocket" route="/pockets/B" />\n<current-pocket id="B" widgets="9" />'


def _instance(*, persona: str = "WHO I AM"):
    """A minimal instance: no soul, so identity resolves through the config
    branch and nothing but the surface varies between the tests below."""
    return SimpleNamespace(
        backend=None,
        soul_manager=None,
        config={"soul_persona": persona, "system_prompt": ""},
        created_from_updated_at=_STAMP,
        last_active=datetime.now(UTC),
        active_runs=0,
    )


async def _assemble(**kwargs) -> AssembledPrompt:
    """Assemble through the real seam, so the layer ORDER under test is the one
    the cloud path actually uses rather than one this test made up."""
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
# The layer is wired into the cloud path at all
# ---------------------------------------------------------------------------


async def test_the_surface_layer_is_assembled_between_identity_and_the_tail():
    """Order is behaviour, not decoration: above the tail is what puts the
    preamble outside the "Your Knowledge Base" framing it used to arrive in,
    and outside the volatile region the Claude SDK strips from its client key."""
    assert _SYSTEM_PROMPT_LAYERS == ("identity", "surface", "legacy_tail")

    assembled = await _assemble(
        instructions="RIPPLE LAW: narrate before every tool call.",
        knowledge_context="Acme Dental opens at 9am.",
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    )

    assert assembled.text == (
        "WHO I AM\n\n" + _POCKET_A + "\n\n"
        "RIPPLE LAW: narrate before every tool call.\n\n"
        "## Your Knowledge Base\n"
        "Use the following information from your knowledge base to answer questions. "
        "Always reference this data when relevant instead of "
        "making things up or using tools to search.\n\n"
        "Acme Dental opens at 9am."
    )


# ---------------------------------------------------------------------------
# 1 — navigation moves the digest
# ---------------------------------------------------------------------------


async def test_navigating_from_one_pocket_to_another_changes_the_digest():
    a = await _assemble(surface_preamble=_POCKET_A, surface_cache_key="pocket:A:rev1")
    b = await _assemble(surface_preamble=_POCKET_B, surface_cache_key="pocket:B:rev1")
    assert a.stable_digest != b.stable_digest


async def test_arriving_on_a_surface_from_nowhere_changes_the_digest():
    """The other half of navigation: a session that had no surface and now has
    one. The layer contributes no key when there is nothing to key, so the two
    must still be told apart."""
    none = await _assemble()
    some = await _assemble(surface_preamble=_POCKET_A, surface_cache_key="pocket:A:rev1")
    assert none.stable_digest != some.stable_digest


# ---------------------------------------------------------------------------
# 2 — an unchanged surface holds the digest still
# ---------------------------------------------------------------------------


async def test_two_turns_on_the_same_unchanged_pocket_hold_one_digest():
    """The cache-protecting half. The turns differ in everything per-turn —
    the message, the retrieved knowledge — and agree on the surface."""
    first = await _assemble(
        message="what is on this dashboard?",
        knowledge_context="Acme Dental opens at 9am.",
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    )
    second = await _assemble(
        message="add a chart",
        knowledge_context="Acme Dental closes at 5pm.",
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    )
    assert first.text != second.text, "the turns must actually differ, or this proves nothing"
    assert first.stable_digest == second.stable_digest


# ---------------------------------------------------------------------------
# 3 — the collision half: same text, different key
# ---------------------------------------------------------------------------


async def test_two_preambles_with_one_text_and_two_keys_do_not_collide():
    """The digest follows the KEY, so a handler that has something to say the
    text does not show can say it — the mechanism is here whether or not a
    given handler uses it.

    None do today: every handler that reads mutable state keys on a digest of
    what it rendered, because an invalidation costs a reconnect on the Claude
    SDK backend and re-rendering identical bytes buys nothing. That is a choice
    made per handler, and it is reversible precisely because the layer hashes
    the key rather than the text. If this collapsed to a text hash, a handler
    that later needs a finer key could not have one.
    """
    before = await _assemble(surface_preamble=_POCKET_A, surface_cache_key="pocket:A:rev1")
    after = await _assemble(surface_preamble=_POCKET_A, surface_cache_key="pocket:A:rev2")

    assert before.text == after.text, "the point of this test is that the TEXT is identical"
    assert before.stable_digest != after.stable_digest


async def test_a_moved_preamble_under_one_key_does_not_move_the_digest():
    """The inverse, stated so the trade-off is on the record rather than
    incidental: the digest follows the KEY. A handler that lets its text drift
    under a fixed key has made a claim, and this is the claim it made — which
    is why the key is the handler's answer and not the dispatcher's guess."""
    a = await _assemble(surface_preamble=_POCKET_A, surface_cache_key="pocket:A:rev1")
    b = await _assemble(surface_preamble=_POCKET_B, surface_cache_key="pocket:A:rev1")
    assert a.text != b.text
    assert a.stable_digest == b.stable_digest


# ---------------------------------------------------------------------------
# No surface, and a surface that declined to claim a key
# ---------------------------------------------------------------------------


async def test_no_surface_leaves_no_text_and_no_key():
    """Every non-cloud path — OSS local runs, the channel adapters, a client
    that stamps no hint — lands here, and must be byte-identical to before the
    layer existed."""
    assembled = await _assemble(instructions="RIPPLE LAW: narrate.")
    assert assembled.text == "WHO I AM\n\nRIPPLE LAW: narrate."
    assert assembled.dropped == []


async def test_an_unkeyed_preamble_is_rendered_but_kept_out_of_the_digest():
    """What a handler that will not claim stability gets: its text still
    reaches the agent, and it contributes nothing to the digest — so the digest
    never asserts a stability nobody vouched for."""
    keyless = await _assemble(surface_preamble=_POCKET_A, surface_cache_key=None)
    nothing = await _assemble()

    assert _POCKET_A in keyless.text
    assert keyless.stable_digest == nothing.stable_digest


# ---------------------------------------------------------------------------
# The pool forwards the surface into the prompt it hands the backend
# ---------------------------------------------------------------------------


class _CapturingBackend:
    """Captures what the pool actually hands a backend, on both entry points.

    A real class, not a mock: the pool asks ``run``'s SIGNATURE whether the
    backend takes the digest, and a MagicMock has no signature to read.
    """

    def __init__(self) -> None:
        self.run_prompt: str | None = None
        self.prewarm_prompt: str | None = None

    async def run(self, message: str, *, system_prompt: str = "", **kwargs):  # noqa: ARG002
        self.run_prompt = system_prompt
        return
        yield  # pragma: no cover — makes this an async generator

    async def prewarm(self, *, session_key: str, system_prompt: str, **kwargs):  # noqa: ARG002
        self.prewarm_prompt = system_prompt


async def _pool_with(monkeypatch, backend) -> AgentPool:
    instance = _instance()
    instance.backend = backend
    pool = AgentPool()

    async def _fake_get(agent_id):  # noqa: ARG001
        return instance

    monkeypatch.setattr(pool, "get", _fake_get)
    return pool


async def test_the_pool_puts_the_preamble_in_the_prompt_it_runs(monkeypatch):
    backend = _CapturingBackend()
    pool = await _pool_with(monkeypatch, backend)

    async for _ in pool.run(
        "agent-1",
        "hi",
        "cloud:session:s1:agent-1",
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    ):
        pass

    assert backend.run_prompt is not None
    assert _POCKET_A in backend.run_prompt


async def test_the_pool_puts_the_preamble_in_the_prompt_it_prewarms(monkeypatch):
    """``prewarm`` builds the prompt turn 1 will see. If the surface stopped at
    the pool's signature and never reached the assembly, the prewarmed client
    would carry a prompt with no surface in it and turn 1 would evict it."""
    backend = _CapturingBackend()
    pool = await _pool_with(monkeypatch, backend)

    await pool.prewarm(
        "agent-1",
        "cloud:session:s1:agent-1",
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    )

    assert backend.prewarm_prompt is not None
    assert _POCKET_A in backend.prewarm_prompt


# ---------------------------------------------------------------------------
# Prewarm parity — the reason ``pool.prewarm`` takes the surface at all
# ---------------------------------------------------------------------------


async def test_prewarm_and_turn_one_hash_the_same_behaviour_prefix():
    """``prewarm`` assembles with no message and no knowledge context, because
    both are stripped from the Claude SDK's client cache key. The surface
    preamble is NOT stripped — it now sits above the volatile markers — so
    prewarm has to pass it, and this is the equality that says it does.

    Without it the prewarmed client hashes differently from turn 1, and turn 1
    evicts the client the prewarm just paid ~12s to build: a net loss over not
    prewarming at all.
    """
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    instance = _instance()
    prewarmed = await _assemble(
        instance=instance,
        message="",
        knowledge_context="",
        instructions="RIPPLE LAW: narrate before every tool call.",
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    )
    turn_one = await _assemble(
        instance=instance,
        message="add a chart",
        knowledge_context="Acme Dental opens at 9am.",
        instructions="RIPPLE LAW: narrate before every tool call.",
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    )

    assert prewarmed.text != turn_one.text, "the volatile tail must differ, or this proves nothing"
    assert ClaudeSDKBackend._behavior_prefix(prewarmed.text) == ClaudeSDKBackend._behavior_prefix(
        turn_one.text
    )


async def test_a_prewarm_that_skipped_the_surface_would_be_evicted():
    """The negative that makes the test above load-bearing: pass the surface to
    the run and not to the prewarm — what omitting the kwarg in ``run_core``
    would do — and the prefixes diverge."""
    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    instance = _instance()
    prewarmed_without = await _assemble(
        instance=instance, message="", knowledge_context="", instructions="RIPPLE LAW."
    )
    turn_one = await _assemble(
        instance=instance,
        message="add a chart",
        knowledge_context="Acme Dental opens at 9am.",
        instructions="RIPPLE LAW.",
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    )

    assert ClaudeSDKBackend._behavior_prefix(prewarmed_without.text) != (
        ClaudeSDKBackend._behavior_prefix(turn_one.text)
    )


async def test_the_layer_passes_the_key_through_untouched():
    """The layer's whole contract in one assertion: it does not compute, trim,
    normalise or re-derive the key — a key invented here would be exactly the
    dispatcher-side guess the handlers exist to avoid."""
    ctx = PromptContext(
        instance=None,
        agent_id="agent-1",
        message="",
        instructions="",
        knowledge_context="",
        system_message_override=None,
        surface_preamble=_POCKET_A,
        surface_cache_key="pocket:A:rev1",
    )
    out = await SurfaceContextLayer().render(ctx)
    assert out.text == _POCKET_A
    assert out.cache_key == "pocket:A:rev1"
