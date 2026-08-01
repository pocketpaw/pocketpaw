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
#   3. Same text, different key must NOT collide. This is the seam's whole
#      reason for existing: the pocket preamble lists 12 of N widgets under a
#      1500-char cap, so a real edit can leave the rendered bytes identical.
#      A digest that followed the TEXT would call that "unchanged".
#
# The mirror of (3) — same key, different text — is PA-1's
# ``test_the_digest_is_not_a_hash_of_the_text``, at the assembler. Here it is
# re-proven through the surface layer specifically, because that is the layer
# where someone would be most tempted to hash the text and be done with it.

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


async def test_an_edit_the_preamble_cannot_show_still_moves_the_digest():
    """The pocket preamble renders the first 12 of N widgets under a 1500-char
    cap, so editing widget 13 changes the pocket and changes NO rendered byte.
    The handler sees it (the pocket's ``updatedAt`` moved) and says so in the
    key. Hash the text instead and this is the failure you ship: the digest
    reports "unchanged" and a cached agent keeps describing the old pocket.
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
