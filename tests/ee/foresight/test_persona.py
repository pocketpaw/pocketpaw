# tests/ee/foresight/test_persona.py
# Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — PR 2 adds
# the PawAgent-wrapping contract: when a persona is constructed with
# ``paw_agent=<live PawAgent>``, the prompt composer pulls identity
# context from the soul's bootstrap provider; the soul bridge is bound
# into MemoryTierStub for PR 3's overlay-write path; the convenience
# factory ``SoulSeededPersona.from_paw_agent`` derives display name
# from the soul; ``has_fidelity`` exposes whether the captain-locked
# fidelity floor is met. Backward-compat preserved — the v0.1
# backend-only path (no paw_agent) still works identically.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Pin the v0.1 SoulSeededPersona contract:
#   - OceanDrift rendering produces deterministic prompt blocks.
#   - MemoryTierStub remembers + recalls in LIFO order.
#   - SoulSeededPersona requires backend.complete.
#   - decide() composes a prompt and parses the response into an action.
#   - decide() captures backend exceptions into noop actions.
#   - The response parser tolerates extra whitespace, missing fields,
#     and put=none vs put=<key>:<value>.

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.foresight.persona import (
    MemoryTierStub,
    OceanDrift,
    SoulSeededPersona,
)

# --- OceanDrift -----------------------------------------------------


def test_ocean_drift_baseline_renders_as_baseline_string():
    drift = OceanDrift()
    assert drift.as_prompt_block() == "baseline temperament"


def test_ocean_drift_skips_traits_within_noise_band():
    drift = OceanDrift(conscientiousness=0.1, openness=0.2)
    assert drift.as_prompt_block() == "baseline temperament"


def test_ocean_drift_uses_magnitude_qualifier():
    drift = OceanDrift(conscientiousness=1.5)  # >= 1.0 → noticeably
    rendered = drift.as_prompt_block()
    assert "noticeably more conscientious" in rendered

    drift2 = OceanDrift(conscientiousness=0.5)  # < 1.0 → slightly
    assert "slightly more conscientious" in drift2.as_prompt_block()


def test_ocean_drift_handles_negative_values():
    drift = OceanDrift(agreeableness=-1.2)
    rendered = drift.as_prompt_block()
    assert "noticeably less agreeable" in rendered


def test_ocean_drift_combines_multiple_traits():
    drift = OceanDrift(conscientiousness=1.2, neuroticism=-0.6)
    rendered = drift.as_prompt_block()
    assert "conscientious" in rendered
    assert "less neurotic" in rendered
    assert "; " in rendered  # joins with semicolons


# --- MemoryTierStub -------------------------------------------------


def test_memory_tier_stub_default_tiers_present():
    mem = MemoryTierStub()
    assert set(mem.tiers) == {"core", "episodic", "semantic", "procedural", "graph"}


def test_memory_tier_stub_remember_recall_roundtrip():
    mem = MemoryTierStub()
    mem.remember({"tick": 1, "action": {"action": "noop"}})
    mem.remember({"tick": 2, "action": {"action": "set"}})
    mem.remember({"tick": 3, "action": {"action": "approve"}})

    # LIFO order — most recent first
    recent = mem.recall(limit=2)
    assert len(recent) == 2
    assert recent[0]["tick"] == 2
    assert recent[1]["tick"] == 3


def test_memory_tier_stub_recall_respects_limit():
    mem = MemoryTierStub()
    for i in range(10):
        mem.remember({"tick": i, "action": {}})
    assert len(mem.recall(limit=3)) == 3
    assert len(mem.recall(limit=20)) == 10


# --- SoulSeededPersona ----------------------------------------------


class _StubBackend:
    def __init__(self, response: str = "action=ok; rationale=fine; put=key:value"):
        self._response = response
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response


class _RaisingBackend:
    async def complete(self, prompt: str) -> str:  # noqa: ARG002
        raise RuntimeError("backend down")


def test_persona_requires_backend_with_complete():
    with pytest.raises(TypeError, match="async def complete"):
        SoulSeededPersona(name="x", backend=object())


