# ee/pocketpaw_ee/foresight/scenarios/runner.py
# Updated: 2026-05-25 (feat/foresight-v03-calibration) — PR 3 adds:
#   - ``tier_mix`` parse step in ``ScenarioConfig.from_yaml`` — the
#     scenario YAML can now declare a per-scenario override of the
#     captain-locked 5/15/80 tier mix (RFC §10).
#   - ``run_scenario`` accepts an optional ``backend_pool`` (a
#     pre-built ``list[BaseModelBackend]`` from ``llm.tier_pool``).
#     When supplied, persona i is assigned ``pool[i % len(pool)]``.
#     When not supplied, the runner uses the v0.1 single-backend
#     fallback (DeterministicFakeBackend or whatever the caller
#     hands in via ``backend=``).
#   - ``RunResult.tier_distribution`` field — captures the per-tier
#     persona count so the per-run report can render the cost
#     decomposition (RFC §10 audit table).
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Scenario runner — the v0.1 end-to-end loop. Takes a ScenarioConfig,
# instantiates a ForesightWorld + N SoulSeededPersonas with a chosen
# backend, drives the configured number of ticks, returns a RunResult
# with per-tick aggregates + the final world state.
#
# This is the "minimum end-to-end loop" the captain asked for: 5
# personas, 1 tick, decisions logged — runs in milliseconds with the
# DeterministicFakeBackend; runs against real Claude Code SDK when the
# caller hands it a ClaudeCodeBackend.

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from pocketpaw_ee.foresight.llm.adapter import DeterministicFakeBackend
from pocketpaw_ee.foresight.llm.tier_pool import TierMix, tier_distribution
from pocketpaw_ee.foresight.persona import OceanDrift, SoulSeededPersona
from pocketpaw_ee.foresight.world import ForesightWorld, WorldSnapshot


@dataclass
class PersonaSpec:
    """One persona's declarative configuration.

    v0.1 keeps this flat — name, role, OCEAN drift. v1.0 will add the
    Soul file path, the per-persona tier override, the action-space
    restriction, and the activation cadence (RFC §4 + §7.2 + §7.3).
    """

    name: str
    role: str = "participant"
    ocean: OceanDrift = field(default_factory=OceanDrift)


@dataclass
class ScenarioConfig:
    """One scenario's declarative configuration.

    Minimum fields v0.1 needs:
      - ``name``: scenario identifier (also surfaced in RunResult)
      - ``sub_type``: which of the 7 RFC §4 sub-types (v0.1 supports
        ``decision_forecast`` only; others raise NotImplementedError)
      - ``n_ticks``: ticks to run (default 1, matching the minimum loop)
      - ``personas``: list of PersonaSpec entries
      - ``tier_mix``: PR 3 — optional override of the captain-locked
        5/15/80 tier mix (RFC §10). ``None`` means "use the locked
        default". Loaders coerce the YAML ``tier_mix:`` block to a
        ``TierMix`` instance.

    v1.0 adds tick_cadence, activation policy, action_space,
    instinct_policy_overlay, aggregator, projection, calibration,
    cost_estimate, ui_rail, triggers, permissions — i.e. the full
    RFC §18 example YAML.
    """

    name: str
    sub_type: str = "decision_forecast"
    n_ticks: int = 1
    personas: list[PersonaSpec] = field(default_factory=list)
    tier_mix: TierMix | None = None

    SUPPORTED_SUB_TYPES: tuple[str, ...] = ("decision_forecast",)

    def __post_init__(self) -> None:
        if self.sub_type not in self.SUPPORTED_SUB_TYPES:
            raise NotImplementedError(
                f"v0.1 supports only {self.SUPPORTED_SUB_TYPES}; "
                f"got {self.sub_type!r}. Future PRs add market_sim, "
                "org_change_rehearsal, ops_stress_test, strategic_what_if, "
                "training_rehearsal, discovery_generative (RFC 08 §4)."
            )
        if self.n_ticks < 1:
            raise ValueError(f"n_ticks must be >= 1, got {self.n_ticks}")
        if not self.personas:
            raise ValueError("scenario must declare at least one persona")

    # --- YAML I/O ----------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> ScenarioConfig:
        """Load a scenario from a YAML file.

        v0.2 accepts the flat shape declared by
        ``scenarios/decision_forecast.yaml`` plus the optional
        ``tier_mix:`` block PR 3 introduces. v1.0 will accept the
        full RFC §18 grammar (activation, action_space, etc.).
        """
        with open(path) as fp:
            data = yaml.safe_load(fp) or {}
        personas = [
            PersonaSpec(
                name=p["name"],
                role=p.get("role", "participant"),
                ocean=OceanDrift(**p.get("ocean", {})),
            )
            for p in data.get("personas", [])
        ]
        tier_mix_block = data.get("tier_mix")
        tier_mix: TierMix | None = None
        if tier_mix_block:
            # Coerce the YAML dict to a TierMix; raises if the triple
            # doesn't sum to 1.0.
            tier_mix = TierMix(
                premium=float(tier_mix_block.get("premium", 0.05)),
                mid=float(tier_mix_block.get("mid", 0.15)),
                tail=float(tier_mix_block.get("tail", 0.80)),
            )
        return cls(
            name=data["name"],
            sub_type=data.get("sub_type", "decision_forecast"),
            n_ticks=int(data.get("n_ticks", 1)),
            personas=personas,
            tier_mix=tier_mix,
        )


