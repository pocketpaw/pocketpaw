# saga.py — Saga-pattern rollback for multi-step pocket write SEQUENCES.
# Created: 2026-06-01 (RFC 05 Saga Compensate — first pass).
#
# WHY THIS EXISTS
# ---------------
# `action_executor.run_action` runs exactly ONE write. Three callers use
# it today — the direct `/actions/run` route, `bulk_dispatch` (the SAME
# action fanned across N rows, each independent), and the Instinct
# post-approval re-entry. None of them run a SEQUENCE of DIFFERENT writes
# where a later failure must undo the earlier successes.
#
# A Chain Flow (RFC 13) — or any agent-authored multi-step write flow —
# does exactly that: step 1 reserves inventory, step 2 charges the card,
# step 3 confirms the order. If step 3 fails, steps 1+2 are already
# committed on the backend (inventory held, card charged) and must roll
# back (release the hold, refund the charge), or the backend is left in an
# inconsistent state. RFC 05 open question #2 punted CLIENT-side optimistic
# *UI* rollback as "a flow pattern, not a primitive" — but that is UI state
# (a checkbox flipping back), NOT backend compensation. This module is the
# missing server-side concern.
#
# THE SAGA PATTERN
# ----------------
# Each forward write declares a `compensate:` spec (the inverse write —
# see `action_executor.CompensateSpec`). `run_action_sequence` runs the
# steps in order through `run_action`, tracking each completed (FIRED,
# `ok:true`) write on a stack. On the FIRST failure at step K it stops
# advancing and fires the compensations for the completed steps
# 1..K-1 in REVERSE order (K-1, K-2, ..., 1), each as its own
# `run_action` call against the compensating binding. The backend is left
# consistent: every committed write is undone, newest-first.
#
# DELIBERATE SCOPE (first pass)
# -----------------------------
# * `run_action` is NOT modified — it stays a pure single-write executor so
#   the three existing callers are byte-stable. The saga ORCHESTRATES it.
# * Compensations fire AUTO (never Instinct-gated): pausing a rollback for
#   human approval would leave the backend in the inconsistent state the
#   rollback exists to repair. The forward write's gate is the human
#   checkpoint; the compensation is automatic cleanup.
# * A forward step that PARKS (`requires_instinct` → `instinct_pending`)
#   cannot be part of an AUTO saga — it never fired, so there is nothing to
#   commit and nothing for a later step to depend on. The saga treats a
#   parked step as a sequence failure and rolls back the already-committed
#   steps before it. The "saga over gated forward writes" case (collect all
#   approvals, THEN fire; or per-step approval mid-saga) is a larger design
#   recorded as an open question in the RFC.
# * A compensation that ITSELF fails does NOT abort the rollback — the
#   remaining compensations still fire (a half-rolled-back saga is worse
#   than a fully-attempted one). Failed compensations are collected and
#   surfaced under `compensation_failures` for an operator to reconcile
#   manually; they are NOT auto-compensated recursively.
# * A completed step with NO `compensate` spec is recorded as a
#   `no_compensator` gap (the rollback cannot undo it) rather than silently
#   dropped — the operator sees exactly what was left committed.
#
# OUTCOMES
# --------
# A successful compensation whose `CompensateSpec` declares an `outcome`
# emits a `pocket.outcome` event with `compensated=True` so the audit trail
# closes and the meter can net the rollback against the forward outcome.
# Forward-step outcomes are emitted by the saga too (mirroring what the
# `/actions/run` route does for a single action) so a sequence meters the
# same as N single calls would.
#
# IMPORT-LINTER POSTURE
# ---------------------
# This module is impure in the same way `instinct_bridge` is: it calls
# `outcomes_service.emit_pocket_outcome` (a permitted emitter) but never
# imports a Beanie document class. Backend creds + the per-step bindings
# arrive by parameter — `pockets/service.py` owns all Beanie access. The
# module is added to the `pockets` source_modules list in the import-linter
# contract so the Beanie-pure invariant is locked.

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pocketpaw_ee.cloud.pockets.action_executor import ActionBinding, run_action

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class SagaStep(BaseModel):
    """One step in a write sequence — a named action plus its resolved call.

    Mirrors the shape the `/actions/run` route resolves for a single
    action: the action NAME, the raw binding dict from the persisted
    ``rippleSpec.actions`` block (the executor reads ``method`` /
    ``compensate`` / governance off it — the client never picks the verb),
    and the client-resolved ``path`` / ``params`` (Ripple's ``{...}``
    resolver ran client-side).

    ``idempotency_key`` is optional — a retried saga step carries the same
    key so the backend can dedupe. The compensation gets its OWN key
    derived from the step's, so a retried rollback is also dedupable.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    raw_action: dict[str, Any]
    path: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class CompensationResult(BaseModel):
    """The outcome of one compensation attempt (or a recorded gap).

    ``status`` is one of:

    * ``compensated`` — the inverse write fired and returned ``ok:true``.
    * ``failed`` — the inverse write was attempted but the executor /
      backend rejected it. The rollback continued with the remaining
      compensations; this one needs manual reconciliation.
    * ``no_compensator`` — the completed forward step declared no
      ``compensate`` spec, so its effect could NOT be undone. The
      operator sees exactly what was left committed.

    ``response`` carries the executor's full result dict for a fired
    compensation (``ok`` / ``status`` / ``error`` / ``code``); it is
    ``None`` for a ``no_compensator`` gap.
    """

    model_config = ConfigDict(frozen=True)

    action: str
    status: str
    response: dict[str, Any] | None = None


class SagaResult(BaseModel):
    """The frozen outcome of a `run_action_sequence` call.

    ``ok`` is ``True`` only when EVERY step fired successfully — no
    rollback ran. When ``ok`` is ``False``:

    * ``failed_index`` / ``failed_action`` identify the step that failed.
    * ``failure`` is that step's executor result dict (why it failed).
    * ``completed`` lists the action names of the steps that fired before
      the failure, in execution order (the ones that got compensated).
    * ``compensations`` lists one ``CompensationResult`` per completed
      step, in the REVERSE order they fired (rollback order).
    * ``rolled_back`` is ``True`` when every completed step was
      successfully compensated; ``False`` when at least one compensation
      failed or had no compensator (the backend may be partially
      inconsistent and needs manual attention).

    ``results`` carries every fired forward step's executor result in
    execution order, for the caller / UI to reconcile.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    results: list[dict[str, Any]] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    failed_index: int | None = None
    failed_action: str | None = None
    failure: dict[str, Any] | None = None
    compensations: list[CompensationResult] = Field(default_factory=list)
    rolled_back: bool = False

    @property
    def compensation_failures(self) -> list[CompensationResult]:
        """The compensations that did NOT cleanly undo their step.

        A non-empty list means the backend may be partially inconsistent —
        an operator should reconcile the listed actions by hand.
        """
        return [c for c in self.compensations if c.status != "compensated"]


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _is_parked(result: dict[str, Any]) -> bool:
    """A forward step that routed to Instinct instead of firing."""
    return result.get("code") == "instinct_pending"


