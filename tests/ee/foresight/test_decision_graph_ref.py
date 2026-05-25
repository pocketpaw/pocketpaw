# tests/ee/foresight/test_decision_graph_ref.py
# Created: 2026-05-25 (feat/foresight-v14-decision-graph-stub) — RFC 08
# §14.4 wiring. Covers:
#   - DecisionGraphRef protocol contract — runtime subclass detection
#     using ``isinstance`` against the ``@runtime_checkable`` Protocol.
#   - NoOpDecisionGraphRef determinism — same inputs always produce the
#     same synthetic id.
#   - NoOpDecisionGraphRef returns ``None`` when no seed is configured
#     (preserves v0.1 "field always None" wire shape).
#   - NoOpDecisionGraphRef per-anchor override priority — anchor-level
#     wins over scenario-level when both are set.
#   - ScenarioConfig YAML parsing carries ``precedent_seed`` and
#     ``precedent_seeds`` through to the dataclass.
#   - End-to-end runner: ProjectedDecisions emitted with no seed carry
#     ``forward_precedent_decision_id=None``; emitted with a seed carry
#     a stable synthetic id of the documented shape.
#   - run_scenario accepts a custom DecisionGraphRef and routes lookup
#     through it (drop-in replacement when RFC 07 ships).

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pocketpaw_ee.foresight.decision_graph_ref import (
    SYNTHETIC_PRECEDENT_PREFIX,
    DecisionGraphRef,
    NoOpDecisionGraphRef,
)
from pocketpaw_ee.foresight.scenarios.runner import (
    PersonaSpec,
    ScenarioConfig,
    run_scenario,
)
from pocketpaw_ee.foresight.subtypes import (
    SUB_TYPE_DECISION_FORECAST,
    SUB_TYPE_MARKET_SIM,
)

# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


def test_decision_graph_ref_is_runtime_checkable_protocol():
    """The protocol must be ``@runtime_checkable`` so the runner can do
    isinstance checks (and so tests can detect duck-typed conformance).
    """
    ref = NoOpDecisionGraphRef(seed="x")
    assert isinstance(ref, DecisionGraphRef)


def test_decision_graph_ref_rejects_non_conforming_object():
    """Objects without ``lookup_precedent`` must not satisfy the
    protocol. This is the same gate the runner relies on when callers
    supply their own ref instead of the NoOp default."""

    class _NotARef:
        pass

    assert not isinstance(_NotARef(), DecisionGraphRef)


def test_decision_graph_ref_accepts_duck_typed_conformance():
    """Any class with the right shape conforms — that's what makes
    swapping in the future RFC 07 implementation a drop-in change."""

    class _MyRef:
        def lookup_precedent(
            self,
            anchor_id: str,  # noqa: ARG002
            persona_id: str,  # noqa: ARG002
            scenario_id: str,  # noqa: ARG002
        ) -> str | None:
            return "dec_real_short_id"

    assert isinstance(_MyRef(), DecisionGraphRef)


# ---------------------------------------------------------------------------
# NoOpDecisionGraphRef — determinism and edge cases
# ---------------------------------------------------------------------------


def test_noop_returns_none_when_no_seed_configured():
    """No seed → no precedent. Preserves v0.1 wire-shape contract for
    un-seeded scenarios (the field stays ``None`` on every record)."""
    ref = NoOpDecisionGraphRef()
    result = ref.lookup_precedent(
        anchor_id="decision:x",
        persona_id="persona-1",
        scenario_id="scenario-A",
    )
    assert result is None


def test_noop_returns_none_when_seed_is_empty_string():
    """Empty string is normalized as "no seed configured"."""
    ref = NoOpDecisionGraphRef(seed="")
    assert ref.lookup_precedent("decision:x", "p", "s") is None


def test_noop_deterministic_same_inputs_same_id():
    """Same (anchor, persona, scenario, seed) → same id every call.
    Critical for replay-style backfill jobs that re-emit projections
    and expect to land on the same precedent id idempotently."""
    ref = NoOpDecisionGraphRef(seed="seed-abc")
    first = ref.lookup_precedent("decision:x", "persona-1", "scenario-A")
    second = ref.lookup_precedent("decision:x", "persona-1", "scenario-A")
    third = ref.lookup_precedent("decision:x", "persona-1", "scenario-A")
    assert first == second == third


