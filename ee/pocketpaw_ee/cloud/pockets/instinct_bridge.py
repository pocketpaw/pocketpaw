# instinct_bridge.py — Routes a parked pocket write through Instinct.
# Created: 2026-05-22 (RFC 05 M2b.1) — the impure counterpart to the pure
#   `action_executor`. When `run_action` parks a `requires_instinct`
#   write it returns an `instinct_pending` sentinel with the resolved
#   write under `_park`; the pockets router hands that to
#   `propose_pocket_write` here, which builds an Instinct `Action` and
#   stores it via the global `InstinctStore`. After a human approves the
#   Action, the instinct router's `approve_action` fires
#   `execute_approved_write`, which RE-loads the backend credentials,
#   re-enters `run_action` with `from_instinct=True` (the gate is
#   skipped, the HTTP call is made), then records the result on the
#   Action and emits the outcome.
#
# Why a separate module: `action_executor` is import-linter-pure (no
#   Beanie, no Instinct, no models). This bridge is the layer that is
#   ALLOWED to be impure — it calls `pockets_service` (the sole Beanie
#   writer for the backend-credential collection) and the Instinct store.
#   It does NOT import Beanie document classes directly, so it does not
#   belong in the "pockets — Beanie writes only from service.py"
#   forbidden contract.
#
# Security:
#   * NO TOKEN reaches the Instinct DB. The proposed Action's
#     `parameters._pocket_write` carries method/path/params/idempotency
#     only — the backend credential is re-loaded fresh at execution time.
#   * The allowlist is re-checked at execution: `execute_approved_write`
#     re-enters `run_action`, which runs the allowlist gate again. A
#     write the owner de-authorized between propose and approve is
#     rejected, not fired.
#   * The approver defaults to the pocket OWNER. An optional
#     `approval_route` on the backend config can name a different
#     workspace member (membership validated when the route is set).
#
# Updated: 2026-05-22 (security-review fix for PR #1183 — BLOCKER 1
#   defense-in-depth) — `execute_approved_write` now refuses a parked
#   blob whose `workspace_id` is empty: a write with no tenant to scope
#   it to is unexecutable. The credential re-load
#   (`get_pocket_backend_for_executor`) is itself the tenancy gate — it
#   finds a credential row only when `(workspace_id, pocket_id)` BOTH
#   match, so a tampered blob carrying a foreign `workspace_id` resolves
#   no credentials and is marked failed instead of firing a cross-tenant
#   write. The instinct router's per-`_pocket_write` workspace assertion
#   is the primary gate; this is belt-and-braces.
#
# Updated: 2026-05-25 (RFC 09 Slice 2 — Decision Graph live formation) —
#   `_POCKET_WRITE_SCHEMA` bumped 1 → 2. The parked blob now carries
#   ``correlation_id`` (set from the executor's mint point — Captain
#   Decision 9) and ``parked_policy_event_id`` (populated by Slice 3 when
#   Instinct emits the parked `policy.evaluated(passed=False)` event so
#   the eventual `human.corrected` can chain its `causation_id` back to
#   the policy event). RFC 09 Captain Decision 5: no backward-compat
#   shim — schema-1 blobs that get approved post-deploy are marked
#   failed by the existing schema-mismatch check at line ~220. Drain the
#   Instinct queue before deploying Slice 2.
#   ``execute_approved_write`` reads the correlation_id off the schema-2
#   blob and passes it back into ``run_action(..., correlation_id=...)``
#   so the chain folds under one id end-to-end.

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Schema version stamped onto the parked-write blob in
# `Action.parameters._pocket_write`. Bump when the blob shape changes so a
# stale pending Action approved after a deploy fails loud instead of
# executing a misinterpreted write.
#
# Schema 2 (RFC 09 Slice 2) — adds:
#   * ``correlation_id``: the Decision-Graph chain id minted by the
#     executor at the moment ``agent.proposed`` fired. The Instinct
#     router (Slice 3) reads it back when emitting ``policy.evaluated`` /
#     ``human.corrected`` / ``decision.completed``.
#   * ``parked_policy_event_id``: the id of the
#     ``policy.evaluated(passed=False, reason="parked_for_human_approval")``
#     event emitted by Instinct when the write was parked. Slice 3 uses
#     it as the ``causation_id`` on the subsequent ``human.corrected``
#     event so the chain has a clean cause-arrow from policy → human.
#     ``None`` when not yet populated (e.g. if a future code path parks
#     a write without firing the policy event first).
_POCKET_WRITE_SCHEMA = 2


