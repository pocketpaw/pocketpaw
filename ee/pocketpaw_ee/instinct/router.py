# ee/instinct/router.py — FastAPI router for the Instinct decision pipeline API.
# Created: 2026-03-28 — Propose, approve/reject, list pending, query audit.
# Updated: 2026-06-26 (ISO-2 — physical per-workspace isolation) — the store is
#   no longer one shared ``~/.pocketpaw/instinct.db``. ``_store`` now takes the
#   caller's ``workspace_id`` and routes through
#   ``pocketpaw.stores.get_instinct_store(workspace_id=...)``, so every action +
#   audit read/write hits that tenant's OWN
#   ``~/.pocketpaw/workspaces/<id>/instinct.db`` — and therefore that tenant's
#   own audit hash-chain. The audit ``verify`` and ``corrections`` endpoints,
#   which previously took no workspace, now resolve ``current_workspace_id`` so
#   ``verify_audit_chain`` checks the CALLER'S per-tenant chain (not a global
#   chain spanning tenants — the correct multi-tenant model). The internal
#   helpers ``_forward_to_soul`` and ``_fetch_current_fabric`` take
#   ``workspace_id`` from their endpoint callers (the latter also threads it into
#   the per-workspace Fabric store so it doesn't fail closed on a cloud path).
#   The per-endpoint W4a ``workspace_id`` filter args are UNCHANGED — physical
#   file isolation is additive defense-in-depth on top of the in-row filter.
# Updated: 2026-06-19 (SZD-5b — _pocket_create proposal type) — added a gated
#   starter-Pocket-create proposal kind (TENANCY GATE). ``_pocket_create_blob`` +
#   ``_assert_pocket_create_workspace`` are the peers of the existing blob
#   accessors + tenancy guards. The blob (``Action.parameters._pocket_create``)
#   carries the staged ``pocket_spec`` plus, as SEPARATE top-level fields, the
#   originating ``workspace_id`` and the owner ``user_id`` (kept OUT of the
#   editable spec so the correction flow can't move tenancy/owner). On APPROVE the
#   router emits ``human.corrected`` then fires
#   ``pocket_proposals.executor.execute_approved_pocket_create`` (which creates the
#   Pocket via the workspace-scoped ``pockets.service.create`` after a
#   ``CreatePocketRequest.model_validate`` at entry, and OWNS the chain close). On
#   REJECT the router emits ``human.corrected`` + ``decision.completed(rejected)``
#   itself (the executor never runs on reject — no Pocket is created). The
#   ``_assert_pocket_create_workspace`` 403 runs in ALL FOUR locations (approve /
#   bulk-approve / reject / bulk-reject) BEFORE any mutation — asymmetric tenant
#   scope is no tenant scope (pocketpaw#1183 / #1250). Same exactly-one-terminal
#   discipline as the Fabric-objects gate.
# Updated: 2026-06-19 (SZD-5a — _fabric_objects proposal type) — added a gated
#   Fabric-ontology proposal kind. ``_fabric_objects_blob`` +
#   ``_assert_fabric_objects_workspace`` are the peers of the existing blob
#   accessors + tenancy guards. The blob (``Action.parameters._fabric_objects``)
#   carries {object_types[], objects[], links[], workspace_id}. On APPROVE the
#   router emits ``human.corrected`` then fires
#   ``fabric_proposals.executor.execute_approved_fabric_objects`` (which
#   materialises the ontology via the workspace-scoped, idempotent
#   ``connectors.fabric_ingest.ingest_records`` upsert loop and OWNS the chain
#   close). On REJECT the router emits ``human.corrected`` +
#   ``decision.completed(rejected)`` itself (the executor never runs on reject —
#   no Fabric write happens). The ``_assert_fabric_objects_workspace`` 403 runs
#   in ALL FOUR locations (approve / bulk-approve / reject / bulk-reject) BEFORE
#   any mutation — asymmetric tenant scope is no tenant scope (pocketpaw#1183 /
#   #1250). Same exactly-one-terminal discipline as the external-action gate.
# Updated: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) — added the
#   FIFTH gated proposal kind: the Branch-primitive artifact-change MERGE GATE.
#   ``_artifact_change_blob`` + ``_assert_artifact_change_workspace`` are the
#   peers of the four existing blob accessors + tenancy guards. The blob
#   (``Action.parameters._artifact_change``) carries {scope_type, scope_id,
#   branch, from_version_id, to_version_id, workspace, user_id}. On APPROVE the
#   router emits ``human.corrected`` then fires
#   ``versions.instinct_executor.execute_approved_change`` (MERGE = publish the
#   candidate version via versions.publish + mark it merged + deploy via BP-2's
#   sites.publish_pocket for pocket/site scope; the executor owns the
#   executed/failed terminal). On REJECT the router emits ``human.corrected`` +
#   ``decision.completed(rejected)`` itself then fires
#   ``versions.instinct_executor.discard_rejected_change`` (DISCARD = flip the
#   candidate to reverted; the PUBLISHED pointer is left untouched). The
#   ``_assert_artifact_change_workspace`` 403 runs in ALL FOUR locations
#   (approve / bulk-approve / reject / bulk-reject) BEFORE any mutation —
#   asymmetric tenant scope is no tenant scope (pocketpaw#1183 / #1250). Part A
#   of BP-3 also added an additive ``scope_type`` to ``ProposeRequest`` /
#   ``propose_action`` so an Action can be scoped to a generic artifact.
#   TODO(BP-4): revert/discard semantics deepen in the executor + a history view.
# Updated: 2026-06-16 (feat/instinct-smart-triage) — ``propose_action`` now runs
#   a smart-approval auto-triage hook (``_run_auto_triage``) synchronously after
#   ``store.propose`` succeeds and BEFORE the response returns. The hook calls
#   the cheap-model classifier in ``ee.instinct.auto_triage``; on an APPROVE
#   verdict it auto-approves the Action via ``store.auto_approve`` (writing the
#   hash-chained ``action_auto_approved`` ledger row with the triager's verdict +
#   reasoning, ``actor="system:triager"``) and emits the same Decision-Graph
#   chain events a human approval emits, then returns the auto-approved Action so
#   the human is not notified. Otherwise the original proposal is returned
#   unchanged and the route falls through to the existing human path
#   byte-for-byte. The feature is OFF BY DEFAULT: the approval level defaults to
#   ``ASK`` (triager not invoked) until a workspace opts in to TRIAGE / TRUSTED.
#   The hook is fail-safe (any triager error / low confidence → ESCALATE; v1 has
#   no auto-reject, so a bad action is escalated, never silently killed) and
#   best-effort — a wiring failure can never break the propose response, and an
#   audit-ledger failure (``AuditChainError``) is logged LOUD at ERROR and leaves
#   the proposal PENDING. The classifier reuses the mandate-foreman
#   ``ClaudeCliLlm`` shell-out pattern (``claude -p --output-format json``) —
#   agent mode has NO ANTHROPIC_API_KEY, so ``AsyncAnthropic`` is deliberately
#   NOT used. KNOWN LIMITATION (v1): the ``rule_flagged`` safety signal is read
#   from a caller-supplied ``Action.parameters._triage`` hint (an internal-caller
#   trust input), not re-resolved from the pocket's standing rules here — see the
#   ``_run_auto_triage`` docstring for the rationale + the v2 TODO.
# Updated: 2026-06-11 (feat/external-action-proposal) — added the THIRD gated
#   proposal kind: a gated external-action connector call. ``_external_action_blob``
#   + ``_assert_external_action_workspace`` are the peers of the ``_code_change``
#   / ``_pocket_write`` blob accessor + tenancy guard. The four dispatch handlers
#   (approve_action / bulk_approve_actions / reject_action / bulk_reject_actions)
#   now also branch on an ``_external_action`` blob: on APPROVE the router emits
#   ``human.corrected`` then fires ``external_actions.executor.
#   execute_approved_external_action`` (which OWNS the chain close); on REJECT the
#   router emits ``human.corrected`` + ``decision.completed(rejected)`` itself
#   (the executor never runs on reject). The up-front tenancy assertion runs in
#   all four locations. Same exactly-one-terminal discipline as the pocket-write
#   bridge + belt executor — no double chain close. The blob's
#   ``proposed_event_id`` (the chain-opening ``agent.proposed`` id) is the
#   causation source, resolved via the generic ``_code_change_proposed_event_id``
#   helper (it reads ``blob["proposed_event_id"]``, shared across the two kinds).
#   Rebased over the paw-print ``_customer_reply`` delivery hooks (gap2) — the
#   external-action branches sit BEFORE the customer-reply hooks in approve /
#   reject (all blob kinds are mutually exclusive; customer-reply keeps its
#   last-before-return placement and its no-bulk / no-assert design unchanged).
# Updated: 2026-06-11 (merge origin/dev into integration/sovereignty-waves —
#   Belt code-change union) — reconciled the sovereignty extraction with dev's
#   Belt feature. The chain-emit helpers (``_emit_human_corrected`` /
#   ``_emit_decision_completed_rejected`` / ``_emit_policy_evaluated_approved``)
#   plus ``_chain_actor_human`` / ``_parked_*`` / ``_pocket_write_blob`` /
#   ``_code_change_proposed_event_id`` now live ONLY in
#   ``ee.instinct.chain_emitters`` (single source of truth) and are imported
#   here; their dev-side Belt logic (the ``causation_override`` param +
#   ``_code_change_proposed_event_id``) was folded into that module. The
#   ``_code_change_blob`` / ``_assert_code_change_workspace`` Belt blob helpers
#   stay router-local (peers of ``_pocket_write_blob`` /
#   ``_assert_pocket_write_workspace``).
# Updated: 2026-06-10 (W4a — workspace-scope instinct reads) — closes a
#   cross-tenant decision leak on shared deployments. The instinct store is
#   GLOBAL (one shared DB) and list/pending/audit reads previously passed no
#   workspace, so a workspace-A operator could read workspace-B's pending
#   actions and audit trail. Now ``propose_action`` stamps the caller's active
#   ``current_workspace_id`` on the new action (and its audit rows), and every
#   per-tenant READ — ``pending_actions`` / ``list_actions`` / ``query_audit`` /
#   ``export_audit`` / ``get_audit_entry`` — threads ``current_workspace_id``
#   into the store so results are restricted to that tenant (plus legacy
#   NULL-workspace rows). ``workspace_id`` crosses to the OSS store as a PLAIN
#   str. ``/instinct/audit/verify`` stays GLOBAL on purpose — chain integrity is
#   a property of the whole W2b ledger, and tenancy here is a read filter that
#   never touches the hash chain. The pre-existing approve/reject/bulk
#   ``_assert_pocket_write_workspace`` guard (PR #1183 / RFC 09 Slice 3) already
#   bound the WRITE/escalation paths to the workspace; W4a closes the READ side.
# Updated: 2026-06-10 (sov/w2-instinct — tamper-evident audit) — W2b: added
#   GET /instinct/audit/verify, which walks the audit hash chain (built in
#   pocketpaw.instinct.store) and reports intact / first-broken row so a
#   customer or insurer can prove the ledger was not altered. Declared BEFORE
#   /instinct/audit/{audit_id} to avoid the literal-vs-parameter route
#   collision. GET /instinct/audit/export now also runs verification and
#   stamps an ``X-Audit-Chain-Intact: true|false`` response header. The
#   audit-ledger append in the store is now LOUD (raises AuditChainError on
#   failure); the Decision-Graph chain emits in this router stay best-effort
#   and are a separate concern (the journal, not this ledger, is their source
#   of truth).
# Updated: 2026-03-30 — Added GET /instinct/actions (list all with status filter),
#   GET /instinct/audit/export (JSON export), switched to singleton from ee.api.
# Updated: 2026-04-12 (Move 1 PR-A) — /approve now accepts optional edited fields.
#   When present, the server diffs the stored proposal against the edits, persists
#   a Correction, then approves. GET /instinct/corrections exposes corrections
#   scoped to a pocket or an action so the UI and agents can read them back.
# Updated: 2026-04-13 (Move 2 PR-B) — POST /instinct/actions accepts an optional
#   reasoning_trace + fabric_snapshots body so callers (and the agent tool) can
#   attach decision inputs at propose time. GET /instinct/audit/{id}?hydrate=1
#   returns the audit entry with the trace's referenced IDs expanded into Fabric
#   object snapshots, making the "Why?" drawer possible in the UI.
# Updated: 2026-05-07 (fix/rbac-guards-fabric-instinct-agent-knowledge) — all
#   endpoints now require a valid license + workspace membership. Read/propose
#   endpoints require ``instinct.read``/``instinct.propose`` (MEMBER). Approve,
#   reject, and all audit endpoints require ``instinct.approve``/``instinct.audit``
#   (ADMIN) — governance actions that trigger automations or record corrections.
#   Previously the router had zero auth.
# Updated: 2026-05-07 (feat/rbac-plan-feature-gate) — added router-level
#   ``require_plan_feature("instinct")`` so the entire Instinct API is gated to
#   business-tier (or higher) plans. Closes the plan-tier bypass where a
#   team-plan member who passed the workspace RBAC check still hit Instinct for
#   free.
# Updated: 2026-05-13 (feat/mission-control-facade) — added ``assignee`` query
#   param to GET /instinct/actions/pending (filter The Tray to a single human's
#   queue) plus POST /instinct/actions/bulk-approve and
#   POST /instinct/actions/bulk-reject. Bulk endpoints write N audit rows with
#   a shared ``bulk_id`` UUID so the bulk transaction is replay-able per item
#   and query-able as a unit.
# Updated: 2026-05-22 (RFC 05 M2b.1) — ``approve_action`` now fires a parked
#   pocket write. When the approved Action's ``parameters`` carries a
#   ``_pocket_write`` blob, the route lazy-imports
#   ``ee.cloud.pockets.instinct_bridge`` and calls ``execute_approved_write``
#   — best-effort, failures recorded on the Action, never breaking the
#   approve response. A lazy import avoids an instinct→pockets module-top
#   dependency.
# Updated: 2026-05-22 (security-review fixes for PR #1183) —
#   * BLOCKER 1: ``approve_action`` and ``bulk_approve_actions`` now
#     verify a parked ``_pocket_write`` belongs to the approver's active
#     workspace. ``require_action_any_workspace`` only checks the caller
#     holds the role somewhere; it does NOT bind the action to a
#     workspace. ``_assert_pocket_write_workspace`` raises ``Forbidden``
#     (403) when ``blob["workspace_id"]`` differs from the caller's
#     active workspace, closing a cross-tenant approval-escalation gap.
#   * BLOCKER 2: ``bulk_approve_actions`` now mirrors the single-approve
#     hook — every bulk-approved Action carrying a ``_pocket_write`` blob
#     fires ``execute_approved_write`` best-effort, so bulk-approved
#     pocket writes actually execute instead of silently stalling at
#     ``approved``.
#   * SHOULD-FIX 1: the audit ``approved_by``/``actor`` and the outcome
#     actor are now the AUTHENTICATED user id, not the free-text
#     ``approver`` request field — a caller can no longer forge the
#     audit actor. The request field stays for display only.
#
# Updated: 2026-05-26 (RFC 09 Slice 3 — Instinct emits + reject security fix) —
#   * Decision-Graph chain emits — ``approve_action`` /
#     ``bulk_approve_actions`` now emit ``human.corrected(disposition=
#     accepted|edited)`` per item, chained off the parked
#     ``policy.evaluated`` (the bridge populated the parked blob's
#     ``parked_policy_event_id`` in Slice 3). ``reject_action`` /
#     ``bulk_reject_actions`` emit ``human.corrected(disposition=
#     rejected)`` followed by ``decision.completed(passed=False,
#     action_outcome="rejected")`` to close the chain — the bridge is
#     never invoked on the reject paths, so the router owns the close.
#     The approve paths DO NOT emit ``decision.completed`` — the
#     bridge's ``execute_approved_write`` owns the chain close after
#     the post-approval HTTP call lands (success / re-validation
#     rejection / executor crash all close via
#     ``instinct_bridge._emit_bridge_chain_close``). All emits are
#     best-effort (``record_*`` helpers swallow projection failures
#     internally + the local try/except guards the journal-side
#     failure path so a Decision-Graph wire never breaks an approval /
#     rejection).
#   * Touch-time security fix on reject endpoints —
#     ``reject_action`` and ``bulk_reject_actions`` previously lacked
#     ``current_user`` / ``current_workspace_id`` deps, which meant
#     ``_assert_pocket_write_workspace`` could not run on reject paths.
#     A workspace-A approver could therefore reject a workspace-B
#     ``_pocket_write`` Action — a cross-tenant rejection escalation
#     mirror of the BLOCKER 1 gap closed for approvals in PR #1183. The
#     two deps are added and the assertion runs before any state
#     mutation. Same partial-failure-fails-whole-batch semantics as
#     ``bulk_approve_actions``.
#   * ``bulk_reject_actions`` per-item emit loop — the underlying
#     ``store.bulk_reject`` already iterates per item internally; the
#     router now also loops over the returned ``rejected`` list to fire
#     the per-item chain emits. No semantic change to the bulk-reject
#     response shape (``BulkActionResponse`` with shared ``bulk_id``).
#
# Updated: 2026-06-10 (feat/belt-gate, BS-3 — Belt code-change dispatch) —
#   ``approve_action`` / ``bulk_approve_actions`` now ALSO dispatch a Belt
#   develop-station code change. When the approved Action carries a
#   ``_code_change`` blob (the ``pocketpaw_belt`` MCP server stores it under
#   ``Action.parameters._code_change``), the route lazy-imports
#   ``ee.cloud.belt.executor`` and calls ``execute_approved_change`` — same
#   best-effort / lazy-import / never-break-the-response shape as the
#   pocket-write hook, keyed on a distinct parameters key so the two paths never
#   cross. ``_assert_code_change_workspace`` (the code-change peer of
#   ``_assert_pocket_write_workspace``) binds the Action to the approver's
#   workspace on approve / reject / bulk paths. The executor applies the diff in
#   a fresh worktree, commits, pushes, and opens a PR — it NEVER merges (the
#   captain merges on GitHub; Instinct is the mid gate).
#
# Updated: 2026-06-10 (feat/belt-trace, BS-4 — Belt Decision-Graph chain) —
#   the Belt code-change path now lands in the Decision Graph as ONE chain per
#   station run, mirroring the pocket-write chain. The propose path (belt.py)
#   mints the ``correlation_id`` + emits ``agent.proposed``; this router emits
#   the human-action + terminal events:
#     * approve / bulk-approve — emit ``human.corrected(accepted|edited)`` for
#       the ``_code_change`` blob, threading the ``agent.proposed`` event id
#       (from the blob's ``proposed_event_id``) as causation, then pass the
#       emitted ``human.corrected`` id into ``execute_approved_change(...,
#       human_event_id=...)`` so the executor's terminal ``decision.completed``
#       chains back to it. The executor owns the CLOSE on the approve path
#       (success → landed, failure → failed) — the router does NOT emit a
#       terminal here, so there is no double close.
#     * reject / bulk-reject — emit ``human.corrected(rejected)`` THEN
#       ``decision.completed(passed=False, action_outcome="rejected")`` here
#       (the executor never runs on reject, so the router owns the close), each
#       chaining causation to the prior event. The reason text rides as the
#       rejection comment on the terminal payload.
#   ``_code_change_proposed_event_id`` is the code-change peer of
#   ``_parked_policy_event_id``. All emits are best-effort (the chain folds via
#   ``correlation_id`` even if a causation_id is missing).
#
# Updated: 2026-05-26 (RFC 09 Slice 4 — approve-side policy.evaluated emit) —
#   * Captain Decision 12 (chain symmetry) follow-up — ``approve_action``
#     and ``bulk_approve_actions`` now emit a second
#     ``policy.evaluated(passed=True, policy_name="approve_per_row")``
#     AFTER ``human.corrected`` and BEFORE the bridge call. Today's
#     chain on an approved write reads ``instinct_policy_passed=False``
#     because the only ``policy.evaluated`` event seen by the projection
#     is the parked ``passed=False`` emit from
#     ``instinct_bridge.propose_pocket_write``. The projection's
#     ``_fold_policy`` uses the LAST policy.evaluated seen before the
#     terminal — adding the ``passed=True`` emit on the approve path
#     flips ``Decision.instinct_policy_passed`` to True and replaces the
#     placeholder policy name with the real approval-gate label
#     ("approve_per_row"). The synthetic policy name keeps approved
#     chains queryable as policy gates rather than confusing them with
#     auto-approve chains (the ``"auto"`` synthetic name from the
#     direct-success path). Reject chains keep the last-seen ``False``
#     emit so ``instinct_policy_passed`` stays False on rejection. Best-
#     effort with the same log-and-continue pattern as the other Slice 3
#     helpers — a Decision-Graph wiring failure must never break an
#     approval.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from pocketpaw.instinct.correction import (
    Correction,
    compute_patches,
    summarize_correction,
)
from pocketpaw.instinct.models import (
    Action,
    ActionCategory,
    ActionPriority,
    ActionStatus,
    ActionTrigger,
    AuditEntry,
)
from pocketpaw.instinct.store import AuditChainError
from pocketpaw.instinct.trace import FabricObjectSnapshot, ReasoningTrace
from pocketpaw_ee.cloud._core.deps import (
    current_user,
    current_workspace_id,
    require_plan_feature,
)
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import require_action_any_workspace

