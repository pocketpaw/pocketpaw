# tests/cloud/test_foresight_service_insights_llm.py
# Created: 2026-05-26 (feat/foresight-v10-insights-llm) — RFC 08 v1.0.
# Service-level tests for the LLM toggle on ``get_insights``:
#   - synthesizer="pattern" → existing five-rule path runs unchanged
#     (regression guard).
#   - synthesizer="llm" + valid backend → LLM-driven insights surface
#     through the wire response with the right shape.
#   - synthesizer="llm" + backend failure (rate-limit, malformed JSON,
#     empty output) → falls back to the pattern synthesizer so the wire
#     response never 5xxs.
#   - Tenant filter applies: an LLM call for w1 must NOT leak into w2's
#     insight set.
"""Cloud-side ``get_insights`` LLM branch tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import SetForesightInsightsConfigRequest
from pocketpaw_ee.cloud.models.foresight_prediction_record import (
    ForesightPredictionRecord,
)
from pocketpaw_ee.foresight import insights_llm

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
    persona_id: str = "alice",
    confidence: float = 0.3,
    captured_at: datetime | None = None,
    paired: bool = True,
    run_id: str = "r-1",
    tick_id: int = 0,
) -> None:
    if captured_at is None:
        captured_at = datetime.now(UTC) - timedelta(days=1)
    doc = ForesightPredictionRecord(
        workspace=workspace,
        anchor_id="decision:lease",
        persona_id=persona_id,
        scenario_id="seed",
        run_id=run_id,
        tick_id=tick_id,
        prediction={"modal_outcome": "accept"},
        confidence=confidence,
        captured_at=captured_at,
        observed_at=captured_at + timedelta(seconds=1) if paired else None,
        observed_outcome={"outcome": "reject"} if paired else None,
        paired=paired,
        pair_delta={"outcome": {"match": False, "projected": "accept", "actual": "reject"}}
        if paired
        else None,
    )
    await doc.insert()


class _StubLLMBackend:
    """Mock LLM backend for service-level tests."""

    def __init__(self, *, responses: list[str], raise_on_call: Exception | None = None) -> None:
        self._responses = list(responses)
        self._raise = raise_on_call
        self.calls: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._raise is not None:
            raise self._raise
        if not self._responses:
            return ""
        return self._responses.pop(0)


def _valid_llm_payload() -> str:
    return json.dumps(
        {
            "insights": [
                {
                    "kind": "trend_explainer",
                    "title": "Persona alice underperforms",
                    "body": "Alice's recent calibration falls below the 0.50 floor.",
                    "severity": "warning",
                    "anchor_refs": ["persona:alice"],
                }
            ]
        }
    )


@pytest.fixture(autouse=True)
def _reset_llm_state() -> None:
    """Reset the LLM cache + the test-only backend override between tests."""
    insights_llm.reset_cache()
    foresight_service._set_llm_backend_for_testing(None)
    yield
    insights_llm.reset_cache()
    foresight_service._set_llm_backend_for_testing(None)


# ---------------------------------------------------------------------------
# Pattern path (default) — regression guard
# ---------------------------------------------------------------------------


async def test_get_insights_default_uses_pattern_synthesizer() -> None:
    """No config doc → synthesizer="pattern" → existing five-rule path."""
    # Seed enough prediction records that persona_outlier would fire on
    # the pattern path.
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(3):
        await _seed_record(
            persona_id="alice",
            confidence=0.2,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )

    # Backend should NEVER be called on the pattern path; install a
    # noisy stub to prove it.
    raising = _StubLLMBackend(responses=[], raise_on_call=RuntimeError("must not be called"))
    foresight_service._set_llm_backend_for_testing(raising)

    response = await foresight_service.get_insights(_ctx())
    persona_outliers = [i for i in response.items if i.kind == "persona_outlier"]
    assert len(persona_outliers) >= 1
    assert raising.calls == []


# ---------------------------------------------------------------------------
# LLM path — happy path
# ---------------------------------------------------------------------------


async def test_get_insights_with_llm_synthesizer_uses_llm_backend() -> None:
    """synthesizer="llm" → backend is invoked and its output reaches the wire."""
    # Seed enough records to make the LLM call meaningful.
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(5):
        await _seed_record(
            persona_id="alice",
            confidence=0.4,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )

    # Flip the workspace toggle to "llm".
    await foresight_service.set_insights_config(
        _ctx(),
        SetForesightInsightsConfigRequest(synthesizer="llm"),
    )
    backend = _StubLLMBackend(responses=[_valid_llm_payload()])
    foresight_service._set_llm_backend_for_testing(backend)

    response = await foresight_service.get_insights(_ctx())
    assert len(backend.calls) == 1
    # LLM-driven insights surface with their own kinds.
    kinds = {i.kind for i in response.items}
    assert "trend_explainer" in kinds
    # Stable id discipline — LLM ids carry the llm_ prefix.
    assert any(i.id.startswith("llm_") for i in response.items)


# ---------------------------------------------------------------------------
# LLM failure → fallback to pattern
# ---------------------------------------------------------------------------


async def test_get_insights_with_llm_falls_back_to_pattern_on_failure() -> None:
    """LLM call raises (rate-limit) → pattern rules still produce output."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(3):
        await _seed_record(
            persona_id="alice",
            confidence=0.2,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )

    await foresight_service.set_insights_config(
        _ctx(),
        SetForesightInsightsConfigRequest(synthesizer="llm"),
    )
    # Backend raises — synthesize_insights_llm returns [] which the
    # cloud service interprets as "fall back to pattern".
    backend = _StubLLMBackend(responses=[], raise_on_call=RuntimeError("rate limit"))
    foresight_service._set_llm_backend_for_testing(backend)

    response = await foresight_service.get_insights(_ctx())
    # Backend was called once.
    assert len(backend.calls) == 1
    # Pattern-rule output survives — persona_outlier should fire.
    persona_outliers = [i for i in response.items if i.kind == "persona_outlier"]
    assert len(persona_outliers) >= 1