def _compensation_idempotency_key(step_key: str | None) -> str | None:
    """Derive the compensation's idempotency key from the forward step's.

    A retried rollback must carry a STABLE key so the backend dedupes it,
    but it must DIFFER from the forward write's key (else a backend that
    deduped the forward POST would drop the compensating POST as a replay).
    Suffix the forward key with ``:compensate``. ``None`` forward key →
    ``None`` (the executor mints a fresh server key on each call; a retried
    rollback then can't be deduped, which is acceptable for a first pass —
    a compensation is required to be idempotent on the backend side anyway,
    per the RFC's idempotency contract).
    """
    if not step_key:
        return None
    return f"{step_key}:compensate"


async def run_action_sequence(
    *,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    steps: list[SagaStep],
    base_url: str,
    auth_type: str,
    auth_header: str | None,
    token: str,
    allowed_writes: list[dict[str, Any]],
    correlation_id: UUID | None = None,
    emit_outcomes: bool = True,
) -> SagaResult:
    """Run a sequence of write actions with Saga compensation on failure.

    Runs ``steps`` in order through ``action_executor.run_action``. Each
    step that FIRES (``ok:true``, not parked) is pushed onto a completed
    stack. On the FIRST failure — an ``ok:false`` result OR a parked
    (``instinct_pending``) step — advancing stops and the completed steps'
    compensations fire in REVERSE order.

    Returns a :class:`SagaResult`. ``ok`` is ``True`` iff every step fired;
    otherwise the result carries the failed step, the completed steps, and
    one :class:`CompensationResult` per completed step in rollback order.

    The same backend credentials + ``allowed_writes`` apply to every step
    AND every compensation — a saga is bound to one pocket's one backend.
    A compensation whose method+path the owner has not allow-listed is
    rejected at the executor's allowlist gate, surfaced as a ``failed``
    compensation (the owner must allow-list the inverse write too).

    ``emit_outcomes`` (default ``True``) controls whether forward-step and
    compensation outcomes are emitted onto the bus. The route sets it
    ``True`` so a sequence meters like N single calls; a caller that emits
    outcomes itself (e.g. a future bulk-saga path mirroring
    ``bulk_dispatch``'s direct emit) passes ``False``.
    """
    completed: list[tuple[SagaStep, ActionBinding, dict[str, Any]]] = []
    results: list[dict[str, Any]] = []

    failed_index: int | None = None
    failed_action: str | None = None
    failure: dict[str, Any] | None = None

    for index, step in enumerate(steps):
        # Parse the binding up front so we have the `compensate` spec and
        # the `outcome` name in hand for a step that succeeds. A malformed
        # binding is a step failure (no call fires) — `run_action` would
        # reject it too, but parsing here lets us record the failed action
        # cleanly and skip the network round-trip.
        try:
            binding = ActionBinding.model_validate(step.raw_action)
        except ValidationError as exc:
            failed_index = index
            failed_action = step.action
            failure = {
                "ok": False,
                "action": step.action,
                "error": f"action binding is malformed: {exc.errors()!r}",
                "code": "bad_binding",
                "on_error": [],
            }
            break

        result = await run_action(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            action=step.action,
            raw_action=step.raw_action,
            path=step.path,
            params=step.params,
            base_url=base_url,
            auth_type=auth_type,
            auth_header=auth_header,
            token=token,
            allowed_writes=allowed_writes,
            idempotency_key=step.idempotency_key,
            correlation_id=correlation_id,
        )
        results.append(result)

        if _is_parked(result):
            # A gated forward write parked instead of firing. It is NOT
            # committed, so the sequence cannot continue past it — but the
            # steps BEFORE it ARE committed and must roll back. Treat the
            # park as a sequence failure (see the open question on gated
            # forward writes in the RFC).
            failed_index = index
            failed_action = step.action
            failure = result
            logger.info(
                "saga on pocket %s: step %d (%s) parked for Instinct — "
                "rolling back %d completed step(s)",
                pocket_id,
                index,
                step.action,
                len(completed),
            )
            break

        if not result.get("ok"):
            failed_index = index
            failed_action = step.action
            failure = result
            logger.info(
                "saga on pocket %s: step %d (%s) failed (code=%s) — "
                "rolling back %d completed step(s)",
                pocket_id,
                index,
                step.action,
                result.get("code"),
                len(completed),
            )
            break

        # Fired successfully — record it for possible rollback and emit
        # its forward outcome (mirrors the single-action route).
        completed.append((step, binding, result))
        if emit_outcomes and binding.outcome:
            await _emit_outcome(
                outcome=binding.outcome,
                pocket_id=pocket_id,
                workspace_id=workspace_id,
                action=step.action,
                actor=user_id,
                compensated=False,
            )
    else:
        # Every step fired — no rollback. Happy path.
        return SagaResult(ok=True, results=results, completed=[s.action for s, _, _ in completed])

    # ── failure path: compensate the completed steps in REVERSE ─────────
    compensations = await _compensate(
        completed=completed,
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        user_id=user_id,
        base_url=base_url,
        auth_type=auth_type,
        auth_header=auth_header,
        token=token,
        allowed_writes=allowed_writes,
        correlation_id=correlation_id,
        emit_outcomes=emit_outcomes,
    )
    rolled_back = all(c.status == "compensated" for c in compensations)

    return SagaResult(
        ok=False,
        results=results,
        completed=[s.action for s, _, _ in completed],
        failed_index=failed_index,
        failed_action=failed_action,
        failure=failure,
        compensations=compensations,
        rolled_back=rolled_back,
    )