# Shared Decision-Graph chain-emit helpers — extracted here so the Mission
# Control service can import the same primitives without coupling to this
# router (sov/r2a FIX 3). Re-imported under their original names so the
# router's call sites and the existing test monkeypatches that target
# ``ee.instinct.router._emit_*`` keep working unchanged.
from pocketpaw_ee.instinct.chain_emitters import (
    _code_change_proposed_event_id,
    _emit_decision_completed_rejected,
    _emit_human_corrected,
    _emit_policy_evaluated_approved,
    _pocket_write_blob,
)

logger = logging.getLogger(__name__)


async def _run_auto_triage(action: Any, workspace_id: str) -> Any:
    """Smart-approval auto-triage hook (feat/instinct-smart-triage).

    Sits between ``store.propose`` succeeding and the propose response. Runs the
    cheap-model triager over the freshly proposed Action; on an APPROVE verdict
    the Action is auto-approved AND audited (``store.auto_approve`` →
    ``action_auto_approved`` ledger row), and the auto-approved Action is
    returned so the route skips the human-notification path. On ESCALATE / DENY
    (the common, safe case) the ORIGINAL Action is returned unchanged so the
    route falls through to today's human path byte-for-byte.

    SAFETY:
      * ``ASK`` level → the triager is never invoked; today's behaviour exactly.
        ASK is the DEFAULT (off-by-default per the PRD): with no workspace /
        pocket / env opt-in ``resolve_approval_level`` returns ASK, so this hook
        returns the original proposal untouched until a workspace turns it on.
      * A ``_triage`` hint carrying ``rule_flagged=true`` (a template
        ESCALATE_APPROVAL / BLOCK match) is NEVER auto-approved.
      * Any triager failure fails SAFE to ESCALATE (handled inside the module).
      * The whole hook is wrapped best-effort: a triager wiring failure must
        NEVER break the propose response — it falls back to the human path with
        the original Action.

    The hint object the caller may attach under ``Action.parameters._triage``:
      ``{"rule_flagged": bool, "instinct_rules": [...], "approval_level":
      "ASK"|"TRIAGE"|"TRUSTED" (per-pocket override), "reasoning_trace": {...},
      "fabric_snapshots": [...]}``. All optional.

    KNOWN LIMITATION (v1) — the ``rule_flagged`` safety signal is HINT-DEPENDENT:
    it is read from the caller-supplied ``_triage`` hint, not resolved here from
    the pocket's standing rules. The generic ``propose_action`` endpoint receives
    a free-form Action (title / description / recommendation / parameters) and
    does NOT carry the template ``action_name`` + per-row ``row_context`` that
    ``bundled_templates.resolve_instinct`` needs, so the router cannot cheaply
    re-derive the verdict at this seam. The correct owner of the flag is the
    PROPOSING path that already holds the template + verdict (e.g.
    ``ee.cloud.pockets.instinct_dispatch.gate_action``), which should stamp
    ``rule_flagged`` (and the matched ``instinct_rules``) onto the hint. This is
    sound as long as that path is the only one that turns the level above ASK —
    and ASK-by-default means an un-stamped proposal is never auto-approved
    anyway. The hint is therefore a trust input from an INTERNAL caller, not from
    the wire.
    TODO(instinct-triage v2): resolve ``rule_flagged`` + ``instinct_rules``
    directly from the store via ``action.pocket_id`` (load the pocket's template,
    map the Action back to its template ``action_name`` + ``row_context``, call
    ``resolve_instinct``) so the gate no longer trusts the caller. Tracked as the
    follow-up to this slice.
    """
    try:
        from pocketpaw_ee.instinct.auto_triage import (
            TriageContext,
            maybe_auto_approve,
            resolve_approval_level,
        )

        params = getattr(action, "parameters", None)
        params = params if isinstance(params, dict) else {}
        hint = params.get("_triage")
        hint = hint if isinstance(hint, dict) else {}

        level = resolve_approval_level(pocket_level=hint.get("approval_level"))

        # Resolve the parked-operation blob (one of the gated kinds), if any —
        # it gives the triager the concrete method/path/params to judge and the
        # Decision-Graph correlation id to close the chain on auto-approve.
        parked_blob = (
            _pocket_write_blob(action)
            or _code_change_blob(action)
            or _external_action_blob(action)
            or {}
        )

        # The reasoning trace + fabric snapshots were attached at propose time
        # and persisted into the audit row; surface what the caller passed on
        # the params hint so the triager sees the same decision inputs.
        reasoning_trace = hint.get("reasoning_trace")
        fabric_snapshots = hint.get("fabric_snapshots")

        context = TriageContext(
            workspace_id=str(workspace_id or ""),
            pocket_id=str(getattr(action, "pocket_id", "") or ""),
            action_id=str(getattr(action, "id", "") or ""),
            title=str(getattr(action, "title", "") or ""),
            description=str(getattr(action, "description", "") or ""),
            recommendation=str(getattr(action, "recommendation", "") or ""),
            rule_flagged=bool(hint.get("rule_flagged", False)),
            parked_blob=parked_blob if isinstance(parked_blob, dict) else {},
            reasoning_trace=reasoning_trace if isinstance(reasoning_trace, dict) else {},
            fabric_snapshots=fabric_snapshots if isinstance(fabric_snapshots, list) else [],
            instinct_rules=(
                hint.get("instinct_rules") if isinstance(hint.get("instinct_rules"), list) else []
            ),
        )

        outcome = await maybe_auto_approve(
            store=_store(workspace_id),
            action=action,
            context=context,
            level=level,
        )
        if outcome.auto_approved:
            logger.info(
                "instinct auto-triage AUTO-APPROVED action=%s (workspace=%s, level=%s)",
                context.action_id,
                workspace_id,
                level.value,
            )
        return outcome.action
    except AuditChainError:
        # The tamper-evident ledger append failed (``maybe_auto_approve`` already
        # catches this in the common path, but a chain failure in a chain-emit
        # import or a future store seam could still surface here). This is a
        # ledger-integrity event, NOT a routine triager wiring failure — log
        # LOUD at ERROR with a distinct message. Still fail-safe: return the
        # original PENDING proposal so the human reviews it.
        logger.error(
            "instinct auto-triage hit an audit-ledger failure for action=%s — "
            "ledger-integrity event (distinct from an LLM failure); leaving the "
            "proposal PENDING for the human",
            getattr(action, "id", "?"),
            exc_info=True,
        )
        return action
    except Exception:  # noqa: BLE001 — the hook must NEVER break propose
        logger.warning(
            "instinct auto-triage hook failed for action=%s — falling back to "
            "the human path with the original proposal",
            getattr(action, "id", "?"),
            exc_info=True,
        )
        return action


