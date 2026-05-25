# ee/pocketpaw_ee/foresight/scenarios/runner.py
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

    v1.0 adds tick_cadence, tier_mix, activation policy, action_space,
    instinct_policy_overlay, aggregator, projection, calibration,
    cost_estimate, ui_rail, triggers, permissions — i.e. the full
    RFC §18 example YAML.
    """

    name: str
    sub_type: str = "decision_forecast"
    n_ticks: int = 1
    personas: list[PersonaSpec] = field(default_factory=list)

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

        v0.1 accepts the flat shape declared by
        ``scenarios/decision_forecast.yaml``; v1.0 will accept the
        full RFC §18 grammar.
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
        return cls(
            name=data["name"],
            sub_type=data.get("sub_type", "decision_forecast"),
            n_ticks=int(data.get("n_ticks", 1)),
            personas=personas,
        )


@dataclass
class RunResult:
    """What a scenario run emits.

    v0.1 surfaces just enough to prove the loop closed end-to-end:
      - ``scenario_name``: copy of the scenario's name
      - ``tick_snapshots``: WorldSnapshot per tick (in order)
      - ``final_state``: the world's toy state dict at run end
      - ``actions_logged``: total successful actions across all ticks

    v1.0 adds projected decisions, aggregator metrics, calibration
    buffer writes, cost meter readouts.
    """

    scenario_name: str
    tick_snapshots: list[WorldSnapshot]
    final_state: dict[str, Any]
    actions_logged: int

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
) -> RunResult:
    """Run one scenario end-to-end.

    ``backend`` defaults to ``DeterministicFakeBackend()`` so calling
    ``run_scenario(config)`` works in tests + smoke runs without an
    API key. Production callers hand in a ``ClaudeCodeBackend()`` (or,
    at v1.0, a tier-pool round-robin of three).
    """
    if backend is None:
        backend = DeterministicFakeBackend()

    world = ForesightWorld()
    for spec in config.personas:
        persona = SoulSeededPersona(
            name=spec.name,
            role=spec.role,
            ocean_drift=spec.ocean,
            backend=backend,
            agent_id=uuid4(),
        )
        world.add_agent(persona)

    snapshots: list[WorldSnapshot] = []
    for _ in range(config.n_ticks):
        snapshot = await world.tick()
        snapshots.append(snapshot)

    return RunResult(
        scenario_name=config.name,
        tick_snapshots=snapshots,
        final_state=world.state,
        actions_logged=snapshots[-1].actions_applied if snapshots else 0,
    )
