# Instinct store — async SQLite operations for the decision pipeline.
# Created: 2026-03-28 — Action lifecycle + audit log.
# Updated: 2026-03-30 — Added limit param to _query_actions, list_actions() public method.

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ee.instinct.models import (
    Action,
    ActionCategory,
    ActionContext,
    ActionPriority,
    ActionStatus,
    ActionTrigger,
    AuditCategory,
    AuditEntry,
)

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

CREATE INDEX IF NOT EXISTS idx_actions_pocket ON instinct_actions(pocket_id);
CREATE INDEX IF NOT EXISTS idx_actions_status ON instinct_actions(status);
CREATE INDEX IF NOT EXISTS idx_audit_pocket ON instinct_audit(pocket_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON instinct_audit(timestamp);
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
        )
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO instinct_actions"
                " (id, pocket_id, title, description,"
                " category, status, priority, trigger,"
                " recommendation, parameters, context)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
            await db.commit()

        await self._log(
            action_id=action.id,
            pocket_id=pocket_id,
            actor=f"{trigger.type}:{trigger.source}",
            event="action_proposed",
            description=f"Proposed: {title}",
            ai_recommendation=recommendation,
        )
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

    async def mark_executed(self, action_id: str, outcome: str | None = None) -> Action | None:
        return await self._update_status(
            action_id,
            ActionStatus.EXECUTED,
            outcome=outcome,
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
        **fields: Any,
    ) -> Action | None:
        action = await self.get_action(action_id)
        if not action:
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

    async def pending(self, pocket_id: str | None = None) -> list[Action]:
        return await self._query_actions(status=ActionStatus.PENDING, pocket_id=pocket_id)

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
    ) -> list[Action]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if pocket_id:
            conditions.append("pocket_id = ?")
            params.append(pocket_id)
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
        limit: int = 100,
    ) -> list[AuditEntry]:
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

    # --- Helpers ---

    def _row_to_action(self, row: Any) -> Action:
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
            outcome=row["outcome"],
            error=row["error"],
            approved_by=row["approved_by"],
            rejected_reason=row["rejected_reason"],
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
