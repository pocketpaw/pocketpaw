# ee/pocketpaw_ee/foresight/subtypes/org_change.py
# Created: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) —
# RFC 08 PR 5. Org Change Rehearsal sub-type adapter — RFC §4.3.
#
# Org Change Rehearsal is the "what if we change the policy" sub-type —
# the world is an internal Fabric snapshot, personas are the team souls
# + agent crews that operate the affected pockets, and the intervention
# is an Instinct policy overlay. The tick semantics here are rollout
# events: announcement → training → deadline → escalation, each ticking
# the rollout one beat forward. Personas respond with acceptance /
# resistance / exit / escalation.
#
# v0.5 contract (the minimum the cloud + the calibration loop need):
#   - Anchors: the rollout-event sequence (RFC §4.3 names them
#     announce / training / deadline / escalation). v0.5 ships the
#     fixed four; v1.0 will read ``config.rollout_events`` once that
#     field lands on ScenarioConfig.
#   - Aggregator: rolls personas' last-tick actions into
#     ``adoption_rate`` / ``resistance_rate`` / ``exit_rate`` /
#     ``escalation_count`` per anchor.
#
# v1.0 will:
#   - Drive personas at calendar cadence (1 sim-day / tick is typical)
#     via an injector — v0.5 treats every tick uniformly.
#   - Pull personas from the workspace's actual team-soul roster + the
#     agent crews wired to the affected pockets (v0.5 still takes the
#     inline persona list).
#   - Apply the Instinct policy overlay so the throughput / queue-depth
#     metrics RFC §4.3 calls for emerge from the simulation rather than
#     being computed from action tallies.

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw_ee.foresight.scenarios.runner import ScenarioConfig
    from pocketpaw_ee.foresight.world import WorldSnapshot

SUB_TYPE = "org_change_rehearsal"

# Fixed rollout-event sequence v0.5 fans against. Each event becomes
# one anchor id (``rollout:<event>``); the engine fans projected
# decisions per-anchor so the cloud's storage footprint stays linear
# in the rollout length, not in the persona count.
DEFAULT_ROLLOUT_EVENTS: tuple[str, ...] = (
    "announce",
    "training",
    "deadline",
    "escalation",
)

# Action-token vocabulary the aggregator buckets into adoption outcomes.
# Personas' deterministic-fake responses pick from these via the OCEAN
# drift; tokens outside the bucket land in ``other``.
_ADOPT_ACTIONS: frozenset[str] = frozenset({"accept", "adopt", "comply", "engage"})
_RESIST_ACTIONS: frozenset[str] = frozenset({"resist", "object", "delay", "complain"})
_EXIT_ACTIONS: frozenset[str] = frozenset({"exit", "leave", "quit", "transfer"})
_ESCALATE_ACTIONS: frozenset[str] = frozenset({"escalate", "appeal", "report"})


def anchors_for(config: ScenarioConfig) -> list[str]:
    """Return the rollout-event anchor ids for an Org Change run.

    v0.5 emits the fixed four-step sequence. v1.0 reads
    ``config.rollout_events`` once the YAML grammar adds it.
    """
    return [f"rollout:{event}" for event in DEFAULT_ROLLOUT_EVENTS]


def aggregate(
    snapshots: list[WorldSnapshot],
    config: ScenarioConfig,
) -> dict[str, Any]:
    """Roll the tick snapshots into per-rollout-event adoption dicts.

    v0.5 contract:
      - ``sub_type``: echo the sub-type string.
      - ``rollout``: dict keyed by anchor id (``rollout:<event>``);
        each value carries ``adoptions``, ``resistance``, ``exits``,
        ``escalations``, ``other`` counts plus the derived
        ``adoption_rate``, ``resistance_rate``, ``exit_rate`` rates.
      - ``totals``: cross-event aggregates (population, escalation
        count, modal adoption rate, ticks_run).

    The tick → anchor mapping is positional: tick 0 fans against the
    first anchor (announce), tick 1 against the second (training), and
    so on. When ``n_ticks`` exceeds the rollout length, extra ticks
    fan against the last anchor (escalation) — the v0.5 simplification
    that maps "the rollout finished but the simulation kept ticking"
    onto continued escalation responses.
    """
    anchors = anchors_for(config)
    if not snapshots:
        return {
            "sub_type": SUB_TYPE,
            "rollout": {anchor_id: _empty_bucket() for anchor_id in anchors},
            "totals": {
                "population": len(config.personas),
                "escalation_count": 0,
                "modal_adoption_rate": 0.0,
                "ticks_run": 0,
            },
        }

    buckets: dict[str, dict[str, int]] = defaultdict(_empty_bucket)
    for tick_index, snapshot in enumerate(snapshots):
        anchor_id = anchors[min(tick_index, len(anchors) - 1)]
        for action in snapshot.last_tick_actions:
            if not action.get("ok"):
                continue
            verb = str(action.get("action", "")).strip().lower()
            if verb in _ADOPT_ACTIONS:
                buckets[anchor_id]["adoptions"] += 1
            elif verb in _RESIST_ACTIONS:
                buckets[anchor_id]["resistance"] += 1
            elif verb in _EXIT_ACTIONS:
                buckets[anchor_id]["exits"] += 1
            elif verb in _ESCALATE_ACTIONS:
                buckets[anchor_id]["escalations"] += 1
            else:
                buckets[anchor_id]["other"] += 1

    # Make sure every anchor surfaces even when the run hit zero
    # actions there — the cloud's per-anchor query expects each
    # anchor id to round-trip.
    for anchor_id in anchors:
        buckets.setdefault(anchor_id, _empty_bucket())

    rollout_out: dict[str, dict[str, Any]] = {}
    modal_adoption_rate = 0.0
    escalation_total = 0
    for anchor_id, counts in buckets.items():
        adoptions = counts["adoptions"]
        resistance = counts["resistance"]
        exits = counts["exits"]
        escalations = counts["escalations"]
        other = counts["other"]
        sub_total = adoptions + resistance + exits + escalations + other
        adoption_rate = (adoptions / sub_total) if sub_total else 0.0
        resistance_rate = (resistance / sub_total) if sub_total else 0.0
        exit_rate = (exits / sub_total) if sub_total else 0.0
        rollout_out[anchor_id] = {
            "adoptions": adoptions,
            "resistance": resistance,
            "exits": exits,
            "escalations": escalations,
            "other": other,
            "adoption_rate": adoption_rate,
            "resistance_rate": resistance_rate,
            "exit_rate": exit_rate,
        }
        escalation_total += escalations
        if adoption_rate > modal_adoption_rate:
            modal_adoption_rate = adoption_rate

    return {
        "sub_type": SUB_TYPE,
        "rollout": rollout_out,
        "totals": {
            "population": len(config.personas),
            "escalation_count": escalation_total,
            "modal_adoption_rate": modal_adoption_rate,
            "ticks_run": len(snapshots),
        },
    }


def _empty_bucket() -> dict[str, int]:
    return {
        "adoptions": 0,
        "resistance": 0,
        "exits": 0,
        "escalations": 0,
        "other": 0,
    }


__all__ = [
    "DEFAULT_ROLLOUT_EVENTS",
    "SUB_TYPE",
    "aggregate",
    "anchors_for",
]