def _code_change_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_code_change`` blob on an Action, or ``None``.

    The blob is the Belt develop-station payload the ``pocketpaw_belt`` MCP
    server stores under ``Action.parameters._code_change`` (repo / base_branch /
    diff / summary / task + the originating ``workspace_id``). This is the
    code-change peer of ``_pocket_write_blob`` — the approve path dispatches the
    apply-on-approve executor on its presence, exactly as it dispatches the
    pocket-write bridge on ``_pocket_write``. Anything that is not a dict is
    treated as "no code change".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get("_code_change")
    return blob if isinstance(blob, dict) else None


def _external_action_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_external_action`` blob on an Action, or ``None``.

    The blob is the gated external-action payload
    ``ee.cloud.external_actions.propose`` stores under
    ``Action.parameters._external_action`` (connector_name / scope / action /
    params / params_hash / idempotency_key + the originating ``workspace_id``).
    This is the third gated proposal kind — the approve path dispatches the
    apply-on-approve executor on its presence, exactly as it dispatches the
    pocket-write bridge on ``_pocket_write`` and the belt executor on
    ``_code_change``. Anything that is not a dict is treated as "no external
    action".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get("_external_action")
    return blob if isinstance(blob, dict) else None


def _artifact_change_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_artifact_change`` blob on an Action, or ``None``.

    The blob is the Branch-primitive merge-gate payload a producer stores under
    ``Action.parameters._artifact_change``:
    ``{scope_type, scope_id, branch, from_version_id, to_version_id, workspace,
    user_id}``. This is the FIFTH gated proposal kind (BP-3) — the peer of
    ``_pocket_write`` / ``_code_change`` / ``_external_action`` / ``_belt_plan``.
    On APPROVE the router dispatches the merge executor (publish the target
    version + deploy); on REJECT it dispatches the discard. Anything not a dict
    is treated as "no artifact change".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get("_artifact_change")
    return blob if isinstance(blob, dict) else None


def _assert_artifact_change_workspace(action: Any, current_workspace: str) -> None:
    """Reject approving / rejecting an artifact change from another workspace.

    SECURITY (BP-3) — the merge gate is the human REVIEW/MERGE gate over a
    version candidate; approving it MOVES THE PUBLISHED POINTER and triggers a
    DEPLOY. ``require_action_any_workspace("instinct.approve")`` only proves the
    caller holds the role SOMEWHERE; this binds the artifact-change Action to the
    caller's active workspace. The artifact's tenancy lives on the blob's
    ``workspace`` (with ``workspace_id`` accepted as an alias). A blob whose
    workspace differs from the caller's active workspace → 403, BEFORE any state
    mutation, on BOTH the approve and the reject side (asymmetric tenant scope is
    no tenant scope — pocketpaw#1183 / #1250). A non-artifact-change Action (no
    blob) is unaffected.

    FAIL-CLOSED on an empty claim — a blob whose workspace resolves to "" (the
    key absent or null) is a HARD 403 (``instinct.missing_workspace_in_blob``),
    not a pass-through. An artifact change's tenancy is mandatory; without it the
    gate cannot verify the caller owns the artifact, so allowing it would let an
    attacker propose a workspace-less blob targeting a victim's pocket and have
    any operator approve (publish + DEPLOY) it. There is no legitimate
    empty-workspace case here.
    """
    blob = _artifact_change_blob(action)
    if blob is None:
        return
    blob_workspace = str(blob.get("workspace") or blob.get("workspace_id") or "")
    # SECURITY — an absent/empty workspace claim is a HARD 403, never a
    # pass-through. The old ``if blob_workspace and ...`` short-circuited: a blob
    # whose workspace resolved to "" skipped the tenancy check entirely, so an
    # attacker could propose an artifact change with workspace="" and
    # scope_id=<victim pocket> and have ANY operator in ANY workspace approve
    # (publish + DEPLOY) or reject (discard) it. There is no legitimate
    # empty-workspace case for an artifact change — its tenancy is mandatory —
    # so we fail closed before the equality check on both the approve and reject
    # side.
    if not blob_workspace:
        raise Forbidden(
            "instinct.missing_workspace_in_blob",
            "This artifact change has no workspace claim — cannot verify tenancy",
        )
    if blob_workspace != current_workspace:
        raise Forbidden(
            "instinct.cross_workspace_approval",
            "This artifact change belongs to a different workspace",
        )


def _belt_plan_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_belt_plan`` blob on an Action, or ``None``.

    The blob is the mandate FOREMAN's shift-plan payload
    (``ee.cloud.mandates.service.trigger_shift`` stores it under
    ``Action.parameters._belt_plan``: the PlanProposal + mandate/shift ids +
    budget snapshot + chain correlation fields). The fourth peer of
    ``_pocket_write`` / ``_code_change`` / ``_external_action`` — the approve
    path dispatches the plan executor on its presence; reject closes the chain
    here in the router. Anything that is not a dict is treated as "no plan".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get("_belt_plan")
    return blob if isinstance(blob, dict) else None


def _assert_belt_plan_workspace(action: Any, current_workspace: str) -> None:
    """Reject approving/rejecting a mandate shift plan from another workspace.

    Mirror of ``_assert_code_change_workspace`` for the ``_belt_plan`` blob —
    its tenancy lives entirely on the blob's ``workspace_id``.
    """
    blob = _belt_plan_blob(action)
    if blob is None:
        return
    blob_workspace = str(blob.get("workspace_id") or "")
    if blob_workspace and blob_workspace != current_workspace:
        raise Forbidden(
            "instinct.cross_workspace_approval",
            "This shift plan belongs to a different workspace",
        )


def _belt_plan_proposed_event_id(blob: dict[str, Any]) -> Any:
    """Pull the ``proposed_event_id`` off a ``_belt_plan`` blob (same field
    contract as the code-change blob) for ``human.corrected`` causation."""
    return _code_change_proposed_event_id(blob)


async def _mark_plan_rejected_safe(action: Any, reason: str) -> None:
    """Best-effort shift-record update for a rejected ``belt_plan`` Action.

    The router owns the CHAIN close on reject (the executor never runs); this
    lazy-imported hook only reflects the rejection onto the mandate's ShiftDoc
    (state=done, outcome carries the reason) so the pawprints feed reads it.
    Never breaks the reject response."""
    try:
        from pocketpaw_ee.cloud.mandates import executor as mandate_executor

        await mandate_executor.mark_plan_rejected(action, reason)
    except Exception:  # noqa: BLE001 — read-model nudge must never break reject
        logger.debug("mandate: mark_plan_rejected hook failed (non-fatal)", exc_info=True)


async def _emit_belt_run_updated_safe(
    *, workspace_id: str, action_id: str, status: str, stage: str
) -> None:
    """Publish ``belt_run_updated`` for an approve / reject lifecycle change
    (SC-2) on the WORKSPACE REALTIME BUS.

    The bus is the required path: approve happens in the Tray AFTER the chat turn
    ended, so there is no per-session SSE sink to drain into — only the
    workspace-scoped bus fan-out reaches the /belt page. Lazy-imports the belt
    console service so the instinct package keeps no module-top dependency on
    ``ee.cloud.belt`` (same lazy-import discipline the executor / bridge hooks
    use). Best-effort: a bus / import failure is swallowed so the approve /
    reject response is never broken."""
    try:
        from pocketpaw_ee.cloud.belt.service import emit_belt_run_updated

        await emit_belt_run_updated(
            workspace_id=workspace_id, action_id=action_id, status=status, stage=stage
        )
    except Exception:  # noqa: BLE001 — emit must never break approve / reject
        logger.debug("belt: belt_run_updated emit failed (non-fatal)", exc_info=True)


def _assert_code_change_workspace(action: Any, current_workspace: str) -> None:
    """Reject approving a Belt code change from another workspace.

    Mirror of ``_assert_pocket_write_workspace`` for the ``_code_change`` blob.
    ``require_action_any_workspace("instinct.approve")`` only proves the caller
    holds the role SOMEWHERE; this binds the code-change Action to the caller's
    active workspace. A code change carries no pocket the way a parked write
    does, so its tenancy lives entirely on the blob's ``workspace_id``. A blob
    whose ``workspace_id`` differs from the caller's active workspace → 403.
    A non-code-change Action (no blob) is unaffected.
    """
    blob = _code_change_blob(action)
    if blob is None:
        return
    blob_workspace = str(blob.get("workspace_id") or "")
    if blob_workspace and blob_workspace != current_workspace:
        raise Forbidden(
            "instinct.cross_workspace_approval",
            "This code change belongs to a different workspace",
        )


def _assert_external_action_workspace(action: Any, current_workspace: str) -> None:
    """Reject approving / rejecting an external action from another workspace.

    Mirror of ``_assert_code_change_workspace`` for the ``_external_action``
    blob. ``require_action_any_workspace("instinct.approve")`` only proves the
    caller holds the role SOMEWHERE; this binds the external-action Action to the
    caller's active workspace. An external action carries no pocket the way a
    parked write does, so its tenancy lives entirely on the blob's
    ``workspace_id``. A blob whose ``workspace_id`` differs from the caller's
    active workspace → 403. A non-external-action Action (no blob) is unaffected.
    """
    blob = _external_action_blob(action)
    if blob is None:
        return
    blob_workspace = str(blob.get("workspace_id") or "")
    if blob_workspace and blob_workspace != current_workspace:
        raise Forbidden(
            "instinct.cross_workspace_approval",
            "This external action belongs to a different workspace",
        )


def _fabric_objects_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_fabric_objects`` blob on an Action, or ``None``.

    The blob is the gated Fabric-ontology payload
    ``ee.cloud.fabric_proposals.propose`` stores under
    ``Action.parameters._fabric_objects`` (object_types / objects / links + the
    originating ``workspace_id``). This is a peer gated proposal kind — the
    approve path dispatches the apply-on-approve executor on its presence,
    exactly as it dispatches the external-action executor on ``_external_action``.
    Anything that is not a dict is treated as "no fabric objects".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get("_fabric_objects")
    return blob if isinstance(blob, dict) else None


def _assert_fabric_objects_workspace(action: Any, current_workspace: str) -> None:
    """Reject approving / rejecting a Fabric-ontology write from another workspace.

    Mirror of ``_assert_external_action_workspace`` for the ``_fabric_objects``
    blob. ``require_action_any_workspace("instinct.approve")`` only proves the
    caller holds the role SOMEWHERE; this binds the Fabric-objects Action to the
    caller's active workspace. A Fabric write carries no pocket the way a parked
    write does, so its tenancy lives entirely on the blob's ``workspace_id``. A
    blob whose ``workspace_id`` differs from the caller's active workspace → 403,
    on BOTH the approve and reject side (asymmetric tenant scope is no tenant
    scope — pocketpaw#1183 / #1250). A non-fabric-objects Action (no blob) is
    unaffected.
    """
    blob = _fabric_objects_blob(action)
    if blob is None:
        return
    blob_workspace = str(blob.get("workspace_id") or "")
    if blob_workspace and blob_workspace != current_workspace:
        raise Forbidden(
            "instinct.cross_workspace_approval",
            "This fabric-objects write belongs to a different workspace",
        )


