# ee/pocketpaw_ee/cloud/models/foresight_prediction_record.py
# Created: 2026-05-26 (feat/foresight-v10-prediction-record-persist) —
# RFC 08 v1.0 PR 10. PredictionRecord Beanie document — the real Mongo
# persistence behind RFC 08 §9 calibration loop (CAPTURE → OBSERVE →
# PAIR → AGGREGATE → CORRECT).
#
# Mirrors the engine's ``ee.foresight.calibration.PredictionRecord``
# dataclass with the cloud-rule-#3 tenancy invariant (``workspace`` is
# Indexed and required). v0.5 held PredictionRecord in-memory only and
# the §11.5 aggregate / §11.6 insights endpoints used proxies
# (``ForesightBacktest.gate_decision.observed`` for rolling accuracy,
# ``ForesightProjectedDecision.confidence`` for per-persona calibration).
# v1.0 replaces those proxies with reads off this collection.
#
# Sole writer: ``ee.cloud.foresight.service`` (via
# ``emit_prediction_record`` + ``pair_prediction``). The import-linter
# contract in ``ee/pyproject.toml`` lists this doc alongside the other
# foresight Beanie docs so router / dto / domain / models stay clean.
#
# Indexes are picked for the §11.5 + §11.6 read paths:
#   - ``(workspace, captured_at)`` — rolling-accuracy window scan over
#     all records inside the tenant + window.
#   - ``(workspace, anchor_id, captured_at)`` — per-anchor lookup so a
#     future Decision-Graph join (anchor across runs) stays cheap.
#   - ``(workspace, persona_id, captured_at)`` — per-persona calibration
#     read for the insights synthesizer's persona_outlier rule.
#
# The optional ``observed_at`` / ``observed_outcome`` / ``pair_delta``
# fields are filled by ``pair_prediction`` when an outcome lands; until
# then ``paired`` is False. Filtering ``paired=True`` is the canonical
# rolling-accuracy read filter.

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ForesightPredictionRecord(TimestampedDocument):
    """One projected outcome held until reality lands (or persists
    permanently on a backtest fan-out).

    Fields:
      - ``workspace`` — tenancy key (Indexed for fast list queries).
      - ``anchor_id`` — sub-type-specific anchor identifier (matches
        the ``decision:<name>`` / ``segment:<role>`` / ``rollout:<event>``
        namespace used by ProjectedDecision).
      - ``persona_id`` — the persona whose modal action drove the
        prediction. Empty string when no persona acted at this tick
        (the engine still emits a record so the per-anchor timeline
        stays dense).
      - ``scenario_id`` — the scenario name / template identifier
        (``"decision_forecast"`` / ``"market_sim"`` / ``"org_change"``
        for the v0.5 bundled set).
      - ``run_id`` — the ForesightRun / ForesightBacktest document id
        (hex string) this prediction belongs to.
      - ``tick_id`` — zero-based tick index inside the run.
      - ``prediction`` — projected-outcome payload (the engine's per-tick
        modal outcome dict). Stored as a JSON blob so future sub-types
        can add keys without a model migration.
      - ``confidence`` — aggregate confidence in (0.0, 1.0).
      - ``captured_at`` — server-side timestamp when the prediction was
        emitted by the engine. Drives the §11.5 rolling-accuracy window
        filter (NOT ``createdAt`` — captured_at echoes the engine clock
        the buffer used in v0.5 so historical re-emits land in the same
        bucket).
      - ``observed_at`` — server-side timestamp when the matching real
        outcome landed; ``None`` while the record stays unpaired.
      - ``observed_outcome`` — the actual outcome dict the operator
        supplied (backtests) or the listener wired in once reality
        landed (forward sims, v1.1+). ``None`` while unpaired.
      - ``paired`` — ``True`` once observation lands; the §11.5
        rolling-accuracy read filters on this so unpaired projections
        never inflate the denominator.
      - ``pair_delta`` — per-metric diff dict produced by
        :func:`ee.foresight.calibration._compute_delta` at pairing time.
        ``None`` until paired. Numeric metrics → signed difference;
        string metrics → ``{"match": bool, "projected": str,
        "actual": str}``; missing metrics → ``{"missing_in": "..."}``.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    anchor_id: str = ""
    persona_id: str = ""
    scenario_id: str = ""
    run_id: str = ""
    tick_id: int = Field(default=0, ge=0)
    prediction: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    captured_at: datetime
    observed_at: datetime | None = None
    observed_outcome: dict[str, Any] | None = None
    paired: bool = False
    pair_delta: dict[str, Any] | None = None

    class Settings:
        name = "foresight_prediction_records"
        indexes = [
            # Window scan inside a workspace — §11.5 rolling accuracy.
            [("workspace", 1), ("captured_at", 1)],
            # Per-anchor lookup — future Decision-Graph join + insights
            # tier-imbalance rule (per-anchor calibration drift).
            [("workspace", 1), ("anchor_id", 1), ("captured_at", 1)],
            # Per-persona lookup — §11.6 persona_outlier rule scan.
            [("workspace", 1), ("persona_id", 1), ("captured_at", 1)],
        ]


__all__ = ["ForesightPredictionRecord"]
