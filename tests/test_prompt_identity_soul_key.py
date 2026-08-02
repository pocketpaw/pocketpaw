# tests/test_prompt_identity_soul_key.py
# Created: 2026-08-02 (PA-3b, feat/prompt-assembler-seam) — pins the split
# between the soul content that survives ordinary interaction and the soul
# content that does not, now that the identity layer's cache key can see it.
#
# WHAT PA-3b MEASURED, because these tests only make sense against the numbers.
# A soul birthed through PocketPaw's own path, 8 turns of ordinary conversation,
# nobody deliberately mutating anything:
#
#   Bond level: X/100          moved on 8/8 turns   (observe() strengthens it)
#   Memories: N                moved on 8/8 turns   (observe() stores entries)
#   [domain] confidence=X      moved on 7/8 turns   (evidence count climbs)
#   ## Self-Understanding      moved on 7/8 turns   (same counter, rendered)
#   ## Current State           moved on 1/8 turns as deployed (density-driven
#                              focus), 8/8 with a companion soul's energy drain
#   everything else            held on 8/8 turns
#
# So fingerprinting the soul block WHOLE — the first shape PA-3b proposed — is
# refuted: it would rebuild the cached agent on every single turn, which is the
# trade PR #1842 explicitly refused. The shape that survives the measurement is
# the decomposition: key what held, exclude what moved.
#
# The properties held here:
#   1. Ordinary turns move the identity TEXT and do not move its cache key.
#      The text assertion is not decoration — without it the test would pass
#      against a key that is constant because it hashes nothing.
#   2. A real soul edit — persona, core memory, name — DOES move the key. This
#      is the direction PA-1's key was blind to in both eyes: with `updatedAt`
#      dead (beanie skips `_`-prefixed init actions) that key reduced to the
#      entity override alone, so a soul-enabled agent with no override carried
#      ONE key for the whole life of its instance.
#   3. A recalled memory's CONTENT is keyed while the counters beside it are
#      not. This is the property PA-6 needs and the one that is easiest to lose.
#   4. An entity override drops the soul's claim, because the override replaced
#      the text the claim describes.
#   5. PA-3b MOVED NO BYTES. `## Current State` and `## Self-Understanding` sit
#      mid-block inside `to_system_prompt()`; giving them their own layer would
#      relocate them to the end of the prompt and change what
#      `ClaudeSDKBackend._behavior_prefix` retains, invalidating every warm
#      client live at deploy. Held as an equality on a full realistic prompt.
#
# EACH TEST NAMES THE MUTATION THAT BREAKS IT, and each mutation was run.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from pocketpaw.agents.pool import AgentPool
from pocketpaw.bootstrap.protocol import BootstrapContext
from pocketpaw.prompt import AgentIdentityLayer, PromptContext
from pocketpaw.soul._bridge import SoulBootstrapProvider, _stable_identity_projection

pytestmark = pytest.mark.asyncio

_STAMP = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

# A realistic soul-shaped identity block: the stable sections, the two volatile
# ones in the positions soul-protocol renders them (mid-block, NOT at the end),
# and the safety guardrail tail. Shared by the stub soul and the byte goldens.
_IDENTITY = (
    "You are Paw.\n"
    "Archetype: The Helpful Assistant\n"
    "\n"
    "## Personality\n"
    "Openness: 0.7 | Conscientiousness: 0.8\n"
    "\n"
    "## Current State\n"
    "Mood: satisfied | Energy: 84% | Focus: high\n"
    "\n"
    "## Persona Memory\n"
    "I am Paw, a persistent AI companion.\n"
    "\n"
    "## Self-Understanding\n"
    "\n"
    "- synchronous database (high confidence, 35 supporting interactions)"
)


class _StubProvider:
    """A bootstrap provider whose every field is under the test's control.

    The real provider is exercised too (see the live-soul tests below); this one
    exists so a single line of `knowledge` can be moved in isolation, which a
    real soul will not do on command.
    """

    def __init__(self, identity: str, knowledge: list[str], key: str | None) -> None:
        self._ctx = BootstrapContext(
            name="Paw",
            identity=identity,
            soul="",
            style="",
            knowledge=list(knowledge),
            identity_cache_key=key,
        )

    async def get_context(self) -> BootstrapContext:
        return self._ctx


