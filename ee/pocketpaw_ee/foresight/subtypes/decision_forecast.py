# ee/pocketpaw_ee/foresight/subtypes/decision_forecast.py
# Created: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) —
# RFC 08 PR 5. Adapter for the Decision Forecast sub-type — RFC §4.1.
#
# Decision Forecast is the narrowest, highest-fidelity sub-type the v0.1
# loop has been carrying since PR 1. The adapter here is a thin
# pass-through that returns the runner's default behavior so the
# get_adapter() dispatch in ``subtypes/__init__.py`` has a uniform
# interface across all three v0.5 sub-types.
#
# Anchors for Decision Forecast: the operator points at a specific
# decision (a lease, a contract amendment, an offer). v0.5 treats the
# scenario's *name* as the anchor id when the scenario doesn't declare
# an explicit ``anchors:`` list — that mirrors the v0.1 contract where
# the scenario name doubles as the decision identifier in the run report.
#
# Aggregator: an outcome histogram across the run's tick snapshots.
# v0.5 emits the modal "put" key the engine's last tick recorded, plus
# the raw count of actions logged across the run. v1.0 will fold the
# counterfactual-branch distribution per RFC §4.1.

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw_ee.foresight.scenarios.runner import ScenarioConfig
    from pocketpaw_ee.foresight.world import WorldSnapshot

SUB_TYPE = "decision_forecast"


def anchors_for(config: ScenarioConfig) -> list[str]:
    """Return the list of anchor ids this scenario forecasts against.

    v0.5 defaults to a single anchor named after the scenario itself —
    the v0.1 convention that the scenario name and the decision id
    line up. v1.0 will read ``config.anchors`` when that field lands
    on ScenarioConfig (RFC §18 example YAML calls for an explicit
    ``anchors:`` block).
    """
    return [f"decision:{config.name}"]


def aggregate(
    snapshots: list[WorldSnapshot],
    config: ScenarioConfig,
) -> dict[str, Any]:
    """Roll the tick snapshots into a Decision Forecast outcome dict.

    v0.5 contract:
      - ``sub_type``: echo the sub-type string for downstream consumers.
      - ``modal_outcome``: the modal ``put`` map across all successful
        actions in the final tick. Empty dict when no action stored a
        ``put`` block (the deterministic-fake backend does this when
        the persona elects ``put=none``).
      - ``actions_total``: cumulative successful action count.
      - ``ticks_run``: number of tick snapshots.

    The aggregator and calibration loop (PR 4) read ``modal_outcome``
    when scoring the backtest gate. Returning ``{}`` for that field is
    the v0.1 fallback — pairs against actuals will count as mismatches
    rather than crashing.
    """
    if not snapshots:
        return {
            "sub_type": SUB_TYPE,
            "modal_outcome": {},
            "actions_total": 0,
            "ticks_run": 0,
        }
    final = snapshots[-1]
    puts_in_final: Counter[frozenset[tuple[str, Any]]] = Counter()
    for action in final.last_tick_actions:
        if not action.get("ok"):
            continue
        put = action.get("put")
        if isinstance(put, dict):
            # Stringify so Counter keys stay hashable even when the
            # action's put map carries nested structures.
            puts_in_final[frozenset(put.items())] += 1
    modal_outcome: dict[str, Any] = {}
    if puts_in_final:
        most_common_items, _count = puts_in_final.most_common(1)[0]
        modal_outcome = dict(most_common_items)
    return {
        "sub_type": SUB_TYPE,
        "modal_outcome": modal_outcome,
        "actions_total": final.actions_applied,
        "ticks_run": len(snapshots),
    }


__all__ = ["SUB_TYPE", "aggregate", "anchors_for"]
