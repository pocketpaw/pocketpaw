# tests/test_prompt_budget.py
# Created: 2026-08-03 (PA-5, feat/prompt-assembler-seam) — pins the budget, the
# priority ladder, the two cuts and the two new layers.
#
# The properties held here:
#   1. AN ACTUALLY OVER-FILLED BUDGET drops LOW before MEDIUM before HIGH and
#      never drops CRITICAL. Driven through the REAL layer list with real
#      content and a real budget, not by unit-testing a comparator — the thing
#      that breaks is the interaction, not the ``<``.
#   2. EVERY CUT IS REPORTED, in ``dropped`` and in the log, for both kinds.
#   3. THE CAP IS BUDGET-INDEPENDENT. This is the one that protects the cache
#      key: two assemblies that share a layer's key must carry identical bytes
#      for it, and they only do while the cut is a function of the layer's own
#      text rather than of what is left of the budget.
#   4. BYTE-NEUTRALITY UNDER BUDGET. An assembly that fits produces exactly what
#      an unbounded one does.
#   5. THE TWO NEW LAYERS SIT ABOVE THE VOLATILE REGION — checked with them
#      actually rendering, because the guard in
#      ``test_prompt_instructions_layer.py`` skips empty layers and both are
#      empty on every path that exists today.
#   6. WHAT THEIR KEYS DISCRIMINATE, including the halves PA-5 filed them
#      without.
#
# Every test names the mutation that must break it. Each was applied and re-run
# on 2026-08-03; the outcomes are in the commit body.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from pocketpaw.agents.pool import _SYSTEM_PROMPT_LAYERS, AgentPool
from pocketpaw.prompt import (
    AssembledPrompt,
    AtlasPrimerLayer,
    LayerOutput,
    Priority,
    PromptContext,
    UserInfoLayer,
    assemble,
    prompt_layer_registry,
)
from pocketpaw.prompt.passthrough import _KNOWLEDGE_HEADER
from pocketpaw.prompt.retrieval import _MEMORY_HEADER

pytestmark = pytest.mark.asyncio

_STAMP = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixture — every droppable layer rendering the SAME number of characters
# ---------------------------------------------------------------------------
#
# Equal sizes are not cosmetic. The budget gives priority FIRST REFUSAL on what
# is left rather than enforcing a strict prefix (see
# ``test_a_layer_that_fits_is_admitted_after_a_bigger_one_was_skipped``), so with
# unequal blocks a large MEDIUM can be skipped while a small LOW still fits —
# correct, and it would make the ladder below unreadable. Equal blocks take that
# interaction out of the ladder test and give it its own.
_BLOCK = 400

_IDENTITY = "P" * _BLOCK
_ATLAS = "A" * _BLOCK
_USER = "U" * _BLOCK
_SURFACE = "S" * _BLOCK
_INSTRUCTIONS = "I" * _BLOCK
# The two tail layers wrap their content in a fixed header, so the payload is
# padded to land the RENDERED block on ``_BLOCK`` exactly.
_KNOWLEDGE = "K" * (_BLOCK - len(_KNOWLEDGE_HEADER))
_RECALL = "M" * (_BLOCK - len(_MEMORY_HEADER))

assert len(_KNOWLEDGE) > 0 and len(_RECALL) > 0, "a header outgrew the fixture block size"

# All seven layers, so the fully assembled text is exactly this many chars of
# content (the ``\n\n`` joins are deliberately not charged to the budget — see
# ``assembler._fit_to_budget``).
_TOTAL = _BLOCK * 7

_SURFACE_KEY = "pocket:p-42:2026-08-02T09:14:00Z"
_TENANT = "workspace:w-7"
_USER_ID = "user-3"


class _FakeBootstrapProvider:
    def __init__(self, identity: str) -> None:
        self._identity = identity

    async def get_context(self):
        return SimpleNamespace(identity=self._identity, knowledge=[], identity_cache_key="soul:k")


class _FakeSoul:
    async def context_for(self, message: str, **_kwargs) -> str:
        return _RECALL


