# tests/ee/foresight/test_insights_llm.py
# Created: 2026-05-26 (feat/foresight-v10-insights-llm) — RFC 08 v1.0.
# Unit tests for the LLM-driven insights synthesizer in
# ``ee/pocketpaw_ee/foresight/insights_llm.py``. Exercises:
#   - Happy path: backend returns valid JSON → list of Insight rows.
#   - Malformed JSON output → returns ``[]`` (the cloud-side
#     ``get_insights`` then falls back to pattern rules).
#   - Partial-valid output: some items pass validation, others get
#     skipped with a warning.
#   - Severity coercion: unknown severity values land as ``"info"`` so
#     the UI's severity → colour mapping stays defined.
#   - Backend exceptions (rate-limit / connection) → returns ``[]``.
#   - Cache hit: second call with same input data returns cached output
#     without re-calling the backend; the cache is workspace-scoped so
#     a second workspace's call doesn't poison the first's cache.
#   - Stable id discipline: same input always yields the same id;
#     differently-prompted output yields different ids.
"""Unit tests for the LLM-driven foresight insights synthesizer."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pocketpaw_ee.foresight.insights import (
    ConfidenceDriftInput,
    LatestBacktestGate,
    PerPersonaCalibration,
    RollingAccuracyPoint,
    TierDistributionDelta,
)
from pocketpaw_ee.foresight.insights_llm import (
    LLM_KIND_VOCABULARY,
    LLMInsightsInput,
    RecentPredictionRecordSummary,
    configure_cache,
    reset_cache,
    synthesize_insights_llm,
)

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
_DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    """Ensure each test sees a clean LRU + the module-default TTL."""
    reset_cache()
    yield
    reset_cache()


def _make_bundle(
    *,
    workspace_id: str = "w-test",
    period_key: str = "2026_W21",
) -> LLMInsightsInput:
    """Build a representative bundle with one insight in every input."""
    return LLMInsightsInput(
        workspace_id=workspace_id,
        period_key=period_key,
        now=_NOW,
        rolling_accuracy=(
            RollingAccuracyPoint(ts=_NOW - 7 * _DAY, accuracy=0.80, sample_count=40),
            RollingAccuracyPoint(ts=_NOW, accuracy=0.55, sample_count=42),
        ),
        confidence_drift=ConfidenceDriftInput(trend="falling", magnitude=0.30),
        per_persona_calibration=(
            PerPersonaCalibration(persona_id="alice", calibration=0.40, sample_count=8),
            PerPersonaCalibration(persona_id="bob", calibration=0.72, sample_count=12),
        ),
        tier_distribution_deltas=(
            TierDistributionDelta(tier="premium", configured=0.05, actual=0.30),
            TierDistributionDelta(tier="tail", configured=0.80, actual=0.55),
        ),
        latest_backtest=LatestBacktestGate(
            backtest_id="bt-1",
            passed=False,
            observed=0.55,
            threshold=0.65,
            completed_at=_NOW - 2 * _DAY,
        ),
        recent_records=(
            RecentPredictionRecordSummary(
                anchor_id="decision:lease",
                persona_id="alice",
                modal_outcome="accept",
                confidence=0.4,
                paired=True,
                observed_outcome="reject",
                captured_at=_NOW - _DAY,
            ),
        ),
    )


class _StubBackend:
    """Mock LLM backend with scriptable responses + call counting."""

    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._raise = raise_on_call
        self.calls: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._raise is not None:
            raise self._raise
        if not self._responses:
            return ""
        return self._responses.pop(0)


def _valid_json_payload(*, count: int = 3) -> str:
    """Build a representative LLM response payload."""
    items: list[dict[str, Any]] = []
    for i in range(count):
        items.append(
            {
                "kind": "trend_explainer" if i == 0 else "outlier_cluster",
                "title": f"Insight {i}",
                "body": f"Reasoning for insight {i} referencing persona:alice",
                "severity": "warning" if i % 2 == 0 else "info",
                "anchor_refs": ["persona:alice"] if i == 0 else [],
            }
        )
    return json.dumps({"insights": items})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_synthesize_happy_path_returns_insights() -> None:
    """Backend returns valid JSON → parsed list of Insight records."""
    backend = _StubBackend(responses=[_valid_json_payload(count=3)])
    bundle = _make_bundle()
    out = await synthesize_insights_llm(bundle, backend)
    assert len(out) == 3
    # Sorted by severity desc (warning > info) then generated_at desc.
    severities = [i.severity for i in out]
    assert severities[0] == "warning"
    # Each insight has a stable id with the ``llm_`` prefix.
    assert all(i.id.startswith("llm_") for i in out)
    # Backend was called once.
    assert len(backend.calls) == 1
    # Prompt mentions the workspace data — the rolling accuracy + drift
    # + persona ids appear in the prompt JSON section.
    prompt = backend.calls[0]
    assert "alice" in prompt
    assert "decision:lease" in prompt
    assert "trend_explainer" in prompt  # kind vocabulary echoed


async def test_synthesize_anchors_passed_through() -> None:
    """``anchor_refs`` from the LLM payload survives parsing."""
    backend = _StubBackend(
        responses=[
            json.dumps(
                {
                    "insights": [
                        {
                            "kind": "persona_pattern",
                            "title": "Alice underperforms",
                            "body": "Alice's calibration is 0.40 below the 0.50 floor.",
                            "severity": "warning",
                            "anchor_refs": ["persona:alice", "tier:premium"],
                        }
                    ]
                }
            )
        ]
    )
    out = await synthesize_insights_llm(_make_bundle(), backend)
    assert len(out) == 1
    assert out[0].anchor_refs == ("persona:alice", "tier:premium")
    assert out[0].kind == "persona_pattern"


# ---------------------------------------------------------------------------
# Error paths — ALL collapse to []
# ---------------------------------------------------------------------------


async def test_synthesize_returns_empty_on_malformed_json() -> None:
    """Output that isn't JSON returns ``[]`` so the caller falls back."""
    backend = _StubBackend(responses=["not actually json {{"])
    out = await synthesize_insights_llm(_make_bundle(), backend)
    assert out == []


