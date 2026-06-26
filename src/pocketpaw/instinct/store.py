# Instinct store — async SQLite operations for the decision pipeline.
# Created: 2026-03-28 — Action lifecycle + audit log.
# Updated: 2026-06-26 (ISO-2 — physical per-workspace isolation) — added
#   aclose(): a best-effort WAL-checkpoint + state reset the workspace-keyed
#   store factory (src/pocketpaw/stores.py) runs when it evicts a per-workspace
#   InstinctStore from its bounded LRU. The store still holds no long-lived
#   connection; aclose exists only so an idle tenant's write-ahead-log sidecar
#   gets truncated instead of growing across 128+ cached tenants. ISO-2 gives
#   each workspace its OWN instinct.db (~/.pocketpaw/workspaces/<id>/instinct.db),
#   so the W2b audit hash-chain below is now PER-FILE: each tenant's chain has its
#   own genesis→…→head and ``verify_audit_chain`` runs PER WORKSPACE (a tenant's
#   auditor verifies only that tenant's chain — the correct multi-tenant model).
#   The W4a in-row ``workspace_id`` read-filter is UNCHANGED — physical file
#   isolation is ADDITIVE defense-in-depth layered on top of it. The store class
#   itself is workspace-agnostic; isolation is entirely in which file the factory
#   hands it.
# Updated: 2026-06-18 (feat/branch-primitive-instinct-gate, BP-3) — ADDITIVE
#   generic scope on the actions table. ``instinct_actions`` now carries a
#   nullable ``scope_type`` column (additive ALTER, mirrors the assignee /
#   workspace_id migrations). ``propose`` accepts an optional ``scope_type`` and
#   stamps it; ``_row_to_action`` reads it back. The per-pocket READS
#   (``_query_actions`` → ``list_actions`` / ``for_pocket`` / ``pending`` and
#   ``pending_count``) are now SCOPE-AWARE: a caller that passes ``scope_type``
#   filters on ``(scope_type, scope_id)`` — the ``scope_id`` reuses the
#   ``pocket_id`` argument/column — while a caller that omits it keeps the legacy
#   pocket_id path EXACTLY. Legacy rows (scope_type NULL) are unaffected by a
#   non-scoped read and still match the plain pocket_id filter. ``scope_type`` is
#   a READ FILTER + a write column only — it is NOT folded into the W2b audit
#   hash (the chain stays content-bound + global).
# Updated: 2026-06-16 (feat/instinct-smart-triage) — added the additive
#   ``auto_approve(action_id, *, verdict, reasoning, confidence=None)`` method
#   for the EE smart-approval auto-triage path (``instinct.auto_triage``). It is
#   a sibling of ``approve``: same atomic ``_update_status`` chokepoint, same
#   hash-chained W2b audit append, but it writes ``event="action_auto_approved"``
#   with ``actor="system:triager"`` and packs the triager's verdict + reasoning
#   into the audit ``context`` so a machine approval is as auditable as a human
#   one. Purely additive — no existing method's behaviour changed.
# Updated: 2026-06-10 (sov/r2a review fixes) — two store-level fixes:
#   FIX 1 — added ``get_audit_entry(audit_id, workspace_id=None)``: a direct
#     single-row SELECT by id with the same ``workspace_id = ? OR workspace_id
#     IS NULL`` scope as ``query_audit``. The EE router previously paged the
#     most-recent 1000 audit rows and matched the id in Python, so a tenant with
#     >1000 audit rows got a 404 on a valid OLDER id. The single-row lookup
#     removes the window. Cross-workspace ids return None under a scoped read.
#   FIX 2 — ``_update_status`` is now ATOMIC with its audit write. Previously the
#     status UPDATE committed, THEN ``_log`` ran on a SECOND connection and could
#     raise ``AuditChainError`` — leaving the action flipped (approved / rejected
#     / executed) with NO audit row. The UPDATE + the audit-row read-head + insert
#     now share ONE connection inside a single transaction (explicit BEGIN, one
#     commit), so either both land or neither does. The per-instance
#     ``self._log_lock`` (REVIEW-1) is still held across the chain read-head +
#     insert. The append is factored into ``_append_audit_locked`` (used by both
#     the standalone ``_log`` and the in-transaction ``_update_status`` path) so
#     the W2b canonical hash and chain linkage are byte-for-byte identical on
#     both paths — workspace_id stays OUT of the hash.
# Updated: 2026-06-10 (W4a — workspace-scope instinct reads) — closes a
#   cross-tenant decision leak on shared deployments. ``instinct_actions`` and
#   ``instinct_audit`` now carry a ``workspace_id`` column. Writes (``propose``,
#   ``_log``) stamp the caller's workspace; the per-tenant READS — ``pending``,
#   ``pending_count``, ``_query_actions`` (so ``list_actions`` / ``for_pocket``
#   inherit it) and ``query_audit`` — take an optional ``workspace_id`` and,
#   when supplied, restrict rows to ``workspace_id = ? OR workspace_id IS NULL``
#   (legacy/global rows predating tenancy stay visible; a None argument leaves
#   the read unscoped for OSS callers). ``workspace_id`` crosses from the EE
#   router as a PLAIN str — the OSS store never imports pocketpaw_ee. Additive
#   ALTER migration mirrors the assignee / hash-chain ones below.
#   AUDIT-CHAIN INVARIANT (do not break): ``workspace_id`` is deliberately NOT
#   part of ``_canonical_audit_payload`` and NOT folded into the hash. The
#   tamper-evident chain (W2b) is GLOBAL per store by design — it spans the
#   whole ledger so linkage and ``verify_audit_chain`` are byte-for-byte
#   unchanged. Tenancy here is purely a READ FILTER on which rows a tenant sees;
#   it does not touch the genesis/prev/entry hashes or the chain walk.
# Updated: 2026-06-10 (sov/w2-instinct — tamper-evident audit) — W2b: the
#   ``instinct_audit`` ledger is now a hash chain. Each row carries
#   ``entry_hash`` = sha256 over the row's canonical content + the previous
#   row's ``entry_hash`` (``prev_hash``). Any insertion, edit, or deletion
#   breaks the chain from that point forward, giving an auditor/insurer
#   verifiable integrity. The canonical serialization lives in
#   ``_canonical_audit_payload`` + ``compute_audit_hash`` and mirrors the
#   EE Decision-Graph ``compute_hash_link`` approach (sha256 of stable,
#   sorted, content-bound fields) so both ledgers hash consistently. The
#   append is a LOUD chokepoint: if the chain write fails, ``_log`` raises
#   ``AuditChainError`` — a decision that cannot be audited must not silently
#   succeed (this is the legal audit trail, distinct from the best-effort
#   Decision-Graph emits in the router). ``verify_audit_chain()`` walks the
#   ledger and reports intact / first-broken row. Legacy boundary: rows
#   written before this change have NULL ``entry_hash``; they are treated as
#   un-chained "legacy" rows. The genesis of the live chain is the first
#   hashed row (``prev_hash=""``); verification skips/repos legacy rows and
#   only enforces the chain over hashed rows. Additive ALTER migration, no
#   data rewrite.
# Updated: 2026-05-21 (feat/instinct-outcome-verification) — issue #1162:
#   mark_executed() now accepts a structured OutcomeVerdict as well as a
#   plain string. A verdict is stored as JSON in the existing ``outcome``
#   TEXT column; _row_to_action() detects JSON-encoded verdicts on read and
#   rebuilds them, falling back to a plain string for legacy free-text rows.
#   No schema migration — the column type is unchanged.
# Updated: 2026-03-30 — Added limit param to _query_actions, list_actions() public method.
# Updated: 2026-04-12 (Move 1 PR-A) — Corrections table + record_correction() and
#   get_corrections*() methods for the correction loop. Human edits between
#   proposal and approval land here, then feed soul-protocol on next proposal.
# Updated: 2026-04-13 (Move 2 PR-A/B) — instinct_fabric_snapshots table +
#   record_fabric_snapshot/get_snapshots_*. propose() now accepts optional
#   reasoning_trace and fabric_snapshots, persisting the trace as JSON inside
#   AuditEntry.context["reasoning_trace"] and keying snapshots to the audit row.
# Updated: 2026-05-13 (feat/mission-control-facade) — added optional ``assignee``
#   column on ``instinct_actions`` (additive migration; pre-existing rows have
#   NULL). ``propose()`` and ``pending()`` now accept an ``assignee`` argument so
#   The Tray in Mission Control can filter to the items awaiting a specific
#   human. ``bulk_approve()`` / ``bulk_reject()`` write N audit rows sharing a
#   single ``bulk_id`` UUID in ``context['bulk_id']`` so the operator can replay
#   per-item or query by the bulk transaction.

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from pocketpaw.instinct.correction import Correction, CorrectionPatch
from pocketpaw.instinct.models import (
    Action,
    ActionCategory,
    ActionContext,
    ActionPriority,
    ActionStatus,
    ActionTrigger,
    AuditCategory,
    AuditEntry,
    OutcomeVerdict,
)
from pocketpaw.instinct.trace import FabricObjectSnapshot, ReasoningTrace

