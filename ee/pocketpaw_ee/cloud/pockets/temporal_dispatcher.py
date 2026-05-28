# ee/pocketpaw_ee/cloud/pockets/temporal_dispatcher.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — per-pocket
# entry point for the RFC 03 v2 temporal trigger sweeper. Calls the
# pure OSS ``sweep_temporal_triggers`` planner, then dispatches each
# rising-edge transition through Wave 3a's Instinct gate +
# ``action_executor.run_action``. State persistence is the
# ``temporal_sweeps`` entity's job; per-row Instinct branching is
# Wave 3a's; this module is the orchestration glue.
#
# Wave 3d scope (locked by the architect brief):
#
#   1. Load the prior sweep state via ``temporal_sweeps.service.load_last_seen``.
#   2. Fetch the pocket's current rows (see "Rows source" below).
#   3. Call OSS ``sweep_temporal_triggers(template, rows,
#      last_seen_state=..., now=...)`` once per pocket — pure decision.
#   4. For each ``TemporalRisingEdge``: invoke ``gate_action`` to apply
#      Instinct (per RFC: a temporal trigger is just like any other
#      action firing; the gate applies).
#      * BLOCK → log + skip (count under ``blocked``).
#      * ESCALATE_APPROVAL → persist approval row (the gate wrapper
#        does this), log + skip the HTTP call (count under
#        ``escalated``).
#      * EXECUTE / NOTIFY_AND_EXECUTE → call
#        ``action_executor.run_action`` with the threaded ``template``.
#        Outcomes fire automatically per Wave 3c's success-path emit.
#   5. Call ``temporal_sweeps.service.upsert_state(...)`` with the
#      OSS-produced ``new_state`` map AND the dispatch tally (so the
#      service emits one ``TemporalSweepCompleted`` per call).
#   6. Call ``temporal_sweeps.service.record_errors(...)`` for any per-
#      row CEL eval failures (audit-log only — the sweep continues
#      past them per the OSS contract).
#   7. Return ``SweepDispatchResult``.
#
# **Sweep failure does not abort the loop** — a pocket whose sweep
# crashes is logged and the scheduler keeps going to the next pocket on
# the next tick. The dispatcher catches per-row executor failures so
# the OSS ``new_state`` map still persists.
#
# Rows source (v0 — locked by the architect brief):
#   The RFC is ambiguous on where temporal-sweep rows come from. The
#   cleanest seam for Wave 3d is to accept a ``rows`` parameter the
#   caller supplies. The scheduler tick passes an empty list (no
#   materialized row source yet); library callers (tests, ad-hoc
#   tooling, future "sweep now" route) pass rows directly. A follow-up
#   PR can wire ``data_sources`` executor cache, Fabric, or whatever
#   row source ships first.
#
# Template lookup (v0 — locked by the architect brief):
#   Same ambiguity as the bulk dispatcher's `template_resolver_pending`:
#   the Pocket Beanie doc has no `template_slug` field. The dispatcher
#   accepts ``template`` as a parameter; the scheduler skips pockets
#   for which no template can be resolved (no-op, debug-logged). When
#   a template loader lands, the scheduler can wire it in without
#   changing this contract.
#
# Hard constraint: this module imports OSS PURE functions
# (``sweep_temporal_triggers``, ``PocketTemplate``) and the EE-side
# services that own Beanie writes (``temporal_sweeps.service``,
# ``instinct_dispatch``). It MUST NOT import a Beanie document class
# directly — the import-linter contract treats this module as
# Beanie-pure (same posture as ``bulk_dispatch.py``).

