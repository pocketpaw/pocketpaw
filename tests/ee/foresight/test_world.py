# tests/ee/foresight/test_world.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Pin the v0.1 ForesightWorld invariants:
#   1. add_agent rejects objects without async `decide`.
#   2. add_agent rejects duplicate agent ids.
#   3. tick() with no active_ids fires every registered persona.
#   4. tick() applies action.put to state (last-writer-wins on collisions).
#   5. tick() captures exceptions from decide() instead of bubbling.
#   6. snapshot() returns post-tick population + actions_applied counters.
#   7. async fan-out: 10 personas in one tick produce 10 action records.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.foresight.world import ForesightWorld


class _StubPersona:
    """Minimal persona used in world tests — no LLM, deterministic output."""

    def __init__(self, response: dict | None = None, raises: Exception | None = None) -> None:
        self._response = response or {"action": "noop", "rationale": "", "put": None}
        self._raises = raises
        self.calls = 0

    async def decide(self, observation: dict) -> dict:  # noqa: ARG002
        self.calls += 1
        if self._raises:
            raise self._raises
        return dict(self._response)


def test_add_agent_rejects_non_persona():
    world = ForesightWorld()
    with pytest.raises(TypeError, match="async def decide"):
        world.add_agent(object())


def test_add_agent_rejects_duplicate_id():
    world = ForesightWorld()
    persona = _StubPersona()
    aid = world.add_agent(persona)
    with pytest.raises(ValueError, match="already registered"):
        world.add_agent(_StubPersona(), agent_id=aid)


async def test_tick_fires_every_active_persona_by_default():
    world = ForesightWorld()
    personas = [_StubPersona() for _ in range(3)]
    for p in personas:
        world.add_agent(p)

    snapshot = await world.tick()

    assert snapshot.tick == 1
    assert snapshot.population == 3
    assert all(p.calls == 1 for p in personas)
    assert len(snapshot.last_tick_actions) == 3
    assert all(a["ok"] for a in snapshot.last_tick_actions)


async def test_tick_applies_put_to_state_last_writer_wins():
    world = ForesightWorld()
    # Two personas write the same key; the last one in the action
    # order wins per the v0.1 last-writer-wins policy.
    world.add_agent(_StubPersona({"action": "set", "put": {"shared_key": "first"}}))
    world.add_agent(_StubPersona({"action": "set", "put": {"shared_key": "second"}}))
    world.add_agent(_StubPersona({"action": "set", "put": {"other_key": 42}}))

    await world.tick()

    assert world.state["shared_key"] == "second"
    assert world.state["other_key"] == 42


async def test_tick_captures_decide_exceptions():
    world = ForesightWorld()
    boom = _StubPersona(raises=RuntimeError("planned failure"))
    quiet = _StubPersona({"action": "ok", "put": {"k": "v"}})
    world.add_agent(boom)
    world.add_agent(quiet)

    snapshot = await world.tick()

    assert snapshot.actions_applied == 1  # only the quiet one
    failed = [a for a in snapshot.last_tick_actions if not a["ok"]]
    assert len(failed) == 1
    assert "RuntimeError" in failed[0]["error"]
    assert "planned failure" in failed[0]["error"]


async def test_tick_rejects_non_dict_decide_return():
    world = ForesightWorld()

    class _BadPersona:
        async def decide(self, observation):  # noqa: ARG002
            return "not a dict"

    world.add_agent(_BadPersona())
    snapshot = await world.tick()
    assert len(snapshot.last_tick_actions) == 1
    assert snapshot.last_tick_actions[0]["ok"] is False
    assert "must return dict" in snapshot.last_tick_actions[0]["error"]


async def test_snapshot_counters_accumulate_across_ticks():
    world = ForesightWorld()
    world.add_agent(_StubPersona({"action": "ok", "put": {"k": "v"}}))

    s1 = await world.tick()
    s2 = await world.tick()
    s3 = await world.tick()

    assert s1.tick == 1
    assert s2.tick == 2
    assert s3.tick == 3
    assert s3.actions_applied == 3


async def test_async_fan_out_runs_concurrently():
    """If the fan-out is actually concurrent, 10 personas that each
    `await asyncio.sleep(0.05)` should finish in ~0.05s, not 0.5s.
    """
    world = ForesightWorld()

    class _SlowPersona:
        async def decide(self, observation):  # noqa: ARG002
            await asyncio.sleep(0.05)
            return {"action": "slow", "put": None}

    for _ in range(10):
        world.add_agent(_SlowPersona())

    import time

    start = time.perf_counter()
    snapshot = await world.tick()
    elapsed = time.perf_counter() - start

    assert len(snapshot.last_tick_actions) == 10
    # Generous bound — 0.25s leaves room for CI noise, fails clearly
    # if the gather is actually sequential (would take 0.5s+).
    assert elapsed < 0.25, f"fan-out not concurrent: {elapsed:.3f}s for 10 personas"