def test_noop_id_shape_matches_synthetic_prefix_contract():
    """The synthesized id starts with the documented prefix and ends
    with a 12-hex-char digest. Downstream code (UI badge, backfill
    detection) keys on the prefix to tell synthetic from real ids."""
    ref = NoOpDecisionGraphRef(seed="seed-abc")
    result = ref.lookup_precedent("decision:x", "persona-1", "scenario-A")
    assert result is not None
    assert result.startswith(SYNTHETIC_PRECEDENT_PREFIX)
    digest = result[len(SYNTHETIC_PRECEDENT_PREFIX) :]
    assert len(digest) == 12
    # All chars are lowercase hex
    assert all(c in "0123456789abcdef" for c in digest)


def test_noop_different_anchors_yield_different_ids():
    """Different anchor ids collide infrequently — sanity check that
    the hash actually mixes the anchor in."""
    ref = NoOpDecisionGraphRef(seed="seed")
    a = ref.lookup_precedent("decision:x", "p", "s")
    b = ref.lookup_precedent("decision:y", "p", "s")
    assert a != b


def test_noop_different_personas_yield_different_ids():
    ref = NoOpDecisionGraphRef(seed="seed")
    a = ref.lookup_precedent("decision:x", "persona-1", "s")
    b = ref.lookup_precedent("decision:x", "persona-2", "s")
    assert a != b


def test_noop_different_scenarios_yield_different_ids():
    ref = NoOpDecisionGraphRef(seed="seed")
    a = ref.lookup_precedent("decision:x", "p", "scenario-A")
    b = ref.lookup_precedent("decision:x", "p", "scenario-B")
    assert a != b


def test_noop_different_seeds_yield_different_ids():
    """The seed is part of the hash — two scenarios with the same
    anchor/persona/scenario_id but different seeds get different ids.
    This is what makes the backfill stream re-keyable on seed rotation.
    """
    ref_a = NoOpDecisionGraphRef(seed="seed-1")
    ref_b = NoOpDecisionGraphRef(seed="seed-2")
    a = ref_a.lookup_precedent("decision:x", "p", "s")
    b = ref_b.lookup_precedent("decision:x", "p", "s")
    assert a != b


# ---------------------------------------------------------------------------
# Per-anchor override priority
# ---------------------------------------------------------------------------


def test_noop_per_anchor_seed_overrides_scenario_seed():
    """Anchor-level override is the v14 grammar's "scenario seed +
    per-anchor pin" pattern. The override seed must produce a different
    id than the scenario-level seed would have."""
    ref_global_only = NoOpDecisionGraphRef(seed="scenario-seed")
    ref_with_override = NoOpDecisionGraphRef(
        seed="scenario-seed",
        per_anchor_seeds={"decision:critical": "anchor-seed"},
    )
    # The non-overridden anchor stays on the scenario seed.
    assert ref_global_only.lookup_precedent(
        "decision:other", "p", "s"
    ) == ref_with_override.lookup_precedent("decision:other", "p", "s")
    # The overridden anchor diverges.
    assert ref_global_only.lookup_precedent(
        "decision:critical", "p", "s"
    ) != ref_with_override.lookup_precedent("decision:critical", "p", "s")


def test_noop_per_anchor_override_falls_back_to_scenario_seed_when_empty():
    """An empty-string override is treated as "remove override" — the
    YAML loader strips empty values but defense-in-depth matters."""
    ref = NoOpDecisionGraphRef(
        seed="scenario-seed",
        per_anchor_seeds={"decision:x": ""},
    )
    bare = NoOpDecisionGraphRef(seed="scenario-seed")
    assert ref.lookup_precedent("decision:x", "p", "s") == bare.lookup_precedent(
        "decision:x", "p", "s"
    )


# ---------------------------------------------------------------------------
# ScenarioConfig YAML parsing carries the precedent seeds
# ---------------------------------------------------------------------------


def _write_yaml(body: str) -> Path:
    """Write a scenario YAML to a temp file for from_yaml round-trip
    tests. Returns the path; caller is responsible for cleanup (we use
    NamedTemporaryFile via context-managed helper inside each test)."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
    tmp.write(body)
    tmp.close()
    return Path(tmp.name)


def test_scenario_config_from_yaml_loads_precedent_seed():
    path = _write_yaml(
        """\
name: yaml-seed-test
sub_type: decision_forecast
n_ticks: 1
precedent_seed: "shared-2026-Q3"
personas:
  - name: p1
    role: approver
"""
    )
    try:
        config = ScenarioConfig.from_yaml(path)
    finally:
        path.unlink()
    assert config.precedent_seed == "shared-2026-Q3"
    assert config.precedent_seeds == {}


def test_scenario_config_from_yaml_loads_per_anchor_seeds():
    path = _write_yaml(
        """\