class _StubSoul:
    """The surface `SoulBootstrapProvider.get_context` reads, and nothing else.

    A live soul cannot be told to hold its bond still while its recalled facts
    change, which is the exact pair this fixture has to separate. Everything
    here renders through the REAL provider, so the stable/volatile split under
    test is the bridge's own.
    """

    name = "Paw"

    def __init__(self, *, recalled: list[str], bond: float, memories: int) -> None:
        self._recalled = recalled
        self.bond = SimpleNamespace(bond_strength=bond)
        self.memory_count = memories
        self.state = SimpleNamespace(mood="neutral", energy=100.0, tired_threshold=0.0)
        self.self_model = SimpleNamespace(get_active_self_images=lambda limit=5: [])

    def to_system_prompt(self) -> str:
        return _IDENTITY

    async def recall(self, query: str, **kwargs):  # noqa: ARG002
        from soul_protocol import MemoryType

        return [SimpleNamespace(type=MemoryType.SEMANTIC, content=c) for c in self._recalled]


def _instance(provider=None, soul=None):
    return SimpleNamespace(
        backend=None,
        soul_manager=(
            SimpleNamespace(bootstrap_provider=provider, soul=soul) if (provider or soul) else None
        ),
        config={"soul_persona": "WHO I AM", "system_prompt": ""},
        created_from_updated_at=_STAMP,
        last_active=datetime.now(UTC),
        active_runs=0,
    )


def _ctx(instance, **kwargs) -> PromptContext:
    return PromptContext(
        instance=instance,
        agent_id=kwargs.pop("agent_id", "agent-1"),
        message=kwargs.pop("message", ""),
        instructions=kwargs.pop("instructions", ""),
        knowledge_context=kwargs.pop("knowledge_context", ""),
        system_message_override=kwargs.pop("system_message_override", None),
        **kwargs,
    )


async def _key(instance, **kwargs) -> str:
    out = await AgentIdentityLayer().render(_ctx(instance, **kwargs))
    assert out.cache_key is not None
    return out.cache_key


async def _live_soul():
    """A soul birthed the way PocketPaw births one, so what drifts here is what
    drifts in production rather than what a fake was written to drift."""
    from soul_protocol import Soul

    from pocketpaw.config import Settings
    from pocketpaw.soul._manager import SoulManager

    mgr = SoulManager.__new__(SoulManager)
    mgr._settings = Settings()
    return await mgr._birth_soul(Soul)


# The conversation the measurement used. Substantive on purpose: short chatter
# never develops a self-image, and the self-model fragment is half of what this
# task had to classify.
_TURNS = [
    (
        "I need to refactor the payment service to use async database calls",
        "Let's start by mapping the synchronous call sites in the payment module.",
    ),
    (
        "the database driver is psycopg2, should I switch to asyncpg",
        "asyncpg is the right choice for an async refactor of a postgres service.",
    ),
    (
        "how do I handle the connection pool during the migration",
        "Run both pools side by side and cut over per repository module.",
    ),
    (
        "what about the existing database transactions in the payment flow",
        "Wrap each transaction in an async context manager and keep the boundaries identical.",
    ),
    (
        "can you review the async payment repository code I wrote",
        "The connection handling looks right; the transaction rollback path needs a test.",
    ),
    (
        "write a test for the payment rollback path please",
        "Here is an async test that forces a rollback and asserts the balance is unchanged.",
    ),
]


# ---------------------------------------------------------------------------
# 1 — ordinary drift must not move the key
# ---------------------------------------------------------------------------


async def test_six_ordinary_turns_move_the_text_and_not_the_key():
    """The half that protects the cache, against a LIVE soul.

    Six turns of ordinary conversation. The rendered identity block changes —
    bond level, memory count, self-image confidence, and the focus band all
    move — and the layer's cache key does not. A backend that bakes the prompt
    into a cached object keeps that object across all six.

    THE MUTATION THAT BREAKS THIS: add `Bond level` (or the self-image line, or
    the memory count) to `stable_knowledge` in `soul/_bridge.py`, or empty
    `_VOLATILE_IDENTITY_SECTIONS`. Both were run against this fixture, which
    holds ONE key across all five turn boundaries: promoting `Bond level` moved
    it at 5 of 5, emptying the section set at 4 of 5.

    THE MUTATION THAT MAKES IT VACUOUS: a key that hashes nothing passes this
    trivially, which is what the `texts` assertion is for — it fails if the
    fixture stops drifting, and then this test is telling the truth about
    nothing.
    """
    from soul_protocol import Interaction

    soul = await _live_soul()
    instance = _instance(provider=SoulBootstrapProvider(soul))

    keys, texts = [], []
    for user, agent in _TURNS:
        out = await AgentIdentityLayer().render(_ctx(instance))
        keys.append(out.cache_key)
        texts.append(out.text)
        await soul.observe(Interaction(user_input=user, agent_output=agent))

    assert len(set(texts)) > 1, (
        "the fixture never drifted — this soul is not exercising the thing under test"
    )
    assert len(set(keys)) == 1, (
        f"ordinary interaction moved the identity cache key {len(set(keys))} ways; "
        "a backend caching on it rebuilds the agent every turn"
    )