@dataclass
class RunResult:
    """What a scenario run emits.

    v0.1 surfaces just enough to prove the loop closed end-to-end:
      - ``scenario_name``: copy of the scenario's name
      - ``tick_snapshots``: WorldSnapshot per tick (in order)
      - ``final_state``: the world's toy state dict at run end
      - ``actions_logged``: total successful actions across all ticks
      - ``tier_distribution``: PR 3 — per-tier persona count when a
        tier pool was used (empty dict for the v0.1 single-backend
        path). Drives the per-run cost report's RFC §10 table.

    v1.0 adds projected decisions, aggregator metrics, calibration
    buffer writes, cost meter readouts.
    """

    scenario_name: str
    tick_snapshots: list[WorldSnapshot]
    final_state: dict[str, Any]
    actions_logged: int
    tier_distribution: dict[str, int] = field(default_factory=dict)

    @property
    def n_ticks(self) -> int:
        return len(self.tick_snapshots)

    def as_wire_dict(self) -> dict[str, Any]:
        """Cheap JSON-serializable view for the API + tests."""
        return {
            "scenario_name": self.scenario_name,
            "n_ticks": self.n_ticks,
            "actions_logged": self.actions_logged,
            "final_state": dict(self.final_state),
            "tier_distribution": dict(self.tier_distribution),
            "tick_snapshots": [
                {
                    "tick": s.tick,
                    "population": s.population,
                    "actions_applied": s.actions_applied,
                    "last_tick_actions": list(s.last_tick_actions),
                }
                for s in self.tick_snapshots
            ],
        }


# --- the runner ------------------------------------------------------


async def run_scenario(
    config: ScenarioConfig,
    *,
    backend: Any | None = None,
    backend_pool: list[Any] | None = None,
) -> RunResult:
    """Run one scenario end-to-end.

    Args:
        config: the scenario configuration (sub-type, n_ticks,
            persona specs, optional tier_mix override).
        backend: single backend to share across all personas. The
            v0.1 path. Defaults to ``DeterministicFakeBackend()`` so
            tests + smoke runs work without an API key.
        backend_pool: PR 3 — pre-built ``list[BaseModelBackend]``
            (e.g. from ``llm.tier_pool.build_tier_pool``). When
            supplied, persona i is assigned ``pool[i % len(pool)]``
            and the ``backend`` arg is ignored. The run's
            ``tier_distribution`` is populated from the pool so the
            per-run report can render the cost decomposition.

    Production callers hand in a ``backend_pool`` built from
    ``TierMix.locked_default()`` (or the scenario's
    ``tier_mix`` override). The pool round-robin assignment matches
    the RFC §7.3 ``List[BaseModelBackend]`` primitive OASIS's
    SocialAgent uses natively.
    """
    if backend_pool is None and backend is None:
        backend = DeterministicFakeBackend()

    world = ForesightWorld()
    tier_dist: dict[str, int] = {}

    for idx, spec in enumerate(config.personas):
        if backend_pool:
            persona_backend = backend_pool[idx % len(backend_pool)]
        else:
            persona_backend = backend
        persona = SoulSeededPersona(
            name=spec.name,
            role=spec.role,
            ocean_drift=spec.ocean,
            backend=persona_backend,
            agent_id=uuid4(),
        )
        world.add_agent(persona)

    if backend_pool:
        tier_dist = tier_distribution(backend_pool[: len(config.personas)])

    snapshots: list[WorldSnapshot] = []
    for _ in range(config.n_ticks):
        snapshot = await world.tick()
        snapshots.append(snapshot)

    return RunResult(
        scenario_name=config.name,
        tick_snapshots=snapshots,
        final_state=world.state,
        actions_logged=snapshots[-1].actions_applied if snapshots else 0,
        tier_distribution=tier_dist,
    )