name: yaml-anchor-seeds
sub_type: decision_forecast
n_ticks: 1
precedent_seed: "scenario-seed"
precedent_seeds:
  "decision:critical": "critical-pin"
  "decision:routine": "routine-pin"
personas:
  - name: p1
    role: approver
"""
    )
    try:
        config = ScenarioConfig.from_yaml(path)
    finally:
        path.unlink()
    assert config.precedent_seed == "scenario-seed"
    assert config.precedent_seeds == {
        "decision:critical": "critical-pin",
        "decision:routine": "routine-pin",
    }


def test_scenario_config_from_yaml_strips_null_and_empty_seeds():
    """Null / empty values in the YAML map are filtered out so the
    NoOp ref doesn't have to handle them. The runner's behavior under
    an explicit-empty override is already covered by
    ``test_noop_per_anchor_override_falls_back_to_scenario_seed_when_empty``;
    this test pins the loader's normalization contract."""
    path = _write_yaml(
        """\
name: yaml-empty-seeds
sub_type: decision_forecast
n_ticks: 1
precedent_seeds:
  "decision:a": "good-seed"
  "decision:b": ""
  "decision:c": null
personas:
  - name: p1
    role: approver
"""
    )
    try:
        config = ScenarioConfig.from_yaml(path)
    finally:
        path.unlink()
    assert config.precedent_seeds == {"decision:a": "good-seed"}


def test_scenario_config_defaults_to_no_seed_when_absent():
    """Existing scenarios without the new fields must keep working — no
    seed means no synthetic ids, no behavior change."""
    path = _write_yaml(
        """\
name: yaml-no-seed
sub_type: decision_forecast
n_ticks: 1
personas:
  - name: p1
    role: approver
"""
    )
    try:
        config = ScenarioConfig.from_yaml(path)
    finally:
        path.unlink()
    assert config.precedent_seed is None
    assert config.precedent_seeds == {}


# ---------------------------------------------------------------------------
# End-to-end: ProjectedDecision now carries forward_precedent_decision_id
# ---------------------------------------------------------------------------


async def test_run_scenario_without_seed_records_carry_none_precedent():
    """Backward-compat: a scenario without a precedent seed produces
    projected-decision records with ``forward_precedent_decision_id=None``.
    """
    config = ScenarioConfig(
        name="no-seed",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=1,
        personas=[PersonaSpec(name="p", role="approver")],
    )
    result = await run_scenario(config)
    assert len(result.projected_decisions) == 1
    assert result.projected_decisions[0]["forward_precedent_decision_id"] is None


async def test_run_scenario_with_seed_records_carry_synthetic_precedent():
    """Smoking-gun test: with a seed configured, every projected record
    carries a non-None synthetic precedent id with the documented prefix.
    """
    config = ScenarioConfig(
        name="seeded",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=2,
        personas=[PersonaSpec(name="p", role="approver")],
        precedent_seed="run-2026-Q3",
    )
    result = await run_scenario(config)
    assert len(result.projected_decisions) == 2
    for record in result.projected_decisions:
        pid = record["forward_precedent_decision_id"]
        assert pid is not None
        assert pid.startswith(SYNTHETIC_PRECEDENT_PREFIX)


async def test_run_scenario_with_seed_synthetic_ids_are_stable_across_ticks():
    """Same anchor/persona/scenario, different ticks → same synthetic
    id. The synthetic id is tick-independent because v0.5's anchor set
    is fixed per run; this property lets backfill jobs key on
    (run, anchor) without thinking about tick ordering."""
    config = ScenarioConfig(
        name="stable",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=3,
        personas=[PersonaSpec(name="p", role="approver")],
        precedent_seed="stable-seed",
    )
    result = await run_scenario(config)
    ids = {r["forward_precedent_decision_id"] for r in result.projected_decisions}
    # Decision Forecast has one anchor; 3 ticks should all produce the
    # same synthetic id because anchor/persona/scenario/seed are the
    # same and tick is NOT part of the hash by design (RFC §14.4 keys
    # forward-precedents on the projection bucket, not the tick).
    #
    # NOTE: persona_id may differ between ticks if the modal persona
    # diverges; in this single-persona scenario it stays constant.
    assert len(ids) == 1


