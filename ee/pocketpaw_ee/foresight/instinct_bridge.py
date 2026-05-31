# ee/pocketpaw_ee/foresight/instinct_bridge.py
# Created: 2026-05-25 (feat/foresight-v08-approval-loop) — RFC 08 §8.
#
# Foresight → Instinct bridge. Pure conversion module that turns a
# persisted ``ProjectedDecision`` (cloud domain value object) into an
# ``InstinctProposal`` shape ready for ``InstinctStore.propose``. The
# cloud-side fan-out (``ee.cloud.foresight.service.emit_projected_decision``)
# imports this lazily and stores the resulting proposal when a scenario
# opted into routing (``route_to_instinct=True``).
#
# Why a separate module:
#   - The conversion logic is pure (no Beanie, no Instinct store call,
#     no engine-side OASIS / CAMEL imports). Keeping it isolated lets
#     the cloud service import it without dragging in the foresight
#     engine's optional-extra surface.
#   - The bridge is engine-side by file location only — it sits in
#     ``ee.pocketpaw_ee.foresight`` so it ships with the foresight
#     namespace, but it imports nothing from ``foresight.persona``,
#     ``foresight.llm``, ``foresight.scenarios``, ``foresight.subtypes``,
#     ``foresight.substrate``, ``foresight.aggregator``, or
#     ``foresight.calibration``. The import-linter contract
#     "Foresight cloud — must not import the engine layer" forbids
#     those specific submodules; ``instinct_bridge`` is deliberately
#     not on that list because it has no engine deps to leak.
#   - RFC 08 §8 contract: Foresight feeds Instinct as EVIDENCE, not as
#     new policy. The Instinct policy still owns "who gates this" and
#     "what the predicate is". This bridge surfaces the projection so
#     the approver UI's Tray can render it; the proposal itself is a
#     non-executing notice (``category=DATA``, no ``_pocket_write`` blob).
#
# Dedupe contract:
#   - The proposal carries a dedupe key under
#     ``parameters._foresight.dedupe_key`` whose value joins
#     ``(workspace_id, run_id, tick_id, anchor_id, persona_id)`` with
#     a ``|`` separator. The cloud caller is responsible for skipping
#     a propose call when an Instinct row with the same key already
#     exists — the bridge stays pure (no store side-effects); the
#     cloud service does the read-before-write.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Dedupe key — joined string so a single SQLite LIKE / equality scan finds
# duplicates without a JSON-path query (instinct_actions stores parameters
# as a JSON TEXT column).
# ---------------------------------------------------------------------------


def build_dedupe_key(
    *,
    workspace_id: str,
    run_id: str,
    tick_id: int,
    anchor_id: str,
    persona_id: str,
) -> str:
    """Stable dedupe key for one (workspace × run × tick × anchor × persona)
    ProjectedDecision bucket.

    Used by the cloud fan-out to skip duplicate Instinct proposals when a
    ProjectedDecision is re-emitted (e.g. a run replay or a scenario
    re-run with the same id). Joined with ``|`` so the key is a single
    string the bridge / store can compare without decomposing.

    Empty ``persona_id`` is preserved as an empty segment (matches the
    engine's convention of emitting one record per anchor even when no
    persona acted), so the key shape is deterministic for every fan-out.
    """
    return f"{workspace_id}|{run_id}|{tick_id}|{anchor_id}|{persona_id}"


# ---------------------------------------------------------------------------
# Output shape — the cloud service constructs an ``ActionTrigger`` and
# calls ``InstinctStore.propose(**kwargs)`` with these fields. We do not
# build the ``ActionTrigger`` here because importing it would couple the
# bridge to ``pocketpaw.instinct.models`` at module top; the cloud
# service threads the import lazily so the engine namespace stays clean.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstinctProposal:
    """Conversion output — the kwargs an Instinct ``propose`` call needs.

    Fields mirror :meth:`pocketpaw.instinct.store.InstinctStore.propose`'s
    signature one-to-one, plus ``trigger_type`` / ``trigger_source`` /
    ``trigger_reason`` so the cloud service can build the
    :class:`pocketpaw.instinct.models.ActionTrigger` without re-importing
    the domain types here.

    The shape is frozen so callers can't mutate the conversion result
    between bridge and store; the dedupe key + provenance live under
    ``parameters._foresight`` so a downstream listener can introspect
    "which ProjectedDecision spawned this proposal" without a second
    round trip.
    """

    pocket_id: str
    title: str
    description: str
    recommendation: str
    category: str  # ActionCategory value — bridge stays string-typed
    priority: str  # ActionPriority value
    parameters: dict[str, Any]
    trigger_type: str
    trigger_source: str
    trigger_reason: str
    assignee: str | None = None