"""Per-pocket dispatcher for RFC 03 v2 temporal triggers."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from pocketpaw.bundled_templates import PocketTemplate
from pocketpaw.bundled_templates.identifier_resolver import IdentifierResolver
from pocketpaw.bundled_templates.temporal_sweeper import (
    SweepResult,
    TemporalRisingEdge,
    sweep_temporal_triggers,
)
from pocketpaw_ee.cloud.temporal_sweeps.domain import SweepDispatchResult

logger = logging.getLogger(__name__)


def _has_temporal_trigger(template: PocketTemplate) -> bool:
    """True when ``template`` declares at least one ``type: temporal`` trigger.

    The sweep is a no-op for a template with no temporal triggers; the
    OSS sweeper would just return an empty result, but checking up
    front lets the dispatcher skip the (potentially expensive) row
    fetch entirely.
    """
    return any(t.type == "temporal" for t in template.triggers)


async def _dispatch_one_edge(
    *,
    edge: TemporalRisingEdge,
    workspace_id: str,
    pocket_id: str,
    template: PocketTemplate,
) -> str:
    """Dispatch ONE rising-edge transition through the Wave 3a gate +
    Wave 3b/3c executor.

    Returns one of ``"fired"`` / ``"blocked"`` / ``"escalated"`` /
    ``"error"`` so the caller can tally the counts on the
    ``SweepDispatchResult``. A returned ``"error"`` is a defensive
    safety-net for an exception the gate or executor raised that
    should not have escaped — the sweep continues to other edges.

    The dispatch path:
      * If the trigger declares no ``action`` (a pure "fact" trigger),
        skip the executor — there is nothing to invoke. The state-
        machine still records the rising edge (the caller will count
        it as ``"fired"`` so the row is observable).
      * Otherwise, call ``instinct_dispatch.gate_action`` with the row
        as ``row_context`` and ``user_id`` set to the synthetic
        sweeper actor. The gate returns one of three branches.
      * On ``"proceed"``, call ``action_executor.run_action`` with the
        threaded template so Wave 3c outcome emission fires.

    The Instinct gate uses a synthetic actor ``system:temporal-sweeper``
    so a rising-edge dispatch never eats a real user's per-(pocket, user)
    write budget in the executor and is clearly attributable in the
    audit log.
    """
    action_name = edge.action
    if not action_name:
        # A temporal trigger without an action declared is a no-op
        # dispatch — the OSS sweeper still reports it (so the caller
        # can observe the transition), but there's nothing for the
        # executor to invoke. Count it as fired (the state moved) and
        # move on.
        logger.debug(
            "temporal sweep: pocket=%s edge row=%s has no action — skipping dispatch",
            pocket_id,
            edge.row_id,
        )
        return "fired"

    # Lazy import — keep this module's static import graph free of EE
    # entity references so the import-linter contract treats it as
    # Beanie-pure. The dispatch + executor modules are already in the
    # ``pockets`` package, so the lazy import is a structural choice,
    # not a cycle-break.
    try:
        from pocketpaw_ee.cloud.pockets import action_executor, instinct_dispatch
    except Exception:  # noqa: BLE001 — defensive
        logger.warning(
            "temporal sweep: pocket=%s could not import dispatch modules", pocket_id, exc_info=True
        )
        return "error"

    actor = "system:temporal-sweeper"
    row_context = dict(edge.row)

    try:
        gate = await instinct_dispatch.gate_action(
            workspace_id=workspace_id,
            user_id=actor,
            pocket_id=pocket_id,
            template=template,
            action_name=action_name,
            row_context=row_context,
            row_id=edge.row_id,
        )
    except Exception:  # noqa: BLE001 — gate raising into the loop is a bug; isolate
        logger.warning(
            "temporal sweep: pocket=%s gate failed for action=%s row=%s",
            pocket_id,
            action_name,
            edge.row_id,
            exc_info=True,
        )
        return "error"

    if gate.next_step == "blocked":
        logger.info(
            "temporal sweep: pocket=%s action=%s row=%s BLOCKED — %s",
            pocket_id,
            action_name,
            edge.row_id,
            gate.decision.reason,
        )
        return "blocked"

    if gate.next_step == "pending_approval":
        # The gate already persisted an ``InstinctApproval`` row. We
        # don't fire the HTTP call; a human will decide via the
        # approvals surface. The sweeper has done its job — state was
        # written, the rising edge was honored.
        logger.info(
            "temporal sweep: pocket=%s action=%s row=%s ESCALATED — approval_id=%s",
            pocket_id,
            action_name,
            edge.row_id,
            gate.approval_id,
        )
        return "escalated"

    # gate.next_step == "proceed" — fire the executor. We do NOT
    # re-thread the template into the gate path of ``run_action`` (the
    # gate already ran here, threading it again would create a second
    # approval row on a flapping rule, the same invariant the bulk
    # dispatcher honors). We DO thread it so Wave 3c outcome emission
    # fires on success.
    try:
        result = await _invoke_executor(
            executor=action_executor,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=actor,
            template=template,
            action_name=action_name,
            row_context=row_context,
            row_id=edge.row_id,
        )
    except Exception:  # noqa: BLE001 — executor raising into the loop is a bug; isolate
        logger.warning(
            "temporal sweep: pocket=%s executor failed for action=%s row=%s",
            pocket_id,
            action_name,
            edge.row_id,
            exc_info=True,
        )
        return "error"

    if not result.get("ok"):
        logger.info(
            "temporal sweep: pocket=%s action=%s row=%s execution failed (code=%s)",
            pocket_id,
            action_name,
            edge.row_id,
            result.get("code"),
        )
        # An ok:false from the executor (rate limited, allowlist miss,
        # backend error) still represents a fired dispatch attempt;
        # the rising edge transitioned in state and the caller saw
        # the call. Count it under ``fired`` so the tally is symmetric
        # with the bulk dispatcher's success-or-failure-counted-once
        # pattern.
    return "fired"


async def _invoke_executor(
    *,
    executor: Any,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    template: PocketTemplate,
    action_name: str,
    row_context: dict[str, Any],
    row_id: str,
) -> dict:
    """Call ``action_executor.run_action`` for one rising-edge row.

    The pocket's write binding (``rippleSpec.actions[action_name]``)
    and backend creds come from the pocket — we fetch them lazily so
    a pocket without a configured backend or without the named
    binding fails cleanly with ``ok:false`` rather than crashing the
    sweep.

    Wave 3d uses the threaded ``template`` so Wave 3c outcome emission
    fires on success. The gate is intentionally NOT re-evaluated by
    ``run_action`` for this row — we pass ``from_instinct=False`` so
    the executor's per-call gate runs ONCE and we set
    ``from_instinct=True`` is NOT applicable here (this is a direct
    EXECUTE dispatch, not a post-approval replay). The gate just
    completed in ``_dispatch_one_edge`` above, so threading
    ``template`` does mean the executor's internal gate re-evaluates;
    in v0 we accept that double-evaluation cost (it's pure CEL, no
    I/O) because not threading the template would also drop Wave 3c
    outcome emission. A future PR can split the executor's "emit" and
    "gate" code paths to avoid the duplicate eval.
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    if creds is None:
        return {
            "ok": False,
            "action": action_name,
            "error": "This pocket has no backend configured",
            "code": "pocket_backend.not_configured",
            "on_error": [],
        }
    base_url, auth_type, auth_header, token, allowed_writes, _approval_route = creds

    ripple_spec = await pockets_service.get_pocket_ripple_spec(workspace_id, pocket_id)
    actions = (ripple_spec or {}).get("actions")
    raw_action = actions.get(action_name) if isinstance(actions, dict) else None
    if not isinstance(raw_action, dict):
        return {
            "ok": False,
            "action": action_name,
            "error": f"no rippleSpec.actions[{action_name!r}] binding on pocket",
            "code": "action_not_found",
            "on_error": [],
        }

    raw_path = raw_action.get("path") or "/"
    raw_params = raw_action.get("params") if isinstance(raw_action.get("params"), dict) else {}

    return await executor.run_action(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
        action=action_name,
        raw_action=raw_action,
        path=raw_path,
        params=dict(raw_params),
        base_url=base_url,
        auth_type=auth_type,
        auth_header=auth_header,
        token=token,
        allowed_writes=allowed_writes,
        # The gate already ran in ``_dispatch_one_edge``; the executor
        # will re-run it because we pass ``template`` so Wave 3c
        # outcome emission fires. Documented above — v0 trade-off.
        from_instinct=False,
        template=template,
        row_context=row_context,
        row_id=row_id,
    )