def _pocket_create_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_pocket_create`` blob on an Action, or ``None``.

    The blob is the gated starter-Pocket-create payload
    ``ee.cloud.pocket_proposals.propose`` stores under
    ``Action.parameters._pocket_create`` (the staged ``pocket_spec`` +, as SEPARATE
    top-level fields, the originating ``workspace_id`` and the owner ``user_id``).
    This is a peer gated proposal kind — the approve path dispatches the
    apply-on-approve executor on its presence, exactly as it dispatches the
    Fabric-objects executor on ``_fabric_objects``. Anything that is not a dict is
    treated as "no pocket create".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get("_pocket_create")
    return blob if isinstance(blob, dict) else None


def _assert_pocket_create_workspace(action: Any, current_workspace: str) -> None:
    """Reject approving / rejecting a starter-Pocket create from another workspace.

    Mirror of ``_assert_fabric_objects_workspace`` for the ``_pocket_create`` blob.
    ``require_action_any_workspace("instinct.approve")`` only proves the caller
    holds the role SOMEWHERE; this binds the Pocket-create Action to the caller's
    active workspace. A proposed Pocket carries no EXISTING pocket the way a parked
    write does, so its tenancy lives entirely on the blob's top-level
    ``workspace_id`` (NEVER inside the editable ``pocket_spec``). A blob whose
    ``workspace_id`` differs from the caller's active workspace → 403, on BOTH the
    approve and reject side (asymmetric tenant scope is no tenant scope —
    pocketpaw#1183 / #1250). A non-pocket-create Action (no blob) is unaffected.
    """
    blob = _pocket_create_blob(action)
    if blob is None:
        return
    blob_workspace = str(blob.get("workspace_id") or "")
    if blob_workspace and blob_workspace != current_workspace:
        raise Forbidden(
            "instinct.cross_workspace_approval",
            "This pocket create belongs to a different workspace",
        )


def _assert_pocket_write_workspace(action: Any, current_workspace: str) -> None:
    """Reject approving a parked pocket write from another workspace.

    ``require_action_any_workspace("instinct.approve")`` only proves the
    caller holds ``instinct.approve`` in SOME workspace — it does not bind
    the Action being approved to that workspace, and the
    ``instinct_actions`` table has no ``workspace_id`` column. Without this
    check a caller with ``instinct.approve`` in workspace A could approve
    a workspace-B parked write and trigger a cross-tenant backend write.

    When the Action carries a ``_pocket_write`` blob whose ``workspace_id``
    differs from the caller's active workspace, raise ``Forbidden`` (403).
    A non-pocket-write Action (no blob) is unaffected — instinct's other
    Action kinds are not tenant-bound by this column.

    Slice 3 (RFC 09) — the reject paths now invoke this check too
    (previously only approve paths did). Same error code so the
    frontend's existing 403 handler covers both.
    """
    blob = _pocket_write_blob(action)
    if blob is None:
        return
    blob_workspace = str(blob.get("workspace_id") or "")
    if blob_workspace and blob_workspace != current_workspace:
        raise Forbidden(
            "instinct.cross_workspace_approval",
            "This action's pocket write belongs to a different workspace",
        )


router = APIRouter(
    tags=["Instinct"],
    dependencies=[Depends(require_license), Depends(require_plan_feature("instinct"))],
)


def _store(workspace_id: str):
    """Return the InstinctStore for ``workspace_id`` (ISO-2 — physical isolation).

    Routes through the workspace-keyed factory so every Instinct action +
    audit-ledger read/write in this router hits the caller's OWN
    ``~/.pocketpaw/workspaces/<id>/instinct.db`` file — and therefore that
    tenant's own audit hash-chain. ``workspace_id`` is the caller's active
    workspace, already resolved by ``current_workspace_id`` on each endpoint. The
    W4a in-row ``workspace_id`` read-filter the endpoints already pass STAYS — the
    physical file split is additive defense-in-depth on top of it.
    """
    from pocketpaw.stores import get_instinct_store

    return get_instinct_store(workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ProposeRequest(BaseModel):
    pocket_id: str
    # BP-3 — optional generic scope. ``None`` (the default) is the legacy
    # pocket scope; set it (e.g. "site") to scope the Action to another
    # artifact type, with ``pocket_id`` carrying the scope id within it.
    scope_type: str | None = None
    title: str
    description: str = ""
    recommendation: str = ""
    trigger: ActionTrigger
    category: ActionCategory = ActionCategory.WORKFLOW
    priority: ActionPriority = ActionPriority.MEDIUM
    parameters: dict[str, Any] = {}
    reasoning_trace: ReasoningTrace | None = Field(
        default=None,
        description=(
            "Optional decision trace: which Fabric objects / soul memories / "
            "KB articles / tool calls the agent consumed to produce this proposal. "
            "Persisted into the audit entry so the Why? drawer can expand it."
        ),
    )
    fabric_snapshots: list[FabricObjectSnapshot] = Field(
        default_factory=list,
        description=(
            "Optional snapshots of the Fabric objects referenced in the trace, "
            "captured at decision time so later live mutations don't erase the reasoning."
        ),
    )


class RejectRequest(BaseModel):
    reason: str = ""


class ApproveRequest(BaseModel):
    """Optional edits and approver metadata for an approval.

    When any of `title`, `description`, `recommendation`, `category`, `priority`,
    or `parameters` differ from the stored proposal, the server computes a
    Correction before approving. Omit the fields to approve unchanged.
    """

    approver: str = "user"
    title: str | None = None
    description: str | None = None
    recommendation: str | None = None
    category: ActionCategory | None = None
    priority: ActionPriority | None = None
    parameters: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ActionsListResponse(BaseModel):
    actions: list[Action]
    total: int


class AuditListResponse(BaseModel):
    entries: list[AuditEntry]
    total: int


class ApproveResponse(BaseModel):
    action: Action
    correction: Correction | None = Field(
        default=None,
        description="Present when the approver edited the proposal before approving.",
    )


class CorrectionsListResponse(BaseModel):
    corrections: list[Correction]
    total: int


class BulkApproveRequest(BaseModel):
    """Body for POST /instinct/actions/bulk-approve.

    ``ids`` is the list of pending action ids to flip to approved. ``note``
    is an optional operator-supplied note tagged onto every audit row in
    the bulk transaction (also surfaced in the shared ``bulk_id`` group).
    """

    ids: list[str] = Field(min_length=1)
    note: str | None = None
    approver: str = "user"


class BulkRejectRequest(BaseModel):
    """Body for POST /instinct/actions/bulk-reject.

    ``reason`` is required — the UI gates the bulk-reject button behind a
    typed reason. The server enforces non-empty so we don't end up with
    silently rejected items that confuse a later audit review.
    """

    ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)
    rejector: str = "user"


class BulkActionResponse(BaseModel):
    """Response shape for both bulk-approve and bulk-reject."""

    bulk_id: str = Field(
        description=(
            "UUID4 hex tag stamped onto every audit row written for this "
            "bulk transaction. Query ``GET /instinct/audit`` and filter "
            "client-side on ``context.bulk_id`` to recover the group."
        ),
    )
    affected: list[Action]
    missing: list[str] = Field(
        default_factory=list,
        description=(
            "IDs that did not flip — either the row didn't exist or it was "
            "not in ``pending`` state. The frontend can surface these "
            "individually so the operator knows which items still need "
            "manual attention."
        ),
    )


# ---------------------------------------------------------------------------
# Action endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/instinct/actions",
    response_model=Action,
    status_code=201,
    dependencies=[Depends(require_action_any_workspace("instinct.propose"))],
)
async def propose_action(
    req: ProposeRequest,
    workspace_id: str = Depends(current_workspace_id),
):
    """Propose a new action for human approval.

    Optional `reasoning_trace` and `fabric_snapshots` let callers attach the
    agent's decision inputs at propose time. They are persisted into the
    resulting audit row for later hydration via `/audit/{id}?hydrate=1`.

    W4a — the proposed action (and its audit rows) are stamped with the
    caller's active workspace so the cross-tenant decision leak is closed at
    the write side: later list/pending/audit reads scoped to a tenant only
    surface that tenant's actions.

    Smart-approval auto-triage (feat/instinct-smart-triage) — after the
    proposal is created, a cheap-model classifier runs synchronously (the
    ``_run_auto_triage`` hook). On an APPROVE verdict the Action is
    auto-approved AND written to the hash-chained audit ledger
    (``action_auto_approved``, ``actor="system:triager"``), and the
    auto-approved Action is returned (the human is not notified). On
    ESCALATE / DENY — and at the ``ASK`` approval level, where the triager is
    never invoked — the original proposal is returned unchanged and the route
    behaves exactly as before. The hook is fail-safe and best-effort: any
    triager failure escalates to the human, and a wiring failure can never
    break this propose response.
    """
    action = await _store(workspace_id).propose(
        pocket_id=req.pocket_id,
        title=req.title,
        description=req.description,
        recommendation=req.recommendation,
        trigger=req.trigger,
        category=req.category,
        priority=req.priority,
        parameters=req.parameters,
        reasoning_trace=req.reasoning_trace,
        fabric_snapshots=list(req.fabric_snapshots) if req.fabric_snapshots else None,
        workspace_id=workspace_id,
        scope_type=req.scope_type,
    )
    return await _run_auto_triage(action, workspace_id)


@router.get(
    "/instinct/actions/pending",
    response_model=list[Action],
    dependencies=[Depends(require_action_any_workspace("instinct.read"))],
)
async def pending_actions(
    pocket_id: str | None = Query(None),
    assignee: str | None = Query(
        None,
        description=(
            "Filter to actions awaiting approval from a specific human "
            "(user id). Drives The Tray in Mission Control so an operator "
            "only sees the items they own. When omitted, behavior is "
            "unchanged from before — every pending item is returned."
        ),
    ),
    workspace_id: str = Depends(current_workspace_id),
):
    """List actions waiting for human approval.

    W4a — scoped to the caller's active workspace (plus legacy NULL-workspace
    rows) so The Tray for tenant A never surfaces tenant B's pending decisions.
    """
    return await _store(workspace_id).pending(
        pocket_id=pocket_id, assignee=assignee, workspace_id=workspace_id
    )


@router.get(
    "/instinct/actions",
    response_model=ActionsListResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.read"))],
)
async def list_actions(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    status: str | None = Query(
        None, description="Filter by status: pending|approved|rejected|executed|failed"
    ),
    limit: int = Query(50, ge=1, le=500, description="Max actions to return"),
    workspace_id: str = Depends(current_workspace_id),
):
    """List all actions with optional status and pocket filters.

    W4a — scoped to the caller's active workspace (plus legacy NULL-workspace
    rows) so a tenant can't enumerate another tenant's actions.
    """
    store = _store(workspace_id)
    status_enum = ActionStatus(status) if status else None
    actions = await store.list_actions(
        pocket_id=pocket_id,
        status=status_enum,
        limit=limit,
        workspace_id=workspace_id,
    )
    return ActionsListResponse(actions=actions, total=len(actions))