# ---------------------------------------------------------------------------
# Sub-type-aware label rendering — the title/description text the operator
# sees in The Tray. Each sub-type has a distinct anchor convention:
#
#   decision_forecast → ``decision:<name>``     ("a single decision to make")
#   market_sim        → ``segment:<role>``      ("how a segment responds")
#   org_change_*      → ``rollout:<event>``     ("how a rollout step lands")
#
# Anything unrecognized falls back to a neutral "Forecast" label so a
# new sub-type added in a later PR doesn't blow up here.
# ---------------------------------------------------------------------------


def _label_for_sub_type(sub_type: str) -> str:
    """Short verb label rendered in the proposal title (e.g. ``Forecast``,
    ``Segment forecast``, ``Rollout forecast``).

    Kept tiny so the conversion stays readable; a future sub-type just
    drops a new branch here without touching the rest of the bridge.
    """
    return {
        "decision_forecast": "Forecast",
        "market_sim": "Segment forecast",
        "org_change_rehearsal": "Rollout forecast",
    }.get(sub_type, "Forecast")


def _humanize_anchor(anchor_id: str) -> str:
    """Strip the ``<kind>:`` prefix off an anchor id and Title-Case the
    remainder so a label like ``decision:lease-renewal`` reads as
    ``Lease-Renewal`` in the operator UI.

    Anchors that don't carry the convention (no colon) come back
    unchanged so the proposal still surfaces a useful string.
    """
    if ":" not in anchor_id:
        return anchor_id
    _, _, suffix = anchor_id.partition(":")
    return suffix.replace("_", "-")


def _priority_from_confidence(confidence: float) -> str:
    """Map projection confidence to an :class:`ActionPriority` value.

    The mapping mirrors the band convention the rest of the codebase
    uses (low / medium / high / critical) without importing the enum.
    Strings are validated on the receiving service when the bridge
    output is handed to ``InstinctStore.propose``.

    Bands:
      - confidence >= 0.9 → critical
      - confidence >= 0.7 → high
      - confidence >= 0.4 → medium
      - otherwise        → low
    """
    if confidence >= 0.9:
        return "critical"
    if confidence >= 0.7:
        return "high"
    if confidence >= 0.4:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Conversion entry point — the cloud service calls this once per
# (anchor × tick) bucket after the ProjectedDecision document is written.
# ---------------------------------------------------------------------------


