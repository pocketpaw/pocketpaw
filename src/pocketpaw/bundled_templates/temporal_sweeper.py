# src/pocketpaw/bundled_templates/temporal_sweeper.py
# Created: 2026-05-28 (feat/rfc-03-v2-temporal) — pure OSS-side
# decision function for the RFC 03 v2 ``temporal`` trigger type.
# Detects rising-edge transitions (false → true) of a per-row CEL
# ``when`` predicate across sweeps. The EE sweeper (``ee/cloud/
# pockets/``) calls this on a cadence (typically hourly); cadence,
# state persistence, and action dispatch are all caller concerns.
"""Temporal trigger sweeper for the RFC 03 v2 schema.

A ``temporal`` trigger declares a CEL predicate. The runtime evaluates
that predicate per-row each sweep. When the predicate transitions
*from false to true* for a given row, the trigger fires — exactly
once per transition. Continuing-true does NOT re-fire. Falling
true→false also does NOT fire but updates internal state so a future
true-transition can fire again.

The function is stateless across calls. The caller hands in the prior
sweep's ``new_state`` mapping and gets back an updated mapping plus
the list of rising edges. Persistence is the caller's job.

Pure library function:

* No I/O, no global mutable state.
* Deterministic given a fixed ``now``.
* Per-row CEL eval failures are isolated — the sweep continues for
  other rows / triggers; the failure is reported via
  :class:`TemporalSweepError`.

Out of scope for PR 2f
----------------------

* The actual sweep cadence (cron / scheduler) — lives in
  ``ee/cloud/pockets/``.
* State persistence between sweeps — caller owns storage.
* Action dispatch when a rising edge fires — caller decides what
  to do with the :class:`TemporalRisingEdge` records.
* ``cron`` / ``source_change`` / ``webhook`` / ``signal`` /
  ``calendar`` / ``manual`` trigger types — they predate v2 and
  are processed elsewhere.
* Bulk fan-out (PR 2e).
* Fabric ``tier: registered`` enforcement (PR 2g).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from pocketpaw.bundled_templates.cel_runtime import (
    CelEvaluationError,
    evaluate_cel,
)
from pocketpaw.bundled_templates.identifier_resolver import (
    IdentifierResolver,
    TemplateIdentifierResolver,
)
from pocketpaw.bundled_templates.schema import PocketTemplate, TriggerDef


class TemporalRisingEdge(BaseModel):
    """One rising-edge transition (false → true) detected by the sweep.

    Carries enough context for the caller to dispatch — the original
    :class:`TriggerDef`, the row that fired, the row's resolved id,
    and the action name (if the trigger declared one).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    trigger: TriggerDef
    row_id: str
    row: dict[str, Any]
    action: str | None


class TemporalSweepError(BaseModel):
    """A single (trigger, row) failure during the sweep.

    The sweep continues past per-row CEL failures so one bad row
    doesn't poison the whole tick. Each failure surfaces as one
    :class:`TemporalSweepError`; the caller can log / alert / skip
    as appropriate.

    ``row_id`` is ``None`` when the failure happened during row-id
    resolution itself (e.g. the row didn't carry the configured
    identifier field).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    trigger: TriggerDef
    row_id: str | None
    action: str | None
    message: str


class SweepResult(BaseModel):
    """Aggregate output of one sweep.

    Three orthogonal slices:

    * ``rising_edges`` — every (trigger, row) that flipped false → true
      on this sweep. Caller dispatches actions / notifications off
      this list.
    * ``new_state`` — the full updated trigger × row state map. The
      caller persists this and passes it back as ``last_seen_state``
      on the next sweep. Includes unchanged rows so the map fully
      describes the post-sweep world; rows whose CEL eval failed
      are deliberately omitted so a future successful eval can still
      detect a rising edge against their prior state.
    * ``errors`` — per-row failures encountered during the sweep.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    rising_edges: list[TemporalRisingEdge]
    new_state: dict[tuple[str, str], bool]
    errors: list[TemporalSweepError]