def _instance():
    return SimpleNamespace(
        backend=None,
        soul_manager=SimpleNamespace(
            bootstrap_provider=_FakeBootstrapProvider(_IDENTITY), soul=_FakeSoul()
        ),
        config={"soul_persona": "BASE PERSONA", "system_prompt": "base extra"},
        created_from_updated_at=_STAMP,
        last_active=datetime.now(UTC),
        active_runs=0,
    )


async def _turn(**kwargs) -> AssembledPrompt:
    """One realistic turn with ALL SEVEN layers rendering, through the real seam.

    The seam matters: the order under test is ``AgentPool``'s own layer list and
    the priorities are the ones the shipped layer classes declare, so a test that
    passes here is a statement about the cloud path rather than about a list this
    file made up.
    """
    return await AgentPool()._assemble_system_prompt(
        kwargs.pop("instance", None) or _instance(),
        agent_id=kwargs.pop("agent_id", "agent-1"),
        message=kwargs.pop("message", "what time do you open?"),
        instructions=kwargs.pop("instructions", _INSTRUCTIONS),
        knowledge_context=kwargs.pop("knowledge_context", _KNOWLEDGE),
        system_message_override=kwargs.pop("system_message_override", None),
        surface_preamble=kwargs.pop("surface_preamble", _SURFACE),
        surface_cache_key=kwargs.pop("surface_cache_key", _SURFACE_KEY),
        atlas_primer=kwargs.pop("atlas_primer", _ATLAS),
        tenant_scope=kwargs.pop("tenant_scope", _TENANT),
        user_info=kwargs.pop("user_info", _USER),
        user_id=kwargs.pop("user_id", _USER_ID),
        **kwargs,
    )


def _dropped(assembled: AssembledPrompt) -> set[str]:
    return {entry.name for entry in assembled.dropped}


def _block(assembled: AssembledPrompt, starts_with: str) -> str:
    """The one ``\\n\\n``-separated block that opens with ``starts_with``.

    By content rather than by index: a positional lookup silently reads the
    NEXT layer's block once the budget drops the one it meant, which is how the
    first draft of ``test_one_key_names_one_text_even_when_the_cap_bit``
    reported a cache bug that was not there.
    """
    matches = [part for part in assembled.text.split("\n\n") if part.startswith(starts_with)]
    assert len(matches) == 1, (
        f"expected exactly one block opening {starts_with!r}, got {len(matches)}"
    )
    return matches[0]


class _StubLayer:
    """A layer with every property under test set explicitly.

    Used only where the claim is about the ASSEMBLER rather than about a shipped
    layer: comparing a budget-dropped layer against one that was never in the
    list at all needs two different lists, which the pool's fixed tuple cannot
    give. Everything else in this file goes through the real seam.
    """

    def __init__(
        self,
        name: str,
        priority: Priority,
        text: str,
        cache_key: str | None,
        *,
        raises: bool = False,
        max_chars: int | None = None,
    ) -> None:
        self.name = name
        self.priority = priority
        self.max_chars = max_chars
        self._text = text
        self._cache_key = cache_key
        self._raises = raises

    async def render(self, ctx: PromptContext) -> LayerOutput:
        if self._raises:
            raise RuntimeError("handler exploded")
        return LayerOutput(text=self._text, cache_key=self._cache_key)


def _core() -> _StubLayer:
    return _StubLayer("core", Priority.CRITICAL, "C" * 100, "core-v1")


_GOLDEN = "\n\n".join(
    [
        _IDENTITY,
        _ATLAS,
        _USER,
        _SURFACE,
        _INSTRUCTIONS,
        f"{_KNOWLEDGE_HEADER}{_KNOWLEDGE}",
        f"{_MEMORY_HEADER}{_RECALL}",
    ]
)


# ---------------------------------------------------------------------------
# 1 — the ladder, driven by actually over-filling the budget
# ---------------------------------------------------------------------------