async def test_the_volatile_sections_are_the_measured_ones():
    """The projection, held directly rather than only through a live soul.

    Both sections named here were measured drifting on ordinary turns, and both
    are stated as whole sections: `## Self-Understanding` renders one line per
    self-image, so a rule counting lines would have to be revisited every time
    the soul learns a domain.

    THE MUTATION THAT BREAKS THIS: drop either name from
    `_VOLATILE_IDENTITY_SECTIONS`. Run: dropping `## Current State` left the
    mood line in the projection; dropping `## Self-Understanding` left the
    evidence count in.
    """
    rendered = (
        "You are Paw.\n"
        "Archetype: The Helpful Assistant\n"
        "\n"
        "## Personality\n"
        "Openness: 0.7 | Conscientiousness: 0.8\n"
        "\n"
        "## Current State\n"
        "Mood: satisfied | Energy: 84% | Focus: high\n"
        "\n"
        "## Persona Memory\n"
        "I am Paw, a persistent AI companion.\n"
        "\n"
        "## Self-Understanding\n"
        "\n"
        "- synchronous database (high confidence, 35 supporting interactions)\n"
        "- connection repository (growing confidence, 10 supporting interactions)\n"
        "\n"
        "## Safety guardrails\n"
        "Do not disclose bond details."
    )
    stable = _stable_identity_projection(rendered)

    # gone: every byte that a counter writes
    assert "Mood:" not in stable
    assert "Energy:" not in stable
    assert "Focus:" not in stable
    assert "supporting interactions" not in stable
    assert "confidence" not in stable

    # kept: everything a soul EDIT writes
    assert "You are Paw." in stable
    assert "Archetype: The Helpful Assistant" in stable
    assert "Openness: 0.7 | Conscientiousness: 0.8" in stable
    assert "I am Paw, a persistent AI companion." in stable
    assert "Do not disclose bond details." in stable


async def test_an_unknown_section_stays_in_the_key():
    """The denylist's direction, which is the whole reason it is a denylist.

    A soul-protocol release that adds a section must be keyed by DEFAULT. The
    cost of being wrong that way is one extra rebuild; the cost of the other
    way is the stale prompt this package exists to prevent.

    THE MUTATION THAT BREAKS THIS: invert `_stable_identity_projection` to keep
    only known-stable headings. Run: the inverted version dropped
    `## Somatic Markers` and the two projections compared equal.
    """
    base = "You are Paw.\n\n## Current State\nMood: neutral | Energy: 100% | Focus: low"
    with_new = base + "\n\n## Somatic Markers\nvalence 0.4"

    assert _stable_identity_projection(base) != _stable_identity_projection(with_new)
    assert "valence 0.4" in _stable_identity_projection(with_new)


# ---------------------------------------------------------------------------
# 2 — a real event must move the key
# ---------------------------------------------------------------------------


async def test_editing_the_core_memory_moves_the_key():
    """The half that protects freshness, against a LIVE soul.

    Editing the persona is the honest event boundary this measurement found: it
    is a deliberate act, it changes what the model reads, and it changes nothing
    that ordinary conversation also changes. Before PA-3b this was invisible to
    the identity key — the soul does not touch the agent document, and
    `created_from_updated_at` is stamped once at `_build` and never refreshed.

    THE MUTATION THAT BREAKS THIS: stop folding `identity_cache_key` into the
    key in `prompt/identity.py`, or add `## Persona Memory` to
    `_VOLATILE_IDENTITY_SECTIONS`. Run: both made the two keys compare equal.
    """
    soul = await _live_soul()
    instance = _instance(provider=SoulBootstrapProvider(soul))

    before = await _key(instance)
    await soul.edit_core_memory(persona="I am Paw. I now specialise in payments infrastructure.")
    after = await _key(instance)

    assert before != after, "a deliberate soul edit must reach a backend that caches the prompt"


