# ee/pocketpaw_ee/cloud/pockets/instinct_dispatch.py
# Updated: 2026-06-21 (feat/szd-finish-enforce, F6 — live enforcement of
# approved workspace-discovered Instinct rules) — `gate_action` now, when the
# DEFAULT-OFF `instinct_enforce_discovered_rules` flag is on, loads the
# workspace's approved discovered rules (`rules.service.get_active_rules`),
# pocket-scopes them, converts each to an OSS `InstinctRule`, drops any whose
# CEL `when` fails to parse or errors on a guarded probe, and merges the clean
# ones FIRST into a model_copy of the template before the UNCHANGED
# `resolve_instinct` call. The template object is NEVER mutated. Fail-safe:
# a `get_active_rules` read failure fails OPEN (proceed on the template
# verdict, WARNING, no 404), and a per-rule CEL error drops THAT rule only —
# a broken discovered rule is inert, never a silent block or 404. The pure
# composer is untouched (it must stay import-linter-pure). When the flag is
# off, `get_active_rules` is never called and `effective_template is template`.
#
# Updated: 2026-06-19 (feat/instinct-gate-integration, security-review FIX 3) —
# `_find_existing_pending_id` now pushes the (action_name, row_id) match INTO
# the approvals query (limit=1) instead of paging a default list and matching
# in Python. A pocket with more pending rows than the page limit could bury the
# target row, the scan would miss it, and the BATCH lane would stack a
# DUPLICATE pending row. The DB-side filter makes dedup correct at any volume.
#
# Updated: 2026-06-19 (feat/instinct-gate-integration, T6/T11) — wired the
# layered/learning gate LIVE. `gate_action` now takes `approval_level`
# (default ASK = dormant) and `dry_run_mode` (default False), and after an
# `ESCALATE_APPROVAL` verdict it calls `instinct_triage.classify_lane` and
# branches by lane:
#   * AUTO       → `approvals_service.auto_approve(...)` (a DECIDED row, no
#                  pending) and `next_step="auto_approved"`.
#   * OPTIMISTIC → an auto_approved row tagged lane=OPTIMISTIC and
#                  `next_step="optimistic_proceed"`.
#   * DRY_RUN    → `next_step="dry_run"` (no row; the executor resolves the
#                  write and audits it but never fires).
#   * ESCALATE   → the EXISTING human-pending path, UNCHANGED, with BATCH
#                  dedup (T11): a second escalate for the same
#                  (pocket, action, row) returns the existing pending id.
#   * BLOCK      → unchanged.
# The triage decision is audited (`category="instinct_triage"`) at every
# TRIAGE call. trust_score + proposed_count come from the trust ledger.
#
# THE SAFETY INVARIANT: default `approval_level=ASK` makes `classify_lane`
# return ESCALATE for every proposal (rule 2), so a caller that passes no
# new args is byte-identical to the pre-integration behavior — every
# escalate becomes a human-pending row. Auto-approval activates ONLY when a
# workspace explicitly opts into a non-ASK level; uncertainty always
# escalates to the human, never silently approves.
#
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — single entry
# point from the runtime into the RFC 03 v2 template-level Instinct.
# Wraps the OSS-side ``resolve_instinct`` pure function with the EE-
# side persistence (``instinct_approvals.service``) and returns a
# typed ``InstinctGateResult`` the action_executor branches on.
#
# Why a separate module: ``action_executor`` is import-linter-pure
# (no Beanie / no models). This wrapper is the impure layer that may
# call ``instinct_approvals.service.create_approval`` (a Beanie writer
# under the import-linter contract for that entity).
#
# Single entry point: future bulk fan-out (Wave 3b) + temporal sweeper
# (Wave 3d) call ``gate_action`` per-row too, so all dispatch flows
# share the same persistence + audit shape.
#
# Hard constraint: this module DOES NOT touch the M2b.1 binding-level
# ``requires_instinct`` flag / ``_park`` sentinel. That flow stays
# intact for backward compatibility — Wave 3a layers a NEW
# template-level gate that runs BEFORE the M2b.1 gate.