def _resolve_approver(
    pocket: dict[str, Any],
    backend_config: dict[str, Any] | None,
) -> str:
    """Return the workspace-member id who should approve a parked write.

    Default: the pocket ``owner``. An optional ``approval_route`` on the
    backend config overrides it::

        {"mode": "owner"}                 → the pocket owner (explicit)
        {"mode": "user", "user_id": "u9"} → a named workspace member

    Membership of a routed ``user_id`` is validated when the route is
    SET (``pockets_service.set_pocket_approval_route``), so a stale
    ``approval_route`` here is trusted; if it is malformed we fall back
    to the owner rather than parking a write nobody can approve.

    TODO (RFC 05): template-escalation — a template author marking an
    action as "escalate to the template publisher" — is a deliberate
    follow-up and not wired here.
    """
    owner = str(pocket.get("owner") or "")
    route = (backend_config or {}).get("approval_route")
    if isinstance(route, dict):
        mode = route.get("mode")
        if mode == "user":
            routed = route.get("user_id")
            if isinstance(routed, str) and routed:
                return routed
        # mode == "owner" (or anything malformed) → fall through to owner.
    return owner


def _row_hint(parked_write: dict[str, Any]) -> str:
    """Build a short, query-stripped row hint for the Action title.

    The hint is the write's path with any query string dropped — a
    query can carry resolved values an operator does not need in a
    title, and stripping it keeps the title stable for the same row.
    """
    path = str(parked_write.get("path") or "")
    return path.split("?", 1)[0]


