# tests/cloud/test_foresight_prediction_record.py
# Created: 2026-05-26 (feat/foresight-v10-prediction-record-persist) —
# RFC 08 v1.0 PR 10. Tests the new ``ForesightPredictionRecord`` Beanie
# document + the cloud service's ``emit_prediction_record`` /
# ``pair_prediction`` functions.
"""Tests for the PredictionRecord persistence layer (RFC 08 §9.1 + PR 10)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.domain import PredictionRecord
from pocketpaw_ee.cloud.models.foresight_prediction_record import (
    ForesightPredictionRecord,
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


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "anchor_id": "decision:lease-renewal",
        "persona_id": "p-anne",
        "tick_id": 0,
        "decision_text": "accept",
        "confidence": 0.7,
        "sub_type": "decision_forecast",
        "scenario_id": "renewal-test",
        "run_id": "67000000000000000000000a",
        "prediction": {"modal_outcome": "accept"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Domain shape
# ---------------------------------------------------------------------------


def test_prediction_record_domain_is_frozen_and_requires_workspace() -> None:
    """Cloud rule #3: ``workspace_id`` is positional, no default."""
    record = PredictionRecord(
        id="rec-1",
        workspace_id="w1",
        captured_at=datetime.now(UTC),
    )
    assert record.workspace_id == "w1"
    # Frozen dataclass — mutation must fail.
    with pytest.raises(Exception):
        record.workspace_id = "w2"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# emit_prediction_record — insert + idempotence
# ---------------------------------------------------------------------------


async def test_emit_prediction_record_inserts_row() -> None:
    """Happy path — one INSERT, returns the domain record with
    workspace + bucket key fields populated."""
    rec = await foresight_service.emit_prediction_record(
        workspace_id="w1",
        payload=_payload(),
    )
    assert isinstance(rec, PredictionRecord)
    assert rec.workspace_id == "w1"
    assert rec.anchor_id == "decision:lease-renewal"
    assert rec.persona_id == "p-anne"
    assert rec.scenario_id == "renewal-test"
    assert rec.run_id == "67000000000000000000000a"
    assert rec.tick_id == 0
    assert rec.confidence == 0.7
    assert rec.paired is False
    assert rec.observed_at is None
    assert rec.observed_outcome is None
    assert rec.pair_delta is None
    # Beanie persisted exactly one doc.
    docs = await ForesightPredictionRecord.find_all().to_list()
    assert len(docs) == 1


async def test_emit_prediction_record_is_idempotent() -> None:
    """Re-emit of the same (workspace, run_id, tick_id, anchor_id,
    persona_id) bucket returns the existing row instead of inserting
    a duplicate."""
    first = await foresight_service.emit_prediction_record(
        workspace_id="w1",
        payload=_payload(),
    )
    second = await foresight_service.emit_prediction_record(
        workspace_id="w1",
        payload=_payload(confidence=0.99),
    )
    assert first.id == second.id  # same Mongo row
    # confidence is the FIRST insertion's value (idempotent dedupe
    # returns the existing row without overwrite).
    assert second.confidence == 0.7
    docs = await ForesightPredictionRecord.find_all().to_list()
    assert len(docs) == 1


async def test_emit_prediction_record_distinct_buckets() -> None:
    """Different bucket quintuples produce separate rows."""
    await foresight_service.emit_prediction_record(
        workspace_id="w1",
        payload=_payload(tick_id=0),
    )
    await foresight_service.emit_prediction_record(
        workspace_id="w1",
        payload=_payload(tick_id=1),  # different tick
    )
    await foresight_service.emit_prediction_record(
        workspace_id="w1",
        payload=_payload(anchor_id="decision:other-lease"),  # different anchor
    )
    docs = await ForesightPredictionRecord.find_all().to_list()
    assert len(docs) == 3


async def test_emit_prediction_record_workspace_isolation() -> None:
    """Same bucket key in a different workspace is a separate row —
    cloud rule #3 + #7 tenancy: tenant filter on every read."""
    await foresight_service.emit_prediction_record(
        workspace_id="w1",
        payload=_payload(),
    )
    await foresight_service.emit_prediction_record(
        workspace_id="w2",
        payload=_payload(),
    )
    w1_docs = await ForesightPredictionRecord.find({"workspace": "w1"}).to_list()
    w2_docs = await ForesightPredictionRecord.find({"workspace": "w2"}).to_list()
    assert len(w1_docs) == 1
    assert len(w2_docs) == 1


async def test_emit_prediction_record_requires_workspace() -> None:
    """Empty workspace_id raises Forbidden (consistent with the rest
    of the foresight service)."""
    with pytest.raises(Forbidden):
        await foresight_service.emit_prediction_record(
            workspace_id="",
            payload=_payload(),
        )


# ---------------------------------------------------------------------------
# pair_prediction — observation stamping
# ---------------------------------------------------------------------------


