# ee/pocketpaw_ee/foresight/world.py
# Updated: 2026-05-25 (feat/foresight-v03-calibration) — PR 3:
#   - Added optional ``oasis.AgentGraph`` integration for the
#     relationship layer (RFC 08 §6.3). When OASIS_AVAILABLE,
#     ``ForesightWorld`` can be constructed with
#     ``use_oasis_graph=True`` (or pass an explicit ``agent_graph=``)
#     to register personas in the OASIS in-memory graph, getting
#     follower-of / colleague-of / approves-for relationships for
#     free. The smoke loop's old behavior (graph-free) is preserved
#     as the default so the PR 1/2 tests keep passing.
#   - The OASIS substrate's ``OasisEnv.step`` is NOT wired into
#     ``tick()`` because OasisEnv requires a Platform (Twitter/Reddit
#     SQLite-backed), and per RFC 08 §6.2 we EXPLICITLY drop
#     Platform in favor of this Fabric-backed world. Instead, we
#     adopt OASIS's per-backend-semaphore pattern (the load-bearing
#     primitive from ``oasis.environment.env._perform_llm_action``)
#     directly in ``tick()`` so the same concurrency-cap semantics
#     hold without dragging Platform in. PR 4+ may revisit
#     ``OasisEnv`` if a Market Sim scenario wants the recsys.
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
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        *,
        use_oasis_graph: bool = False,
        agent_graph: Any | None = None,
        max_concurrent: int = 128,
    ) -> None:
        """Construct the world.

        Args:
            use_oasis_graph: when ``True`` (and ``OASIS_AVAILABLE`` per
                the substrate's tiered import), construct an
                ``oasis.AgentGraph(backend="igraph")`` and register
                every persona in it. Personas can then read
                relationship neighborhoods via the graph; the
                conflict resolver uses persona priorities the graph
                exposes. Default ``False`` preserves the PR 1/2
                graph-free smoke loop.
            agent_graph: pre-constructed ``oasis.AgentGraph`` instance
                (caller-owned). Overrides ``use_oasis_graph`` when
                supplied.
            max_concurrent: per-tick concurrency cap (mirrors OASIS's
                ``OasisEnv.llm_semaphore``; default 128 matches the
                RFC §6.4 Sonnet tier). PR 4+ will swap this for
                per-tier semaphores from ``llm.tier_pool``.
        """
        self._personas: dict[UUID, Any] = {}
        # _state is the toy "world state" v0.1 mutates; v1.0 replaces it
        # with FabricSnapshot. The contract callers see is opaque-dict.
        self._state: dict[str, Any] = {}
        self._tick: int = 0
        self._actions_applied: int = 0
        self._last_tick_actions: list[dict[str, Any]] = []
        self._sem = asyncio.Semaphore(max_concurrent)
        self._agent_graph: Any | None = agent_graph
        # Tracks how persona ids map to OASIS-side integer agent ids
        # (OASIS's AgentGraph keys on ``int``; we key on ``UUID``).
        self._oasis_id_for: dict[UUID, int] = {}
        self._next_oasis_id: int = 0

        if self._agent_graph is None and use_oasis_graph:
            self._agent_graph = self._try_construct_oasis_graph()

    @staticmethod
    def _try_construct_oasis_graph() -> Any | None:
        """Lazy-construct an ``oasis.AgentGraph(backend='igraph')``.

        Returns ``None`` (with a debug log) when OASIS_AVAILABLE is
        False — e.g. missing igraph or one of the lazy-import wars
        flagged in PR 3's substrate __init__. Callers can re-attempt
        later or pass an explicit ``agent_graph=``.
        """
        from pocketpaw_ee.foresight.substrate import oasis  # noqa: PLC0415 — lazy

        if not oasis.OASIS_AVAILABLE or oasis.AgentGraph is None:
            logger.debug(
                "use_oasis_graph=True but OASIS core not loaded "
                "(OASIS_AVAILABLE=%s, error=%s). Falling back to "
                "graph-free registry.",
                oasis.OASIS_AVAILABLE,
                oasis.OASIS_LOAD_ERROR,
            )
            return None
        try:
            return oasis.AgentGraph(backend="igraph")
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Failed to construct oasis.AgentGraph: %s: %s",
                type(exc).__name__,
                exc,
            )
            return None

    @property
    def agent_graph(self) -> Any | None:
        """The OASIS AgentGraph instance, or ``None`` when not used."""
        return self._agent_graph

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

        # PR 3 — register in the OASIS AgentGraph when available.
        # Only personas that are actual ``SocialAgent`` subclasses
        # (i.e. ``PawSocialAgent`` from persona.py) can be added to the
        # AgentGraph since it indexes by ``SocialAgent.social_agent_id``;
        # ``SoulSeededPersona`` (no SocialAgent inheritance) is skipped
        # cleanly. The integer id mapping is tracked in
        # ``_oasis_id_for`` so future edges (follower-of /
        # approves-for) can resolve UUID → int.
        if self._agent_graph is not None:
            oasis_id = self._next_oasis_id
            self._next_oasis_id += 1
            social_agent_id = getattr(persona, "social_agent_id", None)
            if social_agent_id is not None:
                try:
                    self._agent_graph.add_agent(persona)
                    self._oasis_id_for[aid] = int(social_agent_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "OASIS graph add_agent failed for persona %s: %s: %s",
                        aid,
                        type(exc).__name__,
                        exc,
                    )
            else:
                # Plain SoulSeededPersona; we still track an oasis_id
                # for it so cross-persona edges can be wired by the
                # caller, but skip the AgentGraph itself (it would
                # crash without a real SocialAgent).
                self._oasis_id_for[aid] = oasis_id
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

        # PR 3 — wrap each decide() in the semaphore so the OASIS-style
        # per-tier concurrency cap holds. The wrapper preserves
        # ``return_exceptions=True`` semantics by letting the original
        # exception propagate inside the semaphore and catching it on
        # the gather side.
        async def _capped_decide(persona: Any) -> Any:
            async with self._sem:
                return await persona.decide(observation)

        coros = [_capped_decide(self._personas[aid]) for aid in active_ids if aid in self._personas]
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
