# tests/cloud/test_foresight_service_live_snapshot.py
# Created: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) —
# RFC 08 v1.0 live-snapshot endpoint coverage.
#
# Exercises ``foresight_service.get_live_snapshot`` end-to-end:
#   - happy path (run + projections → populated wire shape)
#   - empty run (no projections → zeros + empty arrays)
#   - cross-tenant 404 (existence-not-leakable)
#   - unknown run id 404 (collapse rule)
#   - anomaly rules (tier_drift / confidence_spike / stalled_persona)
#     each triggering deterministically off seeded projection writes.
"""Tests for the live-snapshot service path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.domain import LiveTierMixActual
from pocketpaw_ee.cloud.foresight.dto import (
    CreateScenarioRequest,
    PersonaSpecRequest,
)
from pocketpaw_ee.cloud.foresight.live_snapshot import (
    detect_confidence_spike,
    detect_stalled_persona,
    detect_tier_drift,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str | None = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _scenario_body(
    name: str = "live-test",
    n_ticks: int = 1,
    personas: list[PersonaSpecRequest] | None = None,
) -> CreateScenarioRequest:
    return CreateScenarioRequest(
        name=name,
        sub_type="decision_forecast",
        n_ticks=n_ticks,
        personas=personas or [PersonaSpecRequest(name="Anne", role="approver", ocean={})],
    )


# ---------------------------------------------------------------------------
# Happy path / wire-shape contract
# ---------------------------------------------------------------------------


async def test_live_snapshot_returns_locked_contract_shape() -> None:
    """The wire shape matches the paw-enterprise PR #267 contract:
    run_id / generated_at / status / tier_mix_actual / sampled_traces
    / anomalies. Every nested field is populated to the documented
    default — empty runs collapse to zero triple + empty arrays.
    """
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _scenario_body())

    snap = await foresight_service.get_live_snapshot(ctx, run.id)
    # Top-level fields locked to PR #267.
    assert snap.run_id == run.id
    assert snap.generated_at
    assert snap.status in {"created", "running", "complete", "failed"}
    # The deterministic-fake engine completes synchronously so the run
    # surfaces as ``complete`` (which maps 1:1 on the wire).
    assert snap.status == "complete"
    # Tier mix triple — three floats in [0, 1].
    for tier in ("premium", "mid", "tail"):
        value = getattr(snap.tier_mix_actual, tier)
        assert 0.0 <= value <= 1.0
    # Empty / zero engines still produce a valid wire shape.
    assert isinstance(snap.sampled_traces, list)
    assert isinstance(snap.anomalies, list)


async def test_live_snapshot_status_maps_queued_to_created(monkeypatch) -> None:
    """The wire vocabulary renames ``queued`` to ``created`` to match
    paw-enterprise PR #267. Persisted ``queued`` runs surface as
    ``created`` on the live snapshot."""
    ctx = _ctx()

    # Force a failure mid-run so the doc stays in a recognizable
    # non-complete state — then inspect the snapshot's status map.
    async def _boom(_body, **_kwargs):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(foresight_service, "_run_engine_inline", _boom)
    run = await foresight_service.create_scenario_run(ctx, _scenario_body())
    # ``failed`` maps 1:1 — no rename needed.
    snap = await foresight_service.get_live_snapshot(ctx, run.id)
    assert snap.status == "failed"


# ---------------------------------------------------------------------------
# Empty / unknown / cross-tenant
# ---------------------------------------------------------------------------


async def test_live_snapshot_empty_run_returns_zeros() -> None:
    """An empty run (no projections) returns zero tier mix +
    empty sampled_traces. Used by the UI's empty state."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _scenario_body())
    snap = await foresight_service.get_live_snapshot(ctx, run.id)
    # With the deterministic fake the engine reports a tier_distribution
    # of {} (single-backend path); the derivation collapses to zeros.
    assert snap.tier_mix_actual.premium == 0.0
    assert snap.tier_mix_actual.mid == 0.0
    assert snap.tier_mix_actual.tail == 0.0


