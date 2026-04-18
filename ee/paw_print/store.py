# ee/paw_print/store.py — Async SQLite store for Paw Print widgets and events.
# Created: 2026-04-13 (Move 3 PR-A) — CRUD for PawPrintWidget + append-only
# PawPrintEvent log. Token rotation invalidates any cached copies. Event ingest
# + rate-limit logic lives in PR-B; this module only handles persistence.

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from ee.paw_print.models import PawPrintEvent, PawPrintSpec, PawPrintWidget, _gen_token

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS paw_print_widgets (
    id TEXT PRIMARY KEY,
    pocket_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    name TEXT DEFAULT '',
    spec TEXT NOT NULL,
    allowed_domains TEXT DEFAULT '[]',
    access_token TEXT NOT NULL,
    rate_limit_per_min INTEGER DEFAULT 60,
    per_customer_limit_per_min INTEGER DEFAULT 10,
    event_mapping TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paw_print_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    widget_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    customer_ref TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pp_widgets_pocket ON paw_print_widgets(pocket_id);
CREATE INDEX IF NOT EXISTS idx_pp_widgets_owner ON paw_print_widgets(owner);
CREATE INDEX IF NOT EXISTS idx_pp_events_widget_ts
    ON paw_print_events(widget_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_pp_events_customer
    ON paw_print_events(widget_id, customer_ref);
"""


class PawPrintStore:
    """Async SQLite store — same shape as InstinctStore so the wiring is familiar."""

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
        return aiosqlite.connect(self._db_path)

    # ---------------- Widgets ----------------

    async def create_widget(self, widget: PawPrintWidget) -> PawPrintWidget:
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO paw_print_widgets"
                " (id, pocket_id, owner, name, spec, allowed_domains,"
                " access_token, rate_limit_per_min, per_customer_limit_per_min,"
                " event_mapping, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    widget.id,
                    widget.pocket_id,
                    widget.owner,
                    widget.name,
                    widget.spec.model_dump_json(),
                    json.dumps(widget.allowed_domains),
                    widget.access_token,
                    widget.rate_limit_per_min,
                    widget.per_customer_limit_per_min,
                    json.dumps(
                        {k: v.model_dump() for k, v in widget.event_mapping.items()},
                    ),
                    widget.created_at.isoformat(),
                    widget.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return widget

    async def get_widget(self, widget_id: str) -> PawPrintWidget | None:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paw_print_widgets WHERE id = ?",
                (widget_id,),
            ) as cur:
                row = await cur.fetchone()
                return self._row_to_widget(row) if row else None

    async def list_widgets(
        self, pocket_id: str | None = None, owner: str | None = None, limit: int = 100
    ) -> list[PawPrintWidget]:
        conditions: list[str] = []
        params: list[Any] = []
        if pocket_id:
            conditions.append("pocket_id = ?")
            params.append(pocket_id)
        if owner:
            conditions.append("owner = ?")
            params.append(owner)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM paw_print_widgets {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ) as cur:
                return [self._row_to_widget(row) async for row in cur]

    async def update_spec(self, widget_id: str, spec: PawPrintSpec) -> PawPrintWidget | None:
        existing = await self.get_widget(widget_id)
        if existing is None:
            return None
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "UPDATE paw_print_widgets SET spec = ?, updated_at = ? WHERE id = ?",
                (spec.model_dump_json(), datetime.now().isoformat(), widget_id),
            )
            await db.commit()
        return await self.get_widget(widget_id)

    async def rotate_token(self, widget_id: str) -> PawPrintWidget | None:
        existing = await self.get_widget(widget_id)
        if existing is None:
            return None
        new_token = _gen_token()
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "UPDATE paw_print_widgets SET access_token = ?, updated_at = ? WHERE id = ?",
                (new_token, datetime.now().isoformat(), widget_id),
            )
            await db.commit()
        return await self.get_widget(widget_id)

    async def delete_widget(self, widget_id: str) -> bool:
        await self._ensure_schema()
        async with self._conn() as db:
            cur = await db.execute(
                "DELETE FROM paw_print_widgets WHERE id = ?",
                (widget_id,),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    # ---------------- Events ----------------

    async def record_event(self, event: PawPrintEvent) -> PawPrintEvent:
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO paw_print_events"
                " (widget_id, type, payload, customer_ref, timestamp)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    event.widget_id,
                    event.type,
                    json.dumps(event.payload),
                    event.customer_ref,
                    event.timestamp.isoformat(),
                ),
            )
            await db.commit()
        return event

    async def recent_events(self, widget_id: str, limit: int = 100) -> list[PawPrintEvent]:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paw_print_events WHERE widget_id = ?"
                " ORDER BY timestamp DESC LIMIT ?",
                (widget_id, limit),
            ) as cur:
                return [self._row_to_event(row) async for row in cur]

    async def count_events_since(
        self,
        widget_id: str,
        since: datetime,
        customer_ref: str | None = None,
    ) -> int:
        """Count events in the last window — backs the rate limiter."""
        await self._ensure_schema()
        conditions = ["widget_id = ?", "timestamp >= ?"]
        params: list[Any] = [widget_id, since.isoformat()]
        if customer_ref is not None:
            conditions.append("customer_ref = ?")
            params.append(customer_ref)
        async with self._conn() as db:
            async with db.execute(
                f"SELECT COUNT(*) FROM paw_print_events WHERE {' AND '.join(conditions)}",
                params,
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def within_rate_limit(
        self,
        widget_id: str,
        *,
        overall_per_min: int,
        per_customer_per_min: int,
        customer_ref: str,
        now: datetime | None = None,
    ) -> bool:
        """Return True if the next event from `customer_ref` should be accepted."""
        now = now or datetime.now()
        window_start = now - timedelta(minutes=1)
        total = await self.count_events_since(widget_id, window_start)
        if total >= overall_per_min:
            return False
        per_customer = await self.count_events_since(
            widget_id,
            window_start,
            customer_ref=customer_ref,
        )
        return per_customer < per_customer_per_min

    # ---------------- Helpers ----------------

    def _row_to_widget(self, row: Any) -> PawPrintWidget:
        from ee.paw_print.models import PawPrintEventMapping

        raw_domains = json.loads(row["allowed_domains"]) if row["allowed_domains"] else []
        raw_mapping = json.loads(row["event_mapping"]) if row["event_mapping"] else {}
        mapping = {k: PawPrintEventMapping.model_validate(v) for k, v in raw_mapping.items()}
        spec = PawPrintSpec.model_validate_json(row["spec"])
        return PawPrintWidget(
            id=row["id"],
            pocket_id=row["pocket_id"],
            owner=row["owner"],
            name=row["name"] or "",
            spec=spec,
            allowed_domains=raw_domains,
            access_token=row["access_token"],
            rate_limit_per_min=row["rate_limit_per_min"],
            per_customer_limit_per_min=row["per_customer_limit_per_min"],
            event_mapping=mapping,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_event(self, row: Any) -> PawPrintEvent:
        return PawPrintEvent(
            widget_id=row["widget_id"],
            type=row["type"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            customer_ref=row["customer_ref"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
        )