async def test_nothing_is_dropped_while_everything_fits():
    """The floor of the ladder, and the fixture's own sanity check: seven layers
    rendering ``_BLOCK`` chars each fit a budget of exactly ``_TOTAL``.

    If this fails, every budget below is measuring the wrong thing — most likely
    the joins being charged, which ``_fit_to_budget`` deliberately does not do.

    MUTATION: charge the ``\\n\\n`` separators to the budget. ``retrieval`` no
    longer fits and this fails.
    """
    assembled = await _turn(budget_chars=_TOTAL)

    assert assembled.dropped == []
    assert assembled.text == _GOLDEN


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        (_TOTAL - 1, {"retrieval"}),
        (_TOTAL - _BLOCK - 1, {"retrieval", "legacy_tail"}),
        (_TOTAL - 2 * _BLOCK - 1, {"retrieval", "legacy_tail", "atlas"}),
        (_TOTAL - 3 * _BLOCK - 1, {"retrieval", "legacy_tail", "atlas", "surface"}),
        (_TOTAL - 4 * _BLOCK - 1, {"retrieval", "legacy_tail", "atlas", "surface", "user"}),
        (0, {"retrieval", "legacy_tail", "atlas", "surface", "user"}),
    ],
)
async def test_the_budget_drops_low_then_medium_then_high_and_never_critical(budget, expected):
    """The acceptance property, over an actually over-filled budget rather than a
    comparator.

    Each row tightens the budget by one whole block and names every layer that
    must be gone by then. The ladder runs LOW (``retrieval``, then
    ``legacy_tail``) → MEDIUM (``atlas``) → HIGH (``surface``, then ``user``),
    and stops: the last row is a budget of ZERO, where both CRITICAL layers are
    still present and still whole.

    The two ties are as informative as the ranks. ``retrieval`` and
    ``legacy_tail`` are both LOW and ``retrieval`` goes first because ties fall
    back to LAYER ORDER and it renders last — the right way round, since of the
    two per-message blocks the recall is the more expendable. ``surface`` and
    ``user`` are both HIGH and ``surface`` goes first for the same reason,
    which is the intended reading: who is talking outranks where they are
    looking.

    MUTATION: sort the budget pass by ``-priority``. The first row drops
    ``surface`` instead of ``retrieval`` and this fails. MUTATION: let CRITICAL
    be dropped like anything else — the zero-budget row loses ``identity`` and
    ``instructions`` and this fails.
    """
    assembled = await _turn(budget_chars=budget)

    assert _dropped(assembled) == expected
    assert _IDENTITY in assembled.text, "a CRITICAL layer was dropped"
    assert _INSTRUCTIONS in assembled.text, "a CRITICAL layer was dropped"


async def test_a_critical_layer_over_budget_is_emitted_whole_not_cut_to_fit():
    """The divergence from ``context_builder._assemble_with_budget``, and the
    reason for it.

    That function truncates a CRITICAL block to whatever is left of the budget.
    Here the budget is allowed to overrun instead, because a cut sized from the
    REMAINING budget depends on what the layer's SIBLINGS rendered — so one
    ``cache_key`` would name two different texts, which is #1842 reached through
    the budget. Both CRITICAL layers are keyed, so that cut is exactly the
    unsafe one. Bounding a critical layer means giving it a constant
    ``max_chars``, not a share of the budget.

    MUTATION: restore ``_assemble_with_budget``'s behaviour — truncate CRITICAL
    to ``remaining``. ``identity`` comes back as 10 chars and this fails.
    """
    assembled = await _turn(budget_chars=10)

    assert assembled.text == f"{_IDENTITY}\n\n{_INSTRUCTIONS}"
    assert len(assembled.text) > 10, "the budget must be allowed to overrun for CRITICAL"


async def test_a_layer_that_fits_is_admitted_after_a_bigger_one_was_skipped():
    """Priority buys FIRST REFUSAL on the remaining budget, not a strict prefix.

    ``_assemble_with_budget`` uses ``continue`` rather than ``break`` and that is
    kept: a MEDIUM block too large for what is left is skipped, and a LOW block
    that does fit is still admitted afterwards. It never drops something to make
    room for something less important — it only lets a small block use space a
    large one could not, which delivers strictly more content for the same
    budget.

    Stated as its own test because the ladder above is deliberately built from
    equal blocks to keep this interaction out of it, and an untested behaviour
    that looks like a bug gets "fixed".

    MUTATION: set ``remaining = -1`` alongside ``layer.kept = False``, so the
    first drop takes everything below it. ``retrieval`` is dropped with ``atlas``
    and this fails.

    NOT ``break`` instead of ``continue``, which is the mutation this test was
    first written against and which SURVIVED it: ``break`` abandons the budget
    rather than tightening it, so every remaining layer keeps ``kept=True`` and
    is emitted. It makes the budget weaker, not strict — a different bug, and one
    the ladder catches.
    """
    # Room for both CRITICAL layers, then enough for the small LOW block but not
    # for the oversized MEDIUM one that is offered the space first.
    assembled = await _turn(
        atlas_primer="A" * 1200,
        knowledge_context="",
        surface_preamble="",
        surface_cache_key=None,
        user_info="",
        budget_chars=2 * _BLOCK + 600,
    )

    assert "atlas" in _dropped(assembled), "the oversized MEDIUM block must not fit"
    assert "retrieval" not in _dropped(assembled), (
        "a LOW block that fits must still be admitted after a bigger MEDIUM one "
        "was skipped — priority is first refusal, not a strict prefix"
    )