# Bulk endpoints must be registered BEFORE the parameterised
# ``/instinct/actions/{action_id}/approve`` and ``.../reject`` routes:
# FastAPI matches in registration order and ``bulk-approve`` would
# otherwise be eaten by ``{action_id}`` and fail validation.
@router.post(
    "/instinct/actions/bulk-approve",
    response_model=BulkActionResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.approve"))],
)
async def bulk_approve_actions(
    req: BulkApproveRequest,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> BulkActionResponse:
    """Approve N pending actions in one call.

    Each item is flipped individually (so per-item audit replay still
    works) but every audit row carries a shared ``bulk_id`` UUID under
    ``context.bulk_id``. The operator can query the audit log filtered
    by that key to recover the bulk transaction as a unit. Items that
    are missing or already resolved come back in ``missing`` rather than
    raising — a partial-success surface beats a single all-or-nothing
    failure on the operator console.

    Security (PR #1183):
      * BLOCKER 1 — before flipping anything, every requested Action is
        loaded and any parked ``_pocket_write`` is checked against the
        caller's active workspace. A single cross-workspace item fails
        the whole call with 403 — a partial bulk that silently dropped
        the foreign item would hide the escalation attempt.
      * BLOCKER 2 — after the flip, each approved Action carrying a
        ``_pocket_write`` blob fires ``execute_approved_write`` so
        bulk-approved pocket writes actually execute (the single-approve
        hook is the template).
      * SHOULD-FIX 1 — the audit actor is the authenticated user id, not
        the free-text ``req.approver`` field.
    """
    store = _store(workspace_id)
    approver_id = str(user.id)

    # BLOCKER 1 — verify tenancy on every requested action up front. A
    # missing id simply has no blob to check; it falls through to
    # ``bulk_approve`` and lands in ``missing``.
    for action_id in req.ids:
        action = await store.get_action(action_id)
        if action is not None:
            _assert_pocket_write_workspace(action, workspace_id)
            _assert_code_change_workspace(action, workspace_id)
            _assert_external_action_workspace(action, workspace_id)
            _assert_fabric_objects_workspace(action, workspace_id)
            _assert_pocket_create_workspace(action, workspace_id)
            _assert_belt_plan_workspace(action, workspace_id)
            _assert_artifact_change_workspace(action, workspace_id)

    approved, missing, bulk_id = await store.bulk_approve(
        list(req.ids), approver=approver_id, note=req.note
    )

    # BLOCKER 2 — bulk-approved pocket writes must fire, exactly like the
    # single-approve hook. Best-effort per item: a lazy import keeps the
    # instinct package free of a module-top dependency on ee.cloud.pockets,
    # and any failure is recorded on the Action by the bridge — it must
    # never break the bulk response.
    #
    # RFC 09 Slice 3 — per-item ``human.corrected`` emit slots into the
    # same loop. Disposition is always ``accepted`` for bulk-approve —
    # the endpoint doesn't support edits (the UI doesn't expose them on
    # the bulk bar). The bridge owns the chain close on the approve
    # path so we do NOT emit ``decision.completed`` here.
    for action in approved:
        # BS-3/BS-4 — a Belt ``_code_change`` Action fires the apply-on-approve
        # executor. BS-4: it now carries a Decision-Graph chain
        # (``correlation_id`` minted at propose). Emit the per-item
        # ``human.corrected(accepted)`` here (bulk-approve has no edit surface,
        # so disposition is always ``accepted``), thread its event id into the
        # executor so the terminal ``decision.completed`` chains its causation
        # back to the human approval, then run the executor (which owns the
        # chain close). Same best-effort shape as the pocket-write hook.
        code_change_blob = _code_change_blob(action)
        if code_change_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=code_change_blob,
                action=action,
                user_id=approver_id,
                workspace_id=workspace_id,
                disposition="accepted",
                note=req.note,
                causation_override=_code_change_proposed_event_id(code_change_blob),
            )
            # SC-2 — same approved/gate nudge as the single-approve path; the
            # executor publishes the terminal (landed / failed) afterwards.
            await _emit_belt_run_updated_safe(
                workspace_id=workspace_id,
                action_id=str(action.id),
                status="approved",
                stage="gate",
            )
            try:
                from pocketpaw_ee.cloud.belt import executor as belt_executor

                await belt_executor.execute_approved_change(action, human_event_id=human_event_id)
            except Exception:
                logger.exception(
                    "bulk-approve belt code-change execution failed for %s (non-fatal)",
                    action.id,
                )
            continue

        # A gated ``_external_action`` Action fires the apply-on-approve
        # connector executor. Mirrors the code-change branch above: per-item
        # ``human.corrected(accepted)`` (bulk-approve has no edit surface), the
        # ``agent.proposed`` event id (off the blob's ``proposed_event_id``) as
        # causation, then the executor (which owns the chain close). Same
        # best-effort shape; matched BEFORE the pocket-write branch and
        # ``continue``d so the two never cross.
        external_action_blob = _external_action_blob(action)
        if external_action_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=external_action_blob,
                action=action,
                user_id=approver_id,
                workspace_id=workspace_id,
                disposition="accepted",
                note=req.note,
                causation_override=_code_change_proposed_event_id(external_action_blob),
            )
            try:
                from pocketpaw_ee.cloud.external_actions import (
                    executor as external_action_executor,
                )

                await external_action_executor.execute_approved_external_action(
                    action, human_event_id=human_event_id
                )
            except Exception:
                logger.exception(
                    "bulk-approve external-action execution failed for %s (non-fatal)",
                    action.id,
                )
            continue

        # A bulk-approved gated ``_fabric_objects`` Action materialises its
        # proposed ontology in Fabric. Mirrors the external-action branch above:
        # per-item ``human.corrected(accepted)`` (bulk-approve has no edit
        # surface), the ``agent.proposed`` event id (off the blob's
        # ``proposed_event_id``) as causation, then the executor (which owns the
        # chain close). Matched BEFORE the pocket-write fallthrough and
        # ``continue``d so the kinds never cross.
        fabric_objects_blob = _fabric_objects_blob(action)
        if fabric_objects_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=fabric_objects_blob,
                action=action,
                user_id=approver_id,
                workspace_id=workspace_id,
                disposition="accepted",
                note=req.note,
                causation_override=_code_change_proposed_event_id(fabric_objects_blob),
            )
            try:
                from pocketpaw_ee.cloud.fabric_proposals import (
                    executor as fabric_objects_executor,
                )

                await fabric_objects_executor.execute_approved_fabric_objects(
                    action, human_event_id=human_event_id
                )
            except Exception:
                logger.exception(
                    "bulk-approve fabric-objects execution failed for %s (non-fatal)",
                    action.id,
                )
            continue

        # A bulk-approved gated ``_pocket_create`` Action creates its proposed
        # starter Pocket. Mirrors the Fabric-objects branch above: per-item
        # ``human.corrected(accepted)`` (bulk-approve has no edit surface), the
        # ``agent.proposed`` event id (off the blob's ``proposed_event_id``) as
        # causation, then the executor (which owns the chain close). Matched BEFORE
        # the pocket-write fallthrough and ``continue``d so the kinds never cross.
        pocket_create_blob = _pocket_create_blob(action)
        if pocket_create_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=pocket_create_blob,
                action=action,
                user_id=approver_id,
                workspace_id=workspace_id,
                disposition="accepted",
                note=req.note,
                causation_override=_code_change_proposed_event_id(pocket_create_blob),
            )
            try:
                from pocketpaw_ee.cloud.pocket_proposals import (
                    executor as pocket_create_executor,
                )

                await pocket_create_executor.execute_approved_pocket_create(
                    action, human_event_id=human_event_id
                )
            except Exception:
                logger.exception(
                    "bulk-approve pocket-create execution failed for %s (non-fatal)",
                    action.id,
                )
            continue

        # MANDATES — a bulk-approved ``_belt_plan`` Action fires the plan
        # executor, exactly like the single-approve hook (disposition is always
        # ``accepted`` — bulk-approve has no edit surface). The executor owns
        # the chain close.
        belt_plan_blob = _belt_plan_blob(action)
        if belt_plan_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=belt_plan_blob,
                action=action,
                user_id=approver_id,
                workspace_id=workspace_id,
                disposition="accepted",
                note=req.note,
                causation_override=_belt_plan_proposed_event_id(belt_plan_blob),
            )
            try:
                from pocketpaw_ee.cloud.mandates import executor as mandate_executor

                await mandate_executor.execute_approved_plan(action, human_event_id=human_event_id)
            except Exception:
                logger.exception(
                    "bulk-approve belt_plan execution failed for %s (non-fatal)",
                    action.id,
                )
            continue

        # BP-3 — a bulk-approved ``_artifact_change`` Action MERGES its candidate
        # (publish + deploy), exactly like the single-approve hook. Disposition
        # is always ``accepted`` (bulk-approve has no edit surface). The executor
        # marks the Action executed/failed; matched BEFORE the pocket-write
        # fallthrough and ``continue``d so the kinds never cross.
        artifact_change_blob = _artifact_change_blob(action)
        if artifact_change_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=artifact_change_blob,
                action=action,
                user_id=approver_id,
                workspace_id=workspace_id,
                disposition="accepted",
                note=req.note,
                causation_override=_code_change_proposed_event_id(artifact_change_blob),
            )
            try:
                from pocketpaw_ee.versions import instinct_executor as artifact_executor

                await artifact_executor.execute_approved_change(
                    action, human_event_id=human_event_id
                )
            except Exception:
                logger.exception(
                    "bulk-approve artifact-change merge failed for %s (non-fatal)",
                    action.id,
                )
            continue

        action_blob = _pocket_write_blob(action)
        if action_blob is None:
            continue
        human_event_id = _emit_human_corrected(
            blob=action_blob,
            action=action,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition="accepted",
            note=req.note,
        )
        # Slice 4 — chain symmetry: a second ``policy.evaluated`` event
        # with ``passed=True`` flips ``Decision.instinct_policy_passed``
        # from the parked ``False`` to ``True``. ``causation_id`` points
        # at the just-emitted ``human.corrected`` so the projection's
        # edge graph carries the human → policy causal arrow.
        _emit_policy_evaluated_approved(
            blob=action_blob,
            action=action,
            user_id=approver_id,
            workspace_id=workspace_id,
            causation_event_id=human_event_id,
        )
        try:
            from pocketpaw_ee.cloud.pockets import instinct_bridge

            await instinct_bridge.execute_approved_write(action)
        except Exception:
            logger.exception(
                "bulk-approve pocket-write execution failed for %s (non-fatal)",
                action.id,
            )

    return BulkActionResponse(bulk_id=bulk_id, affected=approved, missing=missing)


@router.post(
    "/instinct/actions/bulk-reject",
    response_model=BulkActionResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.approve"))],
)
async def bulk_reject_actions(
    req: BulkRejectRequest,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> BulkActionResponse:
    """Reject N pending actions in one call. ``reason`` is required.

    Mirrors ``bulk_approve_actions``: shared ``bulk_id``, per-item audit
    rows, partial-success surface via ``missing``. The reason text lands
    on every audit row's ``context.reason`` and on each Action's
    ``rejected_reason`` so the soul-bridge correction pipeline still
    sees the same shape it sees on single-item rejects.

    Slice 3 (RFC 09) — endpoint signature grew ``current_user`` and
    ``current_workspace_id`` deps for the same two reasons as
    ``reject_action``: (a) the touch-time cross-workspace security fix,
    and (b) per-item ``human.corrected`` + ``decision.completed
    (rejected)`` chain emits. Cross-workspace check fails the whole
    batch with 403 — a partial bulk that silently dropped a foreign
    item would hide a cross-tenant rejection-escalation attempt
    (mirror of bulk-approve's BLOCKER 1 behaviour).
    """
    store = _store(workspace_id)
    rejector_id = str(user.id)

    # Touch-time security fix — verify tenancy on every requested
    # action up front, same shape as ``bulk_approve_actions``. A
    # missing id has no blob to check; it falls through to
    # ``bulk_reject`` and lands in ``missing``.
    for action_id in req.ids:
        action = await store.get_action(action_id)
        if action is not None:
            _assert_pocket_write_workspace(action, workspace_id)
            _assert_code_change_workspace(action, workspace_id)
            _assert_external_action_workspace(action, workspace_id)
            _assert_fabric_objects_workspace(action, workspace_id)
            _assert_pocket_create_workspace(action, workspace_id)
            _assert_belt_plan_workspace(action, workspace_id)
            _assert_artifact_change_workspace(action, workspace_id)

    rejected, missing, bulk_id = await store.bulk_reject(
        list(req.ids), reason=req.reason, rejector=rejector_id
    )

    # RFC 09 Slice 3 / BS-4 — per-item ``human.corrected`` + ``decision.
    # completed(rejected)`` emit loop. The store's bulk_reject already
    # iterates per item internally for the audit log; this loop adds
    # the chain emits. An item carries EITHER a ``_pocket_write`` blob OR
    # a ``_code_change`` blob (BS-4) — both close their chain on reject
    # here (the executor never runs on reject). An Action with neither
    # blob has no chain to close and is skipped.
    for action in rejected:
        action_blob = _pocket_write_blob(action)
        if action_blob is not None:
            _emit_human_corrected(
                blob=action_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                disposition="rejected",
                note=req.reason or None,
            )
            _emit_decision_completed_rejected(
                blob=action_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                reason=req.reason,
            )
            continue

        code_change_blob = _code_change_blob(action)
        if code_change_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=code_change_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                disposition="rejected",
                note=req.reason or None,
                causation_override=_code_change_proposed_event_id(code_change_blob),
            )
            _emit_decision_completed_rejected(
                blob=code_change_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                reason=req.reason,
                causation_override=human_event_id,
            )
            # SC-2 — terminal event for a bulk-rejected belt run.
            await _emit_belt_run_updated_safe(
                workspace_id=workspace_id,
                action_id=str(action.id),
                status="rejected",
                stage="done",
            )
            continue

        # A bulk-rejected gated ``_external_action`` Action closes its chain
        # HERE (the executor never runs on reject — the router owns the close).
        # Mirrors the code-change reject branch. Matched AFTER pocket-write +
        # code-change and ``continue``d so the three never cross.
        external_action_blob = _external_action_blob(action)
        if external_action_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=external_action_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                disposition="rejected",
                note=req.reason or None,
                causation_override=_code_change_proposed_event_id(external_action_blob),
            )
            _emit_decision_completed_rejected(
                blob=external_action_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                reason=req.reason,
                causation_override=human_event_id,
            )
            continue

        # A bulk-rejected gated ``_fabric_objects`` Action closes its chain HERE
        # (the executor never runs on reject — the router owns the close). NO
        # Fabric write happens. Mirrors the external-action reject branch.
        # Matched AFTER pocket-write + code-change + external-action and
        # ``continue``d so the kinds never cross.
        fabric_objects_blob = _fabric_objects_blob(action)
        if fabric_objects_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=fabric_objects_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                disposition="rejected",
                note=req.reason or None,
                causation_override=_code_change_proposed_event_id(fabric_objects_blob),
            )
            _emit_decision_completed_rejected(
                blob=fabric_objects_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                reason=req.reason,
                causation_override=human_event_id,
            )
            continue

        # A bulk-rejected gated ``_pocket_create`` Action closes its chain HERE
        # (the executor never runs on reject — the router owns the close). NO
        # Pocket is created. Mirrors the Fabric-objects reject branch. Matched
        # AFTER pocket-write + code-change + external-action + fabric-objects and
        # ``continue``d so the kinds never cross.
        pocket_create_blob = _pocket_create_blob(action)
        if pocket_create_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=pocket_create_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                disposition="rejected",
                note=req.reason or None,
                causation_override=_code_change_proposed_event_id(pocket_create_blob),
            )
            _emit_decision_completed_rejected(
                blob=pocket_create_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                reason=req.reason,
                causation_override=human_event_id,
            )
            continue

        # MANDATES — a bulk-rejected ``_belt_plan`` Action closes its chain
        # here (the plan executor never runs on reject), mirroring the
        # code-change branch above.
        belt_plan_blob = _belt_plan_blob(action)
        if belt_plan_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=belt_plan_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                disposition="rejected",
                note=req.reason or None,
                causation_override=_belt_plan_proposed_event_id(belt_plan_blob),
            )
            _emit_decision_completed_rejected(
                blob=belt_plan_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                reason=req.reason,
                causation_override=human_event_id,
            )
            await _mark_plan_rejected_safe(action, req.reason)
            continue

        # BP-3 — a bulk-rejected ``_artifact_change`` Action DISCARDS its
        # candidate and closes its chain HERE (the merge executor never runs on
        # reject). The published pointer is left untouched. Mirrors the
        # code-change reject branch.
        artifact_change_blob = _artifact_change_blob(action)
        if artifact_change_blob is not None:
            human_event_id = _emit_human_corrected(
                blob=artifact_change_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                disposition="rejected",
                note=req.reason or None,
                causation_override=_code_change_proposed_event_id(artifact_change_blob),
            )
            _emit_decision_completed_rejected(
                blob=artifact_change_blob,
                action=action,
                user_id=rejector_id,
                workspace_id=workspace_id,
                reason=req.reason,
                causation_override=human_event_id,
            )
            try:
                from pocketpaw_ee.versions import instinct_executor as artifact_executor

                await artifact_executor.discard_rejected_change(action)
            except Exception:
                logger.exception(
                    "bulk-reject artifact-change discard failed for %s (non-fatal)",
                    action.id,
                )

    return BulkActionResponse(bulk_id=bulk_id, affected=rejected, missing=missing)