async def test_a_recalled_memorys_content_is_keyed_but_the_counters_are_not():
    """The property PA-6 actually needs, isolated from a live soul's noise.

    A learned fact in `# Key Knowledge` is CONTENT — it changes when the soul
    learns or forgets, which is precisely the change a backend baking the prompt
    into a cached object has to see. The counters rendered beside it are not.
    Both halves are asserted together because keeping one without the other is
    the easy mistake in either direction.

    Worth knowing while reading this: the bridge's auto-recall passes an empty
    query, which scores 0.0 against every memory store, so in production this
    branch returns nothing and no learned fact reaches the prompt at all. That
    is a real bug, filed separately, and it is why this property is pinned on a
    stub rather than discovered on a live soul. The declaration is written for
    the content; fix the recall and this key starts tracking facts with no
    change here.

    THE MUTATION THAT BREAKS THIS: move `stable_knowledge.append(line)` out of
    the recalled-memory branch, or add the counters to it. Run: removing the
    append made the first pair equal; appending the bond level made the second
    pair differ.

    Driven through the REAL `SoulBootstrapProvider.get_context`, not through a
    reimplementation of its split — a test that re-derives which line is stable
    agrees with itself no matter what the bridge does.
    """

    async def key_for(fact: str, bond: float, memories: int) -> str:
        soul = _StubSoul(recalled=[fact], bond=bond, memories=memories)
        ctx = await SoulBootstrapProvider(soul).get_context()
        # sanity: the fixture is actually rendering all three into the prompt
        assert any(fact in k for k in ctx.knowledge)
        assert any(k.startswith("Bond level:") for k in ctx.knowledge)
        assert any(k.startswith("Memories:") for k in ctx.knowledge)
        assert ctx.identity_cache_key is not None
        return ctx.identity_cache_key

    fact_a = await key_for("the captain prefers stacked PRs", 50.0, 8)
    fact_b = await key_for("the captain prefers squash merges", 50.0, 8)
    assert fact_a != fact_b, "a changed learned fact must move the key"

    counter_a = await key_for("the captain prefers stacked PRs", 50.0, 8)
    counter_b = await key_for("the captain prefers stacked PRs", 58.7, 9)
    assert counter_a == counter_b, "the counters beside the fact must not move the key"


async def test_the_layer_carries_the_provider_claim_and_a_placeholder_without_one():
    """The wiring, held at the layer.

    A provider that answers the question gets its answer into the key; one that
    does not (every non-soul provider, and a provider whose `get_context`
    raised) leaves the slot occupied by a literal, so "no claim" can never be
    read as "claimed nothing".

    THE MUTATION THAT BREAKS THIS: set `_NO_CLAIM = ""`. Run: the no-claim key
    ended in a bare `:` and the `endswith(":-")` assertion failed — which is the
    point, because an empty slot is a claim of empty string, not the absence of
    a claim, and `LayerOutput` already rejects that same ambiguity one level up.
    """
    claimed = await _key(_instance(provider=_StubProvider("ID", [], "abc123")))
    unclaimed = await _key(_instance(provider=_StubProvider("ID", [], None)))

    assert claimed.endswith(":abc123")
    assert unclaimed.endswith(":-")
    assert claimed != unclaimed
    # Only the soul slot differs — the revision field carries a timestamp whose
    # own colons make whole-key field counting meaningless, so split from the
    # right, where the layout is fixed.
    assert claimed.rsplit(":", 1)[0] == unclaimed.rsplit(":", 1)[0]
    assert unclaimed.rsplit(":", 1)[1] != ""


async def test_an_entity_override_drops_the_souls_claim():
    """The override REPLACED the text the claim describes.

    Entity-rooms A1 swaps the whole base — soul identity and `# Key Knowledge`
    both — so the soul's digest now describes bytes the model cannot read.
    Keeping it would rebuild an overridden agent's cache every time somebody
    edited a soul whose text is not in the prompt.

    THE MUTATION THAT BREAKS THIS: delete the `soul_key = _NO_CLAIM` line in the
    override branch. Run: the two keys below diverged on a change the prompt
    does not contain.
    """
    override = "You are the front desk. Answer only about opening hours."

    one = await _key(
        _instance(provider=_StubProvider("SOUL A", [], "aaaaaaaa")),
        system_message_override=override,
    )
    two = await _key(
        _instance(provider=_StubProvider("SOUL B", [], "bbbbbbbb")),
        system_message_override=override,
    )

    assert one == two
    assert one.endswith(":-")