logger = logging.getLogger(__name__)


def _serialize_outcome(outcome: str | OutcomeVerdict | None) -> str | None:
    """Serialize an Action outcome for the ``outcome`` TEXT column.

    A structured :class:`OutcomeVerdict` is stored as its JSON dump; a plain
    string is stored as-is (the legacy free-text form). ``None`` stays
    ``None``. The matching read-side decode lives in
    :meth:`InstinctStore._deserialize_outcome`.
    """
    if outcome is None:
        return None
    if isinstance(outcome, OutcomeVerdict):
        return outcome.model_dump_json()
    return outcome


def _parse_iso(value: Any) -> datetime | None:
    """Permissive ISO-8601 parse used by the row mappers.

    SQLite's ``datetime('now')`` default returns a space-separated
    ``YYYY-MM-DD HH:MM:SS`` string while application-side writes use
    ``datetime.isoformat()`` (T-separated). Accept both, return ``None``
    on anything we can't parse so a malformed row doesn't crash a read.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace(" ", "T", 1))
        except ValueError:
            return None
    return None


class AuditChainError(RuntimeError):
    """Raised when the tamper-evident audit ledger cannot be appended to.

    The Instinct audit log is the legal trail a regulated customer hands to
    an auditor or insurer. A decision that cannot be recorded in that trail
    must NOT silently succeed — so a failure to compute or persist the next
    hash-chain link is surfaced loudly rather than swallowed. This is
    deliberately distinct from the Decision-Graph chain emits in the router,
    which are best-effort (the journal is their source of truth, not this
    ledger).
    """


def _canonical_audit_payload(
    *,
    id: str,
    action_id: str | None,
    pocket_id: str | None,
    timestamp: str,
    actor: str,
    event: str,
    category: str,
    description: str,
    context: dict[str, Any] | None,
    ai_recommendation: str | None,
    outcome: str | None,
) -> str:
    """Stable canonical serialization of an audit row's content.

    Determinism is the whole game: the same logical row must serialize
    byte-for-byte identically on write and on later re-verification, or an
    honest ledger would read as tampered. We achieve that with
    ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` over an
    explicit, ordered field set. ``context`` is itself dumped with sorted
    keys so dict-ordering never perturbs the hash. ``prev_hash`` is folded
    in by :func:`compute_audit_hash`, not here, so this payload describes
    only the row's own content.
    """
    return json.dumps(
        {
            "id": id,
            "action_id": action_id,
            "pocket_id": pocket_id,
            "timestamp": timestamp,
            "actor": actor,
            "event": event,
            "category": category,
            "description": description,
            # Re-serialize context canonically so key ordering is irrelevant.
            "context": json.dumps(context or {}, sort_keys=True, separators=(",", ":")),
            "ai_recommendation": ai_recommendation,
            "outcome": outcome,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def compute_audit_hash(canonical_payload: str, prev_hash: str) -> str:
    """Compute one audit-ledger hash link.

    ``sha256(canonical_payload || prev_hash)`` — modeled on the EE
    Decision-Graph ``compute_hash_link`` (``ee.cloud.decisions.domain``):
    hash the row's stable content, then fold in the previous row's hash so
    tampering with any one row invalidates every row after it. The genesis
    row passes ``prev_hash=""`` (nothing folded in). We cannot import the EE
    helper here — the OSS core must not depend on ``pocketpaw_ee`` — so the
    composition is reproduced rather than reused.
    """
    h = hashlib.sha256()
    h.update(canonical_payload.encode("utf-8"))
    if prev_hash:
        h.update(prev_hash.encode("utf-8"))
    return h.hexdigest()


def _workspace_scope(workspace_id: str | None) -> tuple[str | None, list[Any]]:
    """Build the tenancy WHERE fragment + bound params for a scoped read (W4a).

    Returns ``(condition, params)``:

    - ``workspace_id is None`` -> ``(None, [])`` — no scoping. OSS / agent-tool
      callers that don't carry a workspace see everything, exactly as before
      W4a.
    - a concrete workspace -> ``("(workspace_id = ? OR workspace_id IS NULL)",
      [workspace_id])`` — the caller's own rows PLUS legacy/global
      NULL-workspace rows that predate tenancy (see the module header). The
      value is always a bound parameter; the column name is a fixed literal,
      never user input.

    This is a READ FILTER only. It is never folded into the W2b audit hash —
    the global chain linkage and ``verify_audit_chain`` are untouched.
    """
    if workspace_id is None:
        return None, []
    return "(workspace_id = ? OR workspace_id IS NULL)", [workspace_id]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS instinct_actions (
    id TEXT PRIMARY KEY,
    pocket_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'workflow',
    status TEXT DEFAULT 'pending',
    priority TEXT DEFAULT 'medium',
    trigger TEXT NOT NULL,
    recommendation TEXT DEFAULT '',
    parameters TEXT DEFAULT '{}',
    context TEXT DEFAULT '{}',
    outcome TEXT,
    error TEXT,
    approved_by TEXT,
    approved_at TEXT,
    rejected_reason TEXT,
    assignee TEXT,
    -- Generic scope (BP-3): the artifact scope_type this Action belongs to
    -- ("pocket" / "site" / "dashboard" / …). NULL = legacy pocket-scoped row;
    -- a non-scoped read still matches it via the plain pocket_id filter. When
    -- set, the read filters on (scope_type, scope_id) with scope_id == pocket_id.
    scope_type TEXT,
    -- Tenancy (W4a): the owning workspace. NULL = legacy/global row written
    -- before tenancy or by a non-cloud OSS caller; a scoped read still sees it.
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    executed_at TEXT
);

CREATE TABLE IF NOT EXISTS instinct_audit (
    id TEXT PRIMARY KEY,
    action_id TEXT,
    pocket_id TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    category TEXT DEFAULT 'decision',
    description TEXT NOT NULL,
    context TEXT DEFAULT '{}',
    ai_recommendation TEXT,
    outcome TEXT,
    -- Tamper-evidence (W2b): entry_hash = sha256 over this row's canonical
    -- content + prev_hash. Nullable so a fresh schema is created with the
    -- columns and a pre-existing ledger ALTERs in (legacy rows stay NULL).
    prev_hash TEXT,
    entry_hash TEXT,
    -- Tenancy (W4a): owning workspace. This is a READ-FILTER column only — it
    -- is intentionally NOT part of the canonical hash payload, so the global
    -- W2b chain linkage and verify are unaffected. NULL = legacy/global.
    workspace_id TEXT
);

CREATE TABLE IF NOT EXISTS instinct_corrections (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    pocket_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    patches TEXT NOT NULL,
    context_summary TEXT NOT NULL,
    action_title TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instinct_fabric_snapshots (
    id TEXT PRIMARY KEY,
    object_id TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    object_type TEXT DEFAULT '',
    snapshot TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_actions_pocket ON instinct_actions(pocket_id);
CREATE INDEX IF NOT EXISTS idx_actions_status ON instinct_actions(status);
CREATE INDEX IF NOT EXISTS idx_audit_pocket ON instinct_audit(pocket_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON instinct_audit(timestamp);
CREATE INDEX IF NOT EXISTS idx_corrections_pocket ON instinct_corrections(pocket_id);
CREATE INDEX IF NOT EXISTS idx_corrections_action ON instinct_corrections(action_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_audit ON instinct_fabric_snapshots(audit_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_object ON instinct_fabric_snapshots(object_id);
"""

# Tenancy indexes are created AFTER the ALTER migration (see _ensure_schema),
# NOT in SCHEMA_SQL above — on a pre-W4a DB the workspace_id column is added by
# ALTER, and a CREATE INDEX on it inside the same executescript would run first
# and fail with "no such column". (Bug found by live smoke 2026-06-10;
# see tests/cloud/test_w4a_migration.py.)
_WORKSPACE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_actions_workspace ON instinct_actions(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_workspace ON instinct_audit(workspace_id)",
)

# Generic-scope index (BP-3). Created AFTER the ALTER that adds scope_type for
# the same reason the workspace indexes are — on a pre-BP-3 DB the column does
# not exist until the ALTER lands. The compound (scope_type, pocket_id) index
# serves the scope-aware (scope_type, scope_id) read; scope_id reuses pocket_id.
_SCOPE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_actions_scope ON instinct_actions(scope_type, pocket_id)",
)


class InstinctStore:
    """Async SQLite store for the decision pipeline."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._initialized = False
        # Serializes the audit-chain read-head + insert in _log (REVIEW-1):
        # each _log opens its own connection, so without this lock two
        # concurrent _log calls could both read the same prev_hash before
        # either inserts, forking the chain and producing false-positive
        # tamper reports in verify_audit_chain. Per-instance (not module-level)
        # so separate tenants/stores don't contend.
        self._log_lock = asyncio.Lock()

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA_SQL)
            # Additive migration: ``assignee`` column landed in 2026-05-13 for
            # Mission Control's per-human filter. CREATE TABLE IF NOT EXISTS
            # won't add it to a pre-existing DB, so we ALTER and swallow the
            # duplicate-column error that fires on every subsequent boot.
            try:
                await db.execute("ALTER TABLE instinct_actions ADD COLUMN assignee TEXT")
            except aiosqlite.OperationalError:
                pass
            # Additive migration (W2b): tamper-evidence columns on a
            # pre-existing audit ledger. Same swallow-the-duplicate pattern.
            # Legacy rows keep NULL hashes — see ``verify_audit_chain`` for how
            # the legacy/genesis boundary is handled.
            for _col in ("prev_hash", "entry_hash"):
                try:
                    await db.execute(f"ALTER TABLE instinct_audit ADD COLUMN {_col} TEXT")
                except aiosqlite.OperationalError:
                    pass
            # Additive migration (W4a): tenancy column on both the actions and
            # the audit ledger of a pre-existing DB. Same swallow-the-duplicate
            # pattern as above. Pre-existing rows keep NULL workspace_id
            # (legacy/global; a scoped read still sees them — see the header).
            # The audit ALTER is a READ-FILTER column ONLY: workspace_id is not
            # part of the canonical hash payload, so the W2b chain is untouched.
            for _tbl in ("instinct_actions", "instinct_audit"):
                try:
                    await db.execute(f"ALTER TABLE {_tbl} ADD COLUMN workspace_id TEXT")
                except aiosqlite.OperationalError:
                    pass
            # Additive migration (BP-3): the generic ``scope_type`` column on a
            # pre-existing actions table. Same swallow-the-duplicate pattern.
            # Pre-existing rows keep NULL scope_type (legacy pocket-scoped; a
            # non-scoped read still matches them via the plain pocket_id filter).
            try:
                await db.execute("ALTER TABLE instinct_actions ADD COLUMN scope_type TEXT")
            except aiosqlite.OperationalError:
                pass
            # Tenancy indexes created only after the column is guaranteed to
            # exist — see _WORKSPACE_INDEX_SQL note above. Inside SCHEMA_SQL this
            # would fail on a pre-W4a DB. The scope index follows the same rule.
            for _idx in (*_WORKSPACE_INDEX_SQL, *_SCOPE_INDEX_SQL):
                await db.execute(_idx)
            await db.commit()
        self._initialized = True

    def _conn(self) -> aiosqlite.Connection:
        """Return a new connection context manager."""
        return aiosqlite.connect(self._db_path)

    async def aclose(self) -> None:
        """Release this store's on-disk resources (ISO-2).

        Like ``FabricStore``, ``InstinctStore`` holds NO long-lived connection —
        every method opens and closes its own ``aiosqlite.connect()`` per call —
        so there is no socket or cursor to close. What CAN accumulate is a
        write-ahead-log sidecar (``instinct.db-wal`` / ``-shm``). Under
        per-workspace physical isolation (ISO-2) the store factory caches up to
        128 per-workspace handles and evicts the least-recently-used; ``aclose``
        is what the factory runs on eviction so an idle tenant's WAL is truncated
        rather than left to grow, and the next ``_ensure_schema`` re-runs cleanly
        on the cold handle.

        Best-effort and idempotent: a checkpoint failure (DB never created, WAL
        not in use, file vanished) is swallowed — eviction must never raise.
        """
        self._initialized = False
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 — eviction cleanup is best-effort
            logger.debug("InstinctStore.aclose checkpoint skipped", exc_info=True)

    # --- Actions ---

    async def propose(
        self,
        pocket_id: str,
        title: str,
        description: str,
        recommendation: str,
        trigger: ActionTrigger,
        category: ActionCategory = ActionCategory.WORKFLOW,
        priority: ActionPriority = ActionPriority.MEDIUM,
        parameters: dict[str, Any] | None = None,
        context: ActionContext | None = None,
        reasoning_trace: ReasoningTrace | None = None,
        fabric_snapshots: list[FabricObjectSnapshot] | None = None,
        assignee: str | None = None,
        workspace_id: str | None = None,
        scope_type: str | None = None,
    ) -> Action:
        action = Action(
            scope_type=scope_type,
            pocket_id=pocket_id,
            title=title,
            description=description,
            recommendation=recommendation,
            trigger=trigger,
            category=category,
            priority=priority,
            parameters=parameters or {},
            context=context or ActionContext(),
            assignee=assignee,
        )
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO instinct_actions"
                " (id, pocket_id, title, description,"
                " category, status, priority, trigger,"
                " recommendation, parameters, context, assignee, workspace_id,"
                " scope_type)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action.id,
                    pocket_id,
                    title,
                    description,
                    action.category.value,
                    action.status.value,
                    action.priority.value,
                    action.trigger.model_dump_json(),
                    recommendation,
                    json.dumps(parameters or {}),
                    action.context.model_dump_json(),
                    assignee,
                    workspace_id,
                    scope_type,
                ),
            )
            await db.commit()

        audit_context: dict[str, Any] = {}
        if reasoning_trace is not None:
            audit_context["reasoning_trace"] = reasoning_trace.model_dump(mode="json")

        audit_entry = await self._log(
            action_id=action.id,
            pocket_id=pocket_id,
            actor=f"{trigger.type}:{trigger.source}",
            event="action_proposed",
            description=f"Proposed: {title}",
            ai_recommendation=recommendation,
            context=audit_context,
            workspace_id=workspace_id,
        )

        if fabric_snapshots:
            for snapshot in fabric_snapshots:
                snapshot.audit_id = audit_entry.id
                await self.record_fabric_snapshot(snapshot)

        return action

    async def approve(self, action_id: str, approver: str = "user") -> Action | None:
        return await self._update_status(
            action_id,
            ActionStatus.APPROVED,
            approved_by=approver,
            approved_at=datetime.now().isoformat(),
            event="action_approved",
            actor=approver,
        )

    async def auto_approve(
        self,
        action_id: str,
        *,
        approver: str = "system:triager",
        verdict: str,
        reasoning: str,
        confidence: float | None = None,
    ) -> Action | None:
        """Auto-approve an action on a triager verdict, fully audited.

        Additive sibling of :meth:`approve` for the smart-approval auto-triage
        path (EE ``instinct.auto_triage``). The only differences from a human
        approval are:

        * ``event="action_auto_approved"`` (distinct from ``action_approved``
          so the audit/ledger query surface can tell a machine approval from a
          human one), and
        * the triager's ``verdict`` + ``reasoning`` (+ optional ``confidence``)
          are packed into the audit ``context`` so an auto-approval carries the
          same "why" an auditor or insurer needs — every auto-approval is as
          auditable as a human one.

        The actor defaults to ``system:triager``. The write goes through the
        SAME ``_update_status`` chokepoint as ``approve`` / ``reject``, so the
        row flip and the hash-chained audit append land atomically and the
        tamper-evident chain (W2b) covers auto-approvals identically to human
        approvals. ``require_status=PENDING`` guards against double-flipping an
        action that a concurrent path already resolved.
        """
        audit_context: dict[str, Any] = {
            "triager_verdict": verdict,
            "triager_reasoning": reasoning,
        }
        if confidence is not None:
            audit_context["triager_confidence"] = confidence
        return await self._update_status(
            action_id,
            ActionStatus.APPROVED,
            approved_by=approver,
            approved_at=datetime.now().isoformat(),
            event="action_auto_approved",
            actor=approver,
            require_status=ActionStatus.PENDING,
            audit_context=audit_context,
        )

    async def reject(
        self, action_id: str, reason: str = "", rejector: str = "user"
    ) -> Action | None:
        return await self._update_status(
            action_id,
            ActionStatus.REJECTED,
            rejected_reason=reason,
            event="action_rejected",
            actor=rejector,
            extra_desc=f" — {reason}" if reason else "",
        )

    # ------------------------------------------------------------------
    # Bulk lifecycle
    # ------------------------------------------------------------------
    #
    # Mission Control's bulk action bar selects N items and POSTs them in one
    # request. We fan out per-item so the audit replay stays per-item, but
    # tag every audit row with a shared ``bulk_id`` UUID so the operator can
    # query the audit log for the bulk transaction as a unit.

    async def bulk_approve(
        self,
        action_ids: list[str],
        *,
        approver: str = "user",
        note: str | None = None,
        bulk_id: str | None = None,
    ) -> tuple[list[Action], list[str], str]:
        """Approve N pending actions with a shared ``bulk_id`` audit tag.

        Returns ``(approved, missing, bulk_id)``. ``missing`` carries the
        ids that didn't resolve (no matching row, or wrong status). The
        caller chooses how to surface partial failures.
        """
        from uuid import uuid4 as _uuid4

        bulk = bulk_id or _uuid4().hex
        approved: list[Action] = []
        missing: list[str] = []
        for action_id in action_ids:
            action = await self._update_status(
                action_id,
                ActionStatus.APPROVED,
                approved_by=approver,
                approved_at=datetime.now().isoformat(),
                event="action_approved",
                actor=approver,
                require_status=ActionStatus.PENDING,
                audit_context={"bulk_id": bulk, **({"note": note} if note else {})},
            )
            if action is None:
                missing.append(action_id)
            else:
                approved.append(action)
        return approved, missing, bulk

    async def bulk_reject(
        self,
        action_ids: list[str],
        *,
        reason: str,
        rejector: str = "user",
        bulk_id: str | None = None,
    ) -> tuple[list[Action], list[str], str]:
        """Reject N pending actions with a shared ``bulk_id`` audit tag.

        ``reason`` is required to mirror the bulk-reject UX gate (you have
        to type a reason in the action bar before the button enables).
        Returns ``(rejected, missing, bulk_id)`` with the same semantics
        as :meth:`bulk_approve`.
        """
        from uuid import uuid4 as _uuid4

        bulk = bulk_id or _uuid4().hex
        rejected: list[Action] = []
        missing: list[str] = []
        for action_id in action_ids:
            action = await self._update_status(
                action_id,
                ActionStatus.REJECTED,
                rejected_reason=reason,
                event="action_rejected",
                actor=rejector,
                extra_desc=f" — {reason}" if reason else "",
                require_status=ActionStatus.PENDING,
                audit_context={"bulk_id": bulk, "reason": reason},
            )
            if action is None:
                missing.append(action_id)
            else:
                rejected.append(action)
        return rejected, missing, bulk

    async def mark_executed(
        self, action_id: str, outcome: str | OutcomeVerdict | None = None
    ) -> Action | None:
        """Mark an approved action as executed and record its outcome.

        Args:
            action_id: The action to mark executed.
            outcome: What happened. Either a structured
                :class:`OutcomeVerdict` (a checked "did this solve the
                problem" verdict — see issue #1162) or a plain free-text
                string (the legacy form). Both persist to the same column.

        Returns:
            The updated Action, or ``None`` if no such action exists.
        """
        return await self._update_status(
            action_id,
            ActionStatus.EXECUTED,
            outcome=_serialize_outcome(outcome),
            executed_at=datetime.now().isoformat(),
            event="action_executed",
            actor="system",
        )

    async def mark_failed(self, action_id: str, error: str) -> Action | None:
        return await self._update_status(
            action_id,
            ActionStatus.FAILED,
            error=error,
            event="action_failed",
            actor="system",
            extra_desc=f" — {error}",
        )

    async def _update_status(
        self,
        action_id: str,
        status: ActionStatus,
        *,
        event: str,
        actor: str,
        extra_desc: str = "",
        audit_context: dict[str, Any] | None = None,
        require_status: ActionStatus | None = None,
        **fields: Any,
    ) -> Action | None:
        action = await self.get_action(action_id)
        if not action:
            return None
        # ``require_status`` lets bulk callers enforce "only act on rows
        # still in this state" without breaking the existing pending →
        # approved → executed flow used by mark_executed / mark_failed.
        if require_status is not None and action.status != require_status:
            return None

        sets = ["status = ?", "updated_at = datetime('now')"]
        params: list[Any] = [status.value]
        for k, v in fields.items():
            if v is not None:
                sets.append(f"{k} = ?")
                params.append(v)
        params.append(action_id)

        await self._ensure_schema()
        # FIX 2 — the status UPDATE and the lifecycle audit row must land
        # ATOMICALLY: previously the UPDATE committed on its own connection and a
        # SEPARATE ``_log`` call wrote the audit row afterward; if that audit
        # append raised ``AuditChainError`` the action was already flipped
        # (approved / rejected / executed) with NO audit row. We now run the
        # UPDATE + the audit read-head + insert inside ONE transaction on ONE
        # connection (explicit BEGIN, single commit), so either both persist or
        # neither does. The per-instance ``self._log_lock`` (REVIEW-1) is held
        # across the chain read-head + insert exactly as in ``_log``.
        entry = AuditEntry(
            action_id=action_id,
            pocket_id=action.pocket_id,
            actor=actor,
            event=event,
            category=AuditCategory.DECISION,
            description=f"{event.replace('_', ' ').title()}: {action.title}{extra_desc}",
            context=audit_context or {},
        )
        try:
            async with self._log_lock, self._conn() as db:
                await db.execute("BEGIN")
                await db.execute(
                    f"UPDATE instinct_actions SET {', '.join(sets)} WHERE id = ?", params
                )
                # W4a — read back the action's owning workspace so the lifecycle
                # audit row (approve / reject / execute / fail) is stamped with
                # the same tenant the action belongs to. Done with a tiny direct
                # read rather than widening the Action model.
                workspace_id: str | None = None
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT workspace_id FROM instinct_actions WHERE id = ?", (action_id,)
                ) as cur:
                    ws_row = await cur.fetchone()
                    if ws_row is not None and "workspace_id" in ws_row.keys():
                        workspace_id = ws_row["workspace_id"]
                # Same connection, same transaction — the audit append shares the
                # UPDATE's BEGIN, so a chain failure rolls the status flip back.
                await self._append_audit_locked(db, entry, workspace_id=workspace_id)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — re-raised loudly below
            # Mirror ``_log``'s loud posture: a lifecycle decision that cannot be
            # written into the tamper-evident ledger must NOT silently succeed.
            # The transaction has already rolled back (the UPDATE did not land),
            # so the action status is unchanged.
            raise AuditChainError(
                f"failed to append audit entry {entry.id} ({event}) to the "
                f"tamper-evident ledger: {exc}"
            ) from exc
        return await self.get_action(action_id)

    async def get_action(self, action_id: str) -> Action | None:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM instinct_actions WHERE id = ?", (action_id,)
            ) as cur:
                row = await cur.fetchone()
                return self._row_to_action(row) if row else None

    async def pending(
        self,
        pocket_id: str | None = None,
        assignee: str | None = None,
        workspace_id: str | None = None,
        scope_type: str | None = None,
    ) -> list[Action]:
        return await self._query_actions(
            status=ActionStatus.PENDING,
            pocket_id=pocket_id,
            assignee=assignee,
            workspace_id=workspace_id,
            scope_type=scope_type,
        )

    async def pending_count(
        self,
        pocket_id: str | None = None,
        workspace_id: str | None = None,
        scope_type: str | None = None,
    ) -> int:
        cond = "WHERE status = 'pending'"
        params: list[Any] = []
        # BP-3 — scope-aware count, mirroring ``_query_actions``: a scope_type
        # selects (scope_type, scope_id) with scope_id == pocket_id; omitting it
        # keeps the legacy pocket_id-only path so legacy rows still count.
        if scope_type is not None:
            cond += " AND scope_type = ?"
            params.append(scope_type)
            if pocket_id:
                cond += " AND pocket_id = ?"
                params.append(pocket_id)
        elif pocket_id:
            cond += " AND pocket_id = ?"
            params.append(pocket_id)
        # W4a — scope the count to the caller's workspace (plus legacy NULL rows)
        # so a tenant's pending badge never includes another tenant's items.
        ws_cond, ws_params = _workspace_scope(workspace_id)
        if ws_cond:
            cond += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            async with db.execute(f"SELECT COUNT(*) FROM instinct_actions {cond}", params) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def for_pocket(self, pocket_id: str, workspace_id: str | None = None) -> list[Action]:
        return await self._query_actions(pocket_id=pocket_id, workspace_id=workspace_id)

    async def list_actions(
        self,
        pocket_id: str | None = None,
        status: ActionStatus | None = None,
        limit: int = 50,
        workspace_id: str | None = None,
        scope_type: str | None = None,
    ) -> list[Action]:
        """Public method — list actions with optional filters and limit.

        BP-3 — ``scope_type`` makes the listing scope-aware: with it, the
        ``pocket_id`` filter is read as the generic scope id within
        ``scope_type``; without it, the legacy pocket_id-only path runs.
        """
        return await self._query_actions(
            status=status,
            pocket_id=pocket_id,
            limit=limit,
            workspace_id=workspace_id,
            scope_type=scope_type,
        )

    async def _query_actions(
        self,
        status: ActionStatus | None = None,
        pocket_id: str | None = None,
        limit: int = 500,
        assignee: str | None = None,
        workspace_id: str | None = None,
        scope_type: str | None = None,
    ) -> list[Action]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        # BP-3 — scope-aware artifact filter. When ``scope_type`` is given the
        # caller is selecting a GENERIC artifact: filter on (scope_type,
        # scope_id) where ``scope_id`` reuses the ``pocket_id`` column. When
        # ``scope_type`` is omitted the legacy pocket_id-only path runs EXACTLY
        # as before, so pre-BP-3 rows (scope_type NULL) keep matching unchanged.
        if scope_type is not None:
            conditions.append("scope_type = ?")
            params.append(scope_type)
            if pocket_id:
                conditions.append("pocket_id = ?")
                params.append(pocket_id)
        elif pocket_id:
            conditions.append("pocket_id = ?")
            params.append(pocket_id)
        if assignee:
            conditions.append("assignee = ?")
            params.append(assignee)
        # W4a — tenancy scope as an ADDITIONAL condition (caller's workspace plus
        # legacy NULL-workspace rows). ``None`` leaves the listing unscoped for
        # OSS callers.
        ws_cond, ws_params = _workspace_scope(workspace_id)
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM instinct_actions {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ) as cur:
                return [self._row_to_action(row) async for row in cur]

    # --- Audit Log ---

    async def _log(
        self,
        *,
        actor: str,
        event: str,
        description: str,
        action_id: str | None = None,
        pocket_id: str | None = None,
        category: AuditCategory = AuditCategory.DECISION,
        context: dict[str, Any] | None = None,
        ai_recommendation: str | None = None,
        outcome: str | None = None,
        workspace_id: str | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            action_id=action_id,
            pocket_id=pocket_id,
            actor=actor,
            event=event,
            category=category,
            description=description,
            context=context or {},
            ai_recommendation=ai_recommendation,
            outcome=outcome,
        )
        await self._ensure_schema()
        try:
            # Hold the per-instance lock across the read-head + insert so the
            # chain stays linear under concurrent _log calls (REVIEW-1). The
            # standalone audit write owns its own connection + commit.
            async with self._log_lock, self._conn() as db:
                await self._append_audit_locked(db, entry, workspace_id=workspace_id)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — re-raised loudly below
            # Failure posture (W2b): a decision that cannot be written into the
            # tamper-evident ledger must NOT silently succeed. Surface the
            # failure to the caller instead of swallowing it — the audit trail
            # is the governance guarantee. (Contrast: the router's
            # Decision-Graph emits are best-effort.)
            raise AuditChainError(
                f"failed to append audit entry {entry.id} ({event}) to the "
                f"tamper-evident ledger: {exc}"
            ) from exc
        return entry

    async def _append_audit_locked(
        self,
        db: aiosqlite.Connection,
        entry: AuditEntry,
        *,
        workspace_id: str | None,
    ) -> None:
        """Compute one chain link and INSERT the audit row on ``db``.

        Does the W2b read-head + hash + insert but NOT the commit — the caller
        owns transaction boundaries. Two callers share it:

        - :meth:`_log` — opens its own connection, commits immediately.
        - :meth:`_update_status` — runs this inside the SAME transaction as the
          status UPDATE, so the action flip and its audit row land atomically
          (FIX 2). A failure here propagates and rolls back the UPDATE.

        CONTRACT: the caller MUST already hold ``self._log_lock`` so the chain
        read-head + insert stays serialized (REVIEW-1). The W2b canonical hash
        is computed identically on both paths — ``workspace_id`` is appended as
        a plain column ONLY and is deliberately absent from the canonical
        payload and the hash (the chain is GLOBAL; tenancy is a read filter).
        """
        # The SQLite ``timestamp`` column defaults to ``datetime('now')`` and is
        # what a reader sees, but the hash must be computed over a value we
        # control deterministically. We stamp the timestamp ourselves (ISO
        # form, matching the application-side convention) so the canonical
        # payload on write equals the canonical payload on re-verification.
        timestamp = entry.timestamp.isoformat()
        context_json = json.dumps(entry.context)
        canonical = _canonical_audit_payload(
            id=entry.id,
            action_id=entry.action_id,
            pocket_id=entry.pocket_id,
            timestamp=timestamp,
            actor=entry.actor,
            event=entry.event,
            category=entry.category.value,
            description=entry.description,
            context=entry.context,
            ai_recommendation=entry.ai_recommendation,
            outcome=entry.outcome,
        )
        # ``prev_hash`` is the entry_hash of the most-recently inserted hashed
        # row. ``rowid`` is monotonic with insertion order, so it gives a stable
        # chain head even across timestamp ties. Legacy rows with a NULL
        # entry_hash are skipped — the live chain links only hashed rows (the
        # genesis link uses prev_hash="").
        async with db.execute(
            "SELECT entry_hash FROM instinct_audit"
            " WHERE entry_hash IS NOT NULL"
            " ORDER BY rowid DESC LIMIT 1"
        ) as cur:
            prev_row = await cur.fetchone()
        prev_hash = prev_row[0] if prev_row else ""
        entry_hash = compute_audit_hash(canonical, prev_hash)

        # ``workspace_id`` (W4a) is appended as a plain column ONLY. It is
        # deliberately absent from ``canonical`` above and from
        # ``compute_audit_hash`` — the W2b chain is GLOBAL and must hash
        # identically on re-verification regardless of tenancy. Tenancy is a
        # read filter, not chain content.
        await db.execute(
            "INSERT INTO instinct_audit"
            " (id, action_id, pocket_id, timestamp, actor, event,"
            " category, description, context,"
            " ai_recommendation, outcome, prev_hash, entry_hash,"
            " workspace_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.action_id,
                entry.pocket_id,
                timestamp,
                entry.actor,
                entry.event,
                entry.category.value,
                entry.description,
                context_json,
                entry.ai_recommendation,
                entry.outcome,
                prev_hash,
                entry_hash,
                workspace_id,
            ),
        )

    async def log(self, *, actor: str, event: str, description: str, **kwargs: Any) -> AuditEntry:
        """Public audit log method for non-action events."""
        return await self._log(actor=actor, event=event, description=description, **kwargs)

    async def query_audit(
        self,
        pocket_id: str | None = None,
        category: str | None = None,
        event: str | None = None,
        actor: str | None = None,
        limit: int = 100,
        workspace_id: str | None = None,
    ) -> list[AuditEntry]:
        """Query audit entries with optional filters.

        ``actor`` accepts the full colon-qualified identity string the
        audit table stores (``agent:abc123``, ``user:alice``, etc.). It
        is an exact match, not a LIKE — callers who need prefix matching
        should filter in Python on the returned list. Added 2026-04-19
        for the AgentReasoningTab's per-agent view.

        ``workspace_id`` (W4a) is a READ FILTER: when supplied, only the
        caller's tenant's rows (plus legacy NULL-workspace rows) are returned,
        so an auditor for workspace A never sees workspace B's decision trail.
        This filters the returned ROWS only — it does NOT touch the global W2b
        hash chain or ``verify_audit_chain``, which always run over the whole
        ledger so chain integrity stays a property of the complete trail.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if pocket_id:
            conditions.append("pocket_id = ?")
            params.append(pocket_id)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if event:
            conditions.append("event = ?")
            params.append(event)
        if actor:
            conditions.append("actor = ?")
            params.append(actor)
        ws_cond, ws_params = _workspace_scope(workspace_id)
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM instinct_audit {where} ORDER BY timestamp DESC LIMIT ?", params
            ) as cur:
                return [self._row_to_audit(row) async for row in cur]

    async def get_audit_entry(
        self, audit_id: str, workspace_id: str | None = None
    ) -> AuditEntry | None:
        """Fetch a single audit row by id, scoped to a workspace (W4a).

        A direct single-row lookup, so a tenant with more than the
        ``query_audit`` page size of audit rows can still retrieve a valid
        OLDER entry by id (the previous router path paged the most recent N
        rows and matched in Python, 404-ing on anything past the window).

        ``workspace_id`` applies the same ``workspace_id = ? OR workspace_id
        IS NULL`` scope as :meth:`query_audit`: a concrete workspace sees its
        own rows plus legacy/global NULL-workspace rows; ``None`` leaves the
        lookup unscoped for OSS callers. Requesting another tenant's id under a
        scoped read returns ``None`` (never leaking its existence). This is a
        READ FILTER only — it never touches the W2b hash chain.
        """
        conditions = ["id = ?"]
        params: list[Any] = [audit_id]
        ws_cond, ws_params = _workspace_scope(workspace_id)
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)
        where = " AND ".join(conditions)

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM instinct_audit WHERE {where} LIMIT 1", params
            ) as cur:
                row = await cur.fetchone()
                return self._row_to_audit(row) if row else None

    async def export_audit(
        self, pocket_id: str | None = None, workspace_id: str | None = None
    ) -> str:
        entries = await self.query_audit(
            pocket_id=pocket_id, limit=10000, workspace_id=workspace_id
        )
        return json.dumps([e.model_dump(mode="json") for e in entries], indent=2)

    async def verify_audit_chain(self) -> dict[str, Any]:
        """Walk the audit hash chain and report whether it is intact.

        The chain spans the WHOLE FILE (each row's ``prev_hash`` links to the
        previous *hashed* row in this ledger, not within a pocket), so
        verification always runs over the entire table in insertion order
        (``rowid``). It recomputes each row's ``entry_hash`` from the row's
        canonical content + the recomputed running ``prev_hash`` and compares
        against the stored value. The first row that fails to match is the
        break point — any insertion, edit, or deletion of a hashed row shifts
        or invalidates every subsequent link.

        ISO-2: under per-workspace physical isolation each workspace has its OWN
        ``instinct.db``, so "the whole ledger" here is exactly ONE tenant's
        ledger — the chain is per-workspace, with its own genesis→…→head, and
        this verifies that tenant's chain independently. (On a single-tenant OSS
        install, or the legacy shared file, it verifies the one shared chain, as
        before. The method is workspace-agnostic — isolation is entirely in which
        file the factory opened.)

        Legacy boundary: rows written before W2b have a NULL ``entry_hash``.
        They are counted as ``legacy_unhashed`` and skipped — the chain is
        only enforced over hashed rows, whose genesis link uses
        ``prev_hash=""``. A ledger that is entirely legacy verifies as intact
        (there is nothing chained to break) but reports ``hashed=0`` so a
        caller can tell the difference between "proven" and "nothing to
        prove".

        Returns a dict:
          - ``intact`` (bool) — True if every hashed row matches.
          - ``total`` (int) — total rows in the ledger.
          - ``hashed`` (int) — rows participating in the chain.
          - ``legacy_unhashed`` (int) — pre-W2b rows skipped.
          - ``checked`` (int) — hashed rows verified before the first break
            (== ``hashed`` when intact).
          - ``broken_at`` (dict | None) — ``{id, rowid, reason}`` for the
            first failing row, or ``None`` when intact.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT rowid, id, action_id, pocket_id, timestamp, actor,"
                " event, category, description, context, ai_recommendation,"
                " outcome, prev_hash, entry_hash"
                " FROM instinct_audit ORDER BY rowid ASC"
            ) as cur:
                rows = await cur.fetchall()

        total = len(rows)
        hashed = 0
        legacy = 0
        checked = 0
        running_prev = ""  # genesis link for the first hashed row
        broken_at: dict[str, Any] | None = None

        for row in rows:
            if row["entry_hash"] is None:
                # Pre-W2b legacy row — not part of the chain.
                legacy += 1
                continue
            hashed += 1
            context = json.loads(row["context"]) if row["context"] else {}
            canonical = _canonical_audit_payload(
                id=row["id"],
                action_id=row["action_id"],
                pocket_id=row["pocket_id"],
                timestamp=row["timestamp"],
                actor=row["actor"],
                event=row["event"],
                category=row["category"],
                description=row["description"],
                context=context,
                ai_recommendation=row["ai_recommendation"],
                outcome=row["outcome"],
            )
            expected = compute_audit_hash(canonical, running_prev)
            stored_prev = row["prev_hash"] or ""
            if stored_prev != running_prev:
                broken_at = {
                    "id": row["id"],
                    "rowid": row["rowid"],
                    "reason": (
                        "prev_hash mismatch — a preceding row was inserted, edited, or deleted"
                    ),
                }
                break
            if row["entry_hash"] != expected:
                broken_at = {
                    "id": row["id"],
                    "rowid": row["rowid"],
                    "reason": "entry_hash mismatch — this row's content was altered",
                }
                break
            checked += 1
            running_prev = row["entry_hash"]

        return {
            "intact": broken_at is None,
            "total": total,
            "hashed": hashed,
            "legacy_unhashed": legacy,
            "checked": checked,
            "broken_at": broken_at,
        }

    # --- Corrections ---

    async def record_correction(self, correction: Correction) -> Correction:
        """Persist a Correction and log the event to the audit table."""
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO instinct_corrections"
                " (id, action_id, pocket_id, actor, patches,"
                " context_summary, action_title, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    correction.id,
                    correction.action_id,
                    correction.pocket_id,
                    correction.actor,
                    json.dumps([p.model_dump(mode="json") for p in correction.patches]),
                    correction.context_summary,
                    correction.action_title,
                    correction.created_at.isoformat(),
                ),
            )
            await db.commit()

        await self._log(
            action_id=correction.action_id,
            pocket_id=correction.pocket_id,
            actor=correction.actor,
            event="correction_captured",
            description=correction.context_summary,
            context={
                "correction_id": correction.id,
                "patch_count": len(correction.patches),
                "paths": [p.path for p in correction.patches],
            },
        )
        return correction

    async def get_corrections_for_pocket(
        self, pocket_id: str, limit: int = 100
    ) -> list[Correction]:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM instinct_corrections"
                " WHERE pocket_id = ? ORDER BY created_at DESC LIMIT ?",
                (pocket_id, limit),
            ) as cur:
                return [self._row_to_correction(row) async for row in cur]

    async def get_corrections_for_action(self, action_id: str) -> list[Correction]:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM instinct_corrections WHERE action_id = ? ORDER BY created_at DESC",
                (action_id,),
            ) as cur:
                return [self._row_to_correction(row) async for row in cur]

    async def count_corrections_by_path(self, pocket_id: str, path: str) -> int:
        """Return how many corrections on this pocket touched a given path.

        Used by the soul bridge to decide when to promote a pattern from
        episodic to procedural (the 3x-same-path heuristic).
        """
        corrections = await self.get_corrections_for_pocket(pocket_id, limit=1000)
        return sum(1 for c in corrections if any(p.path == path for p in c.patches))

    # --- Fabric object snapshots (decision traces) ---

    async def record_fabric_snapshot(self, snapshot: FabricObjectSnapshot) -> FabricObjectSnapshot:
        """Persist a Fabric object snapshot keyed to the audit entry.

        The snapshot preserves the object's state at decision time so later
        queries can reproduce what the agent actually saw, even if the live
        object has been updated since.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO instinct_fabric_snapshots"
                " (id, object_id, audit_id, object_type, snapshot, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot.id,
                    snapshot.object_id,
                    snapshot.audit_id,
                    snapshot.object_type,
                    json.dumps(snapshot.snapshot),
                    snapshot.created_at.isoformat(),
                ),
            )
            await db.commit()
        return snapshot

    async def get_snapshots_for_audit(self, audit_id: str) -> list[FabricObjectSnapshot]:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM instinct_fabric_snapshots WHERE audit_id = ?"
                " ORDER BY created_at ASC",
                (audit_id,),
            ) as cur:
                return [self._row_to_snapshot(row) async for row in cur]

    async def get_snapshots_for_object(
        self, object_id: str, limit: int = 100
    ) -> list[FabricObjectSnapshot]:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM instinct_fabric_snapshots WHERE object_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (object_id, limit),
            ) as cur:
                return [self._row_to_snapshot(row) async for row in cur]

    # --- Helpers ---

    @staticmethod
    def _deserialize_outcome(raw: Any) -> str | OutcomeVerdict | None:
        """Decode the ``outcome`` column back into a string or OutcomeVerdict.

        Issue #1162 lets ``outcome`` hold a structured verdict, stored as
        JSON. A row written before #1162 (or by a caller passing a string)
        holds plain free text. We try to parse the column as a verdict;
        anything that isn't a verdict-shaped JSON object is returned as the
        original string, so legacy rows keep working unchanged.
        """
        if raw is None:
            return None
        if not isinstance(raw, str):
            return raw
        text = raw.strip()
        # A structured verdict is always a JSON object. Cheap prefix check
        # avoids a json.loads attempt on every plain-text outcome.
        if not text.startswith("{"):
            return raw
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return raw
        if not isinstance(data, dict) or "status" not in data:
            # Valid JSON but not a verdict — treat as legacy free text.
            return raw
        try:
            return OutcomeVerdict.model_validate(data)
        except Exception:  # noqa: BLE001 — malformed verdict, fall back to raw text
            return raw

    def _row_to_action(self, row: Any) -> Action:
        # ``assignee`` landed in 2026-05-13 (Mission Control). Old DBs may
        # still surface a row missing the column despite the migration in
        # ``_ensure_schema`` — use a key check so ``aiosqlite.Row`` (which
        # raises IndexError on unknown keys) doesn't break the read.
        assignee = row["assignee"] if "assignee" in row.keys() else None
        # BP-3 — read the generic scope_type back. Key-checked like ``assignee``
        # so a pre-migration row missing the column doesn't raise. NULL/absent
        # stays None (legacy pocket scope).
        scope_type = row["scope_type"] if "scope_type" in row.keys() else None
        # The SQLite layer stamps created_at/updated_at as ISO strings.
        # Forward them on the rebuilt Action so consumers (outcome window
        # filters, age sorting) see real history instead of "now". Old
        # rows that ever had a NULL fall back to None.
        created_at = _parse_iso(row["created_at"]) if "created_at" in row.keys() else None
        updated_at = _parse_iso(row["updated_at"]) if "updated_at" in row.keys() else None
        return Action(
            id=row["id"],
            scope_type=scope_type,
            pocket_id=row["pocket_id"],
            title=row["title"],
            description=row["description"] or "",
            category=ActionCategory(row["category"]),
            status=ActionStatus(row["status"]),
            priority=ActionPriority(row["priority"]),
            trigger=ActionTrigger.model_validate_json(row["trigger"]),
            recommendation=row["recommendation"] or "",
            parameters=json.loads(row["parameters"]) if row["parameters"] else {},
            context=ActionContext.model_validate_json(row["context"])
            if row["context"]
            else ActionContext(),
            outcome=self._deserialize_outcome(row["outcome"]),
            error=row["error"],
            approved_by=row["approved_by"],
            rejected_reason=row["rejected_reason"],
            assignee=assignee,
            **({"created_at": created_at} if created_at else {}),
            **({"updated_at": updated_at} if updated_at else {}),
        )

    def _row_to_audit(self, row: Any) -> AuditEntry:
        return AuditEntry(
            id=row["id"],
            action_id=row["action_id"],
            pocket_id=row["pocket_id"],
            actor=row["actor"],
            event=row["event"],
            category=AuditCategory(row["category"]),
            description=row["description"],
            context=json.loads(row["context"]) if row["context"] else {},
            ai_recommendation=row["ai_recommendation"],
            outcome=row["outcome"],
        )

    def _row_to_correction(self, row: Any) -> Correction:
        patches_raw = json.loads(row["patches"]) if row["patches"] else []
        return Correction(
            id=row["id"],
            action_id=row["action_id"],
            pocket_id=row["pocket_id"],
            actor=row["actor"],
            patches=[CorrectionPatch.model_validate(p) for p in patches_raw],
            context_summary=row["context_summary"],
            action_title=row["action_title"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_snapshot(self, row: Any) -> FabricObjectSnapshot:
        return FabricObjectSnapshot(
            id=row["id"],
            object_id=row["object_id"],
            audit_id=row["audit_id"],
            object_type=row["object_type"] or "",
            snapshot=json.loads(row["snapshot"]) if row["snapshot"] else {},
            created_at=datetime.fromisoformat(row["created_at"]),
        )