async def test_get_insights_with_llm_falls_back_on_malformed_json() -> None:
    """LLM returns garbage → synthesize_insights_llm returns [] → fallback."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(3):
        await _seed_record(
            persona_id="alice",
            confidence=0.2,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )

    await foresight_service.set_insights_config(
        _ctx(),
        SetForesightInsightsConfigRequest(synthesizer="llm"),
    )
    backend = _StubLLMBackend(responses=["not json {malformed"])
    foresight_service._set_llm_backend_for_testing(backend)

    response = await foresight_service.get_insights(_ctx())
    assert len(backend.calls) == 1
    persona_outliers = [i for i in response.items if i.kind == "persona_outlier"]
    assert len(persona_outliers) >= 1


async def test_get_insights_with_llm_empty_workspace_returns_empty_items() -> None:
    """LLM path on an empty workspace → ``items=[]`` (both LLM AND
    pattern produce nothing on no data)."""
    await foresight_service.set_insights_config(
        _ctx(),
        SetForesightInsightsConfigRequest(synthesizer="llm"),
    )
    backend = _StubLLMBackend(responses=[json.dumps({"insights": []})])
    foresight_service._set_llm_backend_for_testing(backend)

    response = await foresight_service.get_insights(_ctx())
    assert response.items == []


# ---------------------------------------------------------------------------
# Tenant isolation on the LLM path
# ---------------------------------------------------------------------------


async def test_get_insights_with_llm_isolates_across_workspaces() -> None:
    """w1's LLM call doesn't poison w2's cache; toggle is per-workspace."""
    # Seed both workspaces with their own data.
    await _seed_record(workspace="w1", persona_id="alice", run_id="r-1", tick_id=0)
    await _seed_record(workspace="w1", persona_id="alice", run_id="r-1", tick_id=1)
    await _seed_record(workspace="w2", persona_id="bob", run_id="r-1", tick_id=0)
    await _seed_record(workspace="w2", persona_id="bob", run_id="r-1", tick_id=1)

    # Only w1 opts into LLM.
    await foresight_service.set_insights_config(
        _ctx(workspace="w1"),
        SetForesightInsightsConfigRequest(synthesizer="llm"),
    )

    backend = _StubLLMBackend(responses=[_valid_llm_payload()])
    foresight_service._set_llm_backend_for_testing(backend)

    # w1 → LLM path
    w1_response = await foresight_service.get_insights(_ctx(workspace="w1"))
    assert any(i.id.startswith("llm_") for i in w1_response.items)
    assert len(backend.calls) == 1

    # w2 → pattern path (didn't flip toggle); backend NOT called again.
    w2_response = await foresight_service.get_insights(_ctx(workspace="w2"))
    assert all(not i.id.startswith("llm_") for i in w2_response.items)
    assert len(backend.calls) == 1  # still 1 — no extra invocation
    # w2 sees pattern insights for its OWN persona, not w1's alice.
    assert all("alice" not in (i.body + i.title) for i in w2_response.items)