# ---------------------------------------------------------------------------
# 2 — every cut is reported, in ``dropped`` and in the log
# ---------------------------------------------------------------------------


async def test_a_budget_drop_lands_in_dropped_and_in_the_log(caplog):
    """Zero silent drops. Both channels, because they serve different readers:
    ``dropped`` is what a caller can assert on and PA-9 can count, the log is
    what someone reading production output sees.

    MUTATION: delete the ``dropped.append`` in ``_fit_to_budget`` — the first
    assertion fails. Delete the ``logger.warning`` — the second fails.
    """
    with caplog.at_level(logging.INFO, logger="pocketpaw.prompt.assembler"):
        assembled = await _turn(budget_chars=_TOTAL - 1)

    assert [(entry.name, entry.reason) for entry in assembled.dropped] == [
        ("retrieval", f"dropped for budget ({_BLOCK} chars, priority LOW, {_BLOCK - 1} remaining)")
    ]
    assert "Dropped prompt layer 'retrieval'" in caplog.text
    assert "budget exhausted" in caplog.text


async def test_a_cap_truncation_lands_in_dropped_and_in_the_log(caplog):
    """A truncation is a cut too, and the noisier failure mode: a layer that is
    still in the prompt but no longer complete is the one nobody notices.

    MUTATION: return ``text[:cap]`` from ``_apply_cap`` without appending to
    ``dropped`` — the first assertion fails, and the prompt silently carries a
    fragment.
    """
    with caplog.at_level(logging.INFO, logger="pocketpaw.prompt.assembler"):
        assembled = await _turn(atlas_primer="A" * 5000)

    assert [(entry.name, entry.reason) for entry in assembled.dropped] == [
        ("atlas", "truncated to its 2000-char cap (was 5000)")
    ]
    assert "truncated to its 2000-char cap" in caplog.text


async def test_a_capped_layer_is_still_in_the_prompt_and_says_it_was_cut():
    """The cut is visible to the model, not only to the log. ``[...truncated]``
    is what ``context_builder._assemble_with_budget`` appends, byte for byte,
    including the fact that it pushes the block past the cap it is enforcing —
    matched deliberately so PA-7 can keep the channel path's bytes identical.

    MUTATION: cut at ``cap - len(marker)`` so the block honours its cap. The
    length assertion fails, and PA-7 inherits a byte-moving change it did not
    ask for.
    """
    assembled = await _turn(atlas_primer="A" * 5000)

    block = _block(assembled, "A" * 100)
    assert block == "A" * 2000 + "\n[...truncated]"
    assert len(block) == 2000 + len("\n[...truncated]")


# ---------------------------------------------------------------------------
# 3 — the cap is a function of the layer's text, never of the budget
# ---------------------------------------------------------------------------


