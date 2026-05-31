# src/pocketpaw/bundled_templates/bulk_executor.py
# Created: 2026-05-28 (feat/rfc-03-v2-bulk) — pure-library
# implementation of the RFC 03 v2 bulk-action fan-out planner. Given a
# template, a ``kind: bulk`` action name, and a list of selected rows,
# returns an immutable ``BulkPlan`` describing what should happen per
# row PLUS the single-batch approval contract from the RFC. The
# planner is the seam between the Instinct composer (per-row, PR 2d)
# and the EE runtime that actually invokes actions / emits outcomes
# (lives in ``ee/cloud/pockets/``, wired in a follow-up PR).
#
# This module ships the LAST library-layer piece needed before RFC 03
# v2 integration returns to dev. PRs 2c (CEL), 2d (Instinct composer),
# 2e (bulk planner) compose into a single pure-Python pipeline the EE
# runtime consumes; no I/O, no Beanie, no ``pocketpaw_ee`` imports.
"""Bulk-action fan-out planner for RFC 03 v2 templates.

Public surface
--------------

* :class:`BulkPlan` — frozen Pydantic v2 model returned by the
  planner. Carries the resolved action, the per-row executions ready
  to fire, the blocked rows (with the rule that fired), and an
  optional :class:`BulkApprovalRequest` consolidating every row that
  needs approval.
* :class:`RowExecution` — frozen per-row execution descriptor (verdict
  ``EXECUTE`` or ``NOTIFY_AND_EXECUTE``, the full underlying
  :class:`InstinctDecision`, and the notify rules to dispatch in
  parallel).
* :class:`BlockedRow` — frozen per-row block descriptor (the decision
  + the block rule that won).
* :class:`BulkApprovalRequest` — frozen consolidated approval payload.
  ONE request covers ALL approval-needing rows from a single
  ``plan_bulk_execution`` call. The EE runtime enqueues exactly one
  Instinct approval, not N.
* :func:`plan_bulk_execution` — the pure entry point. No I/O, no
  side effects; safe to call from anywhere, including unit tests.
* :class:`BulkExecutionError` — raised for pre-flight failures
  (unknown action, non-bulk action kind). Per-row CEL evaluation
  failures bubble out of :func:`resolve_instinct` as
  :class:`InstinctResolutionError` and are not re-wrapped — the caller
  already has typed error coverage for those.

The RFC contract
----------------

From RFC 03 v2 §"Bulk action execution model"::

    A ``kind: bulk`` action fans out one execution per selected row.
    Each row execution emits its own outcome events. ... If the action
    has ``instinct_policy: require_approval`` (or a top-level approval
    rule matched), the Instinct queue gets ONE approval request that
    authorizes the whole batch — not N requests.

That is the seam this planner ships. The planner does NOT invoke
actions, agents, or HTTP calls; those live in the EE runtime and are
wired separately.

Bucketing rules
---------------

Per-row verdicts from :func:`resolve_instinct` route as follows:

* ``BLOCK`` → :attr:`BulkPlan.blocked`. Carries the matched rule for
  the audit log. Blocked rows NEVER enter :attr:`BulkPlan.executions`
  or the approval request — ``block always wins`` (RFC invariant).
* ``ESCALATE_APPROVAL`` → consolidated into a single
  :class:`BulkApprovalRequest`. Approval-needing rows are deduplicated
  into ONE request regardless of count.
* ``EXECUTE`` → :attr:`BulkPlan.executions` with verdict EXECUTE.
* ``NOTIFY_AND_EXECUTE`` → :attr:`BulkPlan.executions` with verdict
  NOTIFY_AND_EXECUTE and the matched notify rules carried on the
  row's :attr:`RowExecution.notify_rules`.

Reason codes on :class:`BulkApprovalRequest`
-------------------------------------------

* ``operator_overlay_escalated`` — every approval-needing row in this
  batch escalated via a top-level ``instinct_rules`` rule. No row hit
  the per-action floor.
* ``author_floor`` — every approval-needing row escalated via the
  action's own ``instinct_policy: require_approval``. No overlay
  rule fired.
* ``mixed_approval_reasons`` — some rows escalated via overlay, some
  via author floor. Single typed string keeps the contract
  observable; the per-row decisions live on
  :attr:`BulkApprovalRequest.per_row_decisions` for fine-grained
  audit.

Scope (locked by the PR brief)
------------------------------

* Library / pure function. No I/O, no Beanie, no ``pocketpaw_ee``
  imports. The OSS import-linter contract enforces this.
* Result models are **immutable** (``ConfigDict(frozen=True)``).
* Side effects are *planned* (returned in the structure) but not
  *fired* — the EE runtime owns dispatch, approval queue submission,
  agent invocation, HTTP action calls, and outcome event emission.
* Single-row + global action runtimes live in different code paths;
  only ``kind: bulk`` is in scope here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw.bundled_templates.identifier_resolver import (
    IdentifierResolver,
)
from pocketpaw.bundled_templates.instinct_composer import (
    InstinctDecision,
    resolve_instinct,
)
from pocketpaw.bundled_templates.schema import (
    ActionDef,
    InstinctRule,
    PocketTemplate,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


ExecutionVerdict = Literal["EXECUTE", "NOTIFY_AND_EXECUTE"]
"""Subset of :class:`~pocketpaw.bundled_templates.InstinctVerdict` that
indicates the row should proceed to action invocation.