async def _compensate(
    *,
    completed: list[tuple[SagaStep, ActionBinding, dict[str, Any]]],
    workspace_id: str,
    pocket_id: str,
    user_id: str,
    base_url: str,
    auth_type: str,
    auth_header: str | None,
    token: str,
    allowed_writes: list[dict[str, Any]],
    correlation_id: UUID | None,
    emit_outcomes: bool,
) -> list[CompensationResult]:
    """Fire the completed steps' compensations in REVERSE order.

    Best-effort: a compensation that fails does NOT stop the rollback —
    the remaining ones still fire. A completed step with no ``compensate``
    spec is recorded as a ``no_compensator`` gap. Returns one
    :class:`CompensationResult` per completed step, in rollback (reverse)
    order.
    """
    compensations: list[CompensationResult] = []

    for step, binding, _fwd_result in reversed(completed):
        spec = binding.compensate
        if spec is None:
            # No declared undo — record the gap so the operator can see
            # exactly what stayed committed. The backend is NOT consistent.
            logger.warning(
                "saga on pocket %s: step '%s' has no compensate spec — "
                "its effect was NOT rolled back",
                pocket_id,
                step.action,
            )
            compensations.append(
                CompensationResult(action=step.action, status="no_compensator", response=None)
            )
            continue

        # A compensation IS a write binding. Build its raw_action with NO
        # `requires_instinct` (it fires AUTO — never parks) and run it
        # through the same executor + the same allowlist as any write.
        comp_raw = {
            "kind": "write_binding",
            "method": spec.method,
            "path": spec.path,
            "params": dict(spec.params),
        }
        comp_result = await run_action(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            action=f"{step.action}.compensate",
            raw_action=comp_raw,
            path=spec.path,
            params=dict(spec.params),
            base_url=base_url,
            auth_type=auth_type,
            auth_header=auth_header,
            token=token,
            allowed_writes=allowed_writes,
            idempotency_key=_compensation_idempotency_key(step.idempotency_key),
            correlation_id=correlation_id,
        )

        if comp_result.get("ok") and not _is_parked(comp_result):
            if emit_outcomes and spec.outcome:
                await _emit_outcome(
                    outcome=spec.outcome,
                    pocket_id=pocket_id,
                    workspace_id=workspace_id,
                    action=f"{step.action}.compensate",
                    actor=user_id,
                    compensated=True,
                )
            compensations.append(
                CompensationResult(
                    action=step.action, status="compensated", response=dict(comp_result)
                )
            )
        else:
            # The inverse write was rejected — the rollback continues, but
            # this leg needs manual reconciliation. Surface it; do NOT
            # raise (a half-attempted rollback is worse than a fully-
            # attempted one).
            logger.warning(
                "saga on pocket %s: compensation for step '%s' FAILED "
                "(code=%s) — manual reconciliation needed",
                pocket_id,
                step.action,
                comp_result.get("code"),
            )
            compensations.append(
                CompensationResult(action=step.action, status="failed", response=dict(comp_result))
            )

    return compensations