"""Dispatch wrapper around OSS ``resolve_instinct`` + EE persistence."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pocketpaw.bundled_templates import (
    InstinctDecision,
    InstinctResolutionError,
    InstinctRule,
    PocketTemplate,
    resolve_instinct,
)
from pocketpaw.bundled_templates.cel_runtime import CelEvaluationError, evaluate_cel
from pocketpaw.bundled_templates.identifier_resolver import (
    IdentifierResolver,
    TemplateIdentifierResolver,
)
from pocketpaw.bundled_templates.schema import InstinctRulesDef
from pocketpaw.config import get_settings
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.instinct_approvals import service as approvals_service
from pocketpaw_ee.cloud.pockets import trust_ledger
from pocketpaw_ee.cloud.pockets.action_executor import CompensateSpec
from pocketpaw_ee.cloud.pockets.instinct_triage import (
    ApprovalLevel,
    TriageLane,
    TriageProposal,
    classify_lane,
)
from pocketpaw_ee.cloud.rules.service import get_active_rules

logger = logging.getLogger(__name__)

# Two new literals join the original three: ``dry_run`` (the write is
# resolved + audited but never fired) and ``optimistic_proceed`` (the write
# fires now, with a registered compensation handle for bounded rollback).
NextStepT = Literal[
    "proceed",
    "blocked",
    "pending_approval",
    "auto_approved",
    "dry_run",
    "optimistic_proceed",
]


class InstinctGateResult(BaseModel):
    """Outcome of a single ``gate_action`` call.

    Frozen so the executor cannot mutate it. The shape matches what the
    runtime needs to dispatch:

    * ``decision`` — the pure OSS composer's verdict + audit data.
    * ``next_step`` — collapsed branch for the executor (proceed /
      blocked / pending_approval / auto_approved / dry_run /
      optimistic_proceed).
    * ``approval_id`` — set when ``next_step`` is ``pending_approval``
      (the new human-pending row), ``auto_approved`` (the decided AUTO/
      OPTIMISTIC row), and stays ``None`` for proceed / blocked / dry_run.
    * ``notify_rules`` — top-level ``notify`` rules whose ``when``
      matched the row. Empty on BLOCK.
    * ``lane`` — the triage lane ``classify_lane`` selected. Defaults to
      ``ESCALATE`` (the safe lane) so a result built on a non-triage path
      (EXECUTE / BLOCK) reports the most-governed lane.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    decision: InstinctDecision
    next_step: NextStepT
    approval_id: str | None = None
    notify_rules: list[InstinctRule] = []
    lane: TriageLane = TriageLane.ESCALATE


def _matched_rules_payload(decision: InstinctDecision) -> list[dict[str, Any]]:
    """Serialize matched rules to plain dicts so they can ride a
    Beanie ``list[dict]`` field. ``InstinctRule.model_dump`` is the
    canonical serializer."""
    return [r.model_dump() for r in decision.matched_rules]


def _coerce_approval_level(level: ApprovalLevel | str | None) -> ApprovalLevel:
    """Map a raw (possibly stringy / unknown) level onto the enum, default-safe.

    The cloud router reads the per-workspace field (a plain string) or the
    global config default; both may arrive as a bare string. An unknown /
    malformed value falls back to ``ASK`` — the dormant, fail-safe level —
    so a typo on a workspace document can never silently activate the
    triager.
    """
    if isinstance(level, ApprovalLevel):
        return level
    if isinstance(level, str):
        try:
            return ApprovalLevel(level)
        except ValueError:
            logger.warning("unknown instinct approval_level %r — falling back to ASK", level)
    return ApprovalLevel.ASK


def _compensate_from_park(park: dict[str, Any] | None) -> CompensateSpec | None:
    """Pull the binding's ``compensate`` spec off the ``_park`` blob, if any.

    The executor threads the binding's declared ``compensate`` into ``park``
    (gap-3 path); a write with a declared inverse is the reversibility
    signal ``classify_lane`` reads. A malformed compensate blob is treated
    as ABSENT (no compensate) — the safe reading, since a write with no
    trustworthy inverse must never be promoted to AUTO.
    """
    if not park:
        return None
    raw = park.get("compensate")
    if raw is None:
        return None
    if isinstance(raw, CompensateSpec):
        return raw
    if isinstance(raw, dict):
        try:
            return CompensateSpec.model_validate(raw)
        except Exception:  # noqa: BLE001 — a bad inverse is no inverse (safe)
            logger.warning("malformed compensate spec on park blob — treating as no compensate")
            return None
    return None


