# tests/test_prompt_instructions_layer.py
# Created: 2026-08-02 (PA-4, feat/prompt-assembler-seam) — pins the authoritative
# instructions layer, and the layer ORDER it made into a cache contract.
#
# The properties held here:
#   1. BYTE-NEUTRALITY. Splitting ``instructions`` out of ``legacy_tail`` moved
#      nothing. Stated as a transformation rather than a fresh golden: the block
#      ``legacy_tail`` used to render — instructions, blank line, knowledge
#      wrapper — is still present, verbatim and CONTIGUOUS, in the position it
#      held. Unlike PA-3 this task had no reason to move bytes, and a byte moved
#      above ``_behavior_prefix``'s cut invalidates every warm Claude SDK client
#      that is live at deploy.
#   2. ``_behavior_prefix`` is byte-identical across the split, asserted as an
#      EQUALITY against the exact stable blocks so it cannot pass by both sides
#      being empty.
#   3. The layer is KEYED, and keyed on something that discriminates: two runs
#      whose instructions differ must not share a digest. This is the whole
#      reason the split was worth doing — sharing a layer with the knowledge
#      wrapper meant the most stable content in the prompt contributed nothing.
#   4. EVERY keyed layer sits above the volatile region. Held over the whole
#      layer list at once rather than over this one layer, because it is a
#      property of position: a keyed layer ordered below a volatile marker is
#      silently cut out of the warm-client key and looks keyed while behaving
#      unkeyed.
#   5. The ORDER, as pairwise rules with an exhaustiveness guard. PA-5 inserts
#      ``atlas`` and ``user``; that must be two added rules, not a rewritten
#      constant, because a rewrite is where a reorder slips through unnoticed.
#   6. The entity override still beats the surface, and what it keys in each
#      layer.
#
# Every test below names the mutation that must break it. Each was applied and
# re-run on 2026-08-02; the outcomes are recorded in the commit body.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from pocketpaw.agents.pool import _SYSTEM_PROMPT_LAYERS, AgentPool
from pocketpaw.prompt import (
    AssembledPrompt,
    InstructionsLayer,
    LegacyTailLayer,
    PromptContext,
    prompt_layer_registry,
)

pytestmark = pytest.mark.asyncio

_STAMP = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures — a realistic cloud turn: every block a real prompt carries
# ---------------------------------------------------------------------------

# What ``build_behavior_instructions`` actually emits, joined with "\n" as it
# does: the runtime-identity rule, the ripple LAW, the delegation rule, the
# pocket anchor and the member block. Shaped like the real thing because the
# layer's cache key is a digest of exactly these bytes.
_INSTRUCTIONS = "\n".join(
    [
        "<runtime-identity>\nYou are PocketPaw. Slash commands DO NOT EXIST here."
        "\n</runtime-identity>",
        "RIPPLE LAW: default to a ui-spec. Narrate before every tool call.",
        "POCKET DELEGATION: never call add_widget; delegate to the specialist.",
        "<pocket-summary>\n  name: Acme Dental\n  type: dashboard\n</pocket-summary>",
    ]
)

# The same stack with the ripple LAW and the delegation rule OMITTED — what a
# ``ripple_mode="off"`` surface (/sites) produces. Same surface key, same
# override, different rules: the case a key of ``surface + entity override``
# cannot see, and the reason this layer keys on its own bytes instead.
_INSTRUCTIONS_RIPPLE_OFF = "\n".join(
    [
        "<runtime-identity>\nYou are PocketPaw. Slash commands DO NOT EXIST here."
        "\n</runtime-identity>",
        "<pocket-summary>\n  name: Acme Dental\n  type: dashboard\n</pocket-summary>",
    ]
)

_SURFACE = '<current-pocket id="p-42" widgets="12">\n  Acme Dental\n</current-pocket>'
_SURFACE_KEY = "pocket:p-42:2026-08-02T09:14:00Z"

_KNOWLEDGE_BLOCK = (
    "## Your Knowledge Base\n"
    "Use the following information from your knowledge base to answer questions. "
    "Always reference this data when relevant instead of making things up or "
    "using tools to search.\n"
    "\n"
    "Acme Dental opens at 9am."
)

_RECALL_BLOCK = (
    "## Relevant Past Memories\n"
    "Below are memories from previous conversations that are relevant to the "
    "current question. Use them to provide continuity and a personalized "
    "response.\n"
    "\n"
    "- talked about tea yesterday (asked: what time do you open?)"
)

