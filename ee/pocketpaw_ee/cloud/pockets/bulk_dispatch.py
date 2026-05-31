# ee/pocketpaw_ee/cloud/pockets/bulk_dispatch.py
# Created: 2026-05-28 (feat/wave-3b-action-pipeline) — EE-side library
# wrapper around the OSS ``plan_bulk_execution`` planner. Implements the
# RFC 03 v2 §"Bulk action execution model" invariant: a ``kind: bulk``
# action fans out across N selected rows, and ANY rows that need
# approval consolidate into ONE InstinctApproval row carrying every
# row_id — not N separate rows.
#
# Wave 3b scope (locked by the architect brief):
#
# * Calls the pure OSS ``plan_bulk_execution`` once per dispatch.
# * Persists exactly ONE ``InstinctApproval`` row when the plan
#   surfaces an ``approval_request``, with a ``batch=True`` flag and
#   the full per-row payload mapped under ``row_data[<row_id>]``.
# * Fires EACH ``RowExecution`` through ``action_executor.run_action``.
#   The OSS composer already produced the EXECUTE / NOTIFY_AND_EXECUTE
#   verdict, so this wrapper does NOT re-thread ``template`` into the
#   per-row call — that would re-evaluate the gate and risk creating a
#   second approval row on a flapping rule. The brief's "fast-path
#   EXECUTE" hint is realised by skipping the gate entirely on the
#   re-entry.
# * Tenant-isolated via ``pockets_service.get`` — a cross-workspace
#   pocket_id surfaces as ``NotFound`` (Forbidden gets mapped to the
#   same shape so a foreign-workspace pocket is treated as if it does
#   not exist; this matches the existing approvals_service pattern).
#
# Out of scope (and explicitly NOT here):
#
# * Approver re-entry: when ``approve(batch_approval_id)`` lands later,
#   a follow-up PR will iterate ``row_data`` and re-run each row with
#   ``from_instinct=True``. THIS module ships the persistence so the
#   re-run code has data to work with.
# * UI for triggering bulk runs (paw-enterprise concern).
# * Per-agent concurrency / rate-limiting (production hardening).
# * Idempotency keys for retried bulk batches.
#
# Wave 3c addition (2026-05-28): outcome event emission per executed row
# is wired here, NOT via the per-row ``run_action`` invocation. The bulk
# path deliberately does NOT thread ``template`` into ``run_action`` (the
# planner already ran the gate; re-threading would re-evaluate it and
# risk a duplicate approval row). The outcome emitter therefore runs
# from the bulk wrapper directly, once per row whose ``run_action``
# returned ``ok:true``. Rows that fail (``ok:false``) skip emission, the
# same invariant the executor's success-path emit enforces.
#
# Import-linter posture: this module is Beanie-PURE. It calls
# ``instinct_approvals.service.create_approval`` (a permitted writer)
# and ``pockets.service`` (a permitted reader/writer) but never imports
# a Beanie document class. The ee/pyproject.toml import-linter contract
# adds this module to the ``pockets`` source_modules list to lock the
# invariant.