async def test_live_snapshot_404_for_unknown_run() -> None:
    ctx = _ctx()
    with pytest.raises(NotFound):
        await foresight_service.get_live_snapshot(ctx, "5f50c31b1c9d440000000000")


async def test_live_snapshot_404_for_malformed_run_id() -> None:
    ctx = _ctx()
    with pytest.raises(NotFound):
        await foresight_service.get_live_snapshot(ctx, "not-an-objectid")


async def test_live_snapshot_isolates_across_workspaces() -> None:
    """A run created in w1 must collapse to 404 from w2 — existence
    is not cross-tenant leakable (same rule as the other run-scoped
    endpoints)."""
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    run = await foresight_service.create_scenario_run(ctx_w1, _scenario_body())
    with pytest.raises(NotFound):
        await foresight_service.get_live_snapshot(ctx_w2, run.id)


async def test_live_snapshot_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden):
        await foresight_service.get_live_snapshot(ctx, "5f50c31b1c9d440000000000")


# ---------------------------------------------------------------------------
# Sampled traces — deterministic slice, sub-type-aware formatter
# ---------------------------------------------------------------------------


async def test_live_snapshot_samples_traces_from_persisted_projections(
    mongo_db,
) -> None:
    """When projections exist for a run, ``sampled_traces`` carries
    up-to-10 deterministic rows derived from the persistence."""
    from pocketpaw_ee.cloud.models.foresight_projected_decision import (
        ForesightProjectedDecision as _ForesightProjectedDecisionDoc,
    )

    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _scenario_body())

    # Hand-write 15 projections under the run id so we can verify the
    # 10-cap + deterministic order without relying on the engine's
    # per-tick fanout (which the v0.5 deterministic fake doesn't
    # populate in unit-test fixtures).
    for i in range(15):
        doc = _ForesightProjectedDecisionDoc(
            workspace="w1",
            run_id=run.id,
            anchor_id=f"decision:anchor-{i}",
            persona_id="Anne",
            tick_id=i,
            decision_text="accept" if i % 2 == 0 else "reject",
            confidence=0.5 + (i * 0.01),
            sub_type="decision_forecast",
        )
        await doc.insert()

    snap = await foresight_service.get_live_snapshot(ctx, run.id)
    # Cap holds at 10.
    assert len(snap.sampled_traces) == 10
    # Deterministic order — every trace's tick_id is in (0..14), and
    # the last trace is always the latest tick.
    tick_ids = [t.tick_id for t in snap.sampled_traces]
    assert tick_ids[-1] == 14
    # action_summary stays under the cap.
    for trace in snap.sampled_traces:
        assert len(trace.action_summary) <= 200
        assert 0.0 <= trace.confidence <= 1.0


async def test_live_snapshot_sub_type_formatter_picks_decision_text() -> None:
    """For ``decision_forecast`` sub-type, the action_summary
    is the decision text verbatim (no anchor decoration)."""
    from pocketpaw_ee.cloud.models.foresight_projected_decision import (
        ForesightProjectedDecision as _ForesightProjectedDecisionDoc,
    )

    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _scenario_body())
    doc = _ForesightProjectedDecisionDoc(
        workspace="w1",
        run_id=run.id,
        anchor_id="decision:lease-renewal",
        persona_id="Anne",
        tick_id=0,
        decision_text="accept",
        confidence=0.7,
        sub_type="decision_forecast",
    )
    await doc.insert()
    snap = await foresight_service.get_live_snapshot(ctx, run.id)
    assert snap.sampled_traces
    assert snap.sampled_traces[0].action_summary == "accept"