@router.post(
    "/instinct/actions/{action_id}/approve",
    response_model=ApproveResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.approve"))],
)
async def approve_action(
    action_id: str,
    req: ApproveRequest | None = None,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Approve a pending action, optionally with edits.

    If the request body carries edits, the server diffs the stored proposal
    against the incoming shape and persists a Correction alongside the
    approval. Callers that want to approve unchanged can POST with no body.

    Security (PR #1183):
      * BLOCKER 1 — a parked ``_pocket_write`` must belong to the
        approver's active workspace, else 403.
      * SHOULD-FIX 1 — the audit actor + outcome actor are the
        authenticated user id, never the free-text ``req.approver``.
    """
    store = _store(workspace_id)
    before = await store.get_action(action_id)
    if not before:
        raise HTTPException(404, "Action not found")

    # BLOCKER 1 — reject a cross-workspace parked-write approval before
    # any state mutation. ``require_action_any_workspace`` only proved the
    # caller holds ``instinct.approve`` somewhere; this binds the Action
    # to the caller's workspace.
    _assert_pocket_write_workspace(before, workspace_id)
    # Same tenancy gate for a Belt code-change Action (BS-3) — its
    # ``_code_change`` blob carries the workspace, not a pocket.
    _assert_code_change_workspace(before, workspace_id)
    # Same tenancy gate for a gated external-action Action — its
    # ``_external_action`` blob carries the workspace, not a pocket.
    _assert_external_action_workspace(before, workspace_id)
    # Same tenancy gate for a gated Fabric-objects Action — its
    # ``_fabric_objects`` blob carries the workspace, not a pocket. Approving it
    # writes typed objects into the tenant's Fabric, so the cross-workspace gate
    # is mandatory here.
    _assert_fabric_objects_workspace(before, workspace_id)
    # Same tenancy gate for a gated Pocket-create Action — its ``_pocket_create``
    # blob carries the workspace (and owner) on SEPARATE top-level fields, not a
    # pocket. Approving it creates a Pocket in the tenant's workspace, so the
    # cross-workspace gate is mandatory here.
    _assert_pocket_create_workspace(before, workspace_id)
    # Same tenancy gate for a mandate shift-plan Action (belt_plan) — its
    # ``_belt_plan`` blob carries the workspace.
    _assert_belt_plan_workspace(before, workspace_id)
    # BP-3 — same tenancy gate for an artifact-change merge (its
    # ``_artifact_change`` blob carries the workspace). Approving it moves the
    # published pointer + deploys, so the cross-workspace gate is mandatory here.
    _assert_artifact_change_workspace(before, workspace_id)

    req = req or ApproveRequest()
    # SHOULD-FIX 1 — the audit actor is the authenticated identity, not
    # the request body's free-text ``approver``. The body field may still
    # carry a display label, but it can never forge the audit trail.
    approver_id = str(user.id)
    after, edited_fields = _apply_edits(before, req)

    correction: Correction | None = None
    if edited_fields:
        patches = compute_patches(before, after)
        if patches:
            correction = Correction(
                action_id=before.id,
                pocket_id=before.pocket_id,
                actor=approver_id,
                patches=patches,
                context_summary=summarize_correction(before, patches),
                action_title=before.title,
            )
            await store.record_correction(correction)
            await _persist_edits(store, after, edited_fields)
            await _forward_to_soul(correction, after, workspace_id)

    approved = await store.approve(action_id, approver=approver_id)
    if not approved:
        raise HTTPException(404, "Action not found")

    # RFC 09 Slice 3 — emit the ``human.corrected`` chain event BEFORE
    # the bridge fires. Disposition is ``edited`` when the approver
    # adjusted fields (``edited_fields`` is non-empty), ``accepted``
    # otherwise. The bridge owns the chain close on the approve path
    # (``_emit_bridge_chain_close`` from ``execute_approved_write``),
    # so we do NOT emit ``decision.completed`` here — emitting it would
    # double-fire the chain terminal.
    approved_blob = _pocket_write_blob(approved)
    if approved_blob is not None:
        disposition = "edited" if edited_fields else "accepted"
        # ``note`` is the correction's free-text summary when the
        # approver edited; None on a plain approve.
        note = correction.context_summary if correction is not None else None
        human_event_id = _emit_human_corrected(
            blob=approved_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition=disposition,
            note=note,
        )
        # Slice 4 — chain symmetry: a second ``policy.evaluated`` event
        # with ``passed=True`` flips ``Decision.instinct_policy_passed``
        # to True on the approved chain. ``causation_id`` points at the
        # just-emitted ``human.corrected`` so the chain reads policy
        # (fail) → human → policy(pass) → completed as one causal walk.
        _emit_policy_evaluated_approved(
            blob=approved_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            causation_event_id=human_event_id,
        )

    # RFC 05 M2b.1 — when the approved Action carries a parked pocket
    # write (``parameters._pocket_write``), fire it. Best-effort: a
    # lazy import keeps the instinct package free of a module-top
    # dependency on ee.cloud.pockets, and any failure is recorded on the
    # Action by the bridge itself — it must NEVER break this approve
    # response. A non-pocket-write Action (the common case) skips this.
    if approved_blob is not None:
        try:
            from pocketpaw_ee.cloud.pockets import instinct_bridge

            await instinct_bridge.execute_approved_write(approved)
        except Exception:
            logger.exception("pocket-write execution after approval failed (non-fatal)")

    # BS-3 — when the approved Action carries a Belt ``_code_change`` blob,
    # apply the diff in a fresh worktree and open a PR. Same best-effort,
    # lazy-import, never-break-the-approve-response shape as the pocket-write
    # hook above; the executor records success / failure on the Action itself.
    # A non-code-change Action skips this. The captain still merges on GitHub —
    # this opens the PR, it does NOT merge.
    #
    # BS-4 — this is part of the Belt Decision-Graph chain. Emit the
    # ``human.corrected`` event for the code-change approval HERE (the router
    # owns the human-action emit on every approve path), then thread its event
    # id into the executor so the terminal ``decision.completed`` chains its
    # causation back to the approval: ``agent.proposed → human.corrected →
    # decision.completed`` under one correlation_id. The executor owns the
    # chain CLOSE (success or failure) — the router does NOT emit
    # ``decision.completed`` for code_change, mirroring how the pocket-write
    # bridge owns the close on its approve path. No double terminal.
    code_change_blob = _code_change_blob(approved)
    if code_change_blob is not None:
        disposition = "edited" if edited_fields else "accepted"
        note = correction.context_summary if correction is not None else None
        human_event_id = _emit_human_corrected(
            blob=code_change_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition=disposition,
            note=note,
            causation_override=_code_change_proposed_event_id(code_change_blob),
        )
        # SC-2 — publish ``belt_run_updated`` (status=approved, stage=gate) on
        # the workspace bus BEFORE the executor runs so the /belt page flips the
        # run to "applying..." immediately. The executor then publishes the
        # terminal (landed / failed).
        await _emit_belt_run_updated_safe(
            workspace_id=workspace_id, action_id=str(approved.id), status="approved", stage="gate"
        )
        try:
            from pocketpaw_ee.cloud.belt import executor as belt_executor

            await belt_executor.execute_approved_change(approved, human_event_id=human_event_id)
        except Exception:
            logger.exception("belt code-change execution after approval failed (non-fatal)")

    # When the approved Action carries a gated ``_external_action`` blob, fire
    # the connector call. Same best-effort, lazy-import, never-break-the-approve-
    # response shape as the pocket-write + code-change hooks above; the executor
    # records success / failure on the Action itself. A non-external-action
    # Action skips this.
    #
    # The external action is part of its own Decision-Graph chain (the propose
    # helper minted the ``correlation_id`` + emitted ``agent.proposed``). Emit
    # the ``human.corrected`` event for the approval HERE (the router owns the
    # human-action emit on every approve path), citing the ``agent.proposed``
    # event id (stored on the blob as ``proposed_event_id`` — the same field the
    # code-change path reads, so ``_code_change_proposed_event_id`` resolves it)
    # as causation, then thread its event id into the executor so the terminal
    # ``decision.completed`` chains back to the approval:
    # ``agent.proposed → human.corrected → decision.completed`` under one
    # correlation_id. The executor owns the chain CLOSE (success or failure) —
    # the router does NOT emit ``decision.completed`` here, mirroring the
    # pocket-write bridge + belt executor. No double terminal.
    external_action_blob = _external_action_blob(approved)
    if external_action_blob is not None:
        disposition = "edited" if edited_fields else "accepted"
        note = correction.context_summary if correction is not None else None
        human_event_id = _emit_human_corrected(
            blob=external_action_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition=disposition,
            note=note,
            causation_override=_code_change_proposed_event_id(external_action_blob),
        )
        try:
            from pocketpaw_ee.cloud.external_actions import executor as external_action_executor

            await external_action_executor.execute_approved_external_action(
                approved, human_event_id=human_event_id
            )
        except Exception:
            logger.exception("external-action execution after approval failed (non-fatal)")

    # When the approved Action carries a gated ``_fabric_objects`` blob,
    # materialise the proposed ontology in Fabric (workspace-scoped, idempotent
    # ingest). Same best-effort, lazy-import, never-break-the-approve-response
    # shape as the external-action hook above; the executor records success /
    # failure on the Action itself. A non-fabric-objects Action skips this.
    #
    # The Fabric-objects write is part of its own Decision-Graph chain (the
    # propose helper minted the ``correlation_id`` + emitted ``agent.proposed``).
    # Emit the ``human.corrected`` event for the approval HERE (the router owns
    # the human-action emit on every approve path), citing the ``agent.proposed``
    # event id (stored on the blob as ``proposed_event_id`` — the same field the
    # external-action path reads, so ``_code_change_proposed_event_id`` resolves
    # it) as causation, then thread its event id into the executor so the terminal
    # ``decision.completed`` chains back to the approval. The executor owns the
    # chain CLOSE (success or failure) — the router does NOT emit
    # ``decision.completed`` here, mirroring the external-action executor. No
    # double terminal.
    fabric_objects_blob = _fabric_objects_blob(approved)
    if fabric_objects_blob is not None:
        disposition = "edited" if edited_fields else "accepted"
        note = correction.context_summary if correction is not None else None
        human_event_id = _emit_human_corrected(
            blob=fabric_objects_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition=disposition,
            note=note,
            causation_override=_code_change_proposed_event_id(fabric_objects_blob),
        )
        try:
            from pocketpaw_ee.cloud.fabric_proposals import executor as fabric_objects_executor

            await fabric_objects_executor.execute_approved_fabric_objects(
                approved, human_event_id=human_event_id
            )
        except Exception:
            logger.exception("fabric-objects execution after approval failed (non-fatal)")

    # When the approved Action carries a gated ``_pocket_create`` blob, create the
    # proposed starter Pocket via ``pockets.service.create`` (workspace-scoped,
    # owned by the blob's top-level ``user_id``). Same best-effort, lazy-import,
    # never-break-the-approve-response shape as the Fabric-objects hook above; the
    # executor records success / failure on the Action itself. A non-pocket-create
    # Action skips this.
    #
    # The Pocket create is part of its own Decision-Graph chain (the propose helper
    # minted the ``correlation_id`` + emitted ``agent.proposed``). Emit the
    # ``human.corrected`` event for the approval HERE (the router owns the
    # human-action emit on every approve path), citing the ``agent.proposed`` event
    # id (stored on the blob as ``proposed_event_id`` — the same field the
    # Fabric-objects path reads, so ``_code_change_proposed_event_id`` resolves it)
    # as causation, then thread its event id into the executor so the terminal
    # ``decision.completed`` chains back to the approval. The executor owns the
    # chain CLOSE (success or failure) — the router does NOT emit
    # ``decision.completed`` here, mirroring the Fabric-objects executor. No double
    # terminal.
    pocket_create_blob = _pocket_create_blob(approved)
    if pocket_create_blob is not None:
        disposition = "edited" if edited_fields else "accepted"
        note = correction.context_summary if correction is not None else None
        human_event_id = _emit_human_corrected(
            blob=pocket_create_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition=disposition,
            note=note,
            causation_override=_code_change_proposed_event_id(pocket_create_blob),
        )
        try:
            from pocketpaw_ee.cloud.pocket_proposals import executor as pocket_create_executor

            await pocket_create_executor.execute_approved_pocket_create(
                approved, human_event_id=human_event_id
            )
        except Exception:
            logger.exception("pocket-create execution after approval failed (non-fatal)")

    # MANDATES — when the approved Action carries a ``_belt_plan`` blob (the
    # mandate foreman's shift plan), dispatch the plan executor. Same shape as
    # the code-change hook: the router owns the ``human.corrected`` emit, the
    # EXECUTOR owns the chain close (success or failure) — the router never
    # emits ``decision.completed`` for belt_plan. Best-effort, lazy import,
    # never breaks the approve response.
    belt_plan_blob = _belt_plan_blob(approved)
    if belt_plan_blob is not None:
        disposition = "edited" if edited_fields else "accepted"
        note = correction.context_summary if correction is not None else None
        human_event_id = _emit_human_corrected(
            blob=belt_plan_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition=disposition,
            note=note,
            causation_override=_belt_plan_proposed_event_id(belt_plan_blob),
        )
        try:
            from pocketpaw_ee.cloud.mandates import executor as mandate_executor

            await mandate_executor.execute_approved_plan(approved, human_event_id=human_event_id)
        except Exception:
            logger.exception("belt_plan execution after approval failed (non-fatal)")

    # BP-3 — when the approved Action carries an ``_artifact_change`` blob, MERGE
    # the branch: promote the candidate version to published + (for pocket/site
    # scope) trigger the deploy. The executor owns the merge state transitions +
    # marks the Action executed/failed; the router owns the ``human.corrected``
    # emit (citing ``agent.proposed`` as causation, same as the code-change /
    # external-action paths — a merge proposal IS the chain origin). Same
    # best-effort, lazy-import, never-break-the-approve-response shape as the
    # hooks above. A non-artifact-change Action skips this.
    artifact_change_blob = _artifact_change_blob(approved)
    if artifact_change_blob is not None:
        disposition = "edited" if edited_fields else "accepted"
        note = correction.context_summary if correction is not None else None
        human_event_id = _emit_human_corrected(
            blob=artifact_change_blob,
            action=approved,
            user_id=approver_id,
            workspace_id=workspace_id,
            disposition=disposition,
            note=note,
            causation_override=_code_change_proposed_event_id(artifact_change_blob),
        )
        try:
            from pocketpaw_ee.versions import instinct_executor as artifact_executor

            await artifact_executor.execute_approved_change(approved, human_event_id=human_event_id)
        except Exception:
            logger.exception("artifact-change merge after approval failed (non-fatal)")

    # gap2 — when the approved Action carries a ``_customer_reply`` blob (a
    # paw-print customer event awaiting a decision), deliver the owner's reply
    # back to the customer surface. Same best-effort, lazy-import,
    # never-break-the-approve-response shape as the hooks above. The operator's
    # (possibly edited) recommendation is the wording the customer reads, so the
    # edit path above feeds straight into the delivery.
    try:
        from pocketpaw_ee.paw_print.decision_loop import (
            customer_reply_blob,
            deliver_customer_decision,
        )

        if customer_reply_blob(approved) is not None:
            await deliver_customer_decision(approved, declined=False)
    except Exception:
        logger.exception("customer-decision delivery after approval failed (non-fatal)")

    return ApproveResponse(action=approved, correction=correction)


async def _forward_to_soul(correction: Correction, action: Action, workspace_id: str) -> None:
    """Hand off to the soul bridge — always best-effort, never breaks approval.

    ISO-2: takes the caller's ``workspace_id`` so the bridge's InstinctStore is
    the tenant's own per-workspace file, not the shared one.
    """
    try:
        from pocketpaw.instinct.correction_soul_bridge import CorrectionSoulBridge
        from pocketpaw.soul import get_soul_manager

        manager = get_soul_manager()
        if manager is None:
            return
        bridge = CorrectionSoulBridge(soul_manager=manager, store=_store(workspace_id))
        await bridge.record(correction, action)
    except Exception:
        logger.exception("Correction soul-bridge failed (non-fatal)")


@router.post(
    "/instinct/actions/{action_id}/reject",
    response_model=Action,
    dependencies=[Depends(require_action_any_workspace("instinct.approve"))],
)
async def reject_action(
    action_id: str,
    req: RejectRequest | None = None,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Reject a pending action with an optional reason.

    Slice 3 (RFC 09) — endpoint signature grew ``current_user`` and
    ``current_workspace_id`` deps for two reasons:

      1. **Touch-time security fix** — ``require_action_any_workspace``
         only proves the caller holds ``instinct.approve`` SOMEWHERE; it
         does not bind the rejected Action to the caller's workspace.
         Without the workspace dep, ``_assert_pocket_write_workspace``
         could not run on the reject path — a workspace-A approver
         could reject a workspace-B parked write, the mirror of the
         BLOCKER 1 approval-escalation gap closed for approvals in PR
         #1183. Now the same 403 + ``instinct.cross_workspace_approval``
         error code fires on cross-tenant rejections.
      2. **Decision-Graph chain emits** — the rejection emits
         ``human.corrected(disposition=rejected)`` then closes the
         chain with ``decision.completed(passed=False, action_outcome=
         "rejected")``. The actor on both events is the authenticated
         user id (same forge-resistance as ``approve_action``'s
         SHOULD-FIX 1); the workspace + action's pocket form the
         scope. The bridge is NOT invoked on reject so the router owns
         the chain close.
    """
    store = _store(workspace_id)
    before = await store.get_action(action_id)
    if not before:
        raise HTTPException(404, "Action not found")

    # Touch-time security fix — same gate the approve path runs.
    _assert_pocket_write_workspace(before, workspace_id)
    _assert_code_change_workspace(before, workspace_id)
    _assert_external_action_workspace(before, workspace_id)
    # Same tenancy gate for a gated Fabric-objects Action on the REJECT side —
    # asymmetric tenant scope is no tenant scope: a cross-workspace reject must
    # 403 before any mutation, exactly like the approve side.
    _assert_fabric_objects_workspace(before, workspace_id)
    # Same tenancy gate for a gated Pocket-create Action on the REJECT side —
    # asymmetric tenant scope is no tenant scope: a cross-workspace reject must
    # 403 before any mutation, exactly like the approve side.
    _assert_pocket_create_workspace(before, workspace_id)
    # Same tenancy gate for a mandate shift-plan Action (belt_plan) — its
    # ``_belt_plan`` blob carries the workspace.
    _assert_belt_plan_workspace(before, workspace_id)
    # BP-3 — same tenancy gate for an artifact-change merge on the REJECT side.
    # Asymmetric tenant scope is no tenant scope: a cross-workspace reject (which
    # would discard another tenant's candidate) must 403 before any mutation,
    # exactly like the approve side (pocketpaw#1183 / #1250).
    _assert_artifact_change_workspace(before, workspace_id)

    reason = req.reason if req else ""
    rejector_id = str(user.id)
    action = await store.reject(action_id, reason=reason, rejector=rejector_id)
    if not action:
        raise HTTPException(404, "Action not found")

    # RFC 09 Slice 3 — emit ``human.corrected`` then ``decision.completed``
    # to close the chain. Order matters for the narrator: the human
    # action lands before the chain terminal, mirroring the approve
    # path's "human.corrected → execute → decision.completed" ordering.
    rejected_blob = _pocket_write_blob(action)
    if rejected_blob is not None:
        _emit_human_corrected(
            blob=rejected_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=reason or None,
        )
        _emit_decision_completed_rejected(
            blob=rejected_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=reason,
        )

    # BS-4 — a rejected Belt ``_code_change`` Action closes its chain HERE
    # (the executor never runs on reject). ``human.corrected(rejected)`` cites
    # the ``agent.proposed`` event as causation; ``decision.completed(rejected,
    # outcome=reason+comment)`` cites the human event so the chain reads
    # ``agent.proposed → human.corrected → decision.completed`` cleanly. The
    # reason text rides as the rejection comment on the terminal payload.
    code_change_blob = _code_change_blob(action)
    if code_change_blob is not None:
        human_event_id = _emit_human_corrected(
            blob=code_change_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=reason or None,
            causation_override=_code_change_proposed_event_id(code_change_blob),
        )
        _emit_decision_completed_rejected(
            blob=code_change_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=reason,
            causation_override=human_event_id,
        )
        # SC-2 — publish ``belt_run_updated`` (status=rejected, stage=done) on
        # the workspace bus so the /belt page reflects the rejection live. No
        # executor runs on reject, so this is the terminal event for a rejected
        # run.
        await _emit_belt_run_updated_safe(
            workspace_id=workspace_id, action_id=str(action.id), status="rejected", stage="done"
        )

    # A rejected gated ``_external_action`` Action closes its chain HERE (the
    # executor never runs on reject — the router owns the close, mirroring the
    # code-change reject path). ``human.corrected(rejected)`` cites the
    # ``agent.proposed`` event (off the blob's ``proposed_event_id``);
    # ``decision.completed(rejected)`` cites the human event so the chain reads
    # ``agent.proposed → human.corrected → decision.completed``.
    external_action_blob = _external_action_blob(action)
    if external_action_blob is not None:
        human_event_id = _emit_human_corrected(
            blob=external_action_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=reason or None,
            causation_override=_code_change_proposed_event_id(external_action_blob),
        )
        _emit_decision_completed_rejected(
            blob=external_action_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=reason,
            causation_override=human_event_id,
        )

    # A rejected gated ``_fabric_objects`` Action closes its chain HERE (the
    # executor never runs on reject — the router owns the close, mirroring the
    # external-action reject path). NO Fabric write happens.
    # ``human.corrected(rejected)`` cites the ``agent.proposed`` event (off the
    # blob's ``proposed_event_id``); ``decision.completed(rejected)`` cites the
    # human event so the chain reads ``agent.proposed → human.corrected →
    # decision.completed``.
    fabric_objects_blob = _fabric_objects_blob(action)
    if fabric_objects_blob is not None:
        human_event_id = _emit_human_corrected(
            blob=fabric_objects_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=reason or None,
            causation_override=_code_change_proposed_event_id(fabric_objects_blob),
        )
        _emit_decision_completed_rejected(
            blob=fabric_objects_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=reason,
            causation_override=human_event_id,
        )

    # A rejected gated ``_pocket_create`` Action closes its chain HERE (the
    # executor never runs on reject — the router owns the close, mirroring the
    # Fabric-objects reject path). NO Pocket is created.
    # ``human.corrected(rejected)`` cites the ``agent.proposed`` event (off the
    # blob's ``proposed_event_id``); ``decision.completed(rejected)`` cites the
    # human event so the chain reads ``agent.proposed → human.corrected →
    # decision.completed``.
    pocket_create_blob = _pocket_create_blob(action)
    if pocket_create_blob is not None:
        human_event_id = _emit_human_corrected(
            blob=pocket_create_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=reason or None,
            causation_override=_code_change_proposed_event_id(pocket_create_blob),
        )
        _emit_decision_completed_rejected(
            blob=pocket_create_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=reason,
            causation_override=human_event_id,
        )

    # MANDATES — a rejected ``_belt_plan`` Action closes its chain HERE (the
    # plan executor never runs on reject), mirroring the code-change shape:
    # ``human.corrected(rejected)`` cites ``agent.proposed``; the terminal
    # cites the human event. The shift record is updated best-effort.
    belt_plan_blob = _belt_plan_blob(action)
    if belt_plan_blob is not None:
        human_event_id = _emit_human_corrected(
            blob=belt_plan_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=reason or None,
            causation_override=_belt_plan_proposed_event_id(belt_plan_blob),
        )
        _emit_decision_completed_rejected(
            blob=belt_plan_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=reason,
            causation_override=human_event_id,
        )
        await _mark_plan_rejected_safe(action, reason)

    # BP-3 — a rejected ``_artifact_change`` Action DISCARDS its candidate (flips
    # the candidate version to reverted) and closes its chain HERE (the merge
    # executor never runs on reject — the router owns the close, mirroring the
    # code-change reject path). The PUBLISHED pointer is left untouched: a
    # rejection must never move what is live. ``human.corrected(rejected)`` cites
    # ``agent.proposed``; the terminal cites the human event. The discard is a
    # best-effort store nudge.
    artifact_change_blob = _artifact_change_blob(action)
    if artifact_change_blob is not None:
        human_event_id = _emit_human_corrected(
            blob=artifact_change_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            disposition="rejected",
            note=reason or None,
            causation_override=_code_change_proposed_event_id(artifact_change_blob),
        )
        _emit_decision_completed_rejected(
            blob=artifact_change_blob,
            action=action,
            user_id=rejector_id,
            workspace_id=workspace_id,
            reason=reason,
            causation_override=human_event_id,
        )
        try:
            from pocketpaw_ee.versions import instinct_executor as artifact_executor

            await artifact_executor.discard_rejected_change(action)
        except Exception:
            logger.exception("artifact-change discard after rejection failed (non-fatal)")

    # gap2 — a rejected ``_customer_reply`` Action delivers a DECLINED decision
    # (carrying the rejection reason) back to the customer surface. Same
    # best-effort, lazy-import shape as the approve hook. The loop closes either
    # way: the customer always gets an answer, approve or reject.
    try:
        from pocketpaw_ee.paw_print.decision_loop import (
            customer_reply_blob,
            deliver_customer_decision,
        )

        if customer_reply_blob(action) is not None:
            await deliver_customer_decision(action, declined=True)
    except Exception:
        logger.exception("customer-decision delivery after rejection failed (non-fatal)")

    return action


def _apply_edits(before: Action, req: ApproveRequest) -> tuple[Action, set[str]]:
    """Return a copy of `before` with any non-null fields from `req` applied.

    Also returns the set of field names that were actually changed so the
    caller can decide whether to persist them back to the store.
    """
    edited: set[str] = set()
    update: dict[str, Any] = {}
    for field in ("title", "description", "recommendation", "category", "priority"):
        incoming = getattr(req, field)
        if incoming is not None and incoming != getattr(before, field):
            update[field] = incoming
            edited.add(field)
    if req.parameters is not None and req.parameters != before.parameters:
        update["parameters"] = req.parameters
        edited.add("parameters")
    return before.model_copy(update=update), edited


async def _persist_edits(store: Any, action: Action, edited: set[str]) -> None:
    """Persist the human edits back to the store before the approve update.

    Approval itself touches `status` and `approved_*` so we only write the
    content fields that actually changed — no redundant updates.
    """
    import aiosqlite

    assignments: list[str] = []
    params: list[Any] = []
    if "title" in edited:
        assignments.append("title = ?")
        params.append(action.title)
    if "description" in edited:
        assignments.append("description = ?")
        params.append(action.description)
    if "recommendation" in edited:
        assignments.append("recommendation = ?")
        params.append(action.recommendation)
    if "category" in edited:
        assignments.append("category = ?")
        params.append(action.category.value)
    if "priority" in edited:
        assignments.append("priority = ?")
        params.append(action.priority.value)
    if "parameters" in edited:
        import json as _json

        assignments.append("parameters = ?")
        params.append(_json.dumps(action.parameters))

    if not assignments:
        return

    assignments.append("updated_at = datetime('now')")
    params.append(action.id)
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            f"UPDATE instinct_actions SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Correction endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/instinct/corrections",
    response_model=CorrectionsListResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.read"))],
)
async def list_corrections(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    action_id: str | None = Query(None, description="Filter by action ID"),
    limit: int = Query(100, ge=1, le=500),
    workspace_id: str = Depends(current_workspace_id),
):
    """List corrections captured when humans edited proposed actions.

    ISO-2: corrections live in the tenant's own ``instinct.db``, so resolving the
    store by ``workspace_id`` keeps one tenant's corrections out of another's.
    """
    store = _store(workspace_id)
    if action_id:
        corrections = await store.get_corrections_for_action(action_id)
    elif pocket_id:
        corrections = await store.get_corrections_for_pocket(pocket_id, limit=limit)
    else:
        raise HTTPException(400, "Provide pocket_id or action_id")
    return CorrectionsListResponse(corrections=corrections, total=len(corrections))


# ---------------------------------------------------------------------------
# Audit endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/instinct/audit",
    response_model=AuditListResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.audit"))],
)
async def query_audit(
    response: Response,
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    category: str | None = Query(
        None, description="Filter by category: decision|data|config|security"
    ),
    event: str | None = Query(None, description="Filter by event type"),
    actor: str | None = Query(
        None,
        description=(
            "Filter by fully-qualified actor string (e.g. ``agent:abc123`` "
            "or ``user:maya``). Exact match — added 2026-04-19 for the "
            "AgentReasoningTab's per-agent reasoning-trace view."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000, description="Max entries to return"),
    workspace_id: str = Depends(current_workspace_id),
):
    """Query instinct audit log entries with optional filters.

    W4a — scoped to the caller's active workspace (plus legacy NULL-workspace
    rows) so a tenant's auditor only reads that tenant's decision trail. The
    scope is a READ FILTER on which rows come back; ``/instinct/audit/verify``
    stays global because chain integrity is a property of the whole ledger.

    DEPRECATED: Cluster C / PR4 made ``/api/v1/runtime/audit`` the canonical
    audit surface with workspace rollup + FTS. This endpoint stays as the
    decision-trace fetch path (it carries instinct-specific fields that
    haven't been merged into the unified view yet) but new callers should
    prefer /runtime/audit for basic queries. We emit Deprecation + Link
    headers for discoverability.
    """
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</api/v1/runtime/audit>; rel="successor-version"'
    entries = await _store(workspace_id).query_audit(
        pocket_id=pocket_id,
        category=category,
        event=event,
        actor=actor,
        limit=limit,
        workspace_id=workspace_id,
    )
    return AuditListResponse(entries=entries, total=len(entries))


class HydratedAuditEntry(BaseModel):
    """Audit entry with referenced IDs expanded for the Why? drawer."""

    entry: AuditEntry
    reasoning_trace: ReasoningTrace | None = None
    fabric_snapshots: list[FabricObjectSnapshot] = Field(default_factory=list)
    fabric_current: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Live Fabric objects referenced in the trace (current state).",
    )


