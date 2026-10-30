# ee/pocketpaw_ee/foresight/subtypes/market_sim.py
# Created: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) —
# RFC 08 PR 5. Market Sim sub-type adapter — RFC §4.2.
#
# Market Sim is the "classical population shape" — competitor agents,
# customer segments, and channel partners react to external events
# (pricing changes, new entrants, demand shifts). Outcomes are aggregate
# market positions: market_share / win_rate / churn / nps swings per
# segment, not single decisions.
#
# v0.5 contract (the minimum the cloud + the calibration loop need):
#   - Anchors: one anchor per market segment the scenario tracks
#     (``segment:<role>``). Persona ``role`` is the segment id so the
#     YAML stays declarative and the adapter doesn't need a new
#     ScenarioConfig field.
#   - Aggregator: rolls personas' last-tick actions into per-segment
#     win/loss/churn counts and produces the market_position dict the
#     UI's Aggregate panel renders (RFC §11.5).
#
# v1.0 will:
#   - Pull personas from a Fabric synthesizer at 10K-100K scale (the
#     v0.5 path runs at whatever count the operator declares inline).
#   - Drive external events from a tick-scheduled injector (the v0.5
#     path treats each tick as a uniform "market beat" with no event
#     fanout — the cloud's per-tick projected-decision emission is the
#     trace, the injector is PR 7+).
#   - Wire OASIS's per-tier semaphore so the 100K-agent run hits the
#     5/15/80 cost ceiling cleanly.

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketpaw_ee.foresight.scenarios.runner import ScenarioConfig
    from pocketpaw_ee.foresight.world import WorldSnapshot

SUB_TYPE = "market_sim"

# Action-token vocabulary the aggregator buckets into market outcomes.
# Personas' deterministic-fake responses won't always match these —
# anything outside the bucket lands in ``other`` so the contract stays
# total without throwing.
_WIN_ACTIONS: frozenset[str] = frozenset({"buy", "convert", "renew", "refer"})
_LOSS_ACTIONS: frozenset[str] = frozenset({"churn", "decline", "lapse"})
_NEUTRAL_ACTIONS: frozenset[str] = frozenset({"hold", "noop", "watch", "complain"})


def anchors_for(config: ScenarioConfig) -> list[str]:
    """Anchor ids for a Market Sim run — one per segment, dedup'd.

    Each persona's ``role`` is treated as the segment label (the YAML
    convention v0.5 ships: ``role: enterprise``, ``role: smb``, etc.).
    The dedupe step keeps the anchor list short when the operator
    declares a 1000-persona scenario across 5 segments — the engine
    fans projected decisions per-anchor, not per-persona, so anchor
    cardinality drives the cloud's storage footprint.
    """
    segments = []
    seen = set()
    for spec in config.personas:
        seg = (spec.role or "participant").strip().lower()
        anchor_id = f"segment:{seg}"
        if anchor_id in seen:
            continue
        seen.add(anchor_id)
        segments.append(anchor_id)
    if not segments:
        # Defensive default — a YAML with zero personas is rejected
        # upstream by ScenarioConfig.__post_init__, but defaulting here
        # keeps the adapter total.
        return ["segment:all"]
    return segments


