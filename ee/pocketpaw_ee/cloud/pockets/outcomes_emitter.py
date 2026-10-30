# ee/pocketpaw_ee/cloud/pockets/outcomes_emitter.py
# Created: 2026-05-28 (feat/wave-3c-outcomes) — emitter for RFC 03 v2
# template-level outcome events. Per the RFC §"actions[] sub-schema"
# (``outcomes_emitted: list[str]``) and §"Integration touchpoints"
# (Outcomes meter): when an action successfully completes (HTTP 2xx
# from ``action_executor.run_action``), emit each declared outcome
# event so the Outcomes meter can count billable events. Bulk actions
# emit one event per row.
#
# Wave 3c scope (library + wiring, locked by the architect brief):
#
# * Public function ``emit_outcomes(...)`` looks up the action on the
#   template, builds one ``OutcomeEmitted`` event per name in the
#   action's ``outcomes_emitted`` list, fires each via the realtime bus,
#   and appends a single audit-log entry summarising the batch.
# * Wired into ``action_executor.run_action`` on the HTTP 2xx success
#   path (after the success audit, before the return). Failure,
#   blocked, and approval-pending paths DO NOT emit — outcomes are
#   billable; only confirmed success counts.
# * Wired into ``bulk_dispatch._fire_executions`` per successful row,
#   so a bulk run that executes 50 rows fires the row-finalized outcome
#   50 times. The bulk path deliberately does NOT thread ``template``
#   into ``run_action`` (Wave 3b: skipping re-gate-eval on the re-entry);
#   the emitter is therefore invoked directly from the bulk wrapper
#   instead of riding the executor's per-row emit.
#
# Out of scope (separate PRs):
#
# * Outcomes meter consumer / billing logic / persistence of outcome
#   counts. PR 3c ships the emitter + the wire-up; downstream meter
#   consumes the bus events.
# * Outcome persistence (Beanie doc for outcomes ledger). The M2b.2
#   ``PocketOutcomeEvent`` has a ledger writer at ``outcomes/service.py``;
#   the RFC 03 v2 ``OutcomeEmitted`` event will get its own listener in
#   the meter PR.
# * Idempotency for retried action invocations — a retried action that
#   succeeds twice will emit its outcomes twice. The meter PR can
#   dedupe via ``(workspace_id, pocket_id, action_name, row_id,
#   event_name, idempotency_key)`` if needed.
#
# Import-linter posture: this module is Beanie-PURE. It calls the
# realtime bus (a permitted writer) and the security audit log (a
# permitted writer) but never imports a Beanie document class.