_PERSONA = "You are Paw, a warm and curious companion."
_OVERRIDE = "You are the Acme Dental booking assistant. Answer only about bookings."


class _FakeBootstrapProvider:
    def __init__(self, identity: str, knowledge: list[str]) -> None:
        self._identity = identity
        self._knowledge = knowledge

    async def get_context(self):
        return SimpleNamespace(
            identity=self._identity,
            knowledge=list(self._knowledge),
            identity_cache_key="soul:mem118:bond42",
        )


class _FakeSoul:
    async def context_for(self, message: str, **_kwargs) -> str:
        return f"- talked about tea yesterday (asked: {message})"


def _instance(*, knowledge: list[str] | None = None, recall: bool = True):
    return SimpleNamespace(
        backend=None,
        soul_manager=SimpleNamespace(
            bootstrap_provider=_FakeBootstrapProvider(_PERSONA, knowledge or []),
            soul=_FakeSoul() if recall else None,
        ),
        config={"soul_persona": "BASE PERSONA", "system_prompt": "base extra"},
        created_from_updated_at=_STAMP,
        last_active=datetime.now(UTC),
        active_runs=0,
    )


async def _assemble(**kwargs) -> AssembledPrompt:
    """Assemble through the REAL seam, so the order under test is the cloud
    path's own rather than one this file made up."""
    return await AgentPool()._assemble_system_prompt(
        kwargs.pop("instance", None) or _instance(),
        agent_id=kwargs.pop("agent_id", "agent-1"),
        message=kwargs.pop("message", ""),
        instructions=kwargs.pop("instructions", ""),
        knowledge_context=kwargs.pop("knowledge_context", ""),
        system_message_override=kwargs.pop("system_message_override", None),
        **kwargs,
    )


async def _full_turn(**kwargs) -> AssembledPrompt:
    """One realistic cloud turn: identity, surface, instructions, knowledge, recall."""
    return await _assemble(
        message="what time do you open?",
        instructions=kwargs.pop("instructions", _INSTRUCTIONS),
        knowledge_context="Acme Dental opens at 9am.",
        surface_preamble=_SURFACE,
        surface_cache_key=_SURFACE_KEY,
        **kwargs,
    )