async def test_decide_calls_backend_and_parses_action():
    backend = _StubBackend("action=approve; rationale=looks good; put=status:approved")
    persona = SoulSeededPersona(name="p", role="approver", backend=backend)

    result = await persona.decide({"tick": 0, "state": {}, "active_count": 1})

    assert backend.prompts, "backend.complete must be called"
    assert result["action"] == "approve"
    assert result["rationale"] == "looks good"
    assert result["put"] == {"status": "approved"}


async def test_decide_records_outcome_in_memory():
    backend = _StubBackend()
    persona = SoulSeededPersona(name="p", backend=backend)
    await persona.decide({"tick": 5, "state": {}, "active_count": 1})

    recent = persona.memory.recall(limit=1)
    assert len(recent) == 1
    assert recent[0]["tick"] == 5


async def test_decide_captures_backend_exception_as_noop():
    persona = SoulSeededPersona(name="p", backend=_RaisingBackend())
    result = await persona.decide({"tick": 0, "state": {}, "active_count": 1})
    assert result["action"] == "noop"
    assert "backend error" in result["rationale"]
    assert "RuntimeError" in result["rationale"]


async def test_decide_handles_put_none():
    backend = _StubBackend("action=observe; rationale=just looking; put=none")
    persona = SoulSeededPersona(name="p", backend=backend)
    result = await persona.decide({"tick": 0, "state": {}, "active_count": 1})
    assert result["put"] is None


async def test_decide_tolerates_missing_fields():
    backend = _StubBackend("action=ok")  # no rationale, no put
    persona = SoulSeededPersona(name="p", backend=backend)
    result = await persona.decide({"tick": 0, "state": {}, "active_count": 1})
    assert result["action"] == "ok"
    assert result["rationale"] == ""
    assert result["put"] is None


async def test_decide_tolerates_chatty_multiline_response():
    backend = _StubBackend(
        "Sure, here is my answer.\naction=set; rationale=because; put=k:v\nLet me know!"
    )
    persona = SoulSeededPersona(name="p", backend=backend)
    result = await persona.decide({"tick": 0, "state": {}, "active_count": 1})
    assert result["action"] == "set"
    assert result["put"] == {"k": "v"}


async def test_decide_defaults_to_noop_on_empty_response():
    backend = _StubBackend("")
    persona = SoulSeededPersona(name="p", backend=backend)
    result = await persona.decide({"tick": 0, "state": {}, "active_count": 1})
    assert result["action"] == "noop"


# --- PR 2: PawAgent-wrapping (RFC §7.2 fidelity floor) ------------------
#
# These tests cover the persona's new ``paw_agent`` parameter. They use
# stub PawAgent / SoulBridge / SoulBootstrapProvider objects so the
# tests don't depend on a real Soul file or a soul-protocol install
# beyond what pocketpaw already requires (which is true at install time —
# soul-protocol is a base dep, see pyproject.toml).


class _StubBootstrap:
    def __init__(self, context: str = "I am Alice, a real persona seeded from a soul."):
        self._context = context
        self.calls = 0

    def get_context(self) -> str:
        self.calls += 1
        return self._context


class _StubSoulBridge:
    def __init__(self) -> None:
        self.observed: list[Any] = []
        self.recall_returns: list[Any] = []

    async def observe(self, *args, **kwargs) -> None:
        self.observed.append((args, kwargs))

    async def recall(self, *args, **kwargs):  # noqa: ARG002
        return list(self.recall_returns)


class _StubSoul:
    def __init__(self, name: str = "Alice"):
        self.name = name


class _StubPawAgent:
    """Quacks like ``pocketpaw.paw.agent.PawAgent`` — a dataclass with
    soul / bridge / bootstrap_provider / registry / config slots. We
    only stub the slots the persona reaches for at v0.2.
    """

    def __init__(
        self,
        *,
        soul: _StubSoul | None = None,
        bridge: _StubSoulBridge | None = None,
        bootstrap_provider: _StubBootstrap | None = None,
    ):
        self.soul = soul or _StubSoul()
        self.bridge = bridge or _StubSoulBridge()
        self.bootstrap_provider = bootstrap_provider or _StubBootstrap()
        self.registry = None  # PR 3 will exercise this
        self.config = None


def test_persona_without_paw_agent_has_no_fidelity():
    """v0.1 backend-only path — fidelity floor is NOT met."""
    persona = SoulSeededPersona(name="p", backend=_StubBackend())
    assert persona.paw_agent is None
    assert persona.has_fidelity is False


