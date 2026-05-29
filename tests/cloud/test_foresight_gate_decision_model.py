# tests/cloud/test_foresight_gate_decision_model.py
# Created: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) —
# RFC 08 v1.0 — coverage for the ``GateDecision`` Pydantic sub-model
# that tightens ``BacktestRunResponse.gate_decision`` and
# ``BacktestRunListItemResponse.gate_decision``.
#
# Exercises:
#   - happy path composition from a passing/failing ThresholdDecision
#     wire dict
#   - ``reason`` derivation (no_pairs / threshold_met / threshold_unmet
#     / free-form override)
#   - ``evaluated_at`` derivation (raw → updated_at → now fallback)
#   - ``modal_accuracy`` alias matches ``observed``
#   - backward-compat: ``model_dump()`` carries the legacy keys
#   - clamping defensive against out-of-range historical writes
"""Coverage for the GateDecision Pydantic sub-model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import (
    CreateBacktestRequest,
    GateDecision,
    HistoricalAnchorRequest,
    PersonaSpecRequest,
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


def _backtest_body(name: str = "gate-test") -> CreateBacktestRequest:
    return CreateBacktestRequest(
        name=name,
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="Anne", role="approver", ocean={})],
        anchors=[
            HistoricalAnchorRequest(
                anchor_object_id=f"lease:LR-{i}",
                actual_outcome={"outcome": "accept"},
            )
            for i in range(10)
        ],
    )


# ---------------------------------------------------------------------------
# _compose_gate_decision — direct helper tests (no Mongo)
# ---------------------------------------------------------------------------


def test_compose_gate_decision_returns_none_for_none_input() -> None:
    """A None input collapses to None — the gate hasn't been scored yet."""
    out = foresight_service._compose_gate_decision(
        None,
        fallback_threshold=0.65,
        fallback_evaluated_at=None,
    )
    assert out is None


def test_compose_gate_decision_threshold_met_when_passed() -> None:
    """``passed=True`` derives ``reason="threshold_met"``."""
    raw = {
        "passed": True,
        "observed": 0.85,
        "threshold": 0.65,
        "margin": 0.20,
        "n_pairs": 12,
    }
    out = foresight_service._compose_gate_decision(
        raw,
        fallback_threshold=0.65,
        fallback_evaluated_at=datetime(2026, 5, 26, 0, 0, tzinfo=UTC),
    )
    assert out is not None
    assert out.passed is True
    assert out.reason == "threshold_met"
    assert out.observed == 0.85
    assert out.modal_accuracy == 0.85
    assert out.threshold == 0.65
    assert out.margin == pytest.approx(0.20)
    assert out.n_pairs == 12


def test_compose_gate_decision_threshold_unmet_when_failed_with_pairs() -> None:
    """``passed=False`` with ``n_pairs >= 1`` derives
    ``reason="threshold_unmet"``."""
    raw = {
        "passed": False,
        "observed": 0.40,
        "threshold": 0.65,
        "margin": -0.25,
        "n_pairs": 10,
    }
    out = foresight_service._compose_gate_decision(
        raw, fallback_threshold=0.65, fallback_evaluated_at=None
    )
    assert out is not None
    assert out.reason == "threshold_unmet"
    assert out.passed is False
    # ``modal_accuracy`` is the alias of ``observed`` — both populated.
    assert out.modal_accuracy == 0.40
    assert out.observed == 0.40


def test_compose_gate_decision_no_pairs_when_zero() -> None:
    """``n_pairs=0`` derives ``reason="no_pairs"`` regardless of pass/fail."""
    raw = {
        "passed": False,
        "observed": 0.0,
        "threshold": 0.65,
        "margin": -0.65,
        "n_pairs": 0,
    }
    out = foresight_service._compose_gate_decision(
        raw, fallback_threshold=0.65, fallback_evaluated_at=None
    )
    assert out is not None
    assert out.reason == "no_pairs"


def test_compose_gate_decision_honours_explicit_reason_override() -> None:
    """A v1.0+ write that pins a custom ``reason`` (e.g. a future
    ``thin_sample`` state) flows through verbatim — the derivation is
    only a default."""
    raw = {
        "passed": False,
        "observed": 0.40,
        "threshold": 0.65,
        "margin": -0.25,
        "n_pairs": 10,
        "reason": "thin_sample",
    }
    out = foresight_service._compose_gate_decision(
        raw, fallback_threshold=0.65, fallback_evaluated_at=None
    )
    assert out is not None
    assert out.reason == "thin_sample"