def sweep_temporal_triggers(
    template: PocketTemplate,
    rows: list[dict[str, Any]],
    *,
    last_seen_state: dict[tuple[str, str], bool] | None = None,
    resolver: IdentifierResolver | None = None,
    now: datetime | None = None,
    row_id_field: str | None = None,
) -> SweepResult:
    """Evaluate every ``type: temporal`` trigger on ``template`` against
    each row in ``rows`` and report the rising-edge transitions.

    Parameters
    ----------
    template:
        The :class:`PocketTemplate` carrying ``triggers[]``. Only
        entries with ``type == "temporal"`` are processed — all other
        types are silently skipped.
    rows:
        The current row set the temporal predicate should be
        evaluated against. Each row is a plain dict (the CEL evaluator
        wraps via :func:`celpy.json_to_cel`).
    last_seen_state:
        Mapping from ``(trigger_key, row_id)`` to the prior sweep's
        evaluated truth value. ``None`` is treated as empty — every
        currently-true row counts as a rising edge.
    resolver:
        Implements :class:`IdentifierResolver`. Defaults to
        :class:`TemplateIdentifierResolver` built off ``template.state``.
    now:
        Wall-clock injection for the CEL ``within(field, duration)``
        function. Defaults to ``datetime.now(timezone.utc)``. Tests
        pass a fixed value for determinism.
    row_id_field:
        Override the column field used to extract each row's
        identifier. Resolution order: explicit arg →
        ``template.state.id_field`` → ``"id"``.

    Returns
    -------
    :class:`SweepResult`:
        Rising edges, full updated state, and per-row errors.
    """
    if last_seen_state is None:
        last_seen_state = {}
    if resolver is None:
        resolver = TemplateIdentifierResolver(template.state)
    if now is None:
        now = datetime.now(UTC)
    resolved_id_field = (
        row_id_field
        if row_id_field is not None
        else (template.state.id_field if template.state.id_field else "id")
    )

    # Enumerate the temporal triggers up front so we can detect
    # ``action`` collisions and synthesise stable disambiguated keys.
    # ``trigger_key_for[i]`` is the key used by the i-th trigger in
    # ``template.triggers`` (only present for temporal triggers).
    trigger_key_for: dict[int, str] = {}
    action_counts: dict[str, int] = {}
    for idx, trigger in enumerate(template.triggers):
        if trigger.type != "temporal":
            continue
        if trigger.action:
            action_counts[trigger.action] = action_counts.get(trigger.action, 0) + 1
    for idx, trigger in enumerate(template.triggers):
        if trigger.type != "temporal":
            continue
        if trigger.action and action_counts[trigger.action] == 1:
            trigger_key_for[idx] = trigger.action
        elif trigger.action:
            # Multiple temporal triggers share this action name —
            # disambiguate with the template-stable position index.
            trigger_key_for[idx] = f"temporal_{trigger.action}_{idx}"
        else:
            # No action declared — synthesise a positional key.
            trigger_key_for[idx] = f"temporal_{idx}"

    rising_edges: list[TemporalRisingEdge] = []
    new_state: dict[tuple[str, str], bool] = {}
    errors: list[TemporalSweepError] = []

    for idx, trigger in enumerate(template.triggers):
        if trigger.type != "temporal":
            continue

        trigger_key = trigger_key_for[idx]
        expression = trigger.when
        # The schema validator already guarantees ``when`` is set on
        # ``type: temporal``, but guard defensively for direct
        # constructor callers / tests.
        if not expression:
            errors.append(
                TemporalSweepError(
                    trigger=trigger,
                    row_id=None,
                    action=trigger.action,
                    message="temporal trigger missing 'when' expression",
                )
            )
            continue

        for row in rows:
            # Row-id resolution. A missing id field is reported and
            # skipped (we can't key state without an id).
            if resolved_id_field not in row:
                errors.append(
                    TemporalSweepError(
                        trigger=trigger,
                        row_id=None,
                        action=trigger.action,
                        message=(f"row is missing the configured id field {resolved_id_field!r}"),
                    )
                )
                continue
            row_id = str(row[resolved_id_field])

            # Per-row CEL eval — failures isolate, the sweep continues.
            try:
                value = evaluate_cel(expression, row, resolver, now=now)
            except CelEvaluationError as exc:
                errors.append(
                    TemporalSweepError(
                        trigger=trigger,
                        row_id=row_id,
                        action=trigger.action,
                        message=str(exc),
                    )
                )
                # Deliberately do NOT write new_state for this row ×
                # trigger — we preserve the caller's prior state so a
                # future successful eval can still detect a rising
                # edge against it.
                continue

            current = bool(value)
            prior = last_seen_state.get((trigger_key, row_id), False)
            if current and not prior:
                rising_edges.append(
                    TemporalRisingEdge(
                        trigger=trigger,
                        row_id=row_id,
                        row=row,
                        action=trigger.action,
                    )
                )
            new_state[(trigger_key, row_id)] = current

    return SweepResult(
        rising_edges=rising_edges,
        new_state=new_state,
        errors=errors,
    )


__all__ = [
    "SweepResult",
    "TemporalRisingEdge",
    "TemporalSweepError",
    "sweep_temporal_triggers",
]