async def test_one_key_names_one_text_even_when_the_cap_bit():
    """PA-5's real constraint, and the reason the budget drops whole layers
    instead of truncating them.

    A ``cache_key`` names what a layer WOULD have rendered. After a cut the
    prompt carries less than that, so the question is whether two assemblies can
    share one key and differ in bytes — which is #1842 arriving through the
    budget rather than through the backend. They cannot while the cut is
    ``text[:cap]`` for a constant ``cap``: that is a pure function of the layer's
    own text, so ``key ⟹ same text`` still gives ``key ⟹ same emitted bytes``.

    Asserted against the thing that would break it: the SAME atlas text under
    three wildly different budgets, all loose enough to ADMIT the layer (a budget
    that drops it is a different claim, and one
    ``test_a_dropped_keyed_layer_is_a_different_identity_from_an_absent_one``
    makes). Same digest every time, and the same bytes every time — the emitted
    block does not know what the budget was.

    MUTATION: size the cap from the remaining budget (pass ``remaining`` into
    ``_apply_cap`` and use ``min(cap, remaining)``). The three blocks diverge
    while the digest holds still, and this fails — which is the failure it
    exists to name.
    """
    assemblies = [
        await _turn(atlas_primer="A" * 5000, budget_chars=budget)
        for budget in (None, 10 * _TOTAL, 4000)
    ]

    assert {a.stable_digest for a in assemblies} == {assemblies[0].stable_digest}, (
        "the fixture is not holding the digest still — the budget dropped a keyed layer"
    )
    assert len({_block(a, "A" * 100) for a in assemblies}) == 1, (
        "the same digest named two different texts — a budget-dependent cut has "
        "reintroduced #1842 through the budget"
    )


async def test_a_capped_layer_keeps_its_own_key_rather_than_going_unkeyed():
    """The alternative that looks cautious and is the actual bug.

    "I am not sure this key is exact" and "this content does not belong in the
    key" are opposite claims. Answering the first with ``cache_key=None`` drops
    the layer out of the digest entirely, so a capped ``atlas`` that changed from
    one tenant's text to another's would not move the digest AT ALL and the
    second tenant would be served the first's cached agent.

    MUTATION: return ``cache_key=None`` from ``AtlasPrimerLayer.render`` when the
    text exceeds the cap. Both assertions fail.
    """
    long_a = await _turn(atlas_primer="A" * 5000, tenant_scope="workspace:a")
    long_b = await _turn(atlas_primer="B" * 5000, tenant_scope="workspace:b")

    assert _block(long_a, "A" * 100) != _block(long_b, "B" * 100)
    assert long_a.stable_digest != long_b.stable_digest, (
        "two capped tenants share a digest — the layer went unkeyed under its cap"
    )


# ---------------------------------------------------------------------------
# 4 — byte-neutrality, and the digest's account of a drop
# ---------------------------------------------------------------------------


async def test_an_assembly_that_fits_is_byte_identical_to_an_unbounded_one():
    """The budget must be invisible until it bites. A generous budget and no
    budget at all produce the same bytes AND the same digest, so switching one
    on cannot move a prompt by itself.

    MUTATION: subtract the joins from the budget and then re-add them to the
    emitted text — the texts still match but the arithmetic drifts; easier to
    catch with the ladder above. The mutation this one really guards is a budget
    pass that reorders: emit in priority order and ``_GOLDEN`` fails.
    """
    unbounded = await _turn()
    generous = await _turn(budget_chars=10 * _TOTAL)

    assert unbounded.text == _GOLDEN
    assert generous.text == unbounded.text
    assert generous.stable_digest == unbounded.stable_digest
    assert generous.dropped == []


async def test_the_emitted_order_is_the_layer_list_not_the_priority_order():
    """The budget decides in priority order and must never REORDER, because the
    prompt's order is a cache contract (the prefix is reused left-to-right) and
    an attention contract (the U-curve). ``_assemble_with_budget`` does reorder —
    it sorts by priority and joins in that order — and that is not carried over.

    The two orders genuinely differ here: by priority ``instructions`` (CRITICAL)
    precedes ``atlas`` (MEDIUM); by layer list ``atlas`` comes first.

    MUTATION: join the texts in the budget pass's sorted order. This fails.
    """
    assembled = await _turn(budget_chars=_TOTAL)

    assert assembled.text.index(_ATLAS) < assembled.text.index(_INSTRUCTIONS)
    assert assembled.text == _GOLDEN