def _audit_triage_decision(
    *,
    workspace_id: str,
    pocket_id: str,
    action: str,
    lane: TriageLane,
    trust_score: float,
    proposed_count: int,
    approval_level: ApprovalLevel,
) -> None:
    """Write the classify_lane decision to the append-only audit log.

    Category ``instinct_triage``, severity INFO. Every TRIAGE-level
    classify decision is logged so the triager's reasoning is auditable —
    which lane it chose, the trust score + warmup count it read, and the
    activation level in force. Audit failures must never break the gate, so
    the whole call is wrapped (mirrors ``action_executor._audit_action_run``).
    """
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        get_audit_logger().log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="system:triager",
                action="instinct.triage.classify",
                target=pocket_id,
                status=lane.name.lower(),
                category="instinct_triage",
                workspace_id=workspace_id,
                pocket_action=action,
                lane=lane.name,
                trust_score=trust_score,
                proposed_count=proposed_count,
                approval_level=approval_level.value,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the gate
        logger.warning("instinct triage-decision audit-log write failed", exc_info=True)


def _escalate_body(
    *,
    pocket_id: str,
    action_name: str,
    row_id: str,
    row_context: dict[str, Any],
    decision: InstinctDecision,
    park: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the CreateApprovalRequest body shared by the pending + decided
    write paths. One shape so the audit trail is uniform across lanes."""
    return {
        "pocket_id": pocket_id,
        "action_name": action_name,
        "row_id": row_id,
        "row_data": row_context,
        "verdict": decision.verdict,
        "reason": decision.reason,
        "matched_rules": _matched_rules_payload(decision),
        "park": park,
    }


async def _find_existing_pending_id(
    *, workspace_id: str, user_id: str, pocket_id: str, action_name: str, row_id: str
) -> str | None:
    """T11 — BATCH-lane dedup: return an existing pending approval id for the
    same (pocket, action, row), or ``None``.

    Two escalating gate calls for the SAME row (a flapping rule, a retried
    dispatch, a bulk fan-out hitting the same row twice) must not stack N
    pending rows in the human tray — the human decides the row ONCE.

    Security-review FIX 3: the match is now pushed INTO the query
    (``action_name`` + ``row_id`` filters) with ``limit=1``, instead of listing
    a default page of pending rows and matching in Python. A pocket with more
    pending rows than the list page (default 50) could otherwise bury the
    target row past the page boundary, the Python scan would miss it, and the
    gate would stack a DUPLICATE pending row — exactly the dedup the lane is
    meant to prevent. Letting Mongo do the match makes dedup correct regardless
    of how many other pending rows the pocket carries.

    An empty ``row_id`` is NOT deduped — a row-less escalate has no stable
    identity to group on, so it falls through to a fresh row (conservative).
    """
    if not row_id:
        return None
    existing = await approvals_service.list_approvals(
        workspace_id,
        user_id,
        {
            "pocket_id": pocket_id,
            "status": "pending",
            "action_name": action_name,
            "row_id": row_id,
            "limit": 1,
        },
    )
    if existing:
        return existing[0].get("id")
    return None


async def _load_discovered_instinct_rules(
    *,
    workspace_id: str,
    pocket_id: str,
    template: PocketTemplate,
    row_context: dict[str, Any],
    workspace_context: dict[str, Any] | None,
    resolver: IdentifierResolver | None,
    now: datetime,
) -> list[InstinctRule]:
    """Load approved workspace-discovered rules, pocket-scope + guard them, and
    return clean ``InstinctRule`` objects ready to merge into the template.

    Fail-OPEN at every step — a broken discovered rule (or a store outage) is
    inert, never a block, never a silent 404. The asymmetry with template rules
    (which keep loud-fail) is deliberate: template rules are authored and
    version-controlled; discovered rules are inferred and lower-trust, and can
    only ever ADD a block/escalate, never relax the template floor.

    1. ``get_active_rules`` read failure → log WARNING, return ``[]`` (fall
       through to the pure template path).
    2. Filter: keep a rule only if its ``scope.pocket_id`` is null
       (workspace-wide) OR equals the current ``pocket_id``.
    3. Convert each surviving wire dict to an ``InstinctRule`` via
       ``model_validate`` (parses + validates the CEL ``when``). On a parse
       failure drop THAT rule (WARNING), keep the rest.
    4. Guarded CEL probe: run each converted rule's ``when`` through
       ``evaluate_cel`` against the SAME merged context + resolver the composer
       will use. On ``CelEvaluationError`` drop THAT rule only (WARNING with
       workspace/rule id) so the composer's own eval is a safe re-run.
    """
    try:
        rows = await get_active_rules(workspace_id)
    except Exception:  # noqa: BLE001 — fail-OPEN: a store outage never blocks
        logger.warning(
            "discovered-rule enforcement: get_active_rules read failed for "
            "workspace=%s — falling through to the template-only path",
            workspace_id,
            exc_info=True,
        )
        return []

    # Build the SAME merged context + resolver the composer uses, so the
    # guarded probe is faithful to the real evaluation (row wins on collision).
    merged_context: dict[str, Any] = {}
    if workspace_context:
        merged_context.update(workspace_context)
    merged_context.update(row_context)
    probe_resolver = resolver or TemplateIdentifierResolver(template.state)

    clean: list[InstinctRule] = []
    for row in rows:
        rule_id = row.get("id", "<unknown>")
        # Step 2 — pocket scope. ``None`` pocket_id = workspace-wide.
        scope = row.get("scope") or {}
        scope_pocket = scope.get("pocket_id")
        if scope_pocket is not None and scope_pocket != pocket_id:
            continue

        # Step 3 — convert + validate the CEL ``when``.
        try:
            rule = InstinctRule.model_validate(
                {"when": row.get("when"), "action": row.get("action")}
            )
        except Exception:  # noqa: BLE001 — a malformed discovered rule is dropped
            logger.warning(
                "discovered-rule enforcement: dropping unparseable rule "
                "workspace=%s rule=%s (when=%r action=%r)",
                workspace_id,
                rule_id,
                row.get("when"),
                row.get("action"),
            )
            continue

        # Step 4 — guarded CEL probe. A discovered rule that errors on eval is
        # dropped here so it never reaches the composer's loud-fail raise path.
        try:
            evaluate_cel(rule.when, merged_context, probe_resolver, now=now)
        except CelEvaluationError as exc:
            logger.warning(
                "discovered-rule enforcement: dropping rule whose CEL failed to "
                "evaluate — workspace=%s rule=%s when=%r: %s",
                workspace_id,
                rule_id,
                rule.when,
                exc,
            )
            continue

        clean.append(rule)

    return clean


def _merge_discovered_rules(
    template: PocketTemplate, discovered: list[InstinctRule]
) -> PocketTemplate:
    """Return a shallow copy of ``template`` whose ``instinct_rules.rules`` are
    the discovered rules FOLLOWED BY the template's own rules.

    Discovered rules go FIRST so a discovered ``block`` wins step-1's first-match
    short-circuit; for ``require_approval`` / ``notify`` order is immaterial.
    The template object is NEVER mutated — ``model_copy(deep=False)`` plus a
    freshly-copied ``InstinctRulesDef`` keeps the original intact for any other
    reader (100% backward-compat).
    """
    base_def = template.instinct_rules
    merged_def = InstinctRulesDef(
        escalation=base_def.escalation if base_def else None,
        rules=[*discovered, *(list(base_def.rules) if base_def else [])],
    )
    return template.model_copy(update={"instinct_rules": merged_def}, deep=False)


async def gate_action(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    template: PocketTemplate,
    action_name: str,
    row_context: dict[str, Any],
    workspace_context: dict[str, Any] | None = None,
    row_id: str = "",
    park: dict[str, Any] | None = None,
    resolver: IdentifierResolver | None = None,
    now: datetime | None = None,
    approval_level: ApprovalLevel | str | None = ApprovalLevel.ASK,
    dry_run_mode: bool = False,
) -> InstinctGateResult:
    """Resolve the template-level Instinct verdict for one action+row.

    Calls the pure OSS composer (``resolve_instinct``) first, then
    branches:

    * ``BLOCK`` → persist nothing. Return ``next_step="blocked"``.
    * ``EXECUTE`` / ``NOTIFY_AND_EXECUTE`` → no persistence. Return
      ``next_step="proceed"`` with ``notify_rules`` populated for the
      side-effect dispatcher.
    * ``ESCALATE_APPROVAL`` → run the layered triage router. Read the
      (pocket, action) trust score from the trust ledger, build a
      ``TriageProposal`` from the verdict + the parked write's method/path/
      compensate, call ``classify_lane`` and branch by lane:

        - AUTO → ``approvals_service.auto_approve`` writes a DECIDED row;
          ``next_step="auto_approved"``.
        - OPTIMISTIC → an auto_approved row tagged lane=OPTIMISTIC;
          ``next_step="optimistic_proceed"`` (the executor fires the write
          and registers a bounded compensation handle).
        - DRY_RUN → ``next_step="dry_run"`` (no row; the executor resolves
          and audits the write but never fires it).
        - ESCALATE → the human-pending path (unchanged), with BATCH dedup.

    ``approval_level`` defaults to ``ASK`` — the dormant level under which
    ``classify_lane`` always returns ESCALATE, so EVERY existing caller
    (none pass the new args) is byte-identical to the pre-integration gate.
    A workspace must explicitly opt into ``TRIAGE``/``TRUSTED`` for any
    lane other than ESCALATE to fire. The cloud router reads the
    workspace's level (falling back to the global config default) and
    passes it here.

    ``dry_run_mode`` (from config / the workspace) routes escalating writes
    to DRY_RUN — but only AFTER the BLOCK and ASK floors, so a BLOCK row is
    still blocked and a dormant workspace still escalates to a human even
    when dry-run is globally on.

    Errors:
        ``InstinctResolutionError`` from the composer (unknown action
        on the template, or a CEL eval failure on a rule) is mapped to
        ``NotFound("instinct_action", action_name)``.
    """
    level = _coerce_approval_level(approval_level)

    # F6 — merge approved workspace-discovered rules into the template before
    # the (unchanged) composer call. DEFAULT-OFF: when the flag is off,
    # `get_active_rules` is never called and `effective_template is template`,
    # so the entire discovered branch is dead code on the default path.
    effective_template = template
    if get_settings().instinct_enforce_discovered_rules:
        # Pin a stable `now` shared by the guarded probe and the composer so a
        # time-sensitive CEL `when` evaluates identically in both.
        if now is None:
            now = datetime.now(UTC)
        discovered = await _load_discovered_instinct_rules(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            template=template,
            row_context=row_context,
            workspace_context=workspace_context,
            resolver=resolver,
            now=now,
        )
        if discovered:
            effective_template = _merge_discovered_rules(template, discovered)

    try:
        decision = resolve_instinct(
            effective_template,
            action_name,
            row_context,
            workspace_context,
            resolver=resolver,
            now=now,
        )
    except InstinctResolutionError as exc:
        logger.warning(
            "instinct gate failed for action=%s pocket=%s: %s",
            action_name,
            pocket_id,
            exc,
        )
        raise NotFound("instinct_action", action_name) from exc

    if decision.verdict == "BLOCK":
        return InstinctGateResult(
            decision=decision,
            next_step="blocked",
            approval_id=None,
            notify_rules=[],
            lane=TriageLane.ESCALATE,
        )

    if decision.verdict == "ESCALATE_APPROVAL":
        return await _route_escalation(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            action_name=action_name,
            row_id=row_id,
            row_context=row_context,
            decision=decision,
            park=park,
            level=level,
            dry_run_mode=dry_run_mode,
        )

    # EXECUTE / NOTIFY_AND_EXECUTE — proceed. Notify rules carry through.
    return InstinctGateResult(
        decision=decision,
        next_step="proceed",
        approval_id=None,
        notify_rules=list(decision.notify_rules),
        lane=TriageLane.ESCALATE,
    )


async def _route_escalation(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    action_name: str,
    row_id: str,
    row_context: dict[str, Any],
    decision: InstinctDecision,
    park: dict[str, Any] | None,
    level: ApprovalLevel,
    dry_run_mode: bool,
) -> InstinctGateResult:
    """Run the layered triage router for an ESCALATE_APPROVAL verdict.

    Reads trust, classifies the lane, audits the decision (at TRIAGE+), and
    dispatches per lane. ASK short-circuits to the unchanged human-pending
    path WITHOUT a trust read or audit (zero behavior change + zero new
    I/O on the dormant path).
    """
    # Fast path: dormant triager. classify_lane would return ESCALATE under
    # ASK anyway, but short-circuiting here means the dormant default does
    # not even read the trust ledger or emit a triage audit row — it is
    # byte-identical to the pre-integration escalate path.
    if level == ApprovalLevel.ASK:
        return await _escalate_to_human(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            action_name=action_name,
            row_id=row_id,
            row_context=row_context,
            decision=decision,
            park=park,
        )

    # Active triager — read trust, classify, audit.
    compensate = _compensate_from_park(park)
    method = str((park or {}).get("method") or "POST")
    path = str((park or {}).get("path") or "")
    trust_score, proposed_count = await trust_ledger.get_trust_score(
        workspace_id, pocket_id, action_name
    )

    proposal = TriageProposal(
        action=action_name,
        method=method,  # type: ignore[arg-type]
        path=path,
        compensate=compensate,
        trust_score=trust_score,
        proposed_count=proposed_count,
        instinct_verdict=decision.verdict,
        approval_level=level,
    )
    lane = classify_lane(proposal, dry_run_mode=dry_run_mode)

    _audit_triage_decision(
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        action=action_name,
        lane=lane,
        trust_score=trust_score,
        proposed_count=proposed_count,
        approval_level=level,
    )

    if lane == TriageLane.DRY_RUN:
        # No persistence — the executor resolves + audits the write but
        # never fires it. The dry-run sentinel stays server-side.
        return InstinctGateResult(
            decision=decision,
            next_step="dry_run",
            approval_id=None,
            notify_rules=list(decision.notify_rules),
            lane=lane,
        )

    if lane in (TriageLane.AUTO, TriageLane.OPTIMISTIC):
        body = _escalate_body(
            pocket_id=pocket_id,
            action_name=action_name,
            row_id=row_id,
            row_context=row_context,
            decision=decision,
            park=park,
        )
        reasoning = (
            f"triager lane={lane.name} trust={trust_score:.2f} "
            f"count={proposed_count} verdict={decision.verdict}"
        )
        wire = await approvals_service.auto_approve(
            workspace_id,
            user_id,
            body,
            trust_score=trust_score,
            triager_reasoning=reasoning,
            lane=lane.name,
        )
        next_step: NextStepT = "auto_approved" if lane == TriageLane.AUTO else "optimistic_proceed"
        return InstinctGateResult(
            decision=decision,
            next_step=next_step,
            approval_id=wire["id"],
            notify_rules=list(decision.notify_rules),
            lane=lane,
        )

    # BATCH / ESCALATE → human-pending (BATCH is dedup-then-escalate today;
    # the lane is recorded but routes to the same human queue).
    result = await _escalate_to_human(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        action_name=action_name,
        row_id=row_id,
        row_context=row_context,
        decision=decision,
        park=park,
    )
    # Stamp the classified lane onto the result (the human path defaults the
    # lane field to ESCALATE; record what the triager actually decided).
    return result.model_copy(update={"lane": lane})


async def _escalate_to_human(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    action_name: str,
    row_id: str,
    row_context: dict[str, Any],
    decision: InstinctDecision,
    park: dict[str, Any] | None,
) -> InstinctGateResult:
    """Persist (or dedup to) a pending approval row — the unchanged human path.

    T11 — BATCH dedup: a second escalate for the same (pocket, action, row)
    returns the existing pending row's id instead of creating a duplicate.
    """
    existing_id = await _find_existing_pending_id(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        action_name=action_name,
        row_id=row_id,
    )
    if existing_id is not None:
        logger.info(
            "instinct gate: dedup pending approval for pocket=%s action=%s row=%s → %s",
            pocket_id,
            action_name,
            row_id,
            existing_id,
        )
        return InstinctGateResult(
            decision=decision,
            next_step="pending_approval",
            approval_id=existing_id,
            notify_rules=list(decision.notify_rules),
            lane=TriageLane.ESCALATE,
        )

    body = _escalate_body(
        pocket_id=pocket_id,
        action_name=action_name,
        row_id=row_id,
        row_context=row_context,
        decision=decision,
        park=park,
    )
    wire = await approvals_service.create_approval(workspace_id, user_id, body)
    return InstinctGateResult(
        decision=decision,
        next_step="pending_approval",
        approval_id=wire["id"],
        notify_rules=list(decision.notify_rules),
        lane=TriageLane.ESCALATE,
    )


__all__ = ["InstinctGateResult", "NextStepT", "gate_action"]