"""Outcome event emission for RFC 03 v2 template-level outcomes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pocketpaw.bundled_templates import PocketTemplate
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import OutcomeEmitted

logger = logging.getLogger(__name__)


def _audit_outcomes_emitted(
    *,
    actor: str,
    workspace_id: str,
    pocket_id: str,
    action_name: str,
    row_id: str,
    count: int,
    event_names: list[str],
) -> None:
    """Write a single audit-log entry per ``emit_outcomes`` call.

    One line per call, NOT per event — keeps the audit log readable
    when an action emits many outcomes across many rows. The Outcomes
    meter consumer reads the bus events; this audit entry is for
    debug / forensic review of billable event emission.

    Mirrors the shape of ``action_executor._audit_action_run``: category
    ``pocket_backend_config``, severity INFO (outcomes are routine
    success-path events; a WARNING would crowd the audit log on every
    successful row). Audit failures must not break emission, so the
    call is wrapped — emitting an outcome is the billable side-effect;
    losing the audit line is a smaller failure than losing the event.
    """
    if count == 0:
        # No emissions → no audit entry. Keeps the log clean for the
        # common case of an action with empty ``outcomes_emitted``.
        return
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        fields: dict[str, Any] = {
            "pocket_id": pocket_id,
            "pocket_action": action_name,
            "row_id": row_id,
            "outcome_count": count,
            "outcome_names": event_names,
        }

        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor=actor,
                action="pocket.outcomes.emit",
                target=pocket_id,
                status="emitted",
                category="pocket_backend_config",
                workspace_id=workspace_id,
                **fields,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break emission
        logger.warning("pocket outcomes audit-log write failed", exc_info=True)


def _find_action(template: PocketTemplate, action_name: str) -> Any:
    """Return the ``ActionDef`` matching ``action_name`` on ``template``.

    Raises ``NotFound("outcome_action", action_name)`` when no action
    with that name is declared — defensive; the caller should have
    validated. Matches the ``NotFound`` shape ``instinct_dispatch`` uses
    for an unknown action.
    """
    for action in template.actions:
        if action.name == action_name:
            return action
    raise NotFound("outcome_action", action_name)


async def emit_outcomes(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    template: PocketTemplate,
    action_name: str,
    row_id: str,
    row_context: dict[str, Any],
    audit_metadata: dict[str, Any] | None = None,
) -> list[OutcomeEmitted]:
    """Emit one ``OutcomeEmitted`` event per name in
    ``template.actions[action_name].outcomes_emitted``.

    Steps (per the architect brief):

    1. Look up the ``ActionDef`` for ``action_name`` on ``template``.
       Unknown action → ``NotFound`` (defensive — the caller should
       have validated).
    2. For each name in ``action.outcomes_emitted``, build one
       ``OutcomeEmitted`` event carrying the canonical payload (see
       ``events.OutcomeEmitted`` docstring for the field shape).
    3. ``await emit(event)`` for each via the realtime bus.
    4. Append a single audit-log entry summarising the batch (one
       line per ``emit_outcomes`` call, not per event).
    5. Return the list of emitted events for caller introspection.

    The function is pure with respect to its inputs — no template
    mutation, no row_context mutation. The bus emit + audit entry are
    the only side effects.

    Args
    ----
    workspace_id:
        Tenancy. Rides on every emitted event.
    user_id:
        The actor — the user whose successful action triggered the
        emit. Logged on the audit entry; NOT carried on the event
        payload (the Outcomes meter aggregates by workspace + pocket
        + action; the actor is a debug / forensic detail).
    pocket_id:
        The pocket the action ran on.
    template:
        The validated ``PocketTemplate`` (already passed through
        ``model_validate`` upstream — the schema's
        ``_outcomes_emitted_subset`` validator already enforced that
        every entry in ``outcomes_emitted`` exists in the top-level
        ``outcomes[]`` catalog).
    action_name:
        Must match an entry in ``template.actions``. Unknown →
        ``NotFound``.
    row_id:
        Stable identifier for the row the action ran on. Empty string
        when the action does not bind to a row (e.g. a page-level
        action).
    row_context:
        The row dict at emit time. Captured verbatim on each event so
        the meter sees what payload triggered the emit.
    audit_metadata:
        Reserved for future use (e.g. an idempotency key or a
        correlation id). Currently unused; pass ``None``.

    Returns
    -------
    list[OutcomeEmitted]
        The events that were placed on the bus, in declaration order.
        Empty when ``outcomes_emitted`` is empty.

    Raises
    ------
    NotFound
        ``action_name`` is not declared on ``template``.
    """
    _ = audit_metadata  # reserved for a future idempotency-key path

    action = _find_action(template, action_name)
    names = list(action.outcomes_emitted)

    if not names:
        # No outcomes declared → nothing to emit, no audit entry. The
        # caller doesn't need to wrap the call in a guard.
        return []

    now_iso = datetime.now(UTC).isoformat()
    events: list[OutcomeEmitted] = []

    for event_name in names:
        payload: dict[str, Any] = {
            "event_name": event_name,
            "workspace_id": workspace_id,
            "pocket_id": pocket_id,
            "action_name": action_name,
            "row_id": row_id,
            "row_context_snapshot": dict(row_context),
            "emitted_at": now_iso,
            "template_name": template.name,
            "template_version": template.version,
        }
        evt = OutcomeEmitted(data=payload)
        # ``emit`` swallows bus failures; we still want the event in our
        # return list because the caller is asserting that emission
        # happened from this function's perspective (and the dropped
        # event has already been logged by ``emit``).
        await emit(evt)
        events.append(evt)

    _audit_outcomes_emitted(
        actor=user_id,
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        action_name=action_name,
        row_id=row_id,
        count=len(events),
        event_names=names,
    )
    return events


__all__ = ["emit_outcomes"]