async def _emit_outcome(
    *,
    outcome: str,
    pocket_id: str,
    workspace_id: str,
    action: str,
    actor: str,
    compensated: bool,
) -> None:
    """Emit a forward or compensating outcome, best-effort.

    Lazy import keeps the saga module's static import surface minimal and
    avoids a load-time cycle with the outcomes service. ``emit_pocket_outcome``
    already swallows bus failures, but a wrapper here guards against an
    import-time failure in an environment where the outcomes module isn't
    wired (unit tests that mock the cloud bootstrap) so a metering hiccup
    never breaks the saga's return value.
    """
    try:
        from pocketpaw_ee.cloud.outcomes import service as outcomes_service

        await outcomes_service.emit_pocket_outcome(
            outcome=outcome,
            pocket_id=pocket_id,
            workspace_id=workspace_id,
            action=action,
            actor=actor,
            via_instinct=False,
            instinct_action_id=None,
            compensated=compensated,
        )
    except Exception:  # noqa: BLE001 — outcome emit must never break the saga
        logger.warning(
            "saga outcome emit failed for action=%s pocket=%s (compensated=%s)",
            action,
            pocket_id,
            compensated,
            exc_info=True,
        )


__all__ = [
    "CompensationResult",
    "SagaResult",
    "SagaStep",
    "run_action_sequence",
]
