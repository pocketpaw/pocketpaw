# Instinct store — async SQLite operations for the decision pipeline.
# Created: 2026-03-28 — Action lifecycle + audit log.
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

import json
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
    outcome TEXT
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


class InstinctStore:
    """Async SQLite store for the decision pipeline."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._initialized = False

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
            await db.commit()
        self._initialized = True

    def _conn(self) -> aiosqlite.Connection:
        """Return a new connection context manager."""
        return aiosqlite.connect(self._db_path)

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
    ) -> Action:
        action = Action(
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
                " recommendation, parameters, context, assignee)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        async with self._conn() as db:
            await db.execute(f"UPDATE instinct_actions SET {', '.join(sets)} WHERE id = ?", params)
            await db.commit()

        await self._log(
            action_id=action_id,
            pocket_id=action.pocket_id,
            actor=actor,
            event=event,
            description=f"{event.replace('_', ' ').title()}: {action.title}{extra_desc}",
            context=audit_context,
        )
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
        self, pocket_id: str | None = None, assignee: str | None = None
    ) -> list[Action]:
        return await self._query_actions(
            status=ActionStatus.PENDING, pocket_id=pocket_id, assignee=assignee
        )

    async def pending_count(self, pocket_id: str | None = None) -> int:
        cond = "WHERE status = 'pending'"
        params: list[Any] = []
        if pocket_id:
            cond += " AND pocket_id = ?"
            params.append(pocket_id)
        await self._ensure_schema()
        async with self._conn() as db:
            async with db.execute(f"SELECT COUNT(*) FROM instinct_actions {cond}", params) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def for_pocket(self, pocket_id: str) -> list[Action]:
        return await self._query_actions(pocket_id=pocket_id)

    async def list_actions(
        self,
        pocket_id: str | None = None,
        status: ActionStatus | None = None,
        limit: int = 50,
    ) -> list[Action]:
        """Public method — list actions with optional filters and limit."""
        return await self._query_actions(status=status, pocket_id=pocket_id, limit=limit)

    async def _query_actions(
        self,
        status: ActionStatus | None = None,
        pocket_id: str | None = None,
        limit: int = 500,
        assignee: str | None = None,
    ) -> list[Action]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if pocket_id:
            conditions.append("pocket_id = ?")
            params.append(pocket_id)
        if assignee:
            conditions.append("assignee = ?")
            params.append(assignee)
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
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO instinct_audit"
                " (id, action_id, pocket_id, actor, event,"
                " category, description, context,"
                " ai_recommendation, outcome)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.action_id,
                    entry.pocket_id,
                    entry.actor,
                    entry.event,
                    entry.category.value,
                    entry.description,
                    json.dumps(entry.context),
                    entry.ai_recommendation,
                    entry.outcome,
                ),
            )
            await db.commit()
        return entry

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
    ) -> list[AuditEntry]:
        """Query audit entries with optional filters.

        ``actor`` accepts the full colon-qualified identity string the
        audit table stores (``agent:abc123``, ``user:alice``, etc.). It
        is an exact match, not a LIKE — callers who need prefix matching
        should filter in Python on the returned list. Added 2026-04-19
        for the AgentReasoningTab's per-agent view.
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
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM instinct_audit {where} ORDER BY timestamp DESC LIMIT ?", params
            ) as cur:
                return [self._row_to_audit(row) async for row in cur]

    async def export_audit(self, pocket_id: str | None = None) -> str:
        entries = await self.query_audit(pocket_id=pocket_id, limit=10000)
        return json.dumps([e.model_dump(mode="json") for e in entries], indent=2)

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
        # The SQLite layer stamps created_at/updated_at as ISO strings.
        # Forward them on the rebuilt Action so consumers (outcome window
        # filters, age sorting) see real history instead of "now". Old
        # rows that ever had a NULL fall back to None.
        created_at = _parse_iso(row["created_at"]) if "created_at" in row.keys() else None
        updated_at = _parse_iso(row["updated_at"]) if "updated_at" in row.keys() else None
        return Action(
            id=row["id"],
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