async def test_a_dropped_keyed_layer_is_a_different_identity_from_an_absent_one():
    """The assembler's founding rule, now with the budget as its second producer:
    dropping a layer from ``text`` must never drop it from the digest.

    Three states of ``atlas`` must be three digests — present, dropped for
    budget, and supplied with nothing in it. Collapse any pair and a backend
    caching on the digest hands a cached agent to a prompt that is missing the
    block it was built with.

    MUTATION: keep the layer's REAL key when dropped. "dropped" and "present"
    collapse and this fails.
    """
    present = await _turn(budget_chars=_TOTAL)
    dropped = await _turn(budget_chars=_TOTAL - 2 * _BLOCK - 1)
    empty = await _turn(atlas_primer="", budget_chars=_TOTAL)

    assert "atlas" in _dropped(dropped)
    assert len({present.stable_digest, dropped.stable_digest, empty.stable_digest}) == 3


async def test_a_dropped_layer_is_not_the_same_identity_as_one_never_assembled():
    """The rule stated the way ``_digest``'s docstring states it, and the way
    that can actually fail: "atlas dropped" and "atlas never present" must not be
    one identity.

    The test above cannot catch this. Its three states all keep ``atlas`` IN the
    layer list, so omitting a dropped layer's key still leaves three distinct
    digests — it is a weaker claim than it reads as. The claim that bites needs
    two different layer LISTS, which the pool's fixed tuple cannot give, so this
    one goes through ``assemble`` directly.

    The two prompts here are the SAME BYTES and must be different identities.
    That is the whole point: the text cannot tell you whether a layer was
    considered and dropped or never offered, and a backend that caches on the
    digest has to be able to.

    MUTATION: skip dropped layers when building ``keyed``
    (``if record.cache_key is not None and record.kept``). The two collapse and
    this fails.
    """
    big = _StubLayer("big", Priority.LOW, "B" * 500, "big-v1")
    ctx = _bare_ctx()

    dropped = await assemble([_core(), big], ctx, budget_chars=100)
    never_offered = await assemble([_core()], ctx, budget_chars=100)

    assert _dropped(dropped) == {"big"}
    assert dropped.text == never_offered.text, "the fixture must produce identical bytes"
    assert dropped.stable_digest != never_offered.stable_digest


async def test_dropping_an_unkeyed_layer_leaves_the_digest_alone():
    """The other half, and it is not an oversight. An unkeyed layer is outside
    the digest whether it renders or not; giving only its ABSENCE a key would
    make the digest move on a per-turn condition, which is the exact churn
    ``cache_key=None`` exists to prevent — and the trade #1842 refused.

    ``retrieval`` and ``legacy_tail`` are both unkeyed, so a budget tight enough
    to lose both must leave the digest exactly where it was.

    MUTATION: contribute ``_BUDGET_DROPPED_KEY`` for unkeyed layers too. The
    digest moves and this fails.
    """
    full = await _turn(budget_chars=_TOTAL)
    without_tail = await _turn(budget_chars=_TOTAL - 2 * _BLOCK)

    assert _dropped(without_tail) == {"retrieval", "legacy_tail"}
    assert without_tail.stable_digest == full.stable_digest


# ---------------------------------------------------------------------------
# 5 — both new layers sit above the volatile region
# ---------------------------------------------------------------------------


async def test_the_two_new_keyed_layers_land_inside_the_behaviour_prefix():
    """Constraint 2, verified with the layers actually RENDERING.

    ``test_every_keyed_layer_lands_inside_the_behaviour_prefix`` walks
    ``_SYSTEM_PROMPT_LAYERS`` and was written to cover these two the day they
    were registered — but it skips a layer whose text is empty, and both are
    empty on every path that exists today, so it would report success without
    checking anything. This is the version with content in them.

    What it protects: ``_behavior_prefix`` cuts the warm-client key at the
    EARLIEST volatile marker, so a KEYED layer ordered below one is cut out of
    that key entirely — it declares itself stable, the digest honours that, and
    the Claude SDK never sees it. The layer looks keyed and behaves unkeyed.

    MUTATION: move ``atlas`` and ``user`` to the end of ``_SYSTEM_PROMPT_LAYERS``
    (after ``retrieval``). Both fall outside the prefix and this fails.
    """
    assembled = await _turn()
    prefix = ClaudeSDKBackend._behavior_prefix(assembled.text)

    assert "## Your Knowledge Base" in assembled.text, "the fixture must carry a volatile tail"
    assert "## Relevant Past Memories" in assembled.text
    assert prefix == "\n\n".join([_IDENTITY, _ATLAS, _USER, _SURFACE, _INSTRUCTIONS])