async def test_synthesize_returns_empty_on_empty_output() -> None:
    backend = _StubBackend(responses=[""])
    out = await synthesize_insights_llm(_make_bundle(), backend)
    assert out == []


async def test_synthesize_returns_empty_on_array_at_top_level() -> None:
    """Top-level must be an object — array shape is invalid."""
    backend = _StubBackend(responses=[json.dumps([{"kind": "x", "title": "y"}])])
    out = await synthesize_insights_llm(_make_bundle(), backend)
    assert out == []


async def test_synthesize_returns_empty_when_insights_field_missing() -> None:
    backend = _StubBackend(responses=[json.dumps({"items": []})])
    out = await synthesize_insights_llm(_make_bundle(), backend)
    assert out == []


async def test_synthesize_handles_backend_exception() -> None:
    """Backend raising → returns ``[]`` (fallback to pattern path)."""
    backend = _StubBackend(raise_on_call=RuntimeError("rate limited"))
    out = await synthesize_insights_llm(_make_bundle(), backend)
    assert out == []


async def test_synthesize_handles_backend_timeout() -> None:
    """``backend.complete`` hangs → the asyncio.wait_for timeout fires
    and the call returns ``[]`` so the caller can fall back."""

    class _HangingBackend:
        async def complete(self, prompt: str) -> str:  # noqa: ARG002
            await asyncio.sleep(60)  # longer than our timeout
            return ""

    out = await synthesize_insights_llm(
        _make_bundle(),
        _HangingBackend(),
        timeout_seconds=0.1,
    )
    assert out == []


async def test_synthesize_returns_empty_when_workspace_id_missing() -> None:
    """Defensive — empty workspace_id collapses to ``[]`` (cloud rule #3)."""
    bundle = LLMInsightsInput(
        workspace_id="",
        period_key="2026_W21",
        now=_NOW,
    )
    backend = _StubBackend(responses=[_valid_json_payload()])
    out = await synthesize_insights_llm(bundle, backend)
    assert out == []
    # Backend not called when the guard fires.
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Partial parse
# ---------------------------------------------------------------------------


