# ee/pocketpaw_ee/foresight/world.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# ForesightWorld — Fabric-backed stub world for the v0.1 simulation
# loop. This is the v0.1 substitute for ``oasis.social_platform.Platform``
# described in RFC 08 §6.2 + §7.1. v0.1 implements a tiny in-memory
# overlay (no Fabric snapshot wiring yet) so the smoke loop can run
# without depending on Fabric, MongoDB, or the OASIS src-copy.
#
# The shape of the public methods (``add_agent``, ``tick``, ``snapshot``,
# ``receive``) intentionally matches what RFC 08 §7.1 specifies for the
# v1.0 ``ForesightWorld`` — that way, the v1.0 wiring is a body-swap
# under a stable surface, not an API rewrite.

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4


@dataclass
class WorldSnapshot:
    """Point-in-time view of the world emitted by ``ForesightWorld.snapshot()``.

    v0.1 carries the minimum the smoke loop needs:
      - ``tick``: integer tick counter
      - ``population``: number of registered personas
      - ``actions_applied``: cumulative count across all ticks so far
      - ``last_tick_actions``: list of per-persona action dicts from the
        most recent tick (the audit trail v0.1 surfaces back to callers)

    v1.0 will swap this for a Fabric COW snapshot + a per-tick diff
    surface (see RFC 08 §7.1 ``FabricSnapshot.fork()``).
    """

    tick: int
    population: int
    actions_applied: int
    last_tick_actions: list[dict[str, Any]] = field(default_factory=list)


class ForesightWorld:
    """v0.1 in-memory world stub.

    Three responsibilities:
      1. Hold a registry of persona ids → persona handles (``add_agent``).
      2. Drive a single tick: ask each active persona to ``decide``,
         buffer the action dicts, apply them in submission order
         (no conflict resolver yet — v1.0 work per RFC 08 §7.1).
      3. Emit a ``WorldSnapshot`` describing what just happened.

    Conflict resolution, COW overlays, Fabric snapshot loading, and
    Instinct gating are all deferred to v1.0. v0.1's job is to prove
    the persona → action → world loop closes end-to-end with real LLM
    cognition in the middle.
    """

    def __init__(self) -> None:
        self._personas: dict[UUID, Any] = {}
        # _state is the toy "world state" v0.1 mutates; v1.0 replaces it
        # with FabricSnapshot. The contract callers see is opaque-dict.
        self._state: dict[str, Any] = {}
        self._tick: int = 0
        self._actions_applied: int = 0
        self._last_tick_actions: list[dict[str, Any]] = []

    # --- registry ------------------------------------------------------

    def add_agent(self, persona: Any, *, agent_id: UUID | None = None) -> UUID:
        """Register a persona in the world.

        ``persona`` must expose ``async def decide(observation: dict) -> dict``.
        Returns the assigned agent id (auto-generated if not supplied).

        Duplicate ids raise ``ValueError`` rather than overwriting silently
        so a scenario YAML typo can't quietly drop a persona on the floor.
        """
        if not hasattr(persona, "decide"):
            raise TypeError(
                "persona must expose `async def decide(observation: dict) -> dict`; "
                f"got {type(persona).__name__}"
            )
        aid = agent_id or uuid4()
        if aid in self._personas:
            raise ValueError(f"persona id {aid} already registered")
        self._personas[aid] = persona
        return aid

    @property
    def population(self) -> int:
        return len(self._personas)

    # --- tick ---------------------------------------------------------

    async def tick(self, *, active_ids: list[UUID] | None = None) -> WorldSnapshot:
        """Run one tick.

        Calls ``decide`` on every active persona concurrently via
        ``asyncio.gather`` (the v0.1 stand-in for OASIS's per-backend
        semaphore pool — v1.0 wraps Sonnet at 128, Haiku at 256,
        vLLM at the pool's native parallelism per RFC 08 §6.4).

        ``active_ids=None`` means "every registered persona fires this
        tick" (deterministic activation, the v0.1 default; probabilistic
        and injector activation policies land in v1.0 per RFC §7.4).

        Returns the post-tick ``WorldSnapshot``.
        """
        if active_ids is None:
            active_ids = list(self._personas.keys())

        observation = self._observation_for_active(active_ids)
        coros = [
            self._personas[aid].decide(observation) for aid in active_ids if aid in self._personas
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        # v0.1 conflict policy: append-only, last-writer-wins on
        # (object_id, property). v1.0 replaces this with the
        # actor_priority + seq_in_tick resolver from RFC §7.1.
        applied: list[dict[str, Any]] = []
        for aid, result in zip(active_ids, results):
            if isinstance(result, Exception):
                applied.append(
                    {
                        "agent_id": str(aid),
                        "ok": False,
                        "error": f"{type(result).__name__}: {result}",
                    }
                )
                continue
            if not isinstance(result, dict):
                applied.append(
                    {
                        "agent_id": str(aid),
                        "ok": False,
                        "error": f"decide() must return dict, got {type(result).__name__}",
                    }
                )
                continue
            self._apply_action(aid, result)
            applied.append({"agent_id": str(aid), "ok": True, **result})

        self._tick += 1
        self._actions_applied += sum(1 for a in applied if a.get("ok"))
        self._last_tick_actions = applied
        return self.snapshot()

    def _observation_for_active(self, active_ids: list[UUID]) -> dict[str, Any]:
        """v0.1 observation: just the current world state + tick number.

        v1.0 swaps this for the per-persona Fabric slice (relationships
        + ambient slice) described in RFC §7.5 step 1.
        """
        return {
            "tick": self._tick,
            "state": dict(self._state),
            "active_count": len(active_ids),
        }

    def _apply_action(self, agent_id: UUID, action: dict[str, Any]) -> None:
        """Apply one action to the in-memory state.

        v0.1 contract: an action may carry a ``put`` map of
        ``{state_key: value}``. Anything else is recorded in the action
        log but does not mutate state. v1.0 replaces this with the
        Fabric overlay write path.
        """
        put = action.get("put")
        if isinstance(put, dict):
            for k, v in put.items():
                self._state[k] = v

    # --- snapshot -----------------------------------------------------

    def snapshot(self) -> WorldSnapshot:
        """Cheap O(1) snapshot of post-tick world state."""
        return WorldSnapshot(
            tick=self._tick,
            population=self.population,
            actions_applied=self._actions_applied,
            last_tick_actions=list(self._last_tick_actions),
        )

    @property
    def state(self) -> dict[str, Any]:
        """Read-only view of the world's toy state for tests + the runner."""
        return dict(self._state)