async def test_both_new_layers_are_registered_and_assembled():
    """The pool resolves layers by name, so an unregistered one is a KeyError on
    the first turn rather than a missing block.

    MUTATION: drop either ``register`` call in ``prompt/registry.py``, or either
    name from ``_SYSTEM_PROMPT_LAYERS``.
    """
    assert isinstance(prompt_layer_registry.get("atlas"), AtlasPrimerLayer)
    assert isinstance(prompt_layer_registry.get("user"), UserInfoLayer)
    assert _SYSTEM_PROMPT_LAYERS == (
        "identity",
        "atlas",
        "user",
        "surface",
        "instructions",
        "legacy_tail",
        "retrieval",
    )


async def test_the_declared_priorities_are_the_ones_the_ladder_relies_on():
    """The ranks in one place, so a change to one of them fails HERE — where the
    reason is written down — rather than only in the ladder, where it reads as an
    arithmetic error.

    MUTATION: demote ``instructions`` to HIGH. This fails, and so does the
    zero-budget row of the ladder.
    """
    ranks = {name: prompt_layer_registry.get(name).priority for name in _SYSTEM_PROMPT_LAYERS}

    assert ranks == {
        "identity": Priority.CRITICAL,
        "atlas": Priority.MEDIUM,
        "user": Priority.HIGH,
        "surface": Priority.HIGH,
        "instructions": Priority.CRITICAL,
        "legacy_tail": Priority.LOW,
        "retrieval": Priority.LOW,
    }
    caps = {name: prompt_layer_registry.get(name).max_chars for name in _SYSTEM_PROMPT_LAYERS}
    assert caps == {
        "identity": None,
        "atlas": 2000,
        "user": 500,
        "surface": None,
        "instructions": None,
        "legacy_tail": None,
        "retrieval": None,
    }


# ---------------------------------------------------------------------------
# 6 — what the two keys discriminate, including the halves PA-5 filed without
# ---------------------------------------------------------------------------


async def _atlas_key(**kwargs) -> str | None:
    out = await AtlasPrimerLayer().render(_bare_ctx(**kwargs))
    return out.cache_key


async def _user_key(**kwargs) -> str | None:
    out = await UserInfoLayer().render(_bare_ctx(**kwargs))
    return out.cache_key


def _bare_ctx(**kwargs) -> PromptContext:
    return PromptContext(
        instance=kwargs.pop("instance", None) or _instance(),
        agent_id=kwargs.pop("agent_id", "agent-1"),
        message=kwargs.pop("message", ""),
        instructions=kwargs.pop("instructions", ""),
        knowledge_context=kwargs.pop("knowledge_context", ""),
        system_message_override=kwargs.pop("system_message_override", None),
        **kwargs,
    )


async def test_the_atlas_key_moves_on_the_scope_and_on_the_bytes():
    """PA-5 filed this key as the tenant scope alone. It carries the scope AND a
    digest, because neither half does the job:

    * the SCOPE alone discriminates nothing a live cache can see — cloud agents
      are per-workspace, so it is fixed for the whole lifetime of the cache it
      guards, the same reduction PA-3b recorded for ``created_from_updated_at``;
    * the DIGEST alone would call two tenants running the same primer one
      identity, which is true of the bytes today and stops being true the moment
      the primer reads ``atlas/overlay.py``'s per-tenant availability.

    The digest is exact for the reason ``instructions`` gives: this block is the
    complete artifact, every primitive the store holds, not the first twelve of
    N under a cap. Hashing it cannot under-report the way hashing a surface
    preamble does.

    MUTATION: key on ``ctx.tenant_scope`` alone — the second assertion fails.
    Key on the digest alone — the first fails.
    """
    tenant_a = await _atlas_key(atlas_primer=_ATLAS, tenant_scope="workspace:a")
    tenant_b = await _atlas_key(atlas_primer=_ATLAS, tenant_scope="workspace:b")
    assert tenant_a != tenant_b, "two tenants share an atlas key"

    seed_v1 = await _atlas_key(atlas_primer=_ATLAS, tenant_scope=_TENANT)
    seed_v2 = await _atlas_key(
        atlas_primer=_ATLAS + " Ripple: a graph of runs.", tenant_scope=_TENANT
    )
    assert seed_v1 != seed_v2, "an edited primer did not move its key"