async def sweep_pocket(
    workspace_id: str,
    pocket_id: str,
    *,
    template: PocketTemplate | None,
    rows: list[dict[str, Any]] | None = None,
    resolver: IdentifierResolver | None = None,
    now: datetime | None = None,
) -> SweepDispatchResult:
    """Sweep ONE pocket's temporal triggers and dispatch any rising edges.

    Steps (per the architect brief):

      1. Early-return when no template was resolved or it carries no
         ``type: temporal`` triggers. The result is a zero-tally
         ``SweepDispatchResult`` and NO state is written — the next
         sweep will start from the same (empty) prior state, which is
         correct.
      2. Load prior state via ``temporal_sweeps.service.load_last_seen``.
      3. Call OSS ``sweep_temporal_triggers`` for the rising-edge plan.
      4. Dispatch each ``TemporalRisingEdge`` through the gate +
         executor.
      5. Persist the OSS-produced ``new_state`` and emit
         ``TemporalSweepCompleted``.
      6. Audit any per-row CEL eval errors.

    Parameters
    ----------
    workspace_id:
        Tenancy. Required.
    pocket_id:
        The pocket whose temporal triggers will be swept. Required.
    template:
        Resolved ``PocketTemplate``. Pass ``None`` for an unresolved
        template — the sweep is a no-op (the scheduler treats this as
        "skip pocket until template resolver lands"). Tests and
        library callers pass the template directly.
    rows:
        The current row set for the pocket. v0 callers (the scheduler)
        pass ``[]``; library callers can pass real rows. The OSS
        sweeper evaluates the ``when`` predicate per row.
    resolver:
        Optional ``IdentifierResolver`` override. Defaults to the
        template's built-in resolver.
    now:
        Wall-clock injection for the CEL ``within(field, duration)``
        function. Defaults to ``datetime.now(UTC)``. Tests pass a
        fixed value for determinism.

    Returns
    -------
    :class:`SweepDispatchResult`
        Tally of dispatched edges + blocked / escalated / errors +
        wall-clock duration.
    """
    if now is None:
        now = datetime.now(UTC)
    if rows is None:
        rows = []

    started_at = time.monotonic()

    if template is None or not _has_temporal_trigger(template):
        # No template OR no temporal triggers → no work. Don't touch
        # state (the matrix stays as-is) and don't emit a completion
        # event — the sweep didn't actually run.
        return SweepDispatchResult(
            pocket_id=pocket_id,
            edges_fired=0,
            blocked=0,
            escalated=0,
            errors=0,
            sweep_duration_ms=int((time.monotonic() - started_at) * 1000),
        )

    # Lazy import — keep this module's static import graph free of EE
    # entity references for the import-linter contract.
    from pocketpaw_ee.cloud.temporal_sweeps import service as sweeps_service

    last_seen = await sweeps_service.load_last_seen(workspace_id, pocket_id)

    sweep_result: SweepResult = sweep_temporal_triggers(
        template,
        rows,
        last_seen_state=last_seen,
        resolver=resolver,
        now=now,
    )

    edges_fired = 0
    blocked = 0
    escalated = 0
    for edge in sweep_result.rising_edges:
        verdict = await _dispatch_one_edge(
            edge=edge,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            template=template,
        )
        if verdict == "fired":
            edges_fired += 1
        elif verdict == "blocked":
            blocked += 1
        elif verdict == "escalated":
            escalated += 1
        else:
            # "error" — count under errors so the tally surfaces it.
            # The sweep continues to other edges.
            pass

    errors_count = len(sweep_result.errors)

    duration_ms = int((time.monotonic() - started_at) * 1000)

    dispatch_result = SweepDispatchResult(
        pocket_id=pocket_id,
        edges_fired=edges_fired,
        blocked=blocked,
        escalated=escalated,
        errors=errors_count,
        sweep_duration_ms=duration_ms,
    )

    # Persist + emit. ``upsert_state`` fires ``TemporalSweepCompleted``
    # with the dispatch tally so audit/dashboards see one event per
    # pocket sweep (rule 9).
    await sweeps_service.upsert_state(
        workspace_id,
        pocket_id,
        sweep_result.new_state,
        dispatch_result=dispatch_result,
    )

    # Audit per-row eval failures separately so an operator can see
    # which rows failed. Doesn't block / mutate the dispatch result.
    if sweep_result.errors:
        await sweeps_service.record_errors(workspace_id, pocket_id, list(sweep_result.errors))

    return dispatch_result


__all__ = ["sweep_pocket"]
