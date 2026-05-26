# ee/pocketpaw_ee/foresight/scenarios/runner.py
# Updated: 2026-05-26 (feat/foresight-v10-prediction-record-persist) —
# RFC 08 v1.0 PR 10:
#   - ``run_scenario`` accepts an optional ``on_prediction_record``
#     callback alongside the existing ``on_projected_decision``. The
#     prediction-record callback receives the per-(anchor × tick)
#     projection as a CAPTURE-shaped dict so the cloud's
#     ``foresight_prediction_records`` collection can mirror each
#     emission. Legacy callers (CLI smoke, v0.5 tests) pass nothing
#     and behaviour is unchanged — the in-engine
#     ``RunResult.projected_decisions`` field still carries the same
#     records for direct consumption.
#   - The engine never imports cloud; the callback is injected by
#     closure exactly like ``on_projected_decision`` so the
#     import-linter's "engine → cloud forbidden" contract holds.
# Updated: 2026-05-25 (feat/foresight-v14-decision-graph-stub) — RFC 08
# §14.4 wiring:
#   - ``ScenarioConfig`` grows two optional fields: ``precedent_seed``
#     (scenario-level seed for synthetic precedent ids) and
#     ``precedent_seeds`` (anchor-level override map; anchor-level wins
#     when set). Both default to ``None`` / empty so existing scenarios
#     are unaffected.
#   - ``ScenarioConfig.from_yaml`` parses both blocks — scenario-root
#     ``precedent_seed:`` and the optional ``precedent_seeds:`` map of
#     ``{anchor_id: seed}``. Documented in market_sim.yaml /
#     org_change.yaml / decision_forecast.yaml.
#   - ``run_scenario`` accepts ``decision_graph_ref: DecisionGraphRef``;
#     defaults to ``NoOpDecisionGraphRef`` seeded from the config. Each
#     per-anchor projected record now carries a
#     ``forward_precedent_decision_id`` field — ``None`` when no seed
#     is configured (preserves the v0.1 behavior), a deterministic
#     synthetic id when a seed is configured.
#   - The ``on_projected_decision`` callback signature is **unchanged**
#     at 6 args (anchor, persona, tick, decision, confidence, sub_type)
#     to preserve compatibility with the cloud's existing closure in
#     ``cloud/foresight/service.py`` and the §8 approval-bridge lane.
#     The precedent id surfaces on ``RunResult.projected_decisions``
#     (engine wire shape) so callers consuming the result dataclass
#     directly get the new field immediately; the cloud-side
#     persistence backfill is a follow-up stream that will populate
#     the same field on the Mongo doc.
#   - When RFC 07 actually lands in pocketpaw, the cloud injects a
#     real DecisionGraphRef into ``run_scenario`` (the
#     NoOpDecisionGraphRef is replaced; no wire-shape change).
# Updated: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) — PR 5:
#   - ``SUPPORTED_SUB_TYPES`` lifted to include ``market_sim`` and
#     ``org_change_rehearsal`` alongside ``decision_forecast``. The
#     adapter set lives in ``ee.foresight.subtypes``; dispatch routes
#     to ``get_adapter(sub_type)`` after the tick loop so each sub-type
#     produces its own aggregate shape inside ``RunResult.aggregate``.
#   - ``RunResult.aggregate`` added — the sub-type-specific outcome
#     dict (market segments, rollout adoption rates, decision modal
#     outcome). The v0.1 ``final_state`` field stays for backward
#     compat; the cloud's backtest path reads ``aggregate`` instead.
#   - ``run_scenario`` accepts an optional ``on_projected_decision``
#     callback — the cloud side passes a function that persists one
#     ProjectedDecision record per (anchor × tick) bucket. The runner
#     fans the callback after each tick by walking
#     ``adapter.anchors_for(config)``; per-anchor decision text comes
#     from the modal action verb in the tick's action log. This is the
#     RFC §7.7 per-anchor projection fanout that PR 7 + PR 4 deferred.
#   - ``run_scenario`` accepts an optional ``run_id`` so the cloud can
#     pre-allocate the run document id before the engine ticks; the
#     callback echoes it back to the cloud for cross-referencing.
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

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml  # type: ignore[import-untyped]