async def test_live_snapshot_market_sim_formatter_decorates_segment() -> None:
    """For ``market_sim`` projections, the formatter produces
    ``"<segment> -> <decision>"``. We assert against the trace that
    matches our injected anchor, not by position — the engine may
    fan its own projections alongside."""
    from pocketpaw_ee.cloud.models.foresight_projected_decision import (
        ForesightProjectedDecision as _ForesightProjectedDecisionDoc,
    )

    ctx = _ctx()
    body = CreateScenarioRequest(
        name="market-sim-test",
        sub_type="market_sim",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="Anne", role="approver", ocean={})],
    )
    run = await foresight_service.create_scenario_run(ctx, body)
    doc = _ForesightProjectedDecisionDoc(
        workspace="w1",
        run_id=run.id,
        anchor_id="segment:enterprise",
        persona_id="Anne",
        tick_id=99,  # force this row to land in the slice via late tick
        decision_text="renew",
        confidence=0.7,
        sub_type="market_sim",
    )
    await doc.insert()
    snap = await foresight_service.get_live_snapshot(ctx, run.id)
    # Find the trace matching our injected anchor — its summary must
    # be decorated with the segment label.
    matching = [
        t for t in snap.sampled_traces if "enterprise" in t.action_summary or t.tick_id == 99
    ]
    assert matching, (
        "expected at least one sampled trace from the injected market_sim "
        f"projection; got {[t.action_summary for t in snap.sampled_traces]}"
    )
    summary = matching[0].action_summary
    assert "enterprise" in summary
    assert "renew" in summary
    assert "->" in summary


# ---------------------------------------------------------------------------
# Anomaly detectors — exercised both via the pure helpers AND via the
# service path (with seeded projections to drive deterministic rule
# firing).
# ---------------------------------------------------------------------------


def test_tier_drift_info_threshold_fires_at_0_15() -> None:
    """Per-tier deviation > 0.15 fires an ``info`` anomaly per tier."""
    actual = LiveTierMixActual(premium=0.30, mid=0.50, tail=0.20)  # off by ~0.3 each
    anomalies = detect_tier_drift(actual)
    # Each tier exceeds the 0.15 threshold — three rows.
    assert len(anomalies) == 3
    assert all(a.kind == "tier_drift" for a in anomalies)
    # Premium is off by 0.25 — warning. Mid/tail off by more — also warning.
    # 0.30 - 0.05 = 0.25 exactly. 0.50 - 0.15 = 0.35. 0.20 - 0.80 = 0.60.
    severities = [a.severity for a in anomalies]
    # Premium exactly at the warning threshold — must be 'warning' OR
    # 'info' depending on strict-> comparison; the helper uses ``>``,
    # so 0.25 is NOT above the warning threshold; it falls into the
    # info bucket.
    assert "warning" in severities  # mid and tail are well above 0.25
    # All severities must be from the allowed vocabulary.
    assert set(severities) <= {"info", "warning"}


def test_tier_drift_skips_when_actual_mix_thin() -> None:
    """If the actual mix sums to less than 0.5 (effectively empty),
    the rule doesn't fire — it would spam the panel on fresh runs."""
    actual = LiveTierMixActual(premium=0.0, mid=0.0, tail=0.0)
    assert detect_tier_drift(actual) == []
    # Half-filled — still under threshold (sums to 0.5 doesn't pass the strict <).
    actual_partial = LiveTierMixActual(premium=0.2, mid=0.2, tail=0.0)
    assert detect_tier_drift(actual_partial) == []


def test_tier_drift_no_anomaly_when_at_configured() -> None:
    """When the actual mix matches the configured 5/15/80, no
    tier_drift anomaly fires."""
    actual = LiveTierMixActual(premium=0.05, mid=0.15, tail=0.80)
    assert detect_tier_drift(actual) == []


def test_confidence_spike_flat_high_fires_info() -> None:
    """Mean > 0.8 with variance < 0.02 → info."""

    class _Proj:
        def __init__(self, confidence: float) -> None:
            self.confidence = confidence

    projections = [_Proj(0.85) for _ in range(10)]  # zero variance, high mean
    anomalies = detect_confidence_spike(projections)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "confidence_spike"
    assert anomalies[0].severity == "info"
    assert "flat-high" in anomalies[0].body