class AuditChainBreak(BaseModel):
    """The first row where the audit hash chain failed to verify."""

    id: str = Field(description="Audit entry id of the first broken row.")
    rowid: int = Field(description="SQLite rowid (insertion order) of the broken row.")
    reason: str = Field(description="Why the row failed: content edit vs. prev-link break.")


class AuditVerifyResponse(BaseModel):
    """Result of walking the tamper-evident audit hash chain.

    ``intact`` is the headline an auditor/insurer reads. ``broken_at`` is
    present (non-null) only when ``intact`` is False, pointing at the first
    row that fails to verify. ``legacy_unhashed`` counts pre-W2b rows that
    predate the chain and are not enforced.
    """

    intact: bool
    total: int
    hashed: int
    legacy_unhashed: int
    checked: int
    broken_at: AuditChainBreak | None = None


# /instinct/audit/export and /instinct/audit/verify must be declared BEFORE the
# parameterised /instinct/audit/{audit_id} below — FastAPI routes match in
# registration order, and a literal-vs-parameter collision would otherwise
# route /audit/export (or /audit/verify) to the {audit_id} handler and 404.
@router.get(
    "/instinct/audit/export",
    dependencies=[Depends(require_action_any_workspace("instinct.audit"))],
)
async def export_audit(
    pocket_id: str | None = Query(None, description="Filter by pocket ID"),
    workspace_id: str = Depends(current_workspace_id),
):
    """Export the instinct audit log as JSON for compliance.

    W4a — the exported BODY is scoped to the caller's active workspace (plus
    legacy NULL-workspace rows) so a tenant's compliance export never carries
    another tenant's decision trail. The ``X-Audit-Chain-Intact`` header still
    reflects the WHOLE ledger's integrity (the chain is global by design), so a
    downstream consumer that only handles the file body still learns whether
    the ledger verified at export time. Call ``GET /instinct/audit/verify`` for
    the full break-point detail.
    """
    store = _store(workspace_id)
    data = await store.export_audit(pocket_id=pocket_id, workspace_id=workspace_id)
    verdict = await store.verify_audit_chain()
    return Response(
        content=data,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="instinct_audit.json"',
            "X-Audit-Chain-Intact": "true" if verdict["intact"] else "false",
        },
    )