from pocketpaw_ee.foresight.decision_graph_ref import (
    DecisionGraphRef,
    NoOpDecisionGraphRef,
)
from pocketpaw_ee.foresight.llm.adapter import DeterministicFakeBackend
from pocketpaw_ee.foresight.llm.tier_pool import TierMix, tier_distribution
from pocketpaw_ee.foresight.persona import OceanDrift, SoulSeededPersona
from pocketpaw_ee.foresight.subtypes import SUPPORTED_SUB_TYPES as _SUPPORTED_SUB_TYPES
from pocketpaw_ee.foresight.subtypes import get_adapter
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
    # v14 — RFC 08 §14.4 forward-precedent seed plumbing. The scenario
    # carries an optional global seed plus an optional per-anchor
    # override map; the runner constructs a NoOpDecisionGraphRef from
    # both when the caller doesn't supply a real DecisionGraphRef. When
    # both are absent the runner still attaches a NoOp, and the lookup
    # returns ``None`` so ``forward_precedent_decision_id`` keeps its
    # v0.1 "always None" wire-shape behavior for un-seeded scenarios.
    precedent_seed: str | None = None
    precedent_seeds: dict[str, str] = field(default_factory=dict)

    # PR 5 lifts the supported set to the three sub-types v0.5 ships
    # (decision_forecast + market_sim + org_change_rehearsal). The
    # remaining four — ops_stress_test, strategic_what_if,
    # training_rehearsal, discovery_generative — land in PR 6+ as
    # additional sibling adapters under ``ee.foresight.subtypes``.
    SUPPORTED_SUB_TYPES: tuple[str, ...] = _SUPPORTED_SUB_TYPES

    def __post_init__(self) -> None:
        if self.sub_type not in self.SUPPORTED_SUB_TYPES:
            raise NotImplementedError(
                f"v0.5 supports only {self.SUPPORTED_SUB_TYPES}; "
                f"got {self.sub_type!r}. Future PRs add ops_stress_test, "
                "strategic_what_if, training_rehearsal, and "
                "discovery_generative (RFC 08 §4)."
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
        # v14 — optional forward-precedent seeds. The scenario-root
        # ``precedent_seed:`` is the global default; the optional
        # ``precedent_seeds:`` map carries anchor-level overrides. Both
        # are normalized to strings here so the NoOp lookup contract
        # (which treats the empty string as "no seed configured")
        # stays uniform.
        raw_seed = data.get("precedent_seed")
        precedent_seed: str | None = str(raw_seed) if raw_seed not in (None, "") else None
        precedent_seeds_block = data.get("precedent_seeds") or {}
        precedent_seeds: dict[str, str] = {
            str(anchor_id): str(seed)
            for anchor_id, seed in precedent_seeds_block.items()
            if seed not in (None, "")
        }
        return cls(
            name=data["name"],
            sub_type=data.get("sub_type", "decision_forecast"),
            n_ticks=int(data.get("n_ticks", 1)),
            personas=personas,
            tier_mix=tier_mix,
            precedent_seed=precedent_seed,
            precedent_seeds=precedent_seeds,
        )


@dataclass
class RunResult:
    """What a scenario run emits.

    v0.5 surfaces enough to prove the loop closed end-to-end AND to
    serve the sub-type aggregate the cloud's backtest gate consumes:
      - ``scenario_name``: copy of the scenario's name
      - ``sub_type``: PR 5 — copy of the scenario's sub_type so the
        wire dict carries the dispatch label.
      - ``tick_snapshots``: WorldSnapshot per tick (in order)
      - ``final_state``: the world's toy state dict at run end
      - ``actions_logged``: total successful actions across all ticks
      - ``tier_distribution``: PR 3 — per-tier persona count when a
        tier pool was used (empty dict for the v0.1 single-backend
        path). Drives the per-run cost report's RFC §10 table.
      - ``aggregate``: PR 5 — sub-type-specific outcome dict produced
        by the adapter in ``ee.foresight.subtypes``. Decision Forecast
        emits ``modal_outcome``; Market Sim emits per-segment
        win_rate/churn_rate; Org Change emits per-event adoption_rate.
        The backtest scoring path reads ``aggregate["modal_outcome"]``
        when present (PR 4 contract) and the future per-anchor
        projection fanout reads the sub-type-specific keys.
      - ``projected_decisions``: PR 5 — list of per-anchor projection
        records emitted during the tick loop. The cloud side persists
        these via the ``on_projected_decision`` callback; the runner
        also surfaces them on the result for callers that drive the
        engine outside the cloud (e.g. CLI smoke).
    """

    scenario_name: str
    tick_snapshots: list[WorldSnapshot]
    final_state: dict[str, Any]
    actions_logged: int
    sub_type: str = "decision_forecast"
    tier_distribution: dict[str, int] = field(default_factory=dict)
    aggregate: dict[str, Any] = field(default_factory=dict)
    projected_decisions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def n_ticks(self) -> int:
        return len(self.tick_snapshots)

    def as_wire_dict(self) -> dict[str, Any]:
        """Cheap JSON-serializable view for the API + tests."""
        # Decision Forecast's adapter emits ``modal_outcome`` inside
        # ``aggregate`` — surface it at the wire top level so the v0.1
        # backtest scoring path (which reads ``modal_outcome`` from the
        # engine result dict) keeps working without a special case in
        # the cloud service. PR 4's ``_score_backtest`` already checks
        # both ``modal_outcome`` and ``projected_modal_outcome``.
        modal = self.aggregate.get("modal_outcome") if isinstance(self.aggregate, dict) else None
        wire: dict[str, Any] = {
            "scenario_name": self.scenario_name,
            "sub_type": self.sub_type,
            "n_ticks": self.n_ticks,
            "actions_logged": self.actions_logged,
            "final_state": dict(self.final_state),
            "tier_distribution": dict(self.tier_distribution),
            "aggregate": dict(self.aggregate),
            "projected_decisions": list(self.projected_decisions),
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
        if modal:
            wire["modal_outcome"] = modal
        return wire


# --- the runner ------------------------------------------------------

# PR 5 — per-tick projected-decision callback signature. The cloud side
# (``ee.cloud.foresight.service.emit_projected_decision``) passes a
# function shaped like this into ``run_scenario`` so the engine can
# emit one record per (anchor × tick) bucket without statically
# importing the cloud layer (engine → cloud direction is forbidden by
# the import-linter contract; the cloud injects the callback so the
# arrow points the other way at run-time only).
#
# Args:
#   anchor_id: the sub-type-specific anchor (``decision:<name>`` for
#     Decision Forecast, ``segment:<role>`` for Market Sim,
#     ``rollout:<event>`` for Org Change).
#   persona_id: str(UUID) of the persona whose modal action drove the
#     projection. An empty string when no persona action landed for
#     this anchor at this tick (the engine still emits a record so the
#     per-anchor timeline stays dense).
#   tick_id: zero-based tick index inside the run.
#   decision_text: short string capturing the modal action verb (e.g.
#     "accept", "churn", "escalate"). Used as the wire-level decision
#     payload until v1.0 fans the full action dict.
#   confidence: aggregate confidence in (0.0, 1.0). v0.5 derives this
#     from the share of personas whose action verb matched the modal
#     bucket — high agreement → high confidence.
#   sub_type: the scenario's sub_type (echoed for downstream consumers
#     that index per-anchor records across runs of different sub-types).
ProjectedDecisionCallback = Callable[[str, str, int, str, float, str], Any]

# PR 10 — per-tick PredictionRecord callback. The cloud side passes a
# function that mirrors each (anchor × tick) projection into the
# ``foresight_prediction_records`` Mongo collection so the v1.0
# aggregate + insights endpoints can read paired records persistently
# (replacing the v0.5 ForesightBacktest + ForesightProjectedDecision
# proxies). The dict payload mirrors the engine's per-tick
# projected-decision record so the cloud-side writer is a thin field
# map; the engine never sees Mongo / Beanie / pydantic.
#
# Args:
#   record: dict with keys ``{anchor_id, persona_id, tick_id,
#     decision_text, confidence, sub_type,
#     forward_precedent_decision_id, scenario_id, run_id, prediction}``.
#     The ``prediction`` key carries the per-tick modal-outcome dict
#     the cloud will store as the PredictionRecord's payload.
PredictionRecordCallback = Callable[[dict[str, Any]], Any]


async def run_scenario(
    config: ScenarioConfig,
    *,
    backend: Any | None = None,
    backend_pool: list[Any] | None = None,
    on_projected_decision: ProjectedDecisionCallback | None = None,
    on_prediction_record: PredictionRecordCallback | None = None,
    run_id: str | None = None,
    decision_graph_ref: DecisionGraphRef | None = None,
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
        on_projected_decision: PR 5 — optional callback invoked once
            per (anchor × tick) bucket after each tick lands. The
            cloud service uses this to persist ProjectedDecision
            records as the run unfolds; CLI callers can pass ``None``
            to skip persistence and read the same records back from
            ``RunResult.projected_decisions``. The callback may be
            sync or async — ``run_scenario`` awaits it when it returns
            a coroutine.
        run_id: PR 5 — optional pre-allocated run id the cloud passes
            so the per-tick records cross-reference the same id the
            ForesightRun document carries. The callback receives the
            run id implicitly via the closure the caller supplies.
        decision_graph_ref: v14 — RFC 08 §14.4 forward-precedent lookup
            contract. When the caller doesn't supply one (CLI smoke,
            v0.5 cloud), the runner constructs a default
            :class:`NoOpDecisionGraphRef` seeded from
            ``config.precedent_seed`` + ``config.precedent_seeds``. The
            ref is consulted once per projected-decision record; the
            return value populates the record's
            ``forward_precedent_decision_id`` field. When RFC 07 lands
            in pocketpaw the cloud will inject a real ``DecisionGraphRef``
            that returns Decision-Graph short-ids instead of synthetic
            ones — no wire-shape change.

    Production callers hand in a ``backend_pool`` built from
    ``TierMix.locked_default()`` (or the scenario's
    ``tier_mix`` override). The pool round-robin assignment matches
    the RFC §7.3 ``List[BaseModelBackend]`` primitive OASIS's
    SocialAgent uses natively.
    """
    import inspect  # noqa: PLC0415 — used only when the callback is sync vs async

    if backend_pool is None and backend is None:
        backend = DeterministicFakeBackend()

    # v14 — default to the NoOp ref seeded from the scenario config. The
    # ref is consulted once per projected-decision record below; when no
    # seed is configured the NoOp returns ``None`` and the wire shape
    # carries ``forward_precedent_decision_id=None`` (the v0.1 behavior).
    if decision_graph_ref is None:
        decision_graph_ref = NoOpDecisionGraphRef(
            seed=config.precedent_seed or "",
            per_anchor_seeds=dict(config.precedent_seeds),
        )

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

    adapter = get_adapter(config.sub_type)
    anchors = adapter.anchors_for(config)

    snapshots: list[WorldSnapshot] = []
    projected_records: list[dict[str, Any]] = []
    for tick_index in range(config.n_ticks):
        snapshot = await world.tick()
        snapshots.append(snapshot)
        # Fan one projected-decision record per anchor for this tick.
        # The runner stays sub-type-agnostic here — anchor selection is
        # the adapter's responsibility; decision-text + confidence
        # derivation is a uniform tally of the tick's modal action verb.
        tick_records = _project_tick(
            anchors=anchors,
            snapshot=snapshot,
            tick_index=tick_index,
            sub_type=config.sub_type,
            scenario_id=config.name,
            decision_graph_ref=decision_graph_ref,
        )
        for record in tick_records:
            projected_records.append(record)
            if on_projected_decision is not None:
                maybe_coro = on_projected_decision(
                    record["anchor_id"],
                    record["persona_id"],
                    record["tick_id"],
                    record["decision_text"],
                    record["confidence"],
                    record["sub_type"],
                )
                if inspect.isawaitable(maybe_coro):
                    await maybe_coro
            # PR 10 — PredictionRecord mirror callback. The cloud side
            # hands in a closure that persists each (anchor × tick)
            # projection into the ``foresight_prediction_records``
            # collection. The dict carries the same per-tick payload
            # the projected-decision callback received plus the
            # scenario / run identifiers so the writer can satisfy
            # the cloud's cloud-rule-#3 tenancy invariant without a
            # second engine round-trip. The ``prediction`` key is the
            # per-tick modal-outcome dict (keyed by
            # ``decision_text`` so the cloud-side modal-distribution
            # rollup tallies on the same vocabulary it used in v0.5).
            if on_prediction_record is not None:
                prediction_payload: dict[str, Any] = {
                    "anchor_id": record["anchor_id"],
                    "persona_id": record["persona_id"],
                    "tick_id": record["tick_id"],
                    "decision_text": record["decision_text"],
                    "confidence": record["confidence"],
                    "sub_type": record["sub_type"],
                    "forward_precedent_decision_id": record.get("forward_precedent_decision_id"),
                    "scenario_id": config.name,
                    "run_id": run_id or "",
                    "prediction": {
                        "modal_outcome": record["decision_text"],
                    },
                }
                maybe_coro_pr = on_prediction_record(prediction_payload)
                if inspect.isawaitable(maybe_coro_pr):
                    await maybe_coro_pr

    aggregate = adapter.aggregate(snapshots, config)

    return RunResult(
        scenario_name=config.name,
        sub_type=config.sub_type,
        tick_snapshots=snapshots,
        final_state=world.state,
        actions_logged=snapshots[-1].actions_applied if snapshots else 0,
        tier_distribution=tier_dist,
        aggregate=aggregate,
        projected_decisions=projected_records,
    )


def _project_tick(
    *,
    anchors: list[str],
    snapshot: WorldSnapshot,
    tick_index: int,
    sub_type: str,
    scenario_id: str,
    decision_graph_ref: DecisionGraphRef,
) -> list[dict[str, Any]]:
    """Build one projected-decision record per anchor for this tick.

    v0.5 contract:
      - One record per anchor (the cloud expects every anchor to be
        addressable on every tick, even when no persona acted there).
      - ``decision_text`` is the modal action verb across the tick's
        successful actions; defaults to ``"noop"`` when nothing landed.
      - ``confidence`` is the share of successful actions that picked
        the modal verb (1.0 when one verb dominates, lower under split
        decisions). When the tick produced zero successful actions
        ``confidence`` is 0.0.
      - ``persona_id`` is the modal persona's agent id when one exists,
        else an empty string. v1.0 will fan one record per (anchor ×
        persona) pair; v0.5 stays one-per-anchor so the cloud's storage
        footprint is linear in anchor count, not anchor × persona.

    v14 additions (RFC 08 §14.4):
      - ``forward_precedent_decision_id`` is resolved per record by
        delegating to the supplied ``DecisionGraphRef``. The NoOp
        default returns ``None`` when no scenario seed is configured
        (preserving v0.1 wire shape) and a stable synthetic id when a
        seed is configured. A real Decision-Graph implementation (RFC
        07) will return live short-ids; the field shape is unchanged.
    """
    # Count verbs across successful actions; the persona id of the
    # last-seen action for each verb so we can attribute the modal
    # bucket to a specific persona.
    verb_counts: Counter[str] = Counter()
    verb_persona: dict[str, str] = {}
    successful_total = 0
    for action in snapshot.last_tick_actions:
        if not action.get("ok"):
            continue
        successful_total += 1
        verb = str(action.get("action", "")).strip().lower() or "noop"
        verb_counts[verb] += 1
        agent_id = action.get("agent_id")
        if isinstance(agent_id, str):
            verb_persona[verb] = agent_id

    if verb_counts:
        modal_verb, modal_count = verb_counts.most_common(1)[0]
        confidence = (modal_count / successful_total) if successful_total else 0.0
        persona_id = verb_persona.get(modal_verb, "")
    else:
        modal_verb = "noop"
        confidence = 0.0
        persona_id = ""

    return [
        {
            "anchor_id": anchor_id,
            "persona_id": persona_id,
            "tick_id": tick_index,
            "decision_text": modal_verb,
            "confidence": confidence,
            "sub_type": sub_type,
            "forward_precedent_decision_id": decision_graph_ref.lookup_precedent(
                anchor_id=anchor_id,
                persona_id=persona_id,
                scenario_id=scenario_id,
            ),
        }
        for anchor_id in anchors
    ]