async def test_pair_prediction_flips_paired_flag() -> None:
    """Pair an existing record — paired=True, observed_at + outcome +
    pair_delta stamped."""
    rec = await foresight_service.emit_prediction_record(
        workspace_id="w1",
        payload=_payload(),
    )
    assert rec.paired is False

    paired = await foresight_service.pair_prediction(
        workspace_id="w1",
        record_id=rec.id,
        observed_outcome={"outcome": "accept"},
        pair_delta={"outcome": {"match": True, "projected": "accept", "actual": "accept"}},
    )
    assert paired.paired is True
    assert paired.observed_outcome == {"outcome": "accept"}
    assert paired.pair_delta == {
        "outcome": {"match": True, "projected": "accept", "actual": "accept"}
    }
    assert paired.observed_at is not None


async def test_pair_prediction_unknown_id_raises_not_found() -> None:
    """Unknown / malformed ids collapse to NotFound — existence not
    leakable."""
    with pytest.raises(NotFound):
        await foresight_service.pair_prediction(
            workspace_id="w1",
            record_id="not-an-objectid",
            observed_outcome={},
        )


async def test_pair_prediction_cross_tenant_collapses_to_not_found() -> None:
    """A record in w2 cannot be paired by a w1 caller — collapses to
    NotFound rather than leaking existence across tenants."""
    rec = await foresight_service.emit_prediction_record(
        workspace_id="w2",
        payload=_payload(),
    )
    with pytest.raises(NotFound):
        await foresight_service.pair_prediction(
            workspace_id="w1",
            record_id=rec.id,
            observed_outcome={"outcome": "accept"},
        )
    # The original w2 row stays unpaired.
    untouched = await ForesightPredictionRecord.get(rec.id)
    assert untouched is not None
    assert untouched.paired is False


# ---------------------------------------------------------------------------
# Engine → cloud → Mongo end-to-end smoke
# ---------------------------------------------------------------------------


async def test_create_scenario_run_persists_prediction_records() -> None:
    """End-to-end: a forward scenario run drives the engine, which
    fires the ``on_prediction_record`` callback for each
    (anchor × tick) bucket, which lands one row in
    ``foresight_prediction_records`` per bucket. This is the load-
    bearing wire that proves the proxy → real data-source swap is
    self-consistent — the engine writes the records the aggregate
    endpoint reads.
    """
    from pocketpaw_ee.cloud.foresight.dto import (
        CreateScenarioRequest,
        PersonaSpecRequest,
    )

    ctx = _ctx()
    body = CreateScenarioRequest(
        name="renewal-smoke",
        sub_type="decision_forecast",
        n_ticks=2,
        personas=[PersonaSpecRequest(name="Anne", role="approver", ocean={})],
    )
    run = await foresight_service.create_scenario_run(ctx, body)
    assert run.status == "complete"

    # 1 anchor × 2 ticks = 2 prediction records persisted.
    docs = await ForesightPredictionRecord.find_all().to_list()
    assert len(docs) == 2
    for doc in docs:
        assert doc.workspace == "w1"
        assert doc.scenario_id == "renewal-smoke"
        assert doc.run_id == run.id
        assert doc.anchor_id == "decision:renewal-smoke"
        # Forward sim — records remain unpaired until reality lands.
        assert doc.paired is False
        # Prediction blob carries the modal outcome the rollup tallies.
        assert "modal_outcome" in doc.prediction


async def test_create_backtest_persists_paired_prediction_records() -> None:
    """End-to-end: a backtest run completes scoring, persists one
    PAIRED prediction record per anchor (with ``pair_delta`` stamped
    from the in-memory aggregator pass). This is what the
    rolling-accuracy series reads — without these paired rows the
    §11.5 endpoint can't return real data.
    """
    from pocketpaw_ee.cloud.foresight.dto import (
        CreateBacktestRequest,
        HistoricalAnchorRequest,
        PersonaSpecRequest,
    )

    ctx = _ctx()
    body = CreateBacktestRequest(
        name="onboarding-bt-smoke",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="Anne", role="approver", ocean={})],
        anchors=[
            HistoricalAnchorRequest(
                anchor_object_id=f"lease:LR-{i}",
                actual_outcome={"outcome": "accept"},
                scenario_template="decision_forecast.yaml",
                projection_confidence=0.7,
            )
            for i in range(3)
        ],
    )
    bt = await foresight_service.create_backtest(ctx, body)
    assert bt.status == "complete"

    # Paired records — one per backtest anchor. The forward-sim engine
    # call also writes its own per-(anchor × tick) records; the
    # aggregator path reads paired=True only, which is what the §11.5
    # rolling-accuracy series counts on.
    paired_docs = await ForesightPredictionRecord.find({"paired": True}).to_list()
    assert len(paired_docs) >= 3
    for doc in paired_docs:
        assert doc.observed_at is not None
        assert doc.observed_outcome == {"outcome": "accept"}
        # pair_delta carries the engine's compute_delta output —
        # match flag drives the rolling-accuracy match share.
        assert doc.pair_delta is not None
