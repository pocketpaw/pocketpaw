# pocketpaw/audit/store.py — SQLite-backed store for enterprise audit log entries.
# Created: 2026-03-27
# Uses stdlib sqlite3 (no extra deps). Async interface via run_in_executor.
# Stores entries in pocket.db-adjacent audit_log table with indexes for
# pocket_id, category, and timestamp queries.

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketpaw.audit.models import AuditEntry

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    pocket_id TEXT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'decision',
    description TEXT NOT NULL,
    context TEXT DEFAULT '{}',
    ai_recommendation TEXT,
    outcome TEXT,
    status TEXT DEFAULT 'completed',
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_pocket    ON audit_log(pocket_id);
CREATE INDEX IF NOT EXISTS idx_audit_category  ON audit_log(category);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_actor     ON audit_log(actor);
"""


class AuditStore:
    """SQLite-backed audit log store.

    Stores AuditEntry records and supports filtered queries + CSV/JSON export.
    All public methods are async (run sqlite3 synchronously — it's fast enough
    for audit log volume; aiosqlite not required as a dependency).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            base_dir = Path.home() / ".pocketpaw"
            base_dir.mkdir(parents=True, exist_ok=True)
            db_path = base_dir / "audit.db"
        self.db_path = Path(db_path)
        self._initialized = False

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._get_conn() as conn:
            conn.executescript(_DDL)
            conn.commit()
        self._initialized = True

    async def log_entry(
        self,
        actor: str,
        action: str,
        category: str,
        description: str,
        pocket_id: str | None = None,
        context: dict | None = None,
        ai_recommendation: str | None = None,
        outcome: str | None = None,
        status: str = "completed",
        metadata: dict | None = None,
    ) -> str:
        """Create and persist a new AuditEntry. Returns the entry id."""
        self._ensure_schema()
        entry = AuditEntry(
            actor=actor,
            action=action,
            category=category,  # type: ignore[arg-type]
            description=description,
            pocket_id=pocket_id,
            context=context or {},
            ai_recommendation=ai_recommendation,
            outcome=outcome,
            status=status,  # type: ignore[arg-type]
            metadata=metadata or {},
        )
        row = entry.to_db_row()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO audit_log
                    (id, timestamp, pocket_id, actor, action, category,
                     description, context, ai_recommendation, outcome, status, metadata)
                VALUES
                    (:id, :timestamp, :pocket_id, :actor, :action, :category,
                     :description, :context, :ai_recommendation, :outcome, :status, :metadata)
                """,
                row,
            )
            conn.commit()
        logger.debug("audit: logged %s by %s (%s)", action, actor, entry.id)
        return entry.id

    async def query_entries(
        self,
        pocket_id: str | None = None,
        category: str | None = None,
        actor: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 200,
    ) -> list[AuditEntry]:
        """Query audit entries with optional filters. Returns newest first."""
        self._ensure_schema()
        conditions: list[str] = []
        params: list[Any] = []

        if pocket_id is not None:
            conditions.append("pocket_id = ?")
            params.append(pocket_id)
        if category is not None:
            conditions.append("category = ?")
            params.append(category)
        if actor is not None:
            conditions.append("actor = ?")
            params.append(actor)
        if date_from is not None:
            conditions.append("timestamp >= ?")
            params.append(date_from.isoformat())
        if date_to is not None:
            conditions.append("timestamp <= ?")
            params.append(date_to.isoformat())

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [AuditEntry.from_db_row(dict(row)) for row in rows]

    async def export_csv(
        self,
        pocket_id: str | None = None,
        category: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> bytes:
        """Export filtered entries as CSV bytes."""
        entries = await self.query_entries(
            pocket_id=pocket_id,
            category=category,
            date_from=date_from,
            date_to=date_to,
            limit=10_000,
        )
        buf = io.StringIO()
        fieldnames = [
            "id",
            "timestamp",
            "pocket_id",
            "actor",
            "action",
            "category",
            "description",
            "context",
            "ai_recommendation",
            "outcome",
            "status",
            "metadata",
        ]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for entry in entries:
            row = entry.to_db_row()
            writer.writerow(row)
        return buf.getvalue().encode("utf-8")

    async def export_json(
        self,
        pocket_id: str | None = None,
        category: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> bytes:
        """Export filtered entries as JSON bytes (list of objects)."""
        entries = await self.query_entries(
            pocket_id=pocket_id,
            category=category,
            date_from=date_from,
            date_to=date_to,
            limit=10_000,
        )
        data = [entry.model_dump() for entry in entries]
        return json.dumps(data, default=str).encode("utf-8")

    # ------------------------------------------------------------------
    # Domain helpers — convenience methods for common integration points
    # ------------------------------------------------------------------

    async def log_tool_execution(
        self,
        tool_name: str,
        actor: str,
        description: str,
        context: dict | None = None,
        pocket_id: str | None = None,
        outcome: str | None = None,
        status: str = "completed",
    ) -> str:
        """Log an agent tool execution event."""
        return await self.log_entry(
            actor=actor,
            action="tool_execution",
            category="decision",
            description=description,
            pocket_id=pocket_id,
            context=context or {},
            outcome=outcome,
            status=status,
            metadata={"tool": tool_name},
        )

    async def log_connector_sync(
        self,
        connector_name: str,
        actor: str,
        description: str,
        record_count: int = 0,
        pocket_id: str | None = None,
        status: str = "completed",
    ) -> str:
        """Log a connector data sync event."""
        return await self.log_entry(
            actor=actor,
            action="connector_sync",
            category="data",
            description=description,
            pocket_id=pocket_id,
            status=status,
            metadata={"connector": connector_name, "record_count": record_count},
        )


# ---------------------------------------------------------------------------
# Singleton / FastAPI dependency
# ---------------------------------------------------------------------------

_audit_store: AuditStore | None = None


def get_audit_store() -> AuditStore:
    """Return the global AuditStore singleton."""
    global _audit_store
    if _audit_store is None:
        _audit_store = AuditStore()
    return _audit_store
