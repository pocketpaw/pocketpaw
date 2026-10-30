# tests/ee/foresight/test_scenario_smoke.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# The scenario smoke test — proves the v0.1 end-to-end loop closes:
#   1. Load decision_forecast.yaml.
#   2. Run it through ForesightWorld + SoulSeededPersona + DeterministicFakeBackend.
#   3. Assert the run completes, snapshots are populated, RunResult
#      serializes to a JSON-safe dict.
#
# This is the test the captain asked for ("scenario template that runs
# end-to-end trivially: 5 personas, 1 tick, decisions logged").

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pocketpaw_ee.foresight.llm.adapter import DeterministicFakeBackend
from pocketpaw_ee.foresight.persona import OceanDrift
from pocketpaw_ee.foresight.scenarios.runner import (
    PersonaSpec,
    RunResult,
    ScenarioConfig,
    run_scenario,
)

SCENARIO_YAML = (
    Path(__file__).resolve().parents[3]
    / "ee"
    / "pocketpaw_ee"
    / "foresight"
    / "scenarios"
    / "decision_forecast.yaml"
)


# --- ScenarioConfig validation -------------------------------------


def test_scenario_config_rejects_unsupported_sub_type():
    # PR 5 lifted market_sim + org_change_rehearsal into the supported
    # set; the next-up sub-type is ops_stress_test (PR 6+).
    with pytest.raises(NotImplementedError, match="ops_stress_test"):
        ScenarioConfig(
            name="x",
            sub_type="ops_stress_test",  # not yet in SUPPORTED_SUB_TYPES
            n_ticks=1,
            personas=[PersonaSpec(name="p")],
        )


def test_scenario_config_rejects_zero_ticks():
    with pytest.raises(ValueError, match="n_ticks"):
        ScenarioConfig(name="x", n_ticks=0, personas=[PersonaSpec(name="p")])


def test_scenario_config_rejects_empty_personas():
    with pytest.raises(ValueError, match="at least one persona"):
        ScenarioConfig(name="x", n_ticks=1, personas=[])


def test_scenario_config_loads_decision_forecast_yaml():
    """The shipped yaml file must parse cleanly via from_yaml."""
    assert SCENARIO_YAML.exists(), f"missing scenario yaml: {SCENARIO_YAML}"
    config = ScenarioConfig.from_yaml(SCENARIO_YAML)
    assert config.name == "smoke-decision-forecast"
    assert config.sub_type == "decision_forecast"
    assert config.n_ticks == 1
    assert len(config.personas) == 5
    # Verify OceanDrift round-trips from the yaml
    prakash = next(p for p in config.personas if p.name == "approver-prakash")
    assert prakash.role == "approver"
    assert prakash.ocean.conscientiousness == 1.2


# --- End-to-end smoke -----------------------------------------------


async def test_run_scenario_closes_the_loop_against_yaml():
    """The canonical smoke run — 5 personas, 1 tick, decisions logged."""
    config = ScenarioConfig.from_yaml(SCENARIO_YAML)
    result = await run_scenario(config)

    # Shape contract
    assert isinstance(result, RunResult)
    assert result.scenario_name == "smoke-decision-forecast"
    assert result.n_ticks == 1
    assert len(result.tick_snapshots) == 1
    assert result.tick_snapshots[0].population == 5
    # 5 personas × deterministic backend that returns valid action lines
    # → 5 successful actions on tick 1
    assert result.tick_snapshots[0].actions_applied == 5
    assert result.actions_logged == 5

    # The deterministic backend's cycle includes `put=last_action:<verb>`
    # so the world's state should carry the last writer's verb
    assert "last_action" in result.final_state


async def test_run_scenario_multi_tick_accumulates_actions():
    """3 ticks × 5 personas → 15 successful actions in the final snapshot."""
    config = ScenarioConfig.from_yaml(SCENARIO_YAML)
    config.n_ticks = 3
    result = await run_scenario(config)

    assert result.n_ticks == 3
    # Each snapshot's actions_applied is cumulative, so:
    assert result.tick_snapshots[0].actions_applied == 5
    assert result.tick_snapshots[1].actions_applied == 10
    assert result.tick_snapshots[2].actions_applied == 15
    assert result.actions_logged == 15


async def test_run_result_serializes_to_json_safe_dict():
    config = ScenarioConfig.from_yaml(SCENARIO_YAML)
    result = await run_scenario(config)
    wire = result.as_wire_dict()

    # Must round-trip through json (no datetimes, UUIDs, or other
    # non-serializable surprises in v0.1)
    encoded = json.dumps(wire)
    decoded = json.loads(encoded)

    assert decoded["scenario_name"] == "smoke-decision-forecast"
    assert decoded["n_ticks"] == 1
    assert decoded["actions_logged"] == 5
    assert isinstance(decoded["tick_snapshots"], list)
    assert decoded["tick_snapshots"][0]["population"] == 5


async def test_run_scenario_uses_injected_backend():
    """Caller-supplied backend overrides the DeterministicFakeBackend default."""
    config = ScenarioConfig(
        name="single-persona",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[PersonaSpec(name="solo", role="agent", ocean=OceanDrift())],
    )

    scripted = DeterministicFakeBackend(
        responses=["action=custom; rationale=injected; put=custom_key:custom_value"]
    )
    result = await run_scenario(config, backend=scripted)

    assert scripted.call_count == 1
    assert result.final_state.get("custom_key") == "custom_value"


async def test_run_scenario_with_failing_backend_records_errors():
    """If the backend fails for every persona, the loop still completes —
    every action lands in last_tick_actions with ok=False, and
    actions_applied stays at 0.
    """

    class _BoomBackend:
        async def complete(self, prompt: str) -> str:  # noqa: ARG002
            raise RuntimeError("simulated outage")

    config = ScenarioConfig.from_yaml(SCENARIO_YAML)
    result = await run_scenario(config, backend=_BoomBackend())

    # All five persona errors get captured (the persona converts them
    # to noop actions, so they DO count as ok=True with no put).
    assert len(result.tick_snapshots[0].last_tick_actions) == 5
    # The persona's noop has put=None, so state doesn't mutate.
    assert result.final_state == {}
    # actions_applied counts successful ok=True returns; since the
    # persona catches the exception and returns a noop with put=None,
    # every action IS applied (just with no state effect).
    assert result.tick_snapshots[0].actions_applied == 5