def test_compose_gate_decision_evaluated_at_uses_raw_when_present() -> None:
    """If the persisted gate dict carries ``evaluated_at``, use it
    verbatim."""
    raw = {
        "passed": True,
        "observed": 0.80,
        "threshold": 0.65,
        "margin": 0.15,
        "n_pairs": 10,
        "evaluated_at": "2026-05-26T00:30:00Z",
    }
    out = foresight_service._compose_gate_decision(
        raw,
        fallback_threshold=0.65,
        fallback_evaluated_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert out is not None
    assert out.evaluated_at == "2026-05-26T00:30:00Z"


def test_compose_gate_decision_evaluated_at_falls_back_to_updated_at() -> None:
    """Without an ``evaluated_at`` on the raw dict, fall back to the
    backtest doc's ``updatedAt`` so the wire field is always populated."""
    raw = {
        "passed": True,
        "observed": 0.80,
        "threshold": 0.65,
        "margin": 0.15,
        "n_pairs": 10,
    }
    fallback_dt = datetime(2026, 5, 25, 14, 30, tzinfo=UTC)
    out = foresight_service._compose_gate_decision(
        raw, fallback_threshold=0.65, fallback_evaluated_at=fallback_dt
    )
    assert out is not None
    assert "2026-05-25T14:30:00" in out.evaluated_at


def test_compose_gate_decision_clamps_out_of_range_floats() -> None:
    """A historical write with a stray 1.0001 doesn't 422 the
    response — the helper clamps to [0, 1] defensively."""
    raw = {
        "passed": True,
        "observed": 1.0001,
        "threshold": 1.0,
        "margin": 0.0001,
        "n_pairs": 5,
    }
    out = foresight_service._compose_gate_decision(
        raw, fallback_threshold=0.65, fallback_evaluated_at=None
    )
    assert out is not None
    assert 0.0 <= out.observed <= 1.0
    assert 0.0 <= out.threshold <= 1.0


def test_compose_gate_decision_backward_compat_model_dump_has_legacy_keys() -> None:
    """Existing callers that key on the legacy dict shape
    (``gate_decision["passed"]``) keep working through
    ``model_dump()``."""
    raw = {
        "passed": True,
        "observed": 0.85,
        "threshold": 0.65,
        "margin": 0.20,
        "n_pairs": 12,
    }
    out = foresight_service._compose_gate_decision(
        raw, fallback_threshold=0.65, fallback_evaluated_at=None
    )
    assert out is not None
    dumped = out.model_dump()
    # Legacy keys preserved.
    for legacy_key in ("passed", "observed", "threshold", "margin", "n_pairs"):
        assert legacy_key in dumped
    # New keys exposed.
    for new_key in ("reason", "evaluated_at", "modal_accuracy"):
        assert new_key in dumped


# ---------------------------------------------------------------------------
# Integration — full backtest path
# ---------------------------------------------------------------------------


async def test_backtest_response_carries_structured_gate_decision() -> None:
    """End-to-end: ``create_backtest`` returns a response whose
    ``gate_decision`` is a :class:`GateDecision` instance carrying the
    derived fields."""
    ctx = _ctx()
    out = await foresight_service.create_backtest(ctx, _backtest_body())
    assert out.gate_decision is not None
    assert isinstance(out.gate_decision, GateDecision)
    # Derived fields populated.
    assert out.gate_decision.reason in {
        "no_pairs",
        "threshold_met",
        "threshold_unmet",
    }
    assert out.gate_decision.evaluated_at  # non-empty string


async def test_list_backtests_response_carries_structured_gate_decision() -> None:
    """The list endpoint preserves the structured ``gate_decision``
    so the Aggregate panel can render the pass/fail label per row."""
    ctx = _ctx()
    await foresight_service.create_backtest(ctx, _backtest_body())
    items = await foresight_service.list_backtests(ctx)
    assert len(items) == 1
    assert items[0].gate_decision is not None
    assert isinstance(items[0].gate_decision, GateDecision)


async def test_get_backtest_response_carries_structured_gate_decision() -> None:
    """``get_backtest`` returns the same structured shape so detail
    + list views read consistent fields."""
    ctx = _ctx()
    created = await foresight_service.create_backtest(ctx, _backtest_body())
    fetched = await foresight_service.get_backtest(ctx, created.id)
    assert fetched.gate_decision is not None
    assert isinstance(fetched.gate_decision, GateDecision)
    # Detail + list shape consistency.
    assert fetched.gate_decision == created.gate_decision


async def test_backtest_failed_returns_none_gate_decision() -> None:
    """When the engine raises, the backtest doc lands as ``failed``
    and the response's ``gate_decision`` stays ``None`` (matches
    v0.5 behaviour)."""
    ctx = _ctx()

    async def _boom(_body):
        raise RuntimeError("simulated engine outage")

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(foresight_service, "_run_engine_inline", _boom)
        out = await foresight_service.create_backtest(ctx, _backtest_body(name="fail"))
    assert out.status == "failed"
    assert out.gate_decision is None