async def test_synthesize_partial_valid_skips_bad_items() -> None:
    """Invalid items are skipped; valid siblings survive."""
    payload = {
        "insights": [
            {  # valid
                "kind": "trend_explainer",
                "title": "Good",
                "body": "Good reasoning",
                "severity": "warning",
            },
            {  # missing body
                "kind": "outlier_cluster",
                "title": "Bad",
                "severity": "info",
            },
            "not a dict",  # not even an object
            {  # severity coerced
                "kind": "persona_pattern",
                "title": "Severity-coerce",
                "body": "Reasoning",
                "severity": "extreme",
            },
            {  # empty title
                "kind": "data_quality",
                "title": "   ",
                "body": "x",
                "severity": "info",
            },
        ]
    }
    backend = _StubBackend(responses=[json.dumps(payload)])
    out = await synthesize_insights_llm(_make_bundle(), backend)
    # Two valid items survive (1st + 4th).
    assert len(out) == 2
    titles = {i.title for i in out}
    assert "Good" in titles
    assert "Severity-coerce" in titles
    # Severity coerces to "info".
    coerced = next(i for i in out if i.title == "Severity-coerce")
    assert coerced.severity == "info"


async def test_synthesize_handles_code_fenced_output() -> None:
    """LLMs commonly wrap JSON in ```json fences — the parser strips them."""
    payload = json.dumps(
        {
            "insights": [
                {
                    "kind": "trend_explainer",
                    "title": "Fenced output",
                    "body": "Reasoning",
                    "severity": "info",
                }
            ]
        }
    )
    backend = _StubBackend(responses=[f"```json\n{payload}\n```"])
    out = await synthesize_insights_llm(_make_bundle(), backend)
    assert len(out) == 1
    assert out[0].title == "Fenced output"


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


async def test_synthesize_cache_hit_does_not_call_backend_again() -> None:
    """Two calls with the same workspace + data hash → one backend call."""
    backend = _StubBackend(responses=[_valid_json_payload(count=2), _valid_json_payload(count=99)])
    bundle = _make_bundle()
    first = await synthesize_insights_llm(bundle, backend)
    second = await synthesize_insights_llm(bundle, backend)
    assert [i.id for i in first] == [i.id for i in second]
    # Only one backend call — the second hit the LRU.
    assert len(backend.calls) == 1


async def test_synthesize_cache_isolates_across_workspaces() -> None:
    """Different workspace_ids → independent cache entries."""
    backend = _StubBackend(
        responses=[
            _valid_json_payload(count=1),
            _valid_json_payload(count=2),
        ]
    )
    out_w1 = await synthesize_insights_llm(_make_bundle(workspace_id="w1"), backend)
    out_w2 = await synthesize_insights_llm(_make_bundle(workspace_id="w2"), backend)
    assert len(out_w1) == 1
    assert len(out_w2) == 2
    # Two distinct backend calls — workspace isolation respected.
    assert len(backend.calls) == 2


async def test_synthesize_cache_invalidates_when_data_changes() -> None:
    """A change in the input data invalidates the cache key."""
    backend = _StubBackend(
        responses=[
            _valid_json_payload(count=1),
            _valid_json_payload(count=3),
        ]
    )
    bundle1 = _make_bundle()
    out1 = await synthesize_insights_llm(bundle1, backend)
    assert len(out1) == 1

    # Mutate the rolling accuracy series → new data hash.
    bundle2 = _make_bundle()
    bundle2_changed = LLMInsightsInput(
        **{
            **bundle2.__dict__,
            "rolling_accuracy": (
                RollingAccuracyPoint(ts=_NOW - 7 * _DAY, accuracy=0.95, sample_count=40),
                RollingAccuracyPoint(ts=_NOW, accuracy=0.92, sample_count=42),
            ),
        }
    )
    out2 = await synthesize_insights_llm(bundle2_changed, backend)
    assert len(out2) == 3
    assert len(backend.calls) == 2


