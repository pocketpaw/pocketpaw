# tests/test_prompt_assembler.py
# Created: 2026-08-02 (PA-1, feat/prompt-assembler-seam) — pins the prompt
# assembler seam and the one layer it is proven with (agent identity).
#
# Three things are held here:
#   1. The #1842 property, ONCE and centrally: a second session is never served
#      the first session's prompt, and two sessions whose identity differs
#      produce different ``stable_digest``s — so a backend that keys its agent
#      cache on the digest cannot hand session A's agent to session B. Before
#      this seam existed the same property had to be re-proven per backend, and
#      three backends got it wrong independently.
#   2. The GOLDEN: the assembled text is byte-identical to what
#      ``AgentPool._assemble_system_prompt`` produced BEFORE the refactor. The
#      constants below were captured by running the fixtures in this file
#      against the pre-refactor implementation (commit 1948e335), so they are
#      an anchor to real shipped behaviour, not to the new code.
#   3. The digest covers the KEYED layers only. The unkeyed passthrough (the
#      instructions block, the per-message soul recall, the knowledge wrapper)
#      must NOT churn it — a digest that moved every turn would be correct and
#      would also destroy the agent cache, which is exactly the trade-off
#      ``pydantic_ai``'s 2026-08-01 (f) note refused.
#
# Updated: 2026-08-02 (PA-1 review) — three gaps found in review, each now held:
#   * the ENTITY discriminator on its own. The session-A/session-B test varies
#     the override and the identity branch together, so each masked the other and
#     collapsing the override digest to a constant survived every test. The
#     override is the only part of the identity key that can change while a
#     cached agent is alive, so it is pinned alone.
#   * the two claims ``_digest`` makes in its own comments — the layer name is in
#     the hash, and the field/record separators keep ``("ab","c")`` from hashing
#     like ``("a","bc")``.
#   * the render guard: a raising layer degrades to a dropped layer with a
#     failure key instead of failing the turn, and cancellation still propagates.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from pocketpaw.agents.pool import AgentPool
from pocketpaw.prompt import (
    AssembledPrompt,
    DroppedLayer,
    LayerOutput,
    PromptContext,
    PromptLayerRegistry,
    assemble,
    prompt_layer_registry,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures — a minimal AgentInstance stand-in whose soul answers deterministically
# ---------------------------------------------------------------------------


class _FakeBootstrapProvider:
    def __init__(self, identity: str, knowledge: list[str]) -> None:
        self._identity = identity
        self._knowledge = knowledge

    async def get_context(self):
        return SimpleNamespace(identity=self._identity, knowledge=list(self._knowledge))


class _FakeSoul:
    """Answers ``context_for`` with a fixed block, echoing the query.

    Echoing matters: it is what makes the per-message recall genuinely volatile,
    so a test that pins the digest against it is testing something.
    """

    def __init__(self, recall: str) -> None:
        self._recall = recall

    async def context_for(self, message: str, **_kwargs) -> str:
        return f"{self._recall} (asked: {message})"


def _instance(
    *,
    identity: str | None = None,
    knowledge: list[str] | None = None,
    recall: str | None = None,
    persona: str = "",
    extra: str = "",
    updated_at: datetime | None = None,
):
    soul_manager = None
    if identity is not None or recall is not None:
        soul_manager = SimpleNamespace(
            bootstrap_provider=(
                _FakeBootstrapProvider(identity, knowledge or []) if identity is not None else None
            ),
            soul=_FakeSoul(recall) if recall is not None else None,
        )
    return SimpleNamespace(
        backend=None,
        soul_manager=soul_manager,
        config={"soul_persona": persona, "system_prompt": extra},
        created_from_updated_at=updated_at,
        last_active=datetime.now(UTC),
        active_runs=0,
    )


async def _assemble(instance, **kwargs) -> AssembledPrompt:
    """Call the seam with defaults filled in, so each test names only its subject."""
    return await AgentPool()._assemble_system_prompt(
        instance,
        agent_id=kwargs.pop("agent_id", "agent-1"),
        message=kwargs.pop("message", ""),
        instructions=kwargs.pop("instructions", ""),
        knowledge_context=kwargs.pop("knowledge_context", ""),
        system_message_override=kwargs.pop("system_message_override", None),
        **kwargs,
    )


_STAMP = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The golden — byte-identical to the pre-refactor prompt
# ---------------------------------------------------------------------------

_GOLDEN_SOUL_PATH = (
    "You are Paw, a helpful companion.\n"
    "\n"
    "# Key Knowledge\n"
    "- [semantic] the user drinks tea\n"
    "- Bond level: 42.0/100\n"
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

_GOLDEN_OVERRIDE_PATH = (
    "You are the Acme Dental booking assistant.\n"
    "\n"
    "RIPPLE LAW: narrate before every tool call.\n"
    "\n"
    "## Your Knowledge Base\n"
    "Use the following information from your knowledge base to answer questions. "
    "Always reference this data when relevant instead of making things up or "
    "using tools to search.\n"
    "\n"
    "Acme Dental opens at 9am."
)


async def test_the_assembled_text_is_byte_identical_to_the_pre_refactor_prompt():
    """The safety net for the whole layer migration.

    Every step of the legacy assembly is exercised: soul identity, the
    ``# Key Knowledge`` block, the authoritative instructions, the per-message
    soul recall, and the knowledge-base wrapper.
    """
    assembled = await _assemble(
        _instance(
            identity="You are Paw, a helpful companion.",
            knowledge=["[semantic] the user drinks tea", "Bond level: 42.0/100"],
            recall="- talked about tea yesterday",
            updated_at=_STAMP,
        ),
        message="what time do you open?",
        instructions="RIPPLE LAW: narrate before every tool call.",
        knowledge_context="Acme Dental opens at 9am.",
    )
    assert assembled.text == _GOLDEN_SOUL_PATH


async def test_the_override_path_is_byte_identical_to_the_pre_refactor_prompt():
    """The entity-rooms A1 semantics: the override SWAPS the base, KEEPS the layers."""
    assembled = await _assemble(
        _instance(persona="BASE PERSONA", extra="base extra", updated_at=_STAMP),
        message="what time do you open?",
        instructions="RIPPLE LAW: narrate before every tool call.",
        knowledge_context="Acme Dental opens at 9am.",
        system_message_override="You are the Acme Dental booking assistant.",
    )
    assert assembled.text == _GOLDEN_OVERRIDE_PATH


# ---------------------------------------------------------------------------
# The #1842 property, held at the seam
# ---------------------------------------------------------------------------


async def test_a_second_session_is_not_served_the_first_sessions_prompt():
    """Reported from the product side: create a site, open a BRAND-NEW chat, say
    "hi", and the agent offers to keep working on the site.

    Two halves, both required:
      * the text handed to the backend for session B carries B's content and
        none of A's — the seam builds per call and retains nothing; and
      * the two sessions' digests differ, so a backend keying its agent cache on
        ``stable_digest`` cannot serve A's cached agent to B.
    """
    instance = _instance(persona="BASE PERSONA", updated_at=_STAMP)

    session_a = await _assemble(
        instance,
        message="build me a landing site",
        instructions="<current-pocket id='p1' />\nProject: Acme Dental",
        system_message_override="You are the Acme Dental site assistant.",
    )
    session_b = await _assemble(
        instance,
        message="hi",
        instructions="(no pocket open)",
    )

    assert "Acme Dental" in session_a.text
    assert "(no pocket open)" in session_b.text
    assert "Acme Dental" not in session_b.text, (
        "the new session was assembled with the previous session's prompt"
    )
    assert session_a.stable_digest != session_b.stable_digest, (
        "two sessions with different identities must not share a cached agent"
    )


async def test_the_same_identity_over_two_messages_keeps_one_digest():
    """The counterpart, and the reason the digest hashes KEYS and not TEXT.

    Turn 2's text differs from turn 1's (the soul recall is keyed on the user's
    message) but nothing about WHO the agent is has changed, so the digest must
    hold still. A digest that moved per message would be correct and would also
    throw the agent cache away on every turn.
    """
    instance = _instance(
        identity="You are Paw, a helpful companion.",
        recall="- talked about tea yesterday",
        updated_at=_STAMP,
    )

    turn_1 = await _assemble(instance, message="what time do you open?")
    turn_2 = await _assemble(instance, message="and on sundays?")

    assert turn_1.text != turn_2.text, "the fixture is not exercising the volatile tail"
    assert turn_1.stable_digest == turn_2.stable_digest


async def test_two_entities_on_one_agent_key_apart():
    """The discriminator that actually varies against a LIVE cache.

    ``agent_id`` is fixed per instance and ``revision`` cannot change within the
    cache's lifetime — the pool tears the instance down when the document moves.
    The override is the ONLY component of the identity key that changes while a
    cached agent is alive, and one agent serving two entity rooms is #1842's
    scenario on the surface this seam was built for.

    Held on the override ALONE: same agent, same revision, both taking the
    override branch, differing only in the override string. The broader
    session-A/session-B test varies the override and the fallback branch
    together, so each masks the other.
    """
    instance = _instance(persona="BASE PERSONA", updated_at=_STAMP)

    dental = await _assemble(instance, system_message_override="You are Acme Dental's assistant.")
    bakery = await _assemble(instance, system_message_override="You are Rye Bakery's assistant.")

    assert dental.stable_digest != bakery.stable_digest, (
        "two entity rooms on one agent must not share a cached agent"
    )


async def test_a_changed_agent_revision_changes_the_digest():
    """An edited agent doc is a different identity, even at the same ``agent_id``."""
    before = await _assemble(_instance(persona="BASE PERSONA", updated_at=_STAMP))
    after = await _assemble(
        _instance(persona="BASE PERSONA", updated_at=datetime(2026, 8, 2, 9, 0, tzinfo=UTC))
    )
    assert before.stable_digest != after.stable_digest


async def test_two_agents_do_not_share_a_digest():
    """``agent_id`` is in the key, so byte-identical personas still key apart."""
    instance = _instance(persona="BASE PERSONA", updated_at=_STAMP)
    one = await _assemble(instance, agent_id="agent-1")
    two = await _assemble(instance, agent_id="agent-2")
    assert one.stable_digest != two.stable_digest


async def test_a_knowledge_only_prompt_no_longer_opens_with_a_blank_line():
    """The ONE byte of legacy behaviour this refactor does not preserve.

    The legacy knowledge-base step appended ``f"{prompt}\\n\\n## Your Knowledge
    Base..."`` unconditionally, so a prompt with nothing before it — no
    identity, no persona, no instructions, no recall — came out starting with a
    blank line (captured from the pre-refactor code: ``'\\n\\n## Your Knowledge
    Base...'``). The layer joins conditionally like every other step, so that
    degenerate case loses the leading gap. Pinned rather than left to drift.
    """
    assembled = await _assemble(
        _instance(updated_at=_STAMP),
        knowledge_context="Acme Dental opens at 9am.",
    )
    assert assembled.text.startswith("## Your Knowledge Base")


async def test_a_healthy_turn_drops_nothing():
    """``dropped`` is for layers that failed or were cut for budget. A normal
    turn reports an empty list, not a truncation nobody asked about."""
    assembled = await _assemble(_instance(persona="BASE PERSONA", updated_at=_STAMP))
    assert assembled.dropped == []


# ---------------------------------------------------------------------------
# Forwarding — the digest reaches a backend that asked for it, and only that one
# ---------------------------------------------------------------------------


class _DigestBackend:
    """A backend that has been ported: it declares the parameter.

    A real class on purpose. ``_accepts_prompt_digest`` reads the signature of
    ``run``, and a ``MagicMock`` has no signature to read — it answers False and
    reads as UNPORTED, silently. Worth knowing before PA-6 ports four more
    backends: a mock-based test of the forwarding will pass while proving the
    opposite of what it says.
    """

    def __init__(self) -> None:
        self.last_kwargs: dict = {}

    async def run(self, message: str, *, system_prompt_digest: str = "", **kwargs):
        self.last_kwargs = {"system_prompt_digest": system_prompt_digest, **kwargs}
        return
        yield  # pragma: no cover — makes this an async generator


class _NarrowBackend:
    """An unported backend. ``**kwargs`` must NOT read as opting in — a backend
    that silently swallowed the digest would look ported while keying on nothing."""

    def __init__(self) -> None:
        self.last_kwargs: dict = {}

    async def run(self, message: str, **kwargs):
        self.last_kwargs = kwargs
        return
        yield  # pragma: no cover — makes this an async generator


async def _run_against(monkeypatch, backend):
    instance = _instance(persona="BASE PERSONA", updated_at=_STAMP)
    instance.backend = backend
    pool = AgentPool()

    async def _fake_get(agent_id):
        return instance

    monkeypatch.setattr(pool, "get", _fake_get)
    async for _ in pool.run("agent-1", "hi", "cloud:session:bbb:agent-1"):
        pass
    return instance


async def test_the_digest_is_forwarded_to_a_backend_that_declares_it(monkeypatch):
    """What this pins is the FORWARDING — that the kwarg arrives at all, and
    carries the digest rather than an empty default.

    The value comparison recomputes ``expected`` through the same seam, so it
    cannot catch a wrong digest; only a missing or empty one. That is the half
    worth having here — whether the digest itself is right is what the assembler
    tests above are for.
    """
    backend = _DigestBackend()
    instance = await _run_against(monkeypatch, backend)

    expected = await _assemble(instance, message="hi")
    assert backend.last_kwargs["system_prompt_digest"] == expected.stable_digest
    assert backend.last_kwargs["system_prompt_digest"], "an empty digest is not forwarding"


async def test_the_digest_is_withheld_from_a_backend_that_does_not(monkeypatch):
    backend = _NarrowBackend()
    await _run_against(monkeypatch, backend)
    assert "system_prompt_digest" not in backend.last_kwargs, (
        "an unported backend would raise TypeError on an unexpected keyword"
    )


# ---------------------------------------------------------------------------
# The assembler itself
# ---------------------------------------------------------------------------


class _StubLayer:
    """A layer whose output is handed to it, so a test can state it outright."""

    def __init__(self, name: str, text: str, cache_key: str | None, priority: int = 0) -> None:
        self.name = name
        self.priority = priority
        self._text = text
        self._cache_key = cache_key

    async def render(self, ctx: PromptContext) -> LayerOutput:  # noqa: ARG002
        return LayerOutput(text=self._text, cache_key=self._cache_key)


def _ctx() -> PromptContext:
    return PromptContext(
        instance=None,
        agent_id="agent-1",
        message="",
        instructions="",
        knowledge_context="",
        system_message_override=None,
    )


async def test_a_volatile_layer_is_excluded_from_the_digest():
    """``cache_key is None`` is the whole point of ``LayerOutput``: it is how a
    layer author declares "this content is per-turn", and it is unskippable
    because the field has no default."""
    keyed = _StubLayer("identity", "WHO I AM", "identity:v1")

    first = await assemble([keyed, _StubLayer("recall", "MEMORY A", None)], _ctx())
    second = await assemble([keyed, _StubLayer("recall", "MEMORY B", None)], _ctx())

    assert first.text != second.text
    assert first.stable_digest == second.stable_digest


async def test_a_changed_key_changes_the_digest():
    a = await assemble([_StubLayer("identity", "SAME TEXT", "identity:v1")], _ctx())
    b = await assemble([_StubLayer("identity", "SAME TEXT", "identity:v2")], _ctx())
    assert a.stable_digest != b.stable_digest


async def test_the_digest_is_not_a_hash_of_the_text():
    """Text and key are independent by design — the key is the point of
    indirection that lets a layer's rendering vary without churning the cache."""
    a = await assemble([_StubLayer("identity", "TEXT A", "identity:v1")], _ctx())
    b = await assemble([_StubLayer("identity", "TEXT B", "identity:v1")], _ctx())
    assert a.text != b.text
    assert a.stable_digest == b.stable_digest


async def test_layers_concatenate_in_the_order_given():
    assembled = await assemble(
        [
            _StubLayer("first", "ALPHA", "k1"),
            _StubLayer("second", "BETA", "k2"),
        ],
        _ctx(),
    )
    assert assembled.text == "ALPHA\n\nBETA"


async def test_an_empty_layer_leaves_no_blank_gap():
    assembled = await assemble(
        [
            _StubLayer("first", "ALPHA", "k1"),
            _StubLayer("empty", "", None),
            _StubLayer("second", "BETA", "k2"),
        ],
        _ctx(),
    )
    assert assembled.text == "ALPHA\n\nBETA"


async def test_the_layer_name_is_part_of_the_digest():
    """Pins the claim ``_digest`` makes: two layers cannot swap keys unnoticed.

    Same keys, same order, different layers holding them — that is a different
    prompt, so it must be a different digest.
    """
    a = await assemble(
        [_StubLayer("identity", "X", "k1"), _StubLayer("surface", "Y", "k2")], _ctx()
    )
    b = await assemble(
        [_StubLayer("surface", "X", "k1"), _StubLayer("identity", "Y", "k2")], _ctx()
    )
    assert a.stable_digest != b.stable_digest


async def test_the_key_fields_cannot_run_together():
    """Pins the other claim: ``("ab", "c")`` must not hash like ``("a", "bc")``.

    Without the field/record separators the digest is a plain concatenation and
    these two collide — two different sets of layers, one identity.
    """
    ab_c = await assemble([_StubLayer("ab", "X", "c")], _ctx())
    a_bc = await assemble([_StubLayer("a", "X", "bc")], _ctx())
    assert ab_c.stable_digest != a_bc.stable_digest


async def test_layer_order_is_part_of_the_digest():
    """Swapping two layers produces a different prompt, so it must produce a
    different key — a digest over an unordered set would call them identical."""
    one = _StubLayer("one", "ALPHA", "k1")
    two = _StubLayer("two", "BETA", "k2")
    assert (await assemble([one, two], _ctx())).stable_digest != (
        await assemble([two, one], _ctx())
    ).stable_digest


# ---------------------------------------------------------------------------
# A raising layer degrades; it does not fail the turn
# ---------------------------------------------------------------------------


class _RaisingLayer:
    """PA-2's surface layer fans out to two dozen handlers doing I/O. This is
    what one of them looks like on a bad day."""

    name = "surface"
    priority = 50

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or RuntimeError("handler blew up")

    async def render(self, ctx: PromptContext) -> LayerOutput:  # noqa: ARG002
        raise self._exc


async def test_a_raising_layer_does_not_fail_the_turn():
    """``AgentPool.run`` calls the seam outside any try and before it marks the
    instance busy, so a propagating layer takes the whole turn with it."""
    assembled = await assemble(
        [
            _StubLayer("identity", "WHO I AM", "identity:v1"),
            _RaisingLayer(),
            _StubLayer("tail", "THE REST", None),
        ],
        _ctx(),
    )
    assert assembled.text == "WHO I AM\n\nTHE REST"


async def test_a_raising_layer_says_so_in_dropped():
    assembled = await assemble([_RaisingLayer()], _ctx())
    assert [d.name for d in assembled.dropped] == ["surface"]
    assert "RuntimeError" in assembled.dropped[0].reason


async def test_a_failed_layer_is_not_invisible_to_the_digest():
    """The rule, at the case that motivates it: a layer dropped from ``text``
    must never be dropped from the digest.

    Compared against the layer being ABSENT, not against an empty prompt — a
    failure that contributed no key at all still differs from a working layer
    (one key versus none), so that comparison passes either way and proves
    nothing. What must not collide is "the surface layer failed" with "there was
    no surface layer": same text, and a backend keying on the digest would serve
    one turn's prompt to the other.
    """
    identity = _StubLayer("identity", "WHO I AM", "identity:v1")

    failed = await assemble([identity, _RaisingLayer()], _ctx())
    absent = await assemble([identity], _ctx())

    assert failed.text == absent.text, "the fixture must isolate the digest"
    assert failed.stable_digest != absent.stable_digest


async def test_a_failed_layer_keys_apart_from_a_working_one():
    """The other half: a failure is its own identity, not the rendered one."""
    identity = _StubLayer("identity", "WHO I AM", "identity:v1")

    working = await assemble(
        [identity, _StubLayer("surface", "SURFACE RULES", "surface:v1")], _ctx()
    )
    failed = await assemble([identity, _RaisingLayer()], _ctx())

    assert working.stable_digest != failed.stable_digest


async def test_two_failures_of_one_layer_agree():
    """Both produce a prompt with nothing from that layer in it, so both should
    hash alike — the failure key is deliberately not per-exception."""
    identity = _StubLayer("identity", "WHO I AM", "identity:v1")

    first = await assemble([identity, _RaisingLayer(RuntimeError("timeout"))], _ctx())
    second = await assemble([identity, _RaisingLayer(ValueError("bad json"))], _ctx())

    assert first.stable_digest == second.stable_digest


async def test_a_cancelled_run_is_not_swallowed():
    """Cancellation is the caller tearing the turn down, not a layer failing.
    Degrading it would turn a cancelled turn into a silently truncated prompt."""
    with pytest.raises(asyncio.CancelledError):
        await assemble([_RaisingLayer(asyncio.CancelledError())], _ctx())


async def test_an_empty_cache_key_is_refused():
    """``""`` reads as "stable forever" and is also what someone types when they
    mean "nothing" — the one answer that fails silently in the unsafe direction,
    on a field whose entire job is to force the question."""
    with pytest.raises(ValueError, match="non-empty string or None"):
        LayerOutput(text="X", cache_key="")

    LayerOutput(text="X", cache_key=None)  # the way to say volatile


# ---------------------------------------------------------------------------
# The registry (workspace charter, Pillar 3)
# ---------------------------------------------------------------------------


async def test_the_registry_registers_gets_and_lists():
    registry = PromptLayerRegistry()
    layer = _StubLayer("identity", "WHO I AM", "identity:v1")
    registry.register("identity", layer)
    assert registry.get("identity") is layer
    assert registry.list() == ["identity"]


async def test_the_built_in_layers_are_registered():
    """The pool resolves its layers by name, so an unregistered one is a
    KeyError on the first turn rather than a missing block in the prompt."""
    assert set(prompt_layer_registry.list()) >= {"identity", "legacy_tail"}


async def test_a_dropped_layer_names_itself_and_why():
    """The budget pass fills ``AssembledPrompt.dropped``; the shape is fixed now
    so the consumers that report a truncated prompt can be written against it."""
    dropped = DroppedLayer(name="atlas", reason="budget")
    assert (dropped.name, dropped.reason) == ("atlas", "budget")