def projected_decision_to_instinct_proposal(
    pd: Any,
    scenario_config: dict[str, Any] | None = None,
    *,
    assignee: str | None = None,
) -> InstinctProposal:
    """Convert one ProjectedDecision into the proposal shape Instinct's
    store expects.

    Args:
        pd: a :class:`pocketpaw_ee.cloud.foresight.domain.ProjectedDecision`
            (or any object exposing the same attribute set: ``workspace_id``,
            ``run_id``, ``anchor_id``, ``persona_id``, ``tick_id``,
            ``decision_text``, ``confidence``, ``sub_type``,
            ``forward_precedent_decision_id``). The bridge stays
            duck-typed so test doubles don't need to import the cloud
            domain module.
        scenario_config: the scenario's wire dict (the
            ``CreateScenarioRequest`` body the operator posted). The
            bridge currently pulls ``name`` for the description and
            forwards the rest as provenance. Pass ``None`` when the
            caller doesn't have a body handy (e.g. a future
            calibration-loop replay) — the bridge defaults the
            scenario name to the projection's ``sub_type``.
        assignee: optional human user-id who should see this proposal in
            The Tray. Default ``None`` — the proposal becomes a
            workspace-wide pending row picked up by the unfiltered
            pending feed. Forwarded directly to
            :meth:`InstinctStore.propose`.

    Returns:
        An :class:`InstinctProposal` carrying everything the cloud
        service needs to build the ``ActionTrigger`` + call
        ``store.propose``. The ``parameters`` dict includes the
        ``_foresight`` provenance block so a downstream consumer (the
        Tray UI rendering the "Why?" drawer) can rehydrate the
        originating run / tick / anchor without a second API call.

    Per RFC 08 §8: the proposal is EVIDENCE, not an executing write.
    Category is :class:`ActionCategory.DATA`; no ``_pocket_write`` blob
    is parked; the Instinct policy that already gates the underlying
    real decision still owns the predicate. Approving the surfaced
    proposal simply acknowledges the forecast — it does NOT trigger any
    side-effect beyond the audit row.
    """
    workspace_id = str(getattr(pd, "workspace_id", "") or "")
    run_id = str(getattr(pd, "run_id", "") or "")
    anchor_id = str(getattr(pd, "anchor_id", "") or "")
    persona_id = str(getattr(pd, "persona_id", "") or "")
    tick_id = int(getattr(pd, "tick_id", 0) or 0)
    decision_text = str(getattr(pd, "decision_text", "") or "noop")
    confidence = float(getattr(pd, "confidence", 0.0) or 0.0)
    sub_type = str(getattr(pd, "sub_type", "decision_forecast") or "decision_forecast")
    forward_precedent = getattr(pd, "forward_precedent_decision_id", None)

    scenario_name = ""
    if scenario_config:
        scenario_name = str(scenario_config.get("name") or "")

    label = _label_for_sub_type(sub_type)
    anchor_label = _humanize_anchor(anchor_id) or "anchor"
    title = f"{label}: {decision_text} for {anchor_label} (tick {tick_id})"
    description = (
        f"Foresight projected '{decision_text}' for {anchor_label} at tick {tick_id} "
        f"with confidence {confidence:.2f}."
    )
    if scenario_name:
        description += f" Scenario: {scenario_name}."
    recommendation = (
        "Review this projection before the matching real-world decision "
        "lands. Approving acknowledges the forecast; rejecting flags it "
        "for calibration follow-up."
    )

    dedupe_key = build_dedupe_key(
        workspace_id=workspace_id,
        run_id=run_id,
        tick_id=tick_id,
        anchor_id=anchor_id,
        persona_id=persona_id,
    )

    # ``_foresight`` is the provenance block any consumer (Tray Why?
    # drawer, calibration loop) can read back. The ``dedupe_key`` field
    # is what the cloud's read-before-write check compares against.
    parameters: dict[str, Any] = {
        "_foresight": {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "tick_id": tick_id,
            "anchor_id": anchor_id,
            "persona_id": persona_id,
            "sub_type": sub_type,
            "decision_text": decision_text,
            "confidence": confidence,
            "forward_precedent_decision_id": forward_precedent,
            "scenario_name": scenario_name,
            "dedupe_key": dedupe_key,
        },
    }

    # Synthetic pocket id binds the proposal to the foresight run; the
    # Instinct store treats ``pocket_id`` as a free-form string (no FK).
    # Pending-feed callers can filter by this prefix to recover all
    # foresight-spawned proposals across a single run.
    pocket_id = f"foresight:run:{run_id}" if run_id else "foresight:run:unknown"

    return InstinctProposal(
        pocket_id=pocket_id,
        title=title,
        description=description,
        recommendation=recommendation,
        category="data",
        priority=_priority_from_confidence(confidence),
        parameters=parameters,
        trigger_type="foresight",
        trigger_source=f"run:{run_id}" if run_id else "run:unknown",
        trigger_reason=(
            f"Foresight projected '{decision_text}' at tick {tick_id} for "
            f"{anchor_label}; surfaced as evidence per RFC 08 §8."
        ),
        assignee=assignee,
    )


__all__ = [
    "InstinctProposal",
    "build_dedupe_key",
    "projected_decision_to_instinct_proposal",
]