async def test_cache_ttl_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale cache entries (older than TTL) are evicted on lookup."""
    backend = _StubBackend(
        responses=[
            _valid_json_payload(count=1),
            _valid_json_payload(count=2),
        ]
    )
    # 1-second TTL so we can drive expiry deterministically.
    configure_cache(ttl_seconds=1)

    bundle = _make_bundle()
    first = await synthesize_insights_llm(bundle, backend)
    assert len(first) == 1
    assert len(backend.calls) == 1

    # Fake-advance the clock past TTL.
    import time as _time_mod

    base = _time_mod.monotonic()

    def _later() -> float:
        return base + 5.0  # > 1-second TTL

    monkeypatch.setattr(
        "pocketpaw_ee.foresight.insights_llm.time.monotonic",
        _later,
    )

    second = await synthesize_insights_llm(bundle, backend)
    assert len(second) == 2  # fresh response — cache miss after TTL
    assert len(backend.calls) == 2


# ---------------------------------------------------------------------------
# Stable id discipline
# ---------------------------------------------------------------------------


async def test_stable_ids_are_deterministic_across_runs() -> None:
    """Identical input + identical LLM output → identical ids."""
    backend1 = _StubBackend(responses=[_valid_json_payload(count=2)])
    backend2 = _StubBackend(responses=[_valid_json_payload(count=2)])
    bundle = _make_bundle()

    out1 = await synthesize_insights_llm(bundle, backend1)
    reset_cache()
    out2 = await synthesize_insights_llm(bundle, backend2)

    assert [i.id for i in out1] == [i.id for i in out2]


async def test_stable_ids_differ_when_workspace_changes() -> None:
    """Workspace_id is part of the id hash — same data + different
    workspace → different ids."""
    backend1 = _StubBackend(responses=[_valid_json_payload(count=1)])
    backend2 = _StubBackend(responses=[_valid_json_payload(count=1)])

    out1 = await synthesize_insights_llm(_make_bundle(workspace_id="w1"), backend1)
    out2 = await synthesize_insights_llm(_make_bundle(workspace_id="w2"), backend2)
    assert out1[0].id != out2[0].id


# ---------------------------------------------------------------------------
# Prompt content discipline
# ---------------------------------------------------------------------------


async def test_prompt_includes_kind_vocabulary() -> None:
    """The prompt advertises the bounded kind set so the model stays
    within the documented vocabulary."""
    backend = _StubBackend(responses=[_valid_json_payload(count=0)])
    await synthesize_insights_llm(_make_bundle(), backend)
    prompt = backend.calls[0]
    for kind in LLM_KIND_VOCABULARY:
        assert kind in prompt, f"prompt missing kind vocabulary: {kind}"


async def test_prompt_caps_recent_records() -> None:
    """Bundle with many recent records → prompt only carries the cap."""
    records = tuple(
        RecentPredictionRecordSummary(
            anchor_id=f"decision:rec-{i}",
            persona_id=f"persona-{i}",
            modal_outcome="accept",
            confidence=0.5 + i * 0.001,
            paired=True,
        )
        for i in range(200)
    )
    bundle = LLMInsightsInput(
        workspace_id="w-cap",
        period_key="2026_W21",
        now=_NOW,
        recent_records=records,
        recent_records_cap=10,
    )
    backend = _StubBackend(responses=[json.dumps({"insights": []})])
    await synthesize_insights_llm(bundle, backend)
    prompt = backend.calls[0]
    # Only 10 of the 200 records should be in the prompt — the 11th is
    # excluded by the cap.
    assert "decision:rec-9" in prompt
    assert "decision:rec-10" not in prompt
    assert "decision:rec-150" not in prompt


# ---------------------------------------------------------------------------
# Cap respect
# ---------------------------------------------------------------------------


async def test_response_capped_at_caller_cap() -> None:
    """Even if the LLM returns more items than ``cap``, the result is
    truncated at the cap so the wire response stays bounded."""
    payload = json.dumps(
        {
            "insights": [
                {
                    "kind": "trend_explainer",
                    "title": f"i{i}",
                    "body": f"reasoning {i}",
                    "severity": "info",
                }
                for i in range(20)
            ]
        }
    )
    backend = _StubBackend(responses=[payload])
    out = await synthesize_insights_llm(_make_bundle(), backend, cap=5)
    assert len(out) == 5


async def test_cap_below_one_raises() -> None:
    backend = _StubBackend(responses=[_valid_json_payload()])
    with pytest.raises(ValueError):
        await synthesize_insights_llm(_make_bundle(), backend, cap=0)


async def test_configure_cache_validates_bounds() -> None:
    """``configure_cache`` rejects out-of-range tunables explicitly."""
    with pytest.raises(ValueError):
        configure_cache(ttl_seconds=0)
    with pytest.raises(ValueError):
        configure_cache(capacity=0)