async def propose_pocket_write(
    *,
    pocket: dict[str, Any],
    backend_config: dict[str, Any] | None,
    parked_write: dict[str, Any],
    requested_by: str,
) -> str:
    """Build + store an Instinct ``Action`` for a parked pocket write.

    ``pocket`` is the wire dict from ``pockets_service.get``. ``parked_write``
    is the ``_park`` blob from the executor's ``instinct_pending`` sentinel
    (action / method / path / params / idempotency_key / outcome).
    ``backend_config`` is the non-secret backend summary (may carry an
    ``approval_route``); it is NOT required.

    Returns the proposed Action id. NO token is written — the parked-write
    blob carries only method/path/params/idempotency/outcome plus the
    workspace + requester context; the credential is re-loaded at
    execution.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    pocket_id = str(pocket.get("_id") or pocket.get("id") or "")
    workspace_id = str(pocket.get("workspace") or pocket.get("workspace_id") or "")
    action_name = str(parked_write.get("action") or "")
    method = str(parked_write.get("method") or "")
    hint = _row_hint(parked_write)

    title = f"{action_name} — {method} {hint}".strip(" —")
    # One-line recommendation; the path is already query-stripped via
    # `_row_hint` so no resolved query values leak into the recommendation.
    recommendation = (
        f"Approve to run the '{action_name}' write ({method} {hint}) "
        f"on pocket {pocket.get('name') or pocket_id}."
    )

    trigger = ActionTrigger(
        type="pocket_action",
        source=requested_by,
        reason=f"pocket write '{action_name}' requires approval",
    )

    # The parked-write blob — everything `execute_approved_write` needs to
    # re-run the write, MINUS the credential. `outcome` rides along so the
    # outcome event can be emitted after the gated write succeeds.
    #
    # Schema 2 (RFC 09 Slice 2):
    #   * ``correlation_id``: the Decision-Graph chain id minted by the
    #     executor when ``agent.proposed`` fired. Must round-trip via the
    #     Instinct store so the post-approval re-entry into
    #     ``run_action`` uses the SAME id and the chain folds under one
    #     correlation (instead of producing a second Decision row).
    #   * ``parked_policy_event_id``: ``None`` here — Slice 3 wires the
    #     parked-side ``policy.evaluated`` emit and populates this field
    #     with the event id at that point. The field exists on the
    #     schema-2 blob now so the round-trip contract is stable.
    pocket_write = {
        "schema": _POCKET_WRITE_SCHEMA,
        "action": action_name,
        "method": method,
        "path": parked_write.get("path"),
        "params": parked_write.get("params") or {},
        "idempotency_key": parked_write.get("idempotency_key"),
        "outcome": parked_write.get("outcome"),
        "workspace_id": workspace_id,
        "requested_by": requested_by,
        # RFC 09 Slice 2 — schema-2 chain-correlation fields
        "correlation_id": parked_write.get("correlation_id"),
        "parked_policy_event_id": None,
    }

    approver = _resolve_approver(pocket, backend_config)

    store = get_instinct_store()
    action = await store.propose(
        pocket_id=pocket_id,
        title=title or action_name or "Pocket write",
        description=recommendation,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.EXTERNAL,
        parameters={"_pocket_write": pocket_write},
        assignee=approver or None,
    )
    logger.info(
        "parked pocket write '%s' on pocket %s → Instinct action %s (approver=%s)",
        action_name,
        pocket_id,
        action.id,
        approver or "<owner-unset>",
    )
    return action.id


async def execute_approved_write(action) -> None:  # type: ignore[no-untyped-def]
    """Execute the parked write carried by a freshly-approved Instinct Action.

    Called best-effort from the instinct router's ``approve_action`` after
    ``store.approve()`` succeeds. ``action`` is the approved
    :class:`~pocketpaw.instinct.models.Action`; this function:

      1. Reads ``_pocket_write`` from ``action.parameters``. A missing or
         schema-mismatched blob is marked failed and returns.
      2. RE-loads the pocket's backend credentials via
         ``pockets_service.get_pocket_backend_for_executor`` — NOT a
         snapshot. If the backend was revoked between propose and approve
         the Action is marked failed with ``code:backend_revoked`` and no
         write fires.
      3. Re-enters ``action_executor.run_action`` with
         ``from_instinct=True`` — the instinct gate is skipped, but every
         OTHER gate (rate-limit, base-URL, SSRF, ALLOWLIST, DNS) runs
         again, so a write the owner de-authorized is still rejected.
      4. Records the result: ``store.mark_executed`` on success,
         ``store.mark_failed`` on a rejection.
      5. Emits the ``pocket.outcome`` event on success (M2b.2).

    Never raises — a failure here must not break the approve response.
    The instinct router wraps the call too; this is belt-and-braces.
    """
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud.outcomes import service as outcomes_service
    from pocketpaw_ee.cloud.pockets import action_executor
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    store = get_instinct_store()
    params = getattr(action, "parameters", None) or {}
    blob = params.get("_pocket_write")
    if not isinstance(blob, dict):
        logger.warning("approved action %s carries no _pocket_write blob", action.id)
        return
    if blob.get("schema") != _POCKET_WRITE_SCHEMA:
        await store.mark_failed(
            action.id,
            "parked-write schema mismatch — the write blob is from an "
            "incompatible build and cannot be executed",
        )
        return

    pocket_id = str(action.pocket_id or "")
    workspace_id = str(blob.get("workspace_id") or "")
    action_name = str(blob.get("action") or "")
    requested_by = str(blob.get("requested_by") or "")
    approver = str(getattr(action, "approved_by", "") or "") or "system"

    # RFC 09 Slice 2 — pull the chain correlation_id back off the blob so
    # the post-approval ``run_action`` re-entry folds into the SAME chain
    # the original ``agent.proposed`` opened. A malformed / missing id
    # falls through to None and ``run_action`` will mint a fresh one —
    # the chain still records the executed write, just under a new id.
    # That's better than failing the approve when the only thing wrong
    # is a Decision-Graph wire.
    raw_corr = blob.get("correlation_id")
    parked_correlation_id: UUID | None = None
    if isinstance(raw_corr, str) and raw_corr:
        try:
            parked_correlation_id = UUID(raw_corr)
        except ValueError:
            logger.warning(
                "approved action %s has malformed correlation_id on blob — "
                "post-approval write will mint a fresh chain id",
                action.id,
            )
            parked_correlation_id = None

    # BLOCKER 1 defense-in-depth (PR #1183) — a parked write with no
    # workspace is unexecutable. The credential re-load below is the
    # tenancy gate (it matches `(workspace_id, pocket_id)` together), so
    # an empty workspace_id would never resolve creds anyway — but fail
    # loud here so a malformed blob is recorded, not silently dropped.
    if not workspace_id:
        await store.mark_failed(
            action.id,
            "parked-write blob carries no workspace_id — cannot scope the write",
        )
        return

    # The action's raw binding shape, rebuilt from the parked blob. The
    # executor reads `method` off this dict and re-validates everything.
    raw_action = {
        "kind": "write_binding",
        "method": blob.get("method"),
        "path": blob.get("path"),
        "params": blob.get("params") or {},
    }

    try:
        creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    except Exception:  # noqa: BLE001 — a creds-load failure must not raise into approve
        logger.warning(
            "approved action %s: backend creds load failed for pocket %s",
            action.id,
            pocket_id,
            exc_info=True,
        )
        await store.mark_failed(action.id, "backend credential load failed")
        return

    if creds is None:
        # The backend was revoked between propose and approve — respect
        # the current policy, do NOT fire a write against a deleted
        # credential.
        await store.mark_failed(
            action.id,
            "pocket backend was revoked before approval — no write was made (code:backend_revoked)",
        )
        return

    # The executor-creds tuple is a 6-tuple (M2b.1); `approval_route` is
    # unused at execution — the approver already approved.
    base_url, auth_type, auth_header, token, allowed_writes, _approval_route = creds

    try:
        result = await action_executor.run_action(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=requested_by or approver,
            action=action_name,
            raw_action=raw_action,
            path=str(blob.get("path") or ""),
            params=blob.get("params") or {},
            base_url=base_url,
            auth_type=auth_type,
            auth_header=auth_header,
            token=token,
            allowed_writes=allowed_writes,
            idempotency_key=blob.get("idempotency_key"),
            from_instinct=True,
            # RFC 09 Slice 2 — pass the chain correlation_id back into the
            # executor so the executor's gates run under the same id as
            # the original ``agent.proposed``. When ``parked_correlation_id``
            # is None (schema-2 blob with a malformed id, or a future code
            # path that parks without one), ``run_action`` mints a fresh id.
            correlation_id=parked_correlation_id,
        )
    except Exception:  # noqa: BLE001 — never let an executor crash break approve
        logger.warning("approved action %s: write executor crashed", action.id, exc_info=True)
        await store.mark_failed(action.id, "write executor failed")
        # RFC 09 Slice 2 — close the Decision chain on the executor
        # crash too (otherwise the chain stays open indefinitely until
        # the Slice 4 abandon-sweeper picks it up after 24h).
        _emit_bridge_chain_close(
            passed=False,
            action_outcome="failed",
            error_class="ExecutorCrash",
            correlation_id=parked_correlation_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=requested_by or approver,
        )
        return

    if not result.get("ok"):
        # The post-approval re-validation rejected the write (e.g. the
        # allowlist no longer covers it). Record the rejection on the
        # Action; emit nothing.
        code = result.get("code") or "error"
        err = result.get("error") or "write rejected"
        await store.mark_failed(action.id, f"{err} (code:{code})")
        # RFC 09 Slice 2 — close the chain on the bridge-side failure
        # path (audit Producer 4 site (d)). The error class rides as
        # ``error_class`` so the explain narrator can say *why* it
        # failed (re-validation rejected, etc.). action_executor's
        # gate-8 failure emit is suppressed on the Instinct re-entry
        # (``binding.requires_instinct=True`` branch) so this is the
        # only emit that closes the chain in this case.
        _emit_bridge_chain_close(
            passed=False,
            action_outcome="failed",
            error_class=str(code),
            correlation_id=parked_correlation_id,
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=requested_by or approver,
        )
        return

    await store.mark_executed(
        action.id,
        f"write '{action_name}' executed — HTTP {result.get('status')}",
    )

    # RFC 09 Slice 2 — chain close on the bridge-side success path
    # (audit Producer 4 site (b)). Emit BEFORE ``emit_pocket_outcome``
    # so the outcomes-side ``decision.outcome_attached`` (RFC 07 Slice 2
    # back-reference) finds an existing Decision row to mutate. Order
    # matters: a swap here would lose the late outcome attach.
    _emit_bridge_chain_close(
        passed=True,
        action_outcome="landed",
        error_class=None,
        correlation_id=parked_correlation_id,
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        user_id=requested_by or approver,
    )

    # M2b.2 — emit the outcome AFTER the gated write succeeded. A binding
    # with no `outcome` makes this a no-op. `actor` is the approver: the
    # human who authorized the write is the actor of the resulting
    # business outcome.
    try:
        await outcomes_service.emit_pocket_outcome(
            outcome=blob.get("outcome"),
            pocket_id=pocket_id,
            workspace_id=workspace_id,
            action=action_name,
            actor=approver,
            via_instinct=True,
            instinct_action_id=str(action.id),
        )
    except Exception:  # noqa: BLE001 — emit is best-effort; the write already succeeded
        logger.warning(
            "approved action %s: outcome emit failed (write succeeded)",
            action.id,
            exc_info=True,
        )


def _emit_bridge_chain_close(
    *,
    passed: bool,
    action_outcome: str,
    error_class: str | None,
    correlation_id: UUID | None,
    workspace_id: str,
    pocket_id: str,
    user_id: str,
) -> None:
    """Emit the ``decision.completed`` chain close for the bridge path.

    Producer 4 sites (b) and (d) in RFC 09 audit — the bridge owns the
    chain close on the Instinct re-entry path because the action_executor
    suppresses its own emit when ``binding.requires_instinct=True``.

    Returns early when ``correlation_id`` is None (a parked blob with a
    malformed id). The Slice 4 abandon-sweeper will close any chain that
    accumulates without a terminal after 24h.
    """
    # Late imports — keep the bridge's import surface small at module
    # load and avoid a circular import with the decisions package.
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

    if correlation_id is None:
        # Nothing to close — the original chain never threaded an id
        # through. The next Slice 4 reconciler / abandon-sweeper tick
        # will deal with any orphans.
        return

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
    )
    payload: dict[str, Any] = {
        "passed": passed,
        "action_outcome": action_outcome,
    }
    if error_class:
        payload["error_class"] = error_class
    try:
        record_decision_completed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}", f"pocket:{pocket_id}"],
            payload=payload,
        )
    except Exception:  # noqa: BLE001 — chain close is best-effort
        logger.warning(
            "bridge decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


__all__ = ["propose_pocket_write", "execute_approved_write"]