"""Bulk action dispatch — fan-out across N rows with ONE batch approval."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw.bundled_templates import PocketTemplate
from pocketpaw.bundled_templates.bulk_executor import (
    BulkExecutionError,
    plan_bulk_execution,
)
from pocketpaw.bundled_templates.identifier_resolver import IdentifierResolver
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service

# ---------------------------------------------------------------------------
# Result models — frozen so the caller (service / router) cannot mutate
# in-flight; one event per dispatch keys off these counts.
# ---------------------------------------------------------------------------


class ExecutionResult(BaseModel):
    """One row's execution outcome — what ``run_action`` returned for it.

    ``response`` is the raw executor dict (``ok``, ``status``,
    ``response``, ``error``, etc.) so the caller can serialize it
    onto the wire without re-shaping. ``row_id`` is the stable
    identifier used in audit / batch_approval contexts.
    """

    model_config = ConfigDict(frozen=True)

    row_id: str
    verdict: str
    """Composer verdict — ``EXECUTE`` or ``NOTIFY_AND_EXECUTE``."""
    response: dict[str, Any]
    """The full ``action_executor.run_action`` result dict for this row."""


class BlockedRowResult(BaseModel):
    """One row that the Instinct composer blocked.

    Mirrors the OSS ``BlockedRow`` but flattened for wire-friendly
    serialization. ``reason`` is the composer's typed reason
    string (e.g. ``operator_overlay_blocked``); ``rule_when`` is the
    CEL expression of the first block rule that matched.
    """

    model_config = ConfigDict(frozen=True)

    row_id: str
    reason: str
    rule_when: str


class BulkDispatchResult(BaseModel):
    """Frozen summary of a bulk dispatch call.

    Sum invariant: ``len(executions) + len(blocked) + approval_needed
    == total_rows``, where ``approval_needed`` is
    ``len(approval_row_ids)`` when ``batch_approval_id`` is set, else
    zero. The service-emitted event carries these counts directly.
    """

    model_config = ConfigDict(frozen=True)

    pocket_id: str
    action_name: str
    total_rows: int
    executions: list[ExecutionResult] = Field(default_factory=list)
    blocked: list[BlockedRowResult] = Field(default_factory=list)
    batch_approval_id: str | None = None
    approval_row_ids: list[str] = Field(default_factory=list)
    """Stable row ids that were consolidated into ``batch_approval_id``.
    Empty when no row needed approval."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def dispatch_bulk(
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    template: PocketTemplate,
    action_name: str,
    selected_rows: list[dict[str, Any]],
    *,
    resolver: IdentifierResolver | None = None,
    now: datetime | None = None,
) -> BulkDispatchResult:
    """Fan out a ``kind: bulk`` action across ``selected_rows``.

    Walks the OSS planner once, persists ONE batch approval if any row
    escalated, fires each ready-to-execute row through
    ``action_executor.run_action``, and returns a frozen summary.

    Tenant isolation is enforced via ``pockets_service.get`` — a
    pocket_id that doesn't resolve in the caller's workspace surfaces
    as ``NotFound`` (or is mapped to ``NotFound`` from ``Forbidden``)
    so the endpoint is not a cross-tenant existence oracle.

    Raises
    ------
    NotFound
        Pocket doesn't exist or is in another workspace.
    ValidationError
        ``action_name`` is unknown on the template, or it exists but
        has ``kind != "bulk"``. Both map onto
        ``bulk_action.invalid_action`` so the caller sees one typed
        rejection.
    """
    # Lazy import — pockets.service is the only Beanie-writer in scope
    # for pocket docs, and it sits in the same package. Lazy-imported
    # to keep this module's static import graph free of pocket model
    # references (the import-linter contract treats this module as
    # Beanie-pure).
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.pockets import action_executor
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    # ── 1. tenant-checked pocket fetch ─────────────────────────────
    # ``pockets_service.get`` checks owner / shared_with / workspace
    # visibility. A foreign-workspace pocket raises Forbidden which we
    # remap to NotFound so the dispatch endpoint is not a cross-tenant
    # existence oracle (mirrors the pattern in
    # ``instinct_approvals.service.get_approval``).
    try:
        pocket = await pockets_service.get(pocket_id, user_id)
    except Forbidden as exc:
        raise NotFound("pocket", pocket_id) from exc

    if pocket.get("workspace") != workspace_id:
        # Defense in depth — if the pocket resolved but lives in a
        # different workspace context, treat as not found rather than
        # leaking existence.
        raise NotFound("pocket", pocket_id)

    # ── 2. plan the fan-out via the pure OSS planner ───────────────
    try:
        plan = plan_bulk_execution(
            template,
            action_name,
            selected_rows,
            resolver=resolver,
            now=now,
        )
    except BulkExecutionError as exc:
        # Unknown action OR non-bulk action — both are typed pre-flight
        # failures the OSS planner raises before any row work. Map to
        # a single typed CloudError so the route surfaces 400.
        raise ValidationError("bulk_action.invalid_action", str(exc)) from exc

    # ── 3. persist ONE batch approval when the plan needs it ───────
    batch_approval_id: str | None = None
    approval_row_ids: list[str] = []
    if plan.approval_request is not None:
        approval_row_ids = list(plan.approval_request.row_ids)
        batch_approval_id = await _persist_batch_approval(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            plan=plan,
        )

    # ── 4. fire each ready execution through the executor ──────────
    # NOTE: we deliberately do NOT thread ``template`` into ``run_action``
    # here. The composer already evaluated the gate; re-threading would
    # cause an idempotent re-eval at minimum and a race-created duplicate
    # approval at worst. Wave 3a wired the gate at the per-row entry; the
    # bulk path runs the gate ONCE per row via the planner instead.
    #
    # ``template`` IS still passed through to ``_fire_executions`` for
    # the Wave 3c outcome emission — the bulk path emits outcomes per
    # successful row directly (NOT through ``run_action``'s template-
    # gated emit) because ``run_action`` here doesn't see the template.
    executions: list[ExecutionResult] = await _fire_executions(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        template=template,
        plan=plan,
        pocket=pocket,
        action_executor=action_executor,
        pockets_service=pockets_service,
    )

    # ── 5. flatten blocked rows for the wire ───────────────────────
    blocked = [
        BlockedRowResult(
            row_id=br.row_id,
            reason=br.decision.reason,
            rule_when=br.blocked_by_rule.when,
        )
        for br in plan.blocked
    ]

    return BulkDispatchResult(
        pocket_id=pocket_id,
        action_name=action_name,
        total_rows=plan.total_rows,
        executions=executions,
        blocked=blocked,
        batch_approval_id=batch_approval_id,
        approval_row_ids=approval_row_ids,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _persist_batch_approval(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    plan: Any,  # bulk_executor.BulkPlan — kept Any to dodge a re-import cycle
) -> str:
    """Persist exactly ONE InstinctApproval row that authorises every
    approval-needing row in the batch.

    Per the RFC: even 50 approval-needing rows produce ONE row in the
    approvals queue with all ``row_ids`` listed. The downstream
    approver UI groups by this single id; the future re-entry pipeline
    iterates ``row_data`` to replay each row.

    The wire shape carried on the approval doc:

    * ``row_id`` — empty (this is a batch approval; individual ids are
      under ``row_data``).
    * ``row_data`` — ``{"batch": True, "<row_id>": <row_dict>, ...}``
      so the future re-entry can iterate (excluding the ``batch`` key
      sentinel).
    * ``reason`` — the consolidated reason from the OSS planner
      (``operator_overlay_escalated`` / ``author_floor`` /
      ``mixed_approval_reasons``).
    * ``matched_rules`` — the union of overlay rules that participated
      across all rows (already deduplicated by the planner).
    * ``park`` — the batch shape carrying the action_name + the
      ordered list of row_ids the re-entry will replay. Per-row paths
      are NOT pre-resolved here; Wave 3c's re-entry will use the
      pocket's rippleSpec at approval time.
    """
    request = plan.approval_request

    row_data: dict[str, Any] = {"batch": True}
    for rid, payload in request.rows_data.items():
        # Avoid colliding with the ``batch`` sentinel key. If a row id
        # is literally "batch", prefix it so the sentinel stays
        # unambiguous; the planner stringifies all ids upstream so this
        # is a rare edge case but worth handling.
        if rid == "batch":
            row_data["row:batch"] = payload
        else:
            row_data[rid] = payload

    park = {
        "kind": "bulk",
        "action_name": request.action_name,
        "row_ids": list(request.row_ids),
    }

    body = {
        "pocket_id": pocket_id,
        "action_name": request.action_name,
        "row_id": "",  # batch — individual ids live under row_data
        "row_data": row_data,
        "verdict": "ESCALATE_APPROVAL",
        "reason": request.reason,
        "matched_rules": [rule.model_dump() for rule in request.matched_rules],
        "park": park,
    }
    wire = await approvals_service.create_approval(workspace_id, user_id, body)
    return wire["id"]


async def _fire_executions(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    template: PocketTemplate,
    plan: Any,
    pocket: dict,
    action_executor: Any,
    pockets_service: Any,
) -> list[ExecutionResult]:
    """Invoke ``action_executor.run_action`` for each ready row.

    Each row's executor call is independent — a failure on row N does
    not block rows N+1..N+M. Aggregating per-row results into one
    ``ExecutionResult`` per row keeps the caller's view symmetric with
    the planner's bucketing.

    When the pocket has no configured backend, every execution slot
    yields an ``ok:false`` result with code ``pocket_backend.not_configured``
    instead of raising — the planner already ran, and the approval rows
    (if any) are already persisted; raising would lose that work.
    """
    if not plan.executions:
        return []

    spec = pocket.get("rippleSpec") or {}
    actions = spec.get("actions") if isinstance(spec.get("actions"), dict) else {}
    raw_action = actions.get(plan.action_name) if isinstance(actions, dict) else None

    if not isinstance(raw_action, dict):
        # The action is declared on the template but not in the pocket's
        # rippleSpec — the bulk path needs a write binding to fire.
        # Yield a per-row error so the caller still sees the batch
        # approval that was already persisted.
        return [
            ExecutionResult(
                row_id=row.row_id,
                verdict=row.verdict,
                response={
                    "ok": False,
                    "action": plan.action_name,
                    "error": (f"no rippleSpec.actions[{plan.action_name!r}] binding on pocket"),
                    "code": "action_not_found",
                    "on_error": [],
                },
            )
            for row in plan.executions
        ]

    creds = await pockets_service.get_pocket_backend_for_executor(workspace_id, pocket_id)
    if creds is None:
        return [
            ExecutionResult(
                row_id=row.row_id,
                verdict=row.verdict,
                response={
                    "ok": False,
                    "action": plan.action_name,
                    "error": "This pocket has no backend configured",
                    "code": "pocket_backend.not_configured",
                    "on_error": [],
                },
            )
            for row in plan.executions
        ]
    base_url, auth_type, auth_header, token, allowed_writes, _approval_route = creds

    # Per-row path/params: the OSS planner does not resolve Ripple
    # ``{...}`` expressions — that's a frontend / executor concern. For
    # Wave 3b's library wiring we pass the raw_action's static path +
    # params unchanged. A future PR can add row-context substitution.
    raw_path = raw_action.get("path") or "/"
    raw_params = raw_action.get("params") if isinstance(raw_action.get("params"), dict) else {}

    # Lazy import to keep this module's static import graph minimal.
    from pocketpaw_ee.cloud.pockets import outcomes_emitter

    results: list[ExecutionResult] = []
    for row in plan.executions:
        result_dict = await action_executor.run_action(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
            action=plan.action_name,
            raw_action=raw_action,
            path=raw_path,
            params=dict(raw_params),
            base_url=base_url,
            auth_type=auth_type,
            auth_header=auth_header,
            token=token,
            allowed_writes=allowed_writes,
            # Gate is intentionally NOT re-evaluated for these rows —
            # the planner already produced the verdict. See the comment
            # block in ``dispatch_bulk`` for the reasoning.
        )
        # ── Wave 3c: per-row outcome emission ──────────────────────
        # Each row that returns ``ok:true`` fires its declared
        # outcomes. Failure / ``ok:false`` rows skip emission (same
        # invariant the executor's success-path emit enforces).
        # Emission is wrapped so a hiccup in the bus / audit layer
        # never breaks the bulk dispatch return value.
        if result_dict.get("ok"):
            try:
                await outcomes_emitter.emit_outcomes(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    pocket_id=pocket_id,
                    template=template,
                    action_name=plan.action_name,
                    row_id=row.row_id,
                    row_context=dict(row.row),
                )
            except Exception:  # noqa: BLE001 — emission must not break dispatch
                import logging

                logging.getLogger(__name__).warning(
                    "outcome emission failed for action=%s row=%s",
                    plan.action_name,
                    row.row_id,
                    exc_info=True,
                )
        results.append(
            ExecutionResult(
                row_id=row.row_id,
                verdict=row.verdict,
                response=dict(result_dict),
            )
        )
    return results


__all__ = [
    "BlockedRowResult",
    "BulkDispatchResult",
    "ExecutionResult",
    "dispatch_bulk",
]