async def test_the_override_still_beats_a_soul_change_that_is_in_the_prompt():
    """The must-not-collide half of the test above.

    Dropping the claim under an override must not make the key deaf: two
    DIFFERENT overrides still have to key apart, or one entity's cached agent
    serves another's prompt.
    """
    provider = _StubProvider("SOUL", [], "aaaaaaaa")
    one = await _key(_instance(provider=provider), system_message_override="desk A")
    two = await _key(_instance(provider=provider), system_message_override="desk B")

    assert one != two


# ---------------------------------------------------------------------------
# 3 — the bytes: PA-3b moved none of them
# ---------------------------------------------------------------------------

_KNOWLEDGE = ["[synchronous_database] confidence=0.76", "Bond level: 54.1/100", "Memories: 18"]
_SURFACE = "## Current Surface\nThe user is looking at the pocket 'Q3 Planning'."
_INSTRUCTIONS = "You are operating inside PocketPaw.\n\n## THE LAW\nNever fabricate a tool result."
_KB = "- kb fact one\n- kb fact two"

# What `_behavior_prefix` retains from the full prompt below: the identity block
# with its `# Key Knowledge` excised in place, then the surface, then the
# authoritative instructions — cut at the first volatile tail marker. Written
# out rather than derived, so a byte that moves has to be re-typed here by
# somebody who noticed.
_EXPECTED_PREFIX = f"{_IDENTITY}\n\n{_SURFACE}\n\n{_INSTRUCTIONS}"


async def _full_prompt(claim: str | None) -> str:
    assembled = await AgentPool()._assemble_system_prompt(
        _instance(provider=_StubProvider(_IDENTITY, _KNOWLEDGE, claim)),
        agent_id="agent-1",
        message="how do I total a csv column?",
        instructions=_INSTRUCTIONS,
        knowledge_context=_KB,
        system_message_override=None,
        surface_preamble=_SURFACE,
        surface_cache_key="pocket:q3:v7",
    )
    return assembled.text


async def test_the_soul_block_reaches_the_prompt_whole_and_the_claim_never_touches_it():
    """PA-3b's central constraint, held as two equalities.

    The natural reading of "split the drifting soul-state block out of identity"
    is a second layer. It is the one thing this must not do: `## Current State`
    and `## Self-Understanding` sit in the MIDDLE of `to_system_prompt()`, so a
    layer of their own relocates them to the end of the assembled prompt, and
    the warm-client prefix moves for every client live at deploy.

    So: the identity block appears in the assembled text as ONE contiguous run
    of bytes, and supplying a claim changes nothing about it.

    THE MUTATION THAT BREAKS THIS: render the volatile sections from a separate
    layer — approximated by projecting them out of the identity layer's text.
    Run: the `in` assertion failed, the assembled prompt went 780 -> 626 bytes,
    and the prefix test below lost both sections.
    """
    with_claim = await _full_prompt("abc123")

    assert _IDENTITY in with_claim, "the soul block was split or reordered"
    assert with_claim == await _full_prompt(None)


async def test_the_behavior_prefix_is_byte_identical_to_the_written_expectation():
    """The warm-client cache key, pinned as bytes rather than as a hash.

    Held against a string written out by hand: a diff here shows WHICH bytes
    moved, which a digest comparison would hide.
    """
    prefix = ClaudeSDKBackend._behavior_prefix(await _full_prompt("abc123"))

    assert prefix == _EXPECTED_PREFIX
    # the volatile tail is gone, and so is the mid-prompt knowledge block
    assert "Bond level" not in prefix
    assert "Memories: 18" not in prefix
    assert "## Your Knowledge Base" not in prefix
    assert "## Relevant Past Memories" not in prefix


async def test_the_prefix_still_carries_the_two_sections_the_key_ignores():
    """The honest statement of what this task did NOT fix.

    `## Current State` and `## Self-Understanding` are excluded from the
    assembler's digest and are still inside `_behavior_prefix`'s retained text,
    because moving them is a byte change this task is not allowed to make. So
    the claude_sdk warm client still rebuilds when they drift — measured at 7/8
    substantive turns — while a backend keying on `stable_digest` does not.
    PA-6 removes `_behavior_prefix` and closes the gap; until then the two
    disagree, and it is better to say so here than to let PA-6 discover it.
    """
    prefix = ClaudeSDKBackend._behavior_prefix(await _full_prompt("abc123"))

    assert "Mood: satisfied | Energy: 84% | Focus: high" in prefix
    assert "supporting interactions" in prefix