def test_persona_with_paw_agent_has_fidelity():
    """v0.2 path — fidelity floor IS met when a PawAgent is attached."""
    paw_agent = _StubPawAgent()
    persona = SoulSeededPersona(name="alice", backend=_StubBackend(), paw_agent=paw_agent)
    assert persona.paw_agent is paw_agent
    assert persona.has_fidelity is True


def test_persona_binds_soul_bridge_into_memory_when_paw_agent_provided():
    """The persona should expose the PawAgent's soul bridge through the
    memory-tier stub so PR 3 can route soul overlay writes there."""
    bridge = _StubSoulBridge()
    paw_agent = _StubPawAgent(bridge=bridge)
    persona = SoulSeededPersona(name="p", backend=_StubBackend(), paw_agent=paw_agent)
    assert persona.memory.soul_bridge is bridge


def test_persona_leaves_memory_soul_bridge_none_without_paw_agent():
    """No PawAgent = no soul bridge wired through (PR 3 stays inert)."""
    persona = SoulSeededPersona(name="p", backend=_StubBackend())
    assert persona.memory.soul_bridge is None


async def test_decide_with_paw_agent_pulls_bootstrap_into_prompt():
    """The identity block in the prompt should include the soul's
    bootstrap context when a PawAgent is attached."""
    backend = _StubBackend()
    bootstrap = _StubBootstrap(context="I am Alice, calm and curious.")
    paw_agent = _StubPawAgent(bootstrap_provider=bootstrap)
    persona = SoulSeededPersona(name="alice", backend=backend, paw_agent=paw_agent, role="approver")

    await persona.decide({"tick": 0, "state": {}, "active_count": 1})

    assert bootstrap.calls == 1
    sent_prompt = backend.prompts[-1]
    assert "I am Alice, calm and curious." in sent_prompt
    assert "Scenario role: approver" in sent_prompt


async def test_decide_falls_back_gracefully_when_bootstrap_raises():
    """A broken soul bootstrap must NOT take down the persona's tick."""

    class _ExplodingBootstrap:
        def get_context(self) -> str:
            raise RuntimeError("soul corrupted")

    paw_agent = _StubPawAgent(bootstrap_provider=_ExplodingBootstrap())
    persona = SoulSeededPersona(name="alice", backend=_StubBackend(), paw_agent=paw_agent)

    # Should not raise — the persona swallows the bootstrap error and
    # still drives a tick to completion.
    result = await persona.decide({"tick": 0, "state": {}, "active_count": 1})
    assert "action" in result


async def test_decide_without_paw_agent_keeps_v01_prompt_shape():
    """v0.1 backward-compat — without a PawAgent, the identity block
    stays the local 'You are X, role=Y' shape the smoke test pins."""
    backend = _StubBackend()
    persona = SoulSeededPersona(name="alice", backend=backend, role="approver")

    await persona.decide({"tick": 0, "state": {}, "active_count": 1})

    sent_prompt = backend.prompts[-1]
    assert "You are alice, role=approver." in sent_prompt


def test_from_paw_agent_factory_derives_name_from_soul():
    """The factory should default the persona's name to the soul's name
    when no explicit name is supplied."""
    paw_agent = _StubPawAgent(soul=_StubSoul(name="bob"))
    persona = SoulSeededPersona.from_paw_agent(paw_agent, backend=_StubBackend())
    assert persona.name == "bob"
    assert persona.paw_agent is paw_agent
    assert persona.has_fidelity is True


def test_from_paw_agent_factory_honors_explicit_name_override():
    """Passing an explicit name should override the soul-derived default."""
    paw_agent = _StubPawAgent(soul=_StubSoul(name="bob"))
    persona = SoulSeededPersona.from_paw_agent(paw_agent, backend=_StubBackend(), name="custom")
    assert persona.name == "custom"


def test_from_paw_agent_factory_falls_back_when_soul_missing_name():
    """If the soul somehow has no name, fall back to a clear placeholder."""
    paw_agent = _StubPawAgent(soul=type("EmptySoul", (), {})())
    persona = SoulSeededPersona.from_paw_agent(paw_agent, backend=_StubBackend())
    assert persona.name == "unnamed-persona"