@router.get(
    "/instinct/audit/verify",
    response_model=AuditVerifyResponse,
    dependencies=[Depends(require_action_any_workspace("instinct.audit"))],
)
async def verify_audit_chain(workspace_id: str = Depends(current_workspace_id)):
    """Verify the tamper-evident audit hash chain end to end.

    Walks this workspace's ``instinct_audit`` ledger in insertion order,
    recomputes each row's hash from its canonical content + the running previous
    hash, and reports whether the chain is intact. Any insertion, edit, or
    deletion of a hashed row surfaces as ``intact=false`` with ``broken_at``
    pointing at the first failing row — proof an auditor or insurer can run
    themselves.

    ISO-2: the chain is now PER WORKSPACE. Each tenant has its own
    ``instinct.db`` with its own genesis→…→head chain, so this endpoint verifies
    the CALLER'S chain (resolved via ``current_workspace_id``), not a global
    chain mixing tenants. That is the correct multi-tenant model — a tenant's
    auditor verifies only that tenant's ledger, and one tenant's tampering can
    never flip another tenant's verdict. The chain spans the whole (per-tenant)
    file rather than a single pocket, so there is no pocket filter. Pre-W2b rows
    without a hash are counted under ``legacy_unhashed`` and are not enforced —
    see the store docstring for the genesis/legacy boundary.
    """
    verdict = await _store(workspace_id).verify_audit_chain()
    return AuditVerifyResponse(**verdict)


@router.get(
    "/instinct/audit/{audit_id}",
    response_model=HydratedAuditEntry,
    dependencies=[Depends(require_action_any_workspace("instinct.audit"))],
)
async def get_audit_entry(
    audit_id: str,
    hydrate: int = Query(0, description="Pass 1 to expand referenced IDs"),
    workspace_id: str = Depends(current_workspace_id),
):
    """Fetch a single audit entry, optionally hydrated with referenced content.

    When `hydrate=1`, the response carries:
    - the decoded `reasoning_trace` (if stored)
    - `fabric_snapshots` — immutable snapshots captured at decision time
    - `fabric_current` — live state of the referenced objects (so a reviewer
      can compare what the agent saw against what the object is now)

    W4a — the lookup is scoped to the caller's active workspace, so requesting
    another tenant's audit id returns 404 (never leaking its existence or
    content) rather than the row.

    The lookup is a direct single-row fetch by id (``store.get_audit_entry``),
    so an entry older than the audit query page size is still retrievable —
    the prior path paged the most-recent rows and matched in Python, 404-ing on
    valid ids past that window for a tenant with a large ledger.
    """
    store = _store(workspace_id)
    entry = await store.get_audit_entry(audit_id, workspace_id=workspace_id)
    if entry is None:
        raise HTTPException(404, "Audit entry not found")

    trace = _decode_trace(entry)
    if not hydrate:
        return HydratedAuditEntry(entry=entry, reasoning_trace=trace)

    snapshots: list[FabricObjectSnapshot] = []
    current: list[dict[str, Any]] = []
    if trace is not None:
        snapshots = await store.get_snapshots_for_audit(audit_id)
        current = await _fetch_current_fabric(trace.fabric_queries, workspace_id)

    return HydratedAuditEntry(
        entry=entry,
        reasoning_trace=trace,
        fabric_snapshots=snapshots,
        fabric_current=current,
    )


def _decode_trace(entry: AuditEntry) -> ReasoningTrace | None:
    raw = (entry.context or {}).get("reasoning_trace")
    if not raw:
        return None
    try:
        return ReasoningTrace.model_validate(raw)
    except Exception:
        logger.debug("Failed to decode reasoning_trace on audit %s", entry.id)
        return None


async def _fetch_current_fabric(object_ids: list[str], workspace_id: str) -> list[dict[str, Any]]:
    """Look up live Fabric objects by ID, tolerating a missing ee module.

    ISO-2: takes the caller's ``workspace_id`` so the Fabric store is the
    tenant's own per-workspace file (ISO-1). Passing it is also REQUIRED on a
    cloud path — an unscoped Fabric-store fetch there now fails closed — and it
    keeps the W4a in-row filter on ``get_object``.
    """
    if not object_ids:
        return []
    try:
        from pocketpaw.stores import get_fabric_store

        fabric = get_fabric_store(workspace_id=workspace_id)
    except ImportError:
        return []

    results: list[dict[str, Any]] = []
    for oid in object_ids:
        try:
            obj = await fabric.get_object(oid, workspace_id=workspace_id)
        except Exception:
            obj = None
        if obj is None:
            continue
        results.append(
            {
                "object_id": oid,
                "type_name": getattr(obj, "type_name", ""),
                "properties": getattr(obj, "properties", {}),
            },
        )
    return results
