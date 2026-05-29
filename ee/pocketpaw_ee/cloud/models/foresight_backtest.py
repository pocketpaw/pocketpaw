# ee/pocketpaw_ee/cloud/models/foresight_backtest.py
# Created: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — RFC 08 PR 4.
#
# Foresight backtest document — persistence for retroactive backtest runs
# (RFC 08 §10 + §13.1 gate 7 "Onboarding REQUIRES retroactive backtest on
# customer historical data BEFORE forward sims allowed — trust unlock").
#
# Sibling collection to ``foresight_runs`` (ForesightRun): a backtest is a
# different kind of run — it operates on historical anchors and produces
# an accuracy report scored against the known real outcome. The result
# blob carries the aggregator's CalibrationSummary plus the gate decision
# (passed / observed / threshold / margin). The onboarding gate state
# (``get_onboarding_gate``) is derived from the latest completed backtest
# in a workspace; there is no separate gate-state collection.
#
# Only ``ee.cloud.foresight.service`` may import this module — enforced
# by the import-linter contract in ``ee/pyproject.toml`` (the same
# contract that scopes ForesightRun writes to service.py).
#
# Status vocabulary matches the scenario-run document so listeners can
# share dispatch logic: ``queued | running | complete | failed``.

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ForesightBacktest(TimestampedDocument):
    """One retroactive backtest run, persisted in Mongo.

    Fields:
      - ``workspace`` — tenancy key (Indexed for fast list queries).
      - ``scenario_name`` — operator-supplied label echoed in the UI.
      - ``status`` — ``queued`` / ``running`` / ``complete`` / ``failed``.
      - ``request`` — validated POST body (``CreateBacktestRequest.model_dump()``).
      - ``result`` — engine's ``RunResult.as_wire_dict()`` + the
        aggregator's ``CalibrationSummary.as_wire_dict()`` once the
        backtest completes; ``None`` while queued / running / failed.
      - ``gate_decision`` — the ``ThresholdDecision.as_wire_dict()``
        the aggregator produced. Drives the onboarding gate; populated
        on the same transition that fills ``result``.
      - ``error`` — failure message when the backtest raises.
      - ``threshold`` — the gate threshold this backtest was scored
        against. Persisted alongside the result so a future tuning of
        the default (workspace config override path) doesn't retroactively
        change historical pass/fail labels.
      - ``created_by`` — viewer user id (for audit / display).

    The result + gate_decision pair is what the UI Aggregate panel
    renders ("you scored 0.72 against the 0.65 bar — unlock granted")
    and what the onboarding service reads to compose the gate state
    response.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    scenario_name: str
    status: str = Field(
        default="queued",
        pattern="^(queued|running|complete|failed)$",
    )
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    gate_decision: dict[str, Any] | None = None
    error: str | None = None
    threshold: float = 0.65
    created_by: str = ""

    class Settings:
        name = "foresight_backtests"
        indexes = [
            [("workspace", 1), ("status", 1)],
            # ``createdAt`` ordering is the default Mongo cursor for the
            # list endpoint + the gate-state query (newest passing
            # backtest wins). An explicit composite index keeps both
            # queries cheap once a workspace accumulates dozens of
            # quarterly recalibration backtests.
            [("workspace", 1), ("createdAt", -1)],
        ]