async def test_the_user_key_moves_on_a_profile_edit_that_the_id_cannot_see():
    """The deviation's whole point. PA-5 filed this key as ``user_id``, which
    cannot see an EDIT: a member changes team, the block changes, the id does
    not, and a backend that bakes the prompt into a cached agent keeps
    introducing them by their old role. The usual answer is a document revision
    and there is not one — ``updatedAt`` holds its construction-time value
    forever, because beanie never registers ``TimestampedDocument``'s
    ``_``-prefixed hooks. The bytes are the only honest revision available.

    MUTATION: key on ``ctx.user_id`` alone. This fails.
    """
    before = await _user_key(
        user_info="<about-member>\n  who: Ada · founder\n</about-member>", user_id=_USER_ID
    )
    after = await _user_key(
        user_info="<about-member>\n  who: Ada · CTO\n</about-member>", user_id=_USER_ID
    )

    assert before != after


async def test_two_members_without_a_profile_still_key_apart():
    """The half a digest alone would lose. Two members with no materialized
    ``Person`` both render nothing, so a text digest calls them one identity —
    true of the prompt, and a claim the layer that exists to say WHO is talking
    should not be the one to make. The cost is one extra rebuild when switching
    between two profile-less members on one agent, which is the safe direction.

    MUTATION: key on the digest alone. This fails.
    """
    ada = await _user_key(user_info="", user_id="user-ada")
    bo = await _user_key(user_info="", user_id="user-bo")

    assert ada != bo


async def test_an_absent_channel_still_contributes_a_key():
    """Both layers key even when they render nothing, exactly as ``instructions``
    does. A run carrying the OS primer and a run without it are different
    prompts, and the one thing a digest must never do is call them one identity.

    MUTATION: return ``cache_key=None`` when the text is empty. Both assertions
    fail, and ``test_a_dropped_keyed_layer_is_a_different_identity_from_an_absent_one``
    loses its third state.
    """
    assert await _atlas_key(atlas_primer="") is not None
    assert await _user_key(user_info="") is not None
    assert await _atlas_key(atlas_primer="") != await _atlas_key(atlas_primer=_ATLAS)


# ---------------------------------------------------------------------------
# 7 — the guards PA-1 built still hold with two passes running after them
# ---------------------------------------------------------------------------


async def test_a_failure_a_budget_drop_and_a_success_are_three_identities():
    """PA-1's render guard, re-checked because the budget pass now runs after it
    and reads the record it leaves behind.

    One layer, one name, three fates: it RAISED, it was DROPPED for budget, it
    rendered. All three leave nothing or something different in the text, and all
    three must hash apart — a layer that exploded might have rendered anything,
    while a dropped one rendered something known and too large, and a backend
    caching on the digest has to be able to tell those apart before reusing an
    agent. The guard is also still doing its first job: the raising layer does
    not fail the turn, and ``core`` is still in the prompt.

    Stated over three assemblies rather than two on purpose. The first draft
    compared a failure under a budget against the same failure without one — and
    marking the failed record ``kept=False`` moves BOTH sides identically, so the
    mutation survived a test that claimed to catch it.

    MUTATION: mark the failed record ``kept=False`` in ``assemble``'s except
    branch. Its key becomes ``_BUDGET_DROPPED_KEY``, the first two collapse to
    one identity, and this fails.
    """
    ctx = _bare_ctx()
    raised = await assemble(
        [_core(), _StubLayer("x", Priority.LOW, "", None, raises=True)], ctx, budget_chars=200
    )
    dropped = await assemble(
        [_core(), _StubLayer("x", Priority.LOW, "X" * 500, "x-v1")], ctx, budget_chars=200
    )
    fine = await assemble(
        [_core(), _StubLayer("x", Priority.LOW, "X" * 10, "x-v1")], ctx, budget_chars=200
    )

    assert raised.text == "C" * 100, "one layer must not fail the turn"
    assert [entry.reason for entry in raised.dropped] == ["render raised RuntimeError"]
    assert len({raised.stable_digest, dropped.stable_digest, fine.stable_digest}) == 3