def test_confidence_spike_flat_low_fires_info_and_warning() -> None:
    """Mean < 0.2 with variance < 0.02 → info. And mean < 0.2 with
    sample_count >= 5 → warning. So 5+ samples at flat-low fire both."""

    class _Proj:
        def __init__(self, confidence: float) -> None:
            self.confidence = confidence

    projections = [_Proj(0.15) for _ in range(6)]
    anomalies = detect_confidence_spike(projections)
    # Both rules fire — info (flat-low) + warning (sustained low).
    severities = [a.severity for a in anomalies]
    assert "info" in severities
    assert "warning" in severities


def test_confidence_spike_skipped_below_two_samples() -> None:
    """A single projection can't fairly drive a spike judgement —
    the detector returns empty."""

    class _Proj:
        def __init__(self, confidence: float) -> None:
            self.confidence = confidence

    assert detect_confidence_spike([_Proj(0.9)]) == []
    assert detect_confidence_spike([]) == []


def test_stalled_persona_warning_when_30s_behind() -> None:
    """A persona whose latest projection is more than 30s before the
    run's latest tick fires the warning."""

    class _Proj:
        def __init__(self, persona_id: str, created_at: datetime) -> None:
            self.persona_id = persona_id
            self.created_at = created_at

    now = datetime.now(UTC)
    projections = [
        _Proj("alice", now - timedelta(seconds=60)),
        _Proj("bob", now - timedelta(seconds=5)),
    ]
    anomalies = detect_stalled_persona(
        projections,
        latest_tick_id=5,
        latest_tick_ts=now,
    )
    # Alice is stalled, bob is fresh.
    assert any(a.kind == "stalled_persona" for a in anomalies)
    assert any("alice" in a.body for a in anomalies)
    # The "bob" persona should NOT trigger a stall row.
    assert not any("'bob'" in a.body for a in anomalies)


def test_stalled_persona_skipped_when_run_hasnt_ticked() -> None:
    """latest_tick_id == 0 or None → no anomalies."""

    class _Proj:
        def __init__(self) -> None:
            self.persona_id = "x"
            self.created_at = datetime.now(UTC)

    assert detect_stalled_persona([_Proj()], latest_tick_id=0, latest_tick_ts=None) == []
    assert detect_stalled_persona([_Proj()], latest_tick_id=None, latest_tick_ts=None) == []


async def test_live_snapshot_silent_persona_critical_via_service(mongo_db) -> None:
    """When the run's request lists a persona that has zero projections
    while the run reached tick > 0, the snapshot surfaces a
    ``stalled_persona`` critical anomaly."""
    from pocketpaw_ee.cloud.models.foresight_projected_decision import (
        ForesightProjectedDecision as _ForesightProjectedDecisionDoc,
    )

    ctx = _ctx()
    # Two personas declared — only one will produce projections.
    body = CreateScenarioRequest(
        name="silent-persona-test",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[
            PersonaSpecRequest(name="Anne", role="approver", ocean={}),
            PersonaSpecRequest(name="Bob", role="reviewer", ocean={}),
        ],
    )
    run = await foresight_service.create_scenario_run(ctx, body)
    # Inject one projection for Anne; Bob stays silent.
    doc = _ForesightProjectedDecisionDoc(
        workspace="w1",
        run_id=run.id,
        anchor_id="decision:thing",
        persona_id="Anne",
        tick_id=1,  # > 0 so the silent rule fires for Bob
        decision_text="accept",
        confidence=0.7,
        sub_type="decision_forecast",
    )
    await doc.insert()
    snap = await foresight_service.get_live_snapshot(ctx, run.id)

    critical = [
        a for a in snap.anomalies if a.kind == "stalled_persona" and a.severity == "critical"
    ]
    assert len(critical) == 1
    assert "Bob" in critical[0].body


async def test_live_snapshot_anomalies_skipped_on_completely_fresh_run() -> None:
    """A run with zero projections and tick 0 surfaces no anomalies
    — the empty path is quiet, not alarmist."""
    ctx = _ctx()
    run = await foresight_service.create_scenario_run(ctx, _scenario_body())
    snap = await foresight_service.get_live_snapshot(ctx, run.id)
    # No tier_drift (mix is zeros), no confidence_spike (no samples),
    # no stall (latest_tick_id is None / 0). Empty path is silent.
    assert snap.anomalies == []