def _ctx(**kwargs) -> PromptContext:
    return PromptContext(
        instance=kwargs.pop("instance", None) or _instance(),
        agent_id=kwargs.pop("agent_id", "agent-1"),
        message=kwargs.pop("message", ""),
        instructions=kwargs.pop("instructions", ""),
        knowledge_context=kwargs.pop("knowledge_context", ""),
        system_message_override=kwargs.pop("system_message_override", None),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1 — byte-neutrality: the split moved nothing
# ---------------------------------------------------------------------------

# What ``LegacyTailLayer`` rendered as ONE block before PA-4: the authoritative
# instructions, a blank line, the knowledge wrapper. Written out here so the
# claim "nothing moved" is checkable against the old code's shape rather than
# against a constant regenerated from the new code.
_PRE_PA4_TAIL = f"{_INSTRUCTIONS}\n\n{_KNOWLEDGE_BLOCK}"

_GOLDEN = f"{_PERSONA}\n\n{_SURFACE}\n\n{_PRE_PA4_TAIL}\n\n{_RECALL_BLOCK}"


async def test_the_split_left_the_assembled_bytes_untouched():
    """The main constraint of PA-4, stated as a transformation.

    ``LegacyTailLayer`` rendered instructions-then-knowledge as one block.
    Ordering the new ``instructions`` layer immediately before the tail
    reproduces that concatenation exactly, so the whole old block is still
    present, verbatim and CONTIGUOUS, in the position it held. PA-3 moved bytes
    and argued for it; PA-4 has no such argument to make — a byte moved above
    ``_behavior_prefix``'s cut costs every warm client live at deploy a
    reconnect and buys nothing.

    MUTATION: order the layers ``identity, surface, legacy_tail, instructions,
    retrieval`` (i.e. leave ``instructions`` below the tail). ``_PRE_PA4_TAIL``
    stops appearing contiguously and this fails.
    """
    assembled = await _full_turn()

    assert _PRE_PA4_TAIL in assembled.text, (
        "the instructions and the knowledge wrapper are no longer adjacent — "
        "the split moved bytes, which PA-4 must not do"
    )
    assert assembled.text == _GOLDEN


async def test_the_tail_no_longer_renders_the_instructions():
    """The other half of byte-neutrality: the block moved OUT, it was not copied.

    Two layers each rendering ``ctx.instructions`` would duplicate the ripple LAW
    in every prompt, and the golden above would still pass if the duplicate
    landed adjacent to the original.

    MUTATION: restore ``LegacyTailLayer``'s instructions branch. This fails, and
    so does the golden.
    """
    tail = await LegacyTailLayer().render(
        _ctx(instructions=_INSTRUCTIONS, knowledge_context="Acme Dental opens at 9am.")
    )
    assert tail.text == _KNOWLEDGE_BLOCK
    assert "RIPPLE LAW" not in tail.text
    assert tail.cache_key is None, (
        "the wrapper's content is a per-message KB retrieval; keying it would "
        "move the digest every turn"
    )


async def test_an_absent_knowledge_base_still_leaves_no_blank_gap():
    """The join case the ``_append`` helper used to cover inside the tail.

    Instructions with no KB must not render a trailing separator, and a KB with
    no instructions must not render a leading one. Both were the tail's job and
    are now the assembler's empty-text skip.

    MUTATION: drop ``LegacyTailLayer``'s empty guard and return the header
    unconditionally — an empty ``## Your Knowledge Base`` block appears.
    """
    no_kb = await _assemble(instructions=_INSTRUCTIONS, surface_preamble="", message="")
    assert no_kb.text == f"{_PERSONA}\n\n{_INSTRUCTIONS}"

    no_instructions = await _assemble(knowledge_context="Acme Dental opens at 9am.")
    assert no_instructions.text == f"{_PERSONA}\n\n{_KNOWLEDGE_BLOCK}"


# ---------------------------------------------------------------------------
# 2 — the warm-client prefix did not move
# ---------------------------------------------------------------------------


async def test_the_behaviour_prefix_is_exactly_the_stable_blocks():
    """An EQUALITY, not a containment, and the reason is that containment would
    catch neither failure that matters: a cut landing too early loses a stable
    block and invalidates every warm client, and one landing too late hashes a
    volatile block and rebuilds the subprocess every turn.

    The fixture carries BOTH volatile blocks, so the cut has something to do.

    MUTATION: order ``instructions`` after ``legacy_tail``. The prefix loses the
    whole instruction stack — every ripple LAW change stops rebuilding the warm
    client — and this fails.
    """
    assembled = await _full_turn()

    assert "## Your Knowledge Base" in assembled.text, "the fixture must carry both volatile blocks"
    assert "## Relevant Past Memories" in assembled.text
    assert (
        ClaudeSDKBackend._behavior_prefix(assembled.text)
        == f"{_PERSONA}\n\n{_SURFACE}\n\n{_INSTRUCTIONS}"
    )


async def test_the_behaviour_prefix_survives_the_soul_knowledge_block():
    """The full real shape, with the mid-prompt ``# Key Knowledge`` block that
    ``_strip_soul_knowledge_block`` excises in place.

    Held separately from the test above because it exercises a DIFFERENT strip:
    the tail cut only handles the trailing volatile blocks, and a keyed layer
    ordered after the soul-knowledge excision would still be in the prefix. This
    is the shape a soul-enabled cloud turn actually has.

    MUTATION: order ``instructions`` after ``legacy_tail`` — the prefix collapses
    to identity + surface and this fails.
    """
    assembled = await _full_turn(
        instance=_instance(knowledge=["[semantic] the user drinks tea", "Bond level: 42.0/100"])
    )

    assert "# Key Knowledge" in assembled.text
    assert (
        ClaudeSDKBackend._behavior_prefix(assembled.text)
        == f"{_PERSONA}\n\n{_SURFACE}\n\n{_INSTRUCTIONS}"
    )


async def test_prewarm_and_turn_one_hash_the_same_behaviour_prefix():
    """``prewarm`` passes ``message=""`` and ``knowledge_context=""`` but the SAME
    instructions, so the prefix must be identical or turn 1 evicts the client the
    prewarm just paid ~12s to build. The instructions layer is the reason this
    still holds: it is now the LAST block in the prefix, so anything that moved
    it below the volatile region would break prewarm reuse first.

    MUTATION: order ``instructions`` after ``legacy_tail``. Both sides still
    agree — but on a prefix with no instructions in it, which the equality test
    above is what catches.
    """
    prewarmed = await _assemble(
        message="",
        instructions=_INSTRUCTIONS,
        knowledge_context="",
        surface_preamble=_SURFACE,
        surface_cache_key=_SURFACE_KEY,
    )
    turn_one = await _full_turn()

    assert prewarmed.text != turn_one.text, "the volatile tail must differ, or this proves nothing"
    assert ClaudeSDKBackend._behavior_prefix(prewarmed.text) == ClaudeSDKBackend._behavior_prefix(
        turn_one.text
    )


# ---------------------------------------------------------------------------
# 3 — the layer is keyed, on something that discriminates
# ---------------------------------------------------------------------------


async def test_the_instructions_layer_declares_a_key():
    """It shared ``legacy_tail`` with the knowledge wrapper, whose content is a
    per-message KB retrieval, so one unkeyable block silenced the most stable
    content in the prompt.

    MUTATION: return ``cache_key=None``. This fails, and so does every digest
    test below it.
    """
    out = await InstructionsLayer().render(_ctx(instructions=_INSTRUCTIONS))
    assert out.text == _INSTRUCTIONS
    assert out.cache_key is not None


async def test_two_surfaces_whose_rules_differ_do_not_share_a_digest():
    """The case that decided the key, and the one PA-4's filed key would miss.

    Same surface key, same (absent) entity override, same agent — but the
    resolved profile flipped ``ripple_mode`` to off, so the ripple LAW and the
    delegation rule are gone from the instruction stack. A key of ``surface +
    entity override`` reports these two runs identical; a backend caching an
    agent with the prompt baked in then serves the ripple LAW to a surface whose
    tools cannot honour it. Keyed on its own bytes, the layer sees it.

    MUTATION: key on ``f"{ctx.surface_cache_key}:{override}"`` instead of the
    text digest. This fails.
    """
    ripple_on = await _full_turn(instructions=_INSTRUCTIONS)
    ripple_off = await _full_turn(instructions=_INSTRUCTIONS_RIPPLE_OFF)

    assert ripple_on.text != ripple_off.text, "the fixture is not varying the instructions"
    assert ripple_on.stable_digest != ripple_off.stable_digest


async def test_two_turns_with_the_same_rules_share_one_digest():
    """The must-not-churn half. The instruction stack is the most stable content
    in the prompt; keying it must not cost the cache it was added to protect.
    Two turns differing only in the user's message — which rebuilds the recall
    block — keep one digest.

    MUTATION: key on ``ctx.message`` or on the assembled text. This fails and the
    agent cache is thrown away every turn, which is the trade #1842 refused.
    """
    turn_1 = await _full_turn()
    turn_2 = await _assemble(
        message="and on sundays?",
        instructions=_INSTRUCTIONS,
        knowledge_context="Acme Dental opens at 9am.",
        surface_preamble=_SURFACE,
        surface_cache_key=_SURFACE_KEY,
    )

    assert turn_1.text != turn_2.text, "the fixture is not exercising the volatile tail"
    assert turn_1.stable_digest == turn_2.stable_digest


async def test_no_instructions_and_some_instructions_key_apart():
    """An empty channel still contributes a key. "This run had no rules" and
    "this run had rules" are different prompts, and the one thing a digest must
    never do is call them one identity.

    MUTATION: return ``cache_key=None`` when ``ctx.instructions`` is empty. The
    two collapse to the same digest and this fails.
    """
    empty = await InstructionsLayer().render(_ctx(instructions=""))
    full = await InstructionsLayer().render(_ctx(instructions=_INSTRUCTIONS))

    assert empty.cache_key is not None
    assert empty.cache_key != full.cache_key


# ---------------------------------------------------------------------------
# 4 — every keyed layer sits above the volatile region
# ---------------------------------------------------------------------------


async def test_every_keyed_layer_lands_inside_the_behaviour_prefix():
    """The property the ORDER exists to protect, held over the whole layer list.

    ``_behavior_prefix`` cuts at the EARLIEST ``_VOLATILE_PROMPT_MARKERS`` match
    (``min()`` across them), so a KEYED layer ordered below one of those markers
    is cut out of the warm-client key entirely: it declares itself stable, the
    digest honours that, and the Claude SDK's client key never sees it. The layer
    looks keyed and behaves unkeyed, and nothing else in the suite would say so.

    Written over ``_SYSTEM_PROMPT_LAYERS`` rather than over ``instructions``
    alone so PA-5's ``atlas`` and ``user`` layers are covered the day they are
    registered, without anyone remembering to come back here.

    The fixture gives the soul NO ``knowledge`` items on purpose: with them, the
    identity layer's text carries the mid-prompt ``# Key Knowledge`` block that
    ``_strip_soul_knowledge_block`` excises IN PLACE, so identity's text is
    legitimately not a substring of the prefix. That interaction is pinned by
    ``test_the_behaviour_prefix_survives_the_soul_knowledge_block`` instead.

    MUTATION: order ``instructions`` after ``legacy_tail``. ``instructions``
    reports a key and its text is no longer in the prefix, and this fails.
    """
    ctx = _ctx(
        message="what time do you open?",
        instructions=_INSTRUCTIONS,
        knowledge_context="Acme Dental opens at 9am.",
        surface_preamble=_SURFACE,
        surface_cache_key=_SURFACE_KEY,
    )
    assembled = await _full_turn()
    prefix = ClaudeSDKBackend._behavior_prefix(assembled.text)

    checked = []
    for name in _SYSTEM_PROMPT_LAYERS:
        output = await prompt_layer_registry.get(name).render(ctx)
        if output.cache_key is None or not output.text:
            continue
        checked.append(name)
        assert output.text in prefix, (
            f"the keyed layer {name!r} renders below the volatile cut, so its "
            "cache contribution never reaches the warm-client key"
        )

    assert checked == ["identity", "surface", "instructions"], (
        "the keyed, rendering layers changed — state where the new one sits "
        "relative to the volatile region before updating this list"
    )


# ---------------------------------------------------------------------------
# 5 — the order, as rules PA-5 extends rather than a constant it rewrites
# ---------------------------------------------------------------------------

# Each rule is (earlier, later, why). The WHY is the point: layer order is a
# cache contract and an attention contract, not a matter of taste, so a change
# here has to argue with a reason rather than edit a tuple.
_ORDER_RULES = (
    (
        "identity",
        "surface",
        "identity opens the prompt — the U-curve's strongest position, and the "
        "block that is identical across every turn of a session, so it is also "
        "the longest reusable prefix a prompt cache can hold",
    ),
    (
        "surface",
        "instructions",
        "where the user is, then what the agent must do about it: the rules read "
        "as rules only when the thing they govern is already on the page",
    ),
    (
        "instructions",
        "legacy_tail",
        "rules before reference. The knowledge wrapper frames its contents as "
        "material to consult; anything inside it is read as reference and "
        "routinely ignored, which is why the instructions channel exists "
        "separately at all. It is also the volatile boundary: instructions are "
        "keyed and the wrapper is a per-message KB retrieval, so this is the "
        "line the Claude SDK's warm-client key is cut at",
    ),
    (
        "legacy_tail",
        "retrieval",
        "volatile last, and among the volatile blocks the end of the prompt — "
        "the U-curve's second-strongest position — goes to the memories "
        "retrieved for THIS question rather than to a knowledge-base dump",
    ),
)


async def test_the_layer_order_is_the_one_the_cache_and_the_u_curve_require():
    """Order is behaviour twice over, and neither reason is aesthetic.

    PREFIX CACHING: the assembled prompt is reused left-to-right. Everything
    above the first byte that changes is cacheable; everything below it is paid
    for again. Sorting stable-to-volatile is what makes the cacheable region as
    long as it can be, and ``ClaudeSDKBackend._behavior_prefix`` cuts the
    warm-client key at that same boundary — so a keyed layer moved below it stops
    contributing to the key at all.

    THE U-CURVE: a model attends best to the start and the end of its context and
    worst to the middle. Identity takes the start; the memories chosen for this
    question take the end. What sits between is the material that must be present
    without being attended to moment by moment.

    Reordering any two adjacent layers fails one of the rules below, and each
    rule carries the reason it exists so the failure says what was broken rather
    than that a tuple changed.

    MUTATION: any transposition of ``_SYSTEM_PROMPT_LAYERS``. Verified with
    ``identity``/``surface``, ``surface``/``instructions``,
    ``instructions``/``legacy_tail`` and ``legacy_tail``/``retrieval``.
    """
    order = list(_SYSTEM_PROMPT_LAYERS)
    for earlier, later, why in _ORDER_RULES:
        assert order.index(earlier) < order.index(later), (
            f"{earlier!r} must precede {later!r}: {why}"
        )

    assert order[0] == "identity", "the U-curve's head belongs to who the agent is"
    assert order[-1] == "retrieval", "the U-curve's tail belongs to this question's memories"


async def test_every_assembled_layer_has_stated_where_it_belongs():
    """The guard that makes the rules above extensible instead of decorative.

    PA-5 registers ``atlas`` and ``user``. Adding either to
    ``_SYSTEM_PROMPT_LAYERS`` without adding a rule saying what it must sit
    between leaves its position unconstrained — the test would keep passing while
    the new layer floated anywhere, including below the volatile cut, where a
    keyed layer silently loses its cache contribution. So an unconstrained layer
    fails here.

    MUTATION: append a layer name to ``_SYSTEM_PROMPT_LAYERS`` without a rule.
    This fails.
    """
    constrained = {name for rule in _ORDER_RULES for name in rule[:2]}
    assert constrained == set(_SYSTEM_PROMPT_LAYERS), (
        "a layer is assembled without any rule saying where it belongs — add it "
        "to _ORDER_RULES with the reason, do not widen this assertion"
    )


async def test_the_instructions_layer_is_registered():
    """The pool resolves layers by name, so an unregistered one is a KeyError on
    the first turn rather than a missing block in the prompt.

    MUTATION: drop the ``register`` call in ``prompt/registry.py``.
    """
    assert "instructions" in prompt_layer_registry.list()
    assert isinstance(prompt_layer_registry.get("instructions"), InstructionsLayer)


# ---------------------------------------------------------------------------
# 6 — the entity override still beats the surface
# ---------------------------------------------------------------------------


async def test_the_entity_override_replaces_the_base_and_keeps_every_layer():
    """RFC #14's entity-wins-if-set, observed where it lands: the override SWAPS
    the base persona and the layers below it still append.

    ``compose_entity_profile`` decides the precedence itself (entity wins when
    set, else the surface base) and is pinned in
    ``tests/cloud/test_entity_profile_compose.py``. What is pinned HERE is that
    the decision survives assembly — the surface preamble, the instruction stack
    and both volatile blocks are all still in the prompt, which is what makes it
    an override of the persona rather than of the turn.

    MUTATION: make ``AgentIdentityLayer`` ignore ``ctx.system_message_override``.
    The persona survives and this fails.
    """
    assembled = await _full_turn(system_message_override=_OVERRIDE)

    assert assembled.text.startswith(_OVERRIDE)
    assert _PERSONA not in assembled.text, "the override did not replace the base persona"
    assert _SURFACE in assembled.text, "the override must not swallow the surface"
    assert _INSTRUCTIONS in assembled.text
    assert "## Your Knowledge Base" in assembled.text
    assert "## Relevant Past Memories" in assembled.text


async def test_the_override_reaches_the_digest_through_both_layers_it_changes():
    """What the override keys, stated in one place because the two are easy to
    reason about separately and get wrong together.

    * ``instructions`` — it arrives INSIDE ``ctx.instructions``.
      ``build_behavior_instructions`` appends the override INSTEAD of the ripple
      LAW, the delegation rule and the pocket prompts, so two entities on one
      agent produce different instruction bytes and therefore different keys.
    * ``identity`` — it arrives as ``ctx.system_message_override`` and does
      something else: it SWAPS the base persona. PA-3b's soul-key drop hangs off
      that swap, since an override replaces the soul text and the soul's claim
      then describes bytes that are no longer in the prompt.

    Both see it, deliberately. They key different consequences of one input, and
    neither can drop it — identity would stop keying the swap, instructions would
    stop keying the rules.

    MUTATION: collapse ``InstructionsLayer``'s key to a constant. The first
    assertion fails. Collapse ``AgentIdentityLayer``'s ``override_key`` to a
    constant instead and the second fails.
    """
    dental = await InstructionsLayer().render(_ctx(instructions=f"{_INSTRUCTIONS}\n{_OVERRIDE}"))
    bakery = await InstructionsLayer().render(
        _ctx(instructions=f"{_INSTRUCTIONS}\nYou are Rye Bakery's assistant.")
    )
    assert dental.cache_key != bakery.cache_key, (
        "two entity rooms whose rules differ must not share an instructions key"
    )

    identity = prompt_layer_registry.get("identity")
    swapped = await identity.render(_ctx(system_message_override=_OVERRIDE))
    other = await identity.render(_ctx(system_message_override="You are Rye Bakery's assistant."))
    assert swapped.cache_key != other.cache_key, (
        "the identity layer must keep keying the override — the base swap, and "
        "PA-3b's soul-key drop, both hang off it"
    )
