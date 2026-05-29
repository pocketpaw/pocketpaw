# tests/cloud/test_foresight_service_insights.py
# Created: 2026-05-26 (feat/foresight-v10-prediction-record-persist) —
# RFC 08 v1.0 PR 10. Verifies the rewritten ``get_insights`` reads
# PredictionRecord docs (NOT the v0.5 ForesightProjectedDecision
# proxy) for the per-persona calibration synthesizer input.
"""Tests for ``get_insights`` reading PredictionRecord docs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.models.foresight_prediction_record import (
    ForesightPredictionRecord,
)
from pocketpaw_ee.cloud.models.foresight_projected_decision import (
    ForesightProjectedDecision,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _seed_record(
    *,
    workspace: str = "w1",
    persona_id: str = "p-anne",
    confidence: float = 0.7,
    captured_at: datetime | None = None,
    run_id: str = "run-1",
    tick_id: int = 0,
    anchor_id: str = "decision:lease",
    paired: bool = True,
) -> ForesightPredictionRecord:
    if captured_at is None:
        captured_at = datetime.now(UTC) - timedelta(days=1)
    doc = ForesightPredictionRecord(
        workspace=workspace,
        anchor_id=anchor_id,
        persona_id=persona_id,
        scenario_id="seed",
        run_id=run_id,
        tick_id=tick_id,
        prediction={"modal_outcome": "accept"},
        confidence=confidence,
        captured_at=captured_at,
        observed_at=captured_at + timedelta(seconds=1) if paired else None,
        observed_outcome={"outcome": "accept"} if paired else None,
        paired=paired,
        pair_delta={"outcome": {"match": True, "projected": "accept", "actual": "accept"}}
        if paired
        else None,
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# Reads PredictionRecord (NOT ForesightProjectedDecision proxy)
# ---------------------------------------------------------------------------


async def test_insights_reads_prediction_records_not_projection_proxy() -> None:
    """v0.5 ``get_insights`` derived per-persona calibration from
    ``ForesightProjectedDecision.confidence``. v1.0 sources from
    ``ForesightPredictionRecord.confidence``. We verify the swap by
    seeding ZERO projected-decision docs but enough prediction
    records to trigger the persona_outlier rule.
    """
    # 3 low-confidence records for a single persona — meets the
    # synthesizer's >= 2 sample_count gate and falls below the 0.50
    # floor → persona_outlier insight should fire.
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(3):
        await _seed_record(
            persona_id="p-low-conf",
            confidence=0.2,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )

    # No ForesightProjectedDecision docs exist — the v0.5 path would
    # have produced empty per_persona_calibration here.
    projection_docs = await ForesightProjectedDecision.find_all().to_list()
    assert projection_docs == []

    response = await foresight_service.get_insights(_ctx())
    # The response shape stays — items is a flat list.
    assert isinstance(response.items, list)
    # At least one persona_outlier insight surfaced from the
    # PredictionRecord data.
    persona_outliers = [item for item in response.items if item.kind == "persona_outlier"]
    assert len(persona_outliers) >= 1
    # Insight body / anchor refs mention the low-confidence persona.
    persona_ids_in_refs = {ref for insight in persona_outliers for ref in insight.anchor_refs}
    assert any("p-low-conf" in ref for ref in persona_ids_in_refs)


async def test_insights_empty_workspace_returns_empty_items() -> None:
    """Empty-workspace contract stays — PR 10 must not regress this."""
    response = await foresight_service.get_insights(_ctx())
    assert response.items == []


async def test_insights_workspace_isolation() -> None:
    """w2 seeded prediction records do not leak into w1's insight set."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(3):
        await _seed_record(
            workspace="w2",
            persona_id="p-isolated",
            confidence=0.2,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )
    response = await foresight_service.get_insights(_ctx(workspace="w1"))
    # w1 has no records → empty insights.
    assert response.items == []


async def test_insights_response_shape_unchanged() -> None:
    """Wire-shape backward-compat — every insight row carries the
    locked field set v0.5 emitted."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(3):
        await _seed_record(
            persona_id="p-shape",
            confidence=0.2,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )
    response = await foresight_service.get_insights(_ctx())
    for item in response.items:
        payload = item.model_dump()
        assert {
            "id",
            "kind",
            "title",
            "body",
            "severity",
            "anchor_refs",
            "generated_at",
        } <= set(payload.keys())
        assert item.severity in ("info", "warning", "critical")


async def test_insights_per_persona_excludes_thin_samples() -> None:
    """A persona with a SINGLE record doesn't surface as a calibration
    outlier — the synthesizer needs >= 2 samples per persona to fire
    persona_outlier without being noise-dominated."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    # Only 1 record for this persona — should be filtered out.
    await _seed_record(
        persona_id="p-thin",
        confidence=0.1,
        run_id="r-thin",
        captured_at=captured_at,
    )
    response = await foresight_service.get_insights(_ctx())
    persona_outliers = [item for item in response.items if item.kind == "persona_outlier"]
    # No persona_outlier insight for the thin-sample persona.
    for insight in persona_outliers:
        assert not any("p-thin" in ref for ref in insight.anchor_refs)
