# tests/ee/foresight/test_subtypes.py — RFC 08 PR 5.
# Created: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision)
# — engine-side tests for the new Market Sim and Org Change Rehearsal
# sub-type adapters under ``ee.pocketpaw_ee.foresight.subtypes``.
"""Tests for the sub-type adapters and the end-to-end runner dispatch.

Covers:
  - ``subtypes.get_adapter`` dispatch on supported / unsupported sub_type.
  - Per-adapter ``anchors_for`` / ``aggregate`` contracts.
  - ``run_scenario`` smoke against both new YAML files.
  - Per-tick projected-decision callback fan-out across anchors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.foresight.scenarios.runner import (
    PersonaSpec,
    ScenarioConfig,
    run_scenario,
)
from pocketpaw_ee.foresight.subtypes import (
    SUB_TYPE_DECISION_FORECAST,
    SUB_TYPE_MARKET_SIM,
    SUB_TYPE_ORG_CHANGE,
    SUPPORTED_SUB_TYPES,
    get_adapter,
)

SCENARIO_DIR = (
    Path(__file__).resolve().parents[3] / "ee" / "pocketpaw_ee" / "foresight" / "scenarios"
)
MARKET_SIM_YAML = SCENARIO_DIR / "market_sim.yaml"
ORG_CHANGE_YAML = SCENARIO_DIR / "org_change.yaml"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_supported_sub_types_carries_v05_set():
    assert SUB_TYPE_DECISION_FORECAST in SUPPORTED_SUB_TYPES
    assert SUB_TYPE_MARKET_SIM in SUPPORTED_SUB_TYPES
    assert SUB_TYPE_ORG_CHANGE in SUPPORTED_SUB_TYPES
    assert len(SUPPORTED_SUB_TYPES) == 3


def test_get_adapter_for_market_sim_returns_module_with_contract():
    adapter = get_adapter(SUB_TYPE_MARKET_SIM)
    assert hasattr(adapter, "SUB_TYPE")
    assert adapter.SUB_TYPE == "market_sim"
    assert callable(adapter.anchors_for)
    assert callable(adapter.aggregate)


def test_get_adapter_for_org_change_returns_module_with_contract():
    adapter = get_adapter(SUB_TYPE_ORG_CHANGE)
    assert adapter.SUB_TYPE == "org_change_rehearsal"


def test_get_adapter_for_decision_forecast_returns_passthrough():
    adapter = get_adapter(SUB_TYPE_DECISION_FORECAST)
    assert adapter.SUB_TYPE == "decision_forecast"


def test_get_adapter_unsupported_raises_with_rfc_pointer():
    with pytest.raises(NotImplementedError, match="RFC 08"):
        get_adapter("ops_stress_test")


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def test_market_sim_anchors_dedupe_by_segment():
    adapter = get_adapter(SUB_TYPE_MARKET_SIM)
    config = ScenarioConfig(
        name="t",
        sub_type=SUB_TYPE_MARKET_SIM,
        n_ticks=1,
        personas=[
            PersonaSpec(name="a", role="enterprise"),
            PersonaSpec(name="b", role="enterprise"),
            PersonaSpec(name="c", role="smb"),
        ],
    )
    anchors = adapter.anchors_for(config)
    assert anchors == ["segment:enterprise", "segment:smb"]


def test_org_change_anchors_emit_fixed_rollout_sequence():
    adapter = get_adapter(SUB_TYPE_ORG_CHANGE)
    config = ScenarioConfig(
        name="t",
        sub_type=SUB_TYPE_ORG_CHANGE,
        n_ticks=4,
        personas=[PersonaSpec(name="x", role="manager")],
    )
    anchors = adapter.anchors_for(config)
    assert anchors == [
        "rollout:announce",
        "rollout:training",
        "rollout:deadline",
        "rollout:escalation",
    ]


def test_decision_forecast_anchors_default_to_scenario_name():
    adapter = get_adapter(SUB_TYPE_DECISION_FORECAST)
    config = ScenarioConfig(
        name="renewal-q3",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=1,
        personas=[PersonaSpec(name="p", role="approver")],
    )
    assert adapter.anchors_for(config) == ["decision:renewal-q3"]


# ---------------------------------------------------------------------------
# Aggregate (empty + populated)
# ---------------------------------------------------------------------------


def test_market_sim_aggregate_empty_returns_total_zero():
    adapter = get_adapter(SUB_TYPE_MARKET_SIM)
    config = ScenarioConfig(
        name="t",
        sub_type=SUB_TYPE_MARKET_SIM,
        n_ticks=1,
        personas=[PersonaSpec(name="a", role="enterprise")],
    )
    out = adapter.aggregate([], config)
    assert out["sub_type"] == "market_sim"
    assert out["segments"] == {}
    assert out["totals"]["actions_total"] == 0


def test_org_change_aggregate_empty_returns_rollout_zero():
    adapter = get_adapter(SUB_TYPE_ORG_CHANGE)
    config = ScenarioConfig(
        name="t",
        sub_type=SUB_TYPE_ORG_CHANGE,
        n_ticks=4,
        personas=[PersonaSpec(name="x", role="manager")],
    )
    out = adapter.aggregate([], config)
    assert out["sub_type"] == "org_change_rehearsal"
    assert set(out["rollout"].keys()) == {
        "rollout:announce",
        "rollout:training",
        "rollout:deadline",
        "rollout:escalation",
    }
    assert out["totals"]["escalation_count"] == 0


# ---------------------------------------------------------------------------
# End-to-end runner against YAML — both new sub-types
# ---------------------------------------------------------------------------


async def test_run_scenario_market_sim_from_yaml():
    assert MARKET_SIM_YAML.exists(), f"missing yaml: {MARKET_SIM_YAML}"
    config = ScenarioConfig.from_yaml(MARKET_SIM_YAML)
    assert config.sub_type == "market_sim"

    result = await run_scenario(config)
    assert result.sub_type == "market_sim"
    assert result.aggregate["sub_type"] == "market_sim"
    assert "segments" in result.aggregate
    # Every declared segment must surface in the aggregate (even when
    # the deterministic-fake produced zero matching actions for it).
    declared_segments = {f"segment:{p.role}" for p in config.personas}
    assert declared_segments.issubset(set(result.aggregate["segments"].keys()))


async def test_run_scenario_org_change_from_yaml():
    assert ORG_CHANGE_YAML.exists(), f"missing yaml: {ORG_CHANGE_YAML}"
    config = ScenarioConfig.from_yaml(ORG_CHANGE_YAML)
    assert config.sub_type == "org_change_rehearsal"
    assert config.n_ticks == 4

    result = await run_scenario(config)
    assert result.sub_type == "org_change_rehearsal"
    assert result.aggregate["sub_type"] == "org_change_rehearsal"
    # The fixed four-step rollout always surfaces as four anchors.
    assert set(result.aggregate["rollout"].keys()) == {
        "rollout:announce",
        "rollout:training",
        "rollout:deadline",
        "rollout:escalation",
    }


# ---------------------------------------------------------------------------
# Per-tick projected-decision callback
# ---------------------------------------------------------------------------


async def test_run_scenario_fans_one_projection_per_anchor_per_tick():
    """The callback is invoked exactly once per (anchor × tick) bucket.
    Market Sim with 2 segments × 2 ticks = 4 callback invocations.
    """
    config = ScenarioConfig(
        name="cb-market-sim",
        sub_type=SUB_TYPE_MARKET_SIM,
        n_ticks=2,
        personas=[
            PersonaSpec(name="a", role="enterprise"),
            PersonaSpec(name="b", role="smb"),
        ],
    )

    records: list[tuple] = []

    def _capture(anchor_id, persona_id, tick_id, decision_text, confidence, sub_type):
        records.append((anchor_id, persona_id, tick_id, decision_text, confidence, sub_type))

    result = await run_scenario(config, on_projected_decision=_capture)

    # 2 segments × 2 ticks
    assert len(records) == 4
    # Every record carries the sub_type echo.
    assert all(r[5] == "market_sim" for r in records)
    # Anchors are the dedupe'd segment set.
    anchors_in_records = {r[0] for r in records}
    assert anchors_in_records == {"segment:enterprise", "segment:smb"}
    # Confidence is bounded.
    assert all(0.0 <= r[4] <= 1.0 for r in records)
    # The result also surfaces them on the dataclass.
    assert len(result.projected_decisions) == 4


async def test_run_scenario_supports_async_callback():
    """The callback may be an async function — the runner awaits the
    return value when it's a coroutine."""
    config = ScenarioConfig(
        name="async-cb",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=2,
        personas=[PersonaSpec(name="p", role="approver")],
    )

    awaited: list[str] = []

    async def _async_capture(anchor_id, persona_id, tick_id, decision_text, confidence, sub_type):
        awaited.append(anchor_id)

    result = await run_scenario(config, on_projected_decision=_async_capture)
    # Decision Forecast has one anchor (the scenario name) × 2 ticks
    assert len(awaited) == 2
    assert len(result.projected_decisions) == 2


async def test_run_scenario_without_callback_still_surfaces_records():
    """The callback is optional — the result still carries the
    projected_decisions list so CLI callers can read them."""
    config = ScenarioConfig(
        name="no-cb",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=1,
        personas=[PersonaSpec(name="p", role="approver")],
    )
    result = await run_scenario(config)
    assert len(result.projected_decisions) == 1
    rec = result.projected_decisions[0]
    assert rec["anchor_id"] == "decision:no-cb"
    assert rec["sub_type"] == "decision_forecast"


async def test_run_result_as_wire_dict_includes_aggregate_and_projections():
    config = ScenarioConfig(
        name="wire",
        sub_type=SUB_TYPE_MARKET_SIM,
        n_ticks=1,
        personas=[PersonaSpec(name="a", role="enterprise")],
    )
    result = await run_scenario(config)
    wire = result.as_wire_dict()
    assert wire["sub_type"] == "market_sim"
    assert "aggregate" in wire
    assert "projected_decisions" in wire
    assert wire["aggregate"]["sub_type"] == "market_sim"