def aggregate(
    snapshots: list[WorldSnapshot],
    config: ScenarioConfig,
) -> dict[str, Any]:
    """Roll the tick snapshots into per-segment market-position dicts.

    v0.5 contract:
      - ``sub_type``: echo the sub-type string.
      - ``segments``: dict keyed by anchor id (``segment:<role>``);
        each value carries ``wins``, ``losses``, ``neutral``, ``other``
        counts plus the derived ``win_rate`` and ``churn_rate``.
      - ``totals``: cross-segment aggregates (total actions, modal
        market position by win-rate, ticks_run).

    The aggregator deliberately stays additive across all snapshots so
    the per-run trajectory shows the modal outcome at completion. v1.0
    will split out per-tick deltas and the funnel-stage transitions
    OASIS's recsys layer produces natively.
    """
    if not snapshots:
        return {
            "sub_type": SUB_TYPE,
            "segments": {},
            "totals": {
                "actions_total": 0,
                "modal_segment": None,
                "modal_win_rate": 0.0,
                "ticks_run": 0,
            },
        }

    # Build a name → role index so the persona id (which surfaces in
    # ``last_tick_actions`` via the world's str(uuid)) maps back to the
    # segment. v0.5 keys on persona name in the action log via the
    # deterministic-fake's contract; v1.0 swaps for the agent id once
    # the world surfaces it in the action dict.
    name_to_segment: dict[str, str] = {}
    for spec in config.personas:
        seg = (spec.role or "participant").strip().lower()
        name_to_segment[spec.name] = seg

    segment_counters: dict[str, Counter[str]] = defaultdict(Counter)
    seen_segments = set(name_to_segment.values())

    for snapshot in snapshots:
        for action in snapshot.last_tick_actions:
            if not action.get("ok"):
                continue
            # The world layer's action dict carries the agent id but not
            # the persona's role; we fall back to ``segment:all`` when
            # the role can't be resolved (deterministic-fake backend
            # responds with a tokenized action verb either way).
            verb = str(action.get("action", "")).strip().lower()
            put = action.get("put")
            segment_for_action: str | None = None
            # Heuristic 1 — put carries an explicit segment.
            if isinstance(put, dict):
                explicit = put.get("segment")
                if isinstance(explicit, str):
                    segment_for_action = explicit.strip().lower()
            # Heuristic 2 — map agent_id → persona name via the world's
            # roster. The runner stores personas keyed by UUID; the
            # action dict surfaces ``agent_id`` as the str(UUID). We
            # don't have a back-pointer at this layer so we degrade to
            # the modal segment across the population.
            if segment_for_action is None:
                # Pick the first segment as a stable default so the
                # aggregator stays deterministic across runs even when
                # the population's segment mix is even.
                segment_for_action = next(iter(seen_segments), "all")
            anchor_id = f"segment:{segment_for_action}"
            if verb in _WIN_ACTIONS:
                segment_counters[anchor_id]["wins"] += 1
            elif verb in _LOSS_ACTIONS:
                segment_counters[anchor_id]["losses"] += 1
            elif verb in _NEUTRAL_ACTIONS:
                segment_counters[anchor_id]["neutral"] += 1
            else:
                segment_counters[anchor_id]["other"] += 1

    # Make sure every declared anchor surfaces even if the run produced
    # no actions for it — the cloud's per-anchor query expects every
    # anchor to be addressable.
    for anchor_id in anchors_for(config):
        segment_counters.setdefault(anchor_id, Counter())

    segments_out: dict[str, dict[str, Any]] = {}
    modal_segment = None
    modal_win_rate = 0.0
    actions_total = 0
    for anchor_id, counter in segment_counters.items():
        wins = counter.get("wins", 0)
        losses = counter.get("losses", 0)
        neutral = counter.get("neutral", 0)
        other = counter.get("other", 0)
        sub_total = wins + losses + neutral + other
        win_rate = (wins / sub_total) if sub_total else 0.0
        churn_rate = (losses / sub_total) if sub_total else 0.0
        segments_out[anchor_id] = {
            "wins": wins,
            "losses": losses,
            "neutral": neutral,
            "other": other,
            "win_rate": win_rate,
            "churn_rate": churn_rate,
        }
        actions_total += sub_total
        if win_rate > modal_win_rate:
            modal_win_rate = win_rate
            modal_segment = anchor_id

    return {
        "sub_type": SUB_TYPE,
        "segments": segments_out,
        "totals": {
            "actions_total": actions_total,
            "modal_segment": modal_segment,
            "modal_win_rate": modal_win_rate,
            "ticks_run": len(snapshots),
        },
    }


__all__ = ["SUB_TYPE", "aggregate", "anchors_for"]