# ---------------------------------------------------------------------------
# Cache hit on the service path
# ---------------------------------------------------------------------------


async def test_get_insights_with_llm_caches_repeat_calls() -> None:
    """Repeat GET on stable data → one backend call (cache hit second time)."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(3):
        await _seed_record(
            persona_id="alice",
            confidence=0.4,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )

    await foresight_service.set_insights_config(
        _ctx(),
        SetForesightInsightsConfigRequest(synthesizer="llm"),
    )
    backend = _StubLLMBackend(
        responses=[_valid_llm_payload(), _valid_llm_payload()],
    )
    foresight_service._set_llm_backend_for_testing(backend)

    first = await foresight_service.get_insights(_ctx())
    second = await foresight_service.get_insights(_ctx())
    assert [i.id for i in first.items] == [i.id for i in second.items]
    assert len(backend.calls) == 1


# ---------------------------------------------------------------------------
# synth_source wire field — tracks the synthesizer that ACTUALLY produced
# the rows the caller is reading. Default "pattern" covers the untoggled
# workspace + the LLM-empty fallback path; only a non-empty LLM run flips
# it to "llm".
# ---------------------------------------------------------------------------


async def test_get_insights_default_synth_source_is_pattern() -> None:
    """No config doc → response advertises ``synth_source = "pattern"``."""
    response = await foresight_service.get_insights(_ctx())
    assert response.synth_source == "pattern"


async def test_get_insights_pattern_when_config_pattern() -> None:
    """Config explicitly set to "pattern" → ``synth_source = "pattern"``."""
    await foresight_service.set_insights_config(
        _ctx(),
        SetForesightInsightsConfigRequest(synthesizer="pattern"),
    )
    response = await foresight_service.get_insights(_ctx())
    assert response.synth_source == "pattern"


async def test_get_insights_llm_source_when_llm_produces_output() -> None:
    """Config "llm" + LLM returns non-empty → ``synth_source = "llm"``."""
    # Seed records so the LLM helper has something to summarize. The
    # canned payload is what determines the response items; the seeds
    # just keep the helper from short-circuiting on empty input.
    captured_at = datetime.now(UTC) - timedelta(days=1)
    for i in range(3):
        await _seed_record(
            persona_id="alice",
            confidence=0.4,
            run_id=f"r-{i}",
            tick_id=i,
            captured_at=captured_at,
        )

    await foresight_service.set_insights_config(
        _ctx(),
        SetForesightInsightsConfigRequest(synthesizer="llm"),
    )
    backend = _StubLLMBackend(responses=[_valid_llm_payload()])
    foresight_service._set_llm_backend_for_testing(backend)

    response = await foresight_service.get_insights(_ctx())
    assert response.synth_source == "llm"
    # Sanity: an item with the llm_ id prefix is in there — proves the
    # LLM path ran, not just the toggle reading.
    assert any(i.id.startswith("llm_") for i in response.items)


async def test_get_insights_pattern_source_when_llm_returns_empty() -> None:
    """Config "llm" + LLM returns ``[]`` → fallback → ``synth_source = "pattern"``.

    The user is reading pattern-synthesizer rows (or none, if both
    paths are empty) regardless of the config toggle; the wire source
    has to match what the user actually sees.
    """
    await foresight_service.set_insights_config(
        _ctx(),
        SetForesightInsightsConfigRequest(synthesizer="llm"),
    )
    # LLM helper collapses `{"insights": []}` into an empty list,
    # which the service then treats as "fallback to pattern".
    backend = _StubLLMBackend(responses=[json.dumps({"insights": []})])
    foresight_service._set_llm_backend_for_testing(backend)

    response = await foresight_service.get_insights(_ctx())
    assert response.synth_source == "pattern"
    # Items reflect the pattern synthesizer. With no seeded records the
    # pattern path also produces nothing — the response is correctly
    # empty AND correctly labelled as the pattern source.
    assert all(not i.id.startswith("llm_") for i in response.items)


async def test_get_insights_requires_workspace_under_llm_path() -> None:
    """The tenancy guard still fires on the LLM branch."""
    from pocketpaw_ee.cloud._core.errors import Forbidden

    with pytest.raises(Forbidden):
        await foresight_service.get_insights(_ctx(workspace=None))