``BLOCK`` and ``ESCALATE_APPROVAL`` are routed to their own buckets
and do not appear on :attr:`RowExecution.verdict`.
"""


class RowExecution(BaseModel):
    """A single row ready to fire — verdict is either ``EXECUTE`` or
    ``NOTIFY_AND_EXECUTE``.

    Frozen so the EE runtime can pass the row through audit and
    serialization layers without worrying about downstream mutation.
    The original :class:`InstinctDecision` is preserved on
    :attr:`decision` so callers always have the full per-row rationale.
    """

    model_config = ConfigDict(frozen=True)

    row_id: str
    """Row identifier resolved from the row dict via
    ``row_id_field``. Stable for the lifetime of the plan."""

    row: dict[str, Any]
    """The original row dict, untouched. The EE runtime invokes the
    action against this exact payload."""

    verdict: ExecutionVerdict
    """``EXECUTE`` — invoke the action directly. ``NOTIFY_AND_EXECUTE``
    — invoke AND ping the escalation target via the carried
    ``notify_rules``."""

    decision: InstinctDecision
    """The underlying per-row decision. Carries the verdict, reason,
    matched rules, and audit data; mirrored here for downstream
    inspection."""

    notify_rules: list[InstinctRule] = Field(default_factory=list)
    """Top-level ``notify`` rules whose ``when`` matched for this row.
    Empty on plain ``EXECUTE``; populated on ``NOTIFY_AND_EXECUTE``
    AND on any row whose ``auto`` policy still surfaced a matching
    notify rule (top-level notify can fire alongside auto execute)."""


class BlockedRow(BaseModel):
    """A single row blocked by a top-level rule — does NOT enter
    ``executions`` and does NOT enter the approval request.

    Per RFC ``block always wins``: the EE runtime emits a
    ``blocked_by_rule`` audit-log entry and skips invocation entirely
    for this row.
    """

    model_config = ConfigDict(frozen=True)

    row_id: str
    row: dict[str, Any]
    decision: InstinctDecision
    """Full underlying decision — verdict is always ``BLOCK`` here."""

    blocked_by_rule: InstinctRule
    """The first block rule whose ``when`` matched. Carried separately
    for the audit log even though it is also on
    ``decision.matched_rules``."""


class BulkApprovalRequest(BaseModel):
    """Single consolidated approval request covering every
    approval-needing row in the batch.

    Per RFC: ONE approval request authorises the entire batch — never
    N. Even if 50 rows escalate, the Instinct queue receives this one
    object. On approval, the EE runtime re-runs the per-row action
    invocations for every row in :attr:`row_ids`.

    Frozen — the runtime must not edit an in-flight approval. If the
    batch needs to change, produce a fresh request.
    """

    model_config = ConfigDict(frozen=True)

    action_name: str
    """The bulk action the request authorises (same for every row)."""

    row_ids: list[str]
    """Stable row identifiers, ordered as they appeared in
    ``selected_rows``. May be a subset of the total selection if some
    rows blocked or proceeded clean."""

    rows_data: dict[str, dict[str, Any]]
    """``row_id → row`` map, for the audit log. The EE runtime
    serializes this onto the approval queue so reviewers see the exact
    payload that will be invoked on approval."""

    reason: str
    """One of ``operator_overlay_escalated``, ``author_floor``, or
    ``mixed_approval_reasons``. See module docstring."""

    matched_rules: list[InstinctRule] = Field(default_factory=list)
    """Union (by identity) of every overlay approval rule that
    participated across all rows in the batch. Empty when the entire
    batch escalated via author_floor."""

    per_row_decisions: dict[str, InstinctDecision]
    """``row_id → InstinctDecision`` for fine-grained audit. The
    consolidated ``reason`` collapses some detail; this field
    preserves the per-row truth."""


class BulkPlan(BaseModel):
    """Frozen result of :func:`plan_bulk_execution`.

    Carries every bucket the EE runtime needs for dispatch — ready
    executions, blocked rows, and (optionally) the single batch
    approval request. The resolved :class:`ActionDef` is also exposed
    so the runtime does not have to look it up again.
    """

    model_config = ConfigDict(frozen=True)

    action_name: str
    """The bulk action that was planned (matches the caller's input)."""

    action: ActionDef
    """The resolved action — pre-validated, frozen at template-load
    time."""

    total_rows: int
    """``len(selected_rows)``. Useful for sanity checks: the sum of
    ``len(executions)`` + ``len(blocked)`` + (
    ``len(approval_request.row_ids)`` if present else 0) equals
    ``total_rows``."""

    executions: list[RowExecution] = Field(default_factory=list)
    """Rows ready to fire (verdict ``EXECUTE`` or
    ``NOTIFY_AND_EXECUTE``)."""

    blocked: list[BlockedRow] = Field(default_factory=list)
    """Rows blocked by a top-level block rule."""

    approval_request: BulkApprovalRequest | None = None
    """Single consolidated approval request, or ``None`` when no row
    needed approval."""


class BulkExecutionError(Exception):
    """Raised by :func:`plan_bulk_execution` for pre-flight failures.

    Two failure modes:

    1. **Unknown action** — ``action_name`` is not declared on
       ``template.actions[].name``. Surfaces BEFORE any row evaluation
       runs so the caller fails fast on a typo.
    2. **Non-bulk action** — the named action exists but its ``kind``
       is ``single-row`` or ``global``. The bulk planner is for
       ``kind: bulk`` only; routing the wrong action here would
       silently mis-execute.

    Per-row evaluation failures (CEL errors, identifier misses) bubble
    out of :func:`resolve_instinct` as :class:`InstinctResolutionError`
    and are NOT re-wrapped — callers already have typed coverage for
    those exceptions.
    """


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def plan_bulk_execution(
    template: PocketTemplate,
    action_name: str,
    selected_rows: list[dict[str, Any]],
    *,
    workspace_context: dict[str, Any] | None = None,
    resolver: IdentifierResolver | None = None,
    row_id_field: str | None = None,
    now: datetime | None = None,
) -> BulkPlan:
    """Plan the fan-out for a ``kind: bulk`` action against N rows.

    Walks every selected row through the RFC 03 v2 Instinct composer,
    buckets per-row verdicts (BLOCK, ESCALATE_APPROVAL, EXECUTE,
    NOTIFY_AND_EXECUTE), and consolidates every approval-needing row
    into ONE :class:`BulkApprovalRequest`.

    Parameters
    ----------
    template:
        The fully-validated :class:`PocketTemplate` carrying
        ``actions[]`` and optional ``instinct_rules``.
    action_name:
        Name of the bulk action to plan for. Must exist on the
        template AND have ``kind == "bulk"``; otherwise
        :class:`BulkExecutionError` is raised.
    selected_rows:
        Per-row dicts the EE runtime collected from the user's
        selection. Empty list is valid (returns an empty plan).
    workspace_context:
        Optional workspace-scoped defaults threaded into every per-row
        Instinct composition call. Row context wins on collision per
        the composer's documented contract.
    resolver:
        Optional :class:`IdentifierResolver` reused for every row.
        Defaults to a single
        :class:`TemplateIdentifierResolver` constructed inside
        :func:`resolve_instinct` per call when ``None``; passing one
        explicitly avoids re-constructing it N times.
    row_id_field:
        Field name on each row dict used to derive ``row_id``.
        Resolution order: explicit argument → ``template.state.id_field``
        → ``"id"`` literal. A row missing the chosen field raises
        :class:`BulkExecutionError`.
    now:
        Optional wall-clock for the CEL ``within(...)`` function.
        Defaults to ``datetime.now(UTC)``. Tests should pass a fixed
        value for determinism.

    Returns
    -------
    :class:`BulkPlan`
        Immutable. See class docstring for field semantics.

    Raises
    ------
    BulkExecutionError
        On unknown action or non-bulk action kind.
    InstinctResolutionError
        Bubbles up unchanged from :func:`resolve_instinct` when a
        per-row CEL evaluation fails. Callers should handle both
        exception types.
    """
    if now is None:
        now = datetime.now(UTC)

    # Resolve action up-front so typos surface BEFORE any row work.
    action = _find_bulk_action(template, action_name)

    # ``row_id_field`` resolution per the brief: explicit arg →
    # template.state.id_field → "id" literal. The template default is
    # already ``"id"`` so the chain collapses to the same value in
    # the common case; we keep the explicit lookup for clarity.
    effective_id_field = row_id_field or template.state.id_field or "id"

    executions: list[RowExecution] = []
    blocked: list[BlockedRow] = []
    approval_rows: list[tuple[str, dict[str, Any], InstinctDecision]] = []

    for row in selected_rows:
        row_id = _row_id_for(row, effective_id_field)
        decision = resolve_instinct(
            template,
            action_name,
            row,
            workspace_context,
            resolver=resolver,
            now=now,
        )

        if decision.verdict == "BLOCK":
            # ``block always wins`` — never enters executions or the
            # approval request. The first block rule that matched is
            # already on decision.matched_rules[0]; we expose it
            # separately for the audit log.
            blocked.append(
                BlockedRow(
                    row_id=row_id,
                    row=row,
                    decision=decision,
                    blocked_by_rule=decision.matched_rules[0],
                )
            )
            continue

        if decision.verdict == "ESCALATE_APPROVAL":
            # Consolidate later — collect now and emit one request
            # for the whole batch.
            approval_rows.append((row_id, row, decision))
            continue

        # EXECUTE or NOTIFY_AND_EXECUTE — verdict matches the
        # ExecutionVerdict Literal exactly.
        executions.append(
            RowExecution(
                row_id=row_id,
                row=row,
                verdict=decision.verdict,
                decision=decision,
                notify_rules=list(decision.notify_rules),
            )
        )

    approval_request = _consolidate_approval(action_name, approval_rows)

    return BulkPlan(
        action_name=action_name,
        action=action,
        total_rows=len(selected_rows),
        executions=executions,
        blocked=blocked,
        approval_request=approval_request,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_bulk_action(template: PocketTemplate, action_name: str) -> ActionDef:
    """Look up an action by name and confirm it is ``kind: bulk``.

    Two failure modes per :class:`BulkExecutionError`:

    1. Unknown action — name not present on the template.
    2. Non-bulk action — name present, but ``kind`` is ``single-row``
       or ``global``. Routing the wrong kind through the bulk planner
       would silently mis-execute (e.g., single-row actions don't
       expect fan-out semantics); fail loudly.
    """
    for action in template.actions:
        if action.name == action_name:
            if action.kind != "bulk":
                raise BulkExecutionError(
                    f"not a bulk action: action {action_name!r} has "
                    f"kind={action.kind!r}; expected 'bulk'"
                )
            return action
    raise BulkExecutionError(
        f"unknown action: {action_name!r} is not declared on template "
        f"{template.name!r} (available: "
        f"{sorted(a.name for a in template.actions)})"
    )


def _row_id_for(row: dict[str, Any], id_field: str) -> str:
    """Pull the row identifier and stringify it.

    Per the brief, we stringify (the approval queue serializes row_ids
    as JSON; coercing here keeps the contract narrow). Missing field
    surfaces as a typed :class:`BulkExecutionError` so callers can
    distinguish "bad data" from "bad expression".
    """
    if id_field not in row:
        raise BulkExecutionError(
            f"row is missing id field {id_field!r}; available keys: {sorted(row.keys())}"
        )
    return str(row[id_field])


def _consolidate_approval(
    action_name: str,
    approval_rows: list[tuple[str, dict[str, Any], InstinctDecision]],
) -> BulkApprovalRequest | None:
    """Collapse N per-row approval decisions into one batch request.

    Returns ``None`` when no row needs approval. Otherwise builds a
    single :class:`BulkApprovalRequest` per the RFC contract.

    The consolidated ``reason`` reflects the mix of per-row reasons:

    * All rows ``operator_overlay_escalated`` → ``operator_overlay_escalated``.
    * All rows ``author_floor`` → ``author_floor``.
    * Mixed → ``mixed_approval_reasons``.

    The union of matched rules is built by *identity*: an
    :class:`InstinctRule` is a Pydantic value object, so equal-by-value
    rules from two rows collapse to one entry in the union. This
    keeps the contract observable without requiring callers to
    deduplicate themselves.
    """
    if not approval_rows:
        return None

    row_ids: list[str] = []
    rows_data: dict[str, dict[str, Any]] = {}
    per_row_decisions: dict[str, InstinctDecision] = {}
    union_rules: list[InstinctRule] = []
    reasons: set[str] = set()

    for row_id, row, decision in approval_rows:
        row_ids.append(row_id)
        rows_data[row_id] = row
        per_row_decisions[row_id] = decision
        reasons.add(decision.reason)
        for rule in decision.matched_rules:
            # Deduplicate by value. ``InstinctRule`` is Pydantic; its
            # equality compares fields. ``in`` on the list of values
            # walks linearly — fine for the small N of rules in any
            # realistic template.
            if rule not in union_rules:
                union_rules.append(rule)

    if reasons == {"operator_overlay_escalated"}:
        consolidated_reason = "operator_overlay_escalated"
    elif reasons == {"author_floor"}:
        consolidated_reason = "author_floor"
    else:
        # Any other mix (e.g. {operator_overlay_escalated, author_floor})
        # consolidates to a single typed string per the brief.
        consolidated_reason = "mixed_approval_reasons"

    return BulkApprovalRequest(
        action_name=action_name,
        row_ids=row_ids,
        rows_data=rows_data,
        reason=consolidated_reason,
        matched_rules=union_rules,
        per_row_decisions=per_row_decisions,
    )


__all__ = [
    "BlockedRow",
    "BulkApprovalRequest",
    "BulkExecutionError",
    "BulkPlan",
    "ExecutionVerdict",
    "RowExecution",
    "plan_bulk_execution",
]
