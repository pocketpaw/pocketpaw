# tests/ee/foresight/test_persona.py
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
