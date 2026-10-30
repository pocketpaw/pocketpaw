# ee/pocketpaw_ee/foresight/scenarios/__init__.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
# Scenarios package — v0.1 ships one scenario template
# (decision_forecast.yaml) plus the runner that takes a ScenarioConfig
# end-to-end through World + Persona + Backend.

from __future__ import annotations

from pocketpaw_ee.foresight.scenarios.runner import (
    PersonaSpec,
    RunResult,
    ScenarioConfig,
    run_scenario,
)

__all__ = ["PersonaSpec", "RunResult", "ScenarioConfig", "run_scenario"]