async def test_run_scenario_with_per_anchor_seed_overrides_yield_different_ids():
    """Anchor-level seed plumbing through the runner: two different
    anchors get different synthetic ids when the per-anchor seed map
    diverges."""
    config = ScenarioConfig(
        name="market-anchored",
        sub_type=SUB_TYPE_MARKET_SIM,
        n_ticks=1,
        personas=[
            PersonaSpec(name="a", role="enterprise"),
            PersonaSpec(name="b", role="smb"),
        ],
        precedent_seed="scenario-seed",
        precedent_seeds={
            "segment:enterprise": "enterprise-pin",
            "segment:smb": "smb-pin",
        },
    )
    result = await run_scenario(config)
    by_anchor = {
        r["anchor_id"]: r["forward_precedent_decision_id"] for r in result.projected_decisions
    }
    assert by_anchor["segment:enterprise"] is not None
    assert by_anchor["segment:smb"] is not None
    assert by_anchor["segment:enterprise"] != by_anchor["segment:smb"]


async def test_run_scenario_callback_signature_unchanged_after_v14():
    """The six-arg callback contract is preserved for backward-compat
    with the cloud's existing closure in cloud/foresight/service.py
    (which the §8 approval-bridge lane may also touch). The precedent
    id surfaces only on ``RunResult.projected_decisions``."""
    config = ScenarioConfig(
        name="cb-compat",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=1,
        personas=[PersonaSpec(name="p", role="approver")],
        precedent_seed="cb-seed",
    )

    captured: list[tuple] = []

    def _capture(anchor_id, persona_id, tick_id, decision_text, confidence, sub_type):
        # Six args — same shape as before v14. If this signature broke
        # the §8 lead's closure would fail on merge.
        captured.append((anchor_id, persona_id, tick_id, decision_text, confidence, sub_type))

    result = await run_scenario(config, on_projected_decision=_capture)
    assert len(captured) == 1
    # The precedent id is NOT in the callback args — it's on the record.
    assert result.projected_decisions[0]["forward_precedent_decision_id"] is not None


async def test_run_scenario_accepts_custom_decision_graph_ref():
    """Drop-in replacement contract: when RFC 07 lands its real
    DecisionGraphRef will inject through this kwarg. v0.5 tests the
    plumbing with a fake ref that returns a canned id."""

    class _FakeRef:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def lookup_precedent(
            self,
            anchor_id: str,
            persona_id: str,
            scenario_id: str,
        ) -> str | None:
            self.calls.append((anchor_id, persona_id, scenario_id))
            return "dec_real_rfc07_id"

    fake = _FakeRef()
    config = ScenarioConfig(
        name="custom-ref",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=1,
        personas=[PersonaSpec(name="p", role="approver")],
        # Even with a seed configured, the custom ref wins — it fully
        # replaces the NoOp default.
        precedent_seed="ignored-seed",
    )
    result = await run_scenario(config, decision_graph_ref=fake)
    assert fake.calls  # the ref was consulted
    for record in result.projected_decisions:
        assert record["forward_precedent_decision_id"] == "dec_real_rfc07_id"


async def test_run_scenario_wire_dict_carries_precedent_field():
    """``RunResult.as_wire_dict()`` is what the cloud's
    ``_run_engine_inline`` returns to the persistence layer. The
    forward_precedent_decision_id must survive the dict round-trip."""
    config = ScenarioConfig(
        name="wire-precedent",
        sub_type=SUB_TYPE_DECISION_FORECAST,
        n_ticks=1,
        personas=[PersonaSpec(name="p", role="approver")],
        precedent_seed="wire-seed",
    )
    result = await run_scenario(config)
    wire = result.as_wire_dict()
    projected = wire["projected_decisions"]
    assert len(projected) == 1
    assert projected[0]["forward_precedent_decision_id"] is not None


# ---------------------------------------------------------------------------
# Integration sanity — the shipped scenario YAMLs still parse cleanly
# ---------------------------------------------------------------------------

SHIPPED_YAMLS = ("decision_forecast.yaml", "market_sim.yaml", "org_change.yaml")


@pytest.mark.parametrize("yaml_name", SHIPPED_YAMLS)
def test_shipped_scenario_yamls_parse_with_new_optional_fields(yaml_name: str):
    """All three shipped YAMLs must keep loading cleanly. None of them
    declare ``precedent_seed`` yet — the new fields are strictly
    additive and absent-by-default."""
    yaml_path = (
        Path(__file__).resolve().parents[3]
        / "ee"
        / "pocketpaw_ee"
        / "foresight"
        / "scenarios"
        / yaml_name
    )
    assert yaml_path.exists(), f"missing scenario yaml: {yaml_path}"
    config = ScenarioConfig.from_yaml(yaml_path)
    assert config.precedent_seed is None
    assert config.precedent_seeds == {}
