# ee/pocketpaw_ee/foresight/subtypes/__init__.py
# Created: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) —
# RFC 08 PR 5. Sub-type adapter dispatch package — one module per
# RFC §4 sub-type ships a small, declarative adapter that hands the
# runner the per-tick anchor list and the post-run aggregate shape.
#
# v0.5 ships three of the seven RFC §4 sub-types:
#   - decision_forecast (PR 1) — the v0.1 baseline, anchors = the
#     decision the operator is staring at; aggregator emits an outcome
#     distribution. Lives in the runner as the default branch.
#   - market_sim (PR 5) — competitor + customer-segment personas;
#     anchors = market-position keys (e.g. ``segment:enterprise``);
#     aggregator emits market_share / win_rate / churn.
#   - org_change (PR 5) — internal-role personas; anchors = the rollout
#     events (announcement / training / deadline / escalation);
#     aggregator emits adoption_rate / exit_rate / escalation_count.
#
# Each adapter is a module-level set of pure functions:
#   - ``ANCHORS: tuple[str, ...]`` — fixed anchor ids for v0.5 (v1.0
#     pulls from the scenario YAML's ``anchors:`` block).
#   - ``anchors_for(config) -> list[str]`` — anchor ids per scenario
#     run (currently returns ``list(ANCHORS)``; v1.0 will read
#     ``config.anchors`` once that field lands on ScenarioConfig).
#   - ``aggregate(snapshots, config) -> dict[str, Any]`` — sub-type
#     outcome dict folded into ``RunResult.aggregate``.
#
# The runner (``scenarios/runner.py``) imports lazily inside
# ``run_scenario`` to keep the import surface small for v0.1/v0.2
# callers that never touch market_sim or org_change. PR 6+ will
# add ops_stress_test / strategic_what_if / training_rehearsal /
# discovery_generative as siblings here.

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Public sub-type identifiers — match the RFC §4 catalog.
SUB_TYPE_DECISION_FORECAST = "decision_forecast"
SUB_TYPE_MARKET_SIM = "market_sim"
SUB_TYPE_ORG_CHANGE = "org_change_rehearsal"

# Canonical sub-types known to v0.5. The runner uses this set to gate
# its sub-type validation; adding a new module here + a YAML loads it
# without touching the runner.
SUPPORTED_SUB_TYPES: tuple[str, ...] = (
    SUB_TYPE_DECISION_FORECAST,
    SUB_TYPE_MARKET_SIM,
    SUB_TYPE_ORG_CHANGE,
)


def get_adapter(sub_type: str) -> Any:
    """Return the adapter module for ``sub_type`` or raise
    ``NotImplementedError`` with the RFC §4 pointer.

    Imports are lazy so the engine's import surface stays small —
    ``market_sim`` and ``org_change`` only load when a scenario
    actually targets them.
    """
    if sub_type == SUB_TYPE_MARKET_SIM:
        from pocketpaw_ee.foresight.subtypes import market_sim as _market_sim

        return _market_sim
    if sub_type == SUB_TYPE_ORG_CHANGE:
        from pocketpaw_ee.foresight.subtypes import org_change as _org_change

        return _org_change
    if sub_type == SUB_TYPE_DECISION_FORECAST:
        # Decision Forecast stays in the runner's default branch — the
        # adapter here is a thin pass-through so callers that want a
        # uniform interface still get one.
        from pocketpaw_ee.foresight.subtypes import decision_forecast as _decision_forecast

        return _decision_forecast
    raise NotImplementedError(
        f"sub_type {sub_type!r} is not implemented; supported v0.5 set is "
        f"{SUPPORTED_SUB_TYPES}. See RFC 08 §4 for the seven-sub-type catalog."
    )


# Type alias for the per-tick projected-decision callback the cloud
# passes into the engine. Keeps the runner's signature readable.
#
# Args: (anchor_id, persona_id, tick_id, decision_text, confidence, sub_type)
# Returns: nothing — the callback persists the decision side-effect.
ProjectedDecisionCallback = Callable[[str, str, int, str, float, str], None]


__all__ = [
    "ProjectedDecisionCallback",
    "SUB_TYPE_DECISION_FORECAST",
    "SUB_TYPE_MARKET_SIM",
    "SUB_TYPE_ORG_CHANGE",
    "SUPPORTED_SUB_TYPES",
    "get_adapter",
]
