# ee/paw_bar/store.py — Async SQLite store for Paw Bar widgets and events.
# Updated: 2026-07-11 (W4a spec revisions) — new paw_bar_spec_revisions table:
#   update_spec archives the PRIOR spec with a monotonic per-widget revision
#   number (same transaction as the update); latest_spec_revision reads the
#   newest one; rollback_spec restores it via update_spec (so the rollback is
#   itself archived + reversible) and honors the same workspace scoping.
# Updated: 2026-07-11 (W4a tenancy seam) — paw_bar_widgets gains an in-row
#   workspace_id column + index and a _widget_workspace_scope helper (verbatim
#   clone of _decision_workspace_scope: legacy ''/NULL rows always match, None
#   means unscoped). get/list/update_spec/rotate_token/delete_widget take an
#   optional workspace_id and scope both reads and mutations to that tenant —
#   another tenant's widget id resolves to None / mutates nothing. Hard schema
#   change, no migration: the widget has zero deployments.
# Updated: 2026-07-08 — Renamed widget "Paw Print" → "Paw Bar" (PawBarStore, tables
#   paw_print_*→paw_bar_*, db paw_print.db→paw_bar.db). Hard-rename — widget has zero
#   deployments, so no persisted rows to migrate. The separate one-word audit feed
#   (the past-tense record, spelled as one word) is a DIFFERENT feature, unaffected.
# Created: 2026-04-13 (Move 3 PR-A) — CRUD for PawBarWidget + append-only
# PawBarEvent log. Token rotation invalidates any cached copies. Event ingest
# + rate-limit logic lives in PR-B; this module only handles persistence.
# Updated: 2026-06-11 (gap2 — close the customer decision loop) — Added the
# paw_bar_decisions table + upsert_decision / set_decision /
# get_latest_decision. This is the delivery sink for the back-half of the loop:
# an inbound event raises an Instinct proposal and parks a PENDING DecisionStatus
# here; on human approval/rejection the EE approve hook flips it to
# delivered/declined; the customer surface polls get_latest_decision by
# (widget_id, customer_ref). Pure SQLite — no EE import, OSS-boundary clean.
# Updated: 2026-06-11 (gap-housekeeping) — get_decision_by_action /
# set_decision now take an optional workspace_id and scope the lookup +
# UPDATE to that tenant (via the new _decision_workspace_scope helper, which
# also matches the empty-string/NULL legacy rows). The EE delivery hook threads
# the workspace off the approved Action's blob so a cross-tenant action id flips
# nothing. The hot-lookup indexes the decision-loop needs already ship in
# SCHEMA_SQL: idx_pp_decisions_action covers the instinct_action_id lookup and
# idx_pp_decisions_customer covers the (widget_id, customer_ref) poll.

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from pocketpaw.paw_bar.models import (
    DecisionState,
    DecisionStatus,
    PawBarEvent,
    PawBarSpec,
    PawBarWidget,
    _gen_token,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS paw_bar_widgets (
    id TEXT PRIMARY KEY,
    pocket_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    workspace_id TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS paw_bar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    widget_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    customer_ref TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

-- gap2: the customer-decision delivery sink. One row per inbound event that
-- raised an Instinct proposal; the customer surface polls the latest row for
-- (widget_id, customer_ref) to read the owner's decision.
CREATE TABLE IF NOT EXISTS paw_bar_decisions (
    id TEXT PRIMARY KEY,
    widget_id TEXT NOT NULL,
    customer_ref TEXT NOT NULL,
    event_type TEXT DEFAULT '',
    instinct_action_id TEXT DEFAULT '',
    workspace_id TEXT DEFAULT '',
    state TEXT DEFAULT 'pending',
    reply TEXT DEFAULT '',
    decided_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- W4a spec revisions: every update_spec archives the PRIOR spec here with a
-- monotonic per-widget revision number; rollback restores the latest one
-- (itself an update that archives the current spec).
CREATE TABLE IF NOT EXISTS paw_bar_spec_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    widget_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    spec TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_pp_widgets_pocket ON paw_bar_widgets(pocket_id);
CREATE INDEX IF NOT EXISTS idx_pp_widgets_owner ON paw_bar_widgets(owner);
CREATE INDEX IF NOT EXISTS idx_pp_widgets_workspace ON paw_bar_widgets(workspace_id);
CREATE INDEX IF NOT EXISTS idx_pp_events_widget_ts
    ON paw_bar_events(widget_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_pp_events_customer
    ON paw_bar_events(widget_id, customer_ref);
CREATE INDEX IF NOT EXISTS idx_pp_decisions_customer
    ON paw_bar_decisions(widget_id, customer_ref, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pp_decisions_action
    ON paw_bar_decisions(instinct_action_id);
CREATE INDEX IF NOT EXISTS idx_pp_spec_revisions_widget
    ON paw_bar_spec_revisions(widget_id, revision DESC);
"""


def _decision_workspace_scope(workspace_id: str | None) -> tuple[str | None, list[Any]]:
    """Build the tenancy WHERE fragment + bound params for a scoped decision read.

    Mirrors the Fabric store's ``_workspace_scope`` helper, but decision rows
    store ``workspace_id`` as an EMPTY STRING (the model default) rather than
    SQL NULL when no workspace was set, so a legacy/global row is matched on
    ``= ''`` as well as ``IS NULL``. Returns ``(None, [])`` when ``workspace_id``
    is ``None`` — no scoping, fully backward-compatible.
    """
    if workspace_id is None:
        return None, []
    return "(workspace_id = ? OR workspace_id = '' OR workspace_id IS NULL)", [workspace_id]


def _widget_workspace_scope(workspace_id: str | None) -> tuple[str | None, list[Any]]:
    """Build the tenancy WHERE fragment + bound params for a scoped widget read.

    Verbatim clone of :func:`_decision_workspace_scope` for the widgets table
    (W4a — in-row tenancy). Widget rows store ``workspace_id`` as an EMPTY
    STRING (the model default) rather than SQL NULL when no workspace was set,
    so a legacy/global row is matched on ``= ''`` as well as ``IS NULL``.
    Returns ``(None, [])`` when ``workspace_id`` is ``None`` — no scoping,
    fully backward-compatible.
    """
    if workspace_id is None:
        return None, []
    return "(workspace_id = ? OR workspace_id = '' OR workspace_id IS NULL)", [workspace_id]


class PawBarStore:
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

    async def create_widget(self, widget: PawBarWidget) -> PawBarWidget:
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO paw_bar_widgets"
                " (id, pocket_id, owner, workspace_id, name, spec, allowed_domains,"
                " access_token, rate_limit_per_min, per_customer_limit_per_min,"
                " event_mapping, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    widget.id,
                    widget.pocket_id,
                    widget.owner,
                    widget.workspace_id,
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

    async def get_widget(
        self, widget_id: str, workspace_id: str | None = None
    ) -> PawBarWidget | None:
        ws_cond, ws_params = _widget_workspace_scope(workspace_id)
        sql = "SELECT * FROM paw_bar_widgets WHERE id = ?"
        params: list[Any] = [widget_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
                return self._row_to_widget(row) if row else None

    async def list_widgets(
        self,
        pocket_id: str | None = None,
        owner: str | None = None,
        limit: int = 100,
        workspace_id: str | None = None,
    ) -> list[PawBarWidget]:
        conditions: list[str] = []
        params: list[Any] = []
        if pocket_id:
            conditions.append("pocket_id = ?")
            params.append(pocket_id)
        if owner:
            conditions.append("owner = ?")
            params.append(owner)
        ws_cond, ws_params = _widget_workspace_scope(workspace_id)
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM paw_bar_widgets {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ) as cur:
                return [self._row_to_widget(row) async for row in cur]

    async def update_spec(
        self, widget_id: str, spec: PawBarSpec, workspace_id: str | None = None
    ) -> PawBarWidget | None:
        existing = await self.get_widget(widget_id, workspace_id=workspace_id)
        if existing is None:
            return None
        ws_cond, ws_params = _widget_workspace_scope(workspace_id)
        sql = "UPDATE paw_bar_widgets SET spec = ?, updated_at = ? WHERE id = ?"
        params: list[Any] = [spec.model_dump_json(), datetime.now().isoformat(), widget_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            # Archive the PRIOR spec (monotonic per-widget revision) in the
            # same transaction as the update, so every update_spec leaves a
            # rollback point behind.
            async with db.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM paw_bar_spec_revisions WHERE widget_id = ?",
                (widget_id,),
            ) as cur:
                row = await cur.fetchone()
                next_revision = (row[0] if row else 0) + 1
            await db.execute(
                "INSERT INTO paw_bar_spec_revisions (widget_id, revision, spec) VALUES (?, ?, ?)",
                (widget_id, next_revision, existing.spec.model_dump_json()),
            )
            await db.execute(sql, params)
            await db.commit()
        return await self.get_widget(widget_id, workspace_id=workspace_id)

    async def latest_spec_revision(self, widget_id: str) -> tuple[int, PawBarSpec] | None:
        """Return the most recent archived spec revision, or None when none exist."""
        await self._ensure_schema()
        async with self._conn() as db:
            async with db.execute(
                "SELECT revision, spec FROM paw_bar_spec_revisions"
                " WHERE widget_id = ? ORDER BY revision DESC LIMIT 1",
                (widget_id,),
            ) as cur:
                row = await cur.fetchone()
                if row is None:
                    return None
                return row[0], PawBarSpec.model_validate_json(row[1])

    async def rollback_spec(
        self, widget_id: str, workspace_id: str | None = None
    ) -> PawBarWidget | None:
        """Restore the latest archived spec revision (W4a).

        The restore is itself an ``update_spec`` — the CURRENT spec is archived
        as a new revision before being replaced, so a rollback is always
        auditable and itself reversible. Returns ``None`` when the widget does
        not exist in the caller's workspace scope OR when no revision exists.
        """
        widget = await self.get_widget(widget_id, workspace_id=workspace_id)
        if widget is None:
            return None
        latest = await self.latest_spec_revision(widget_id)
        if latest is None:
            return None
        _, archived_spec = latest
        return await self.update_spec(widget_id, archived_spec, workspace_id=workspace_id)

    async def rotate_token(
        self, widget_id: str, workspace_id: str | None = None
    ) -> PawBarWidget | None:
        existing = await self.get_widget(widget_id, workspace_id=workspace_id)
        if existing is None:
            return None
        new_token = _gen_token()
        ws_cond, ws_params = _widget_workspace_scope(workspace_id)
        sql = "UPDATE paw_bar_widgets SET access_token = ?, updated_at = ? WHERE id = ?"
        params: list[Any] = [new_token, datetime.now().isoformat(), widget_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(sql, params)
            await db.commit()
        return await self.get_widget(widget_id, workspace_id=workspace_id)

    async def delete_widget(self, widget_id: str, workspace_id: str | None = None) -> bool:
        ws_cond, ws_params = _widget_workspace_scope(workspace_id)
        sql = "DELETE FROM paw_bar_widgets WHERE id = ?"
        params: list[Any] = [widget_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            cur = await db.execute(sql, params)
            await db.commit()
            return (cur.rowcount or 0) > 0

    # ---------------- Events ----------------

    async def record_event(self, event: PawBarEvent) -> PawBarEvent:
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO paw_bar_events"
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

    async def recent_events(self, widget_id: str, limit: int = 100) -> list[PawBarEvent]:
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paw_bar_events WHERE widget_id = ? ORDER BY timestamp DESC LIMIT ?",
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
                f"SELECT COUNT(*) FROM paw_bar_events WHERE {' AND '.join(conditions)}",
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

    # ---------------- Decisions (gap2 — the back-half of the loop) ----------------

    async def create_decision(self, decision: DecisionStatus) -> DecisionStatus:
        """Insert a PENDING (or any pre-built) decision row.

        Called from the ingest path right after an Instinct proposal is raised:
        the customer's request is now "we're looking into it" until a human
        decides. One row per inbound event — the latest row for
        ``(widget_id, customer_ref)`` is what the customer surface reads back.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO paw_bar_decisions"
                " (id, widget_id, customer_ref, event_type, instinct_action_id,"
                " workspace_id, state, reply, decided_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.id,
                    decision.widget_id,
                    decision.customer_ref,
                    decision.event_type,
                    decision.instinct_action_id,
                    decision.workspace_id,
                    decision.state.value,
                    decision.reply,
                    decision.decided_by,
                    decision.created_at.isoformat(),
                    decision.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return decision

    async def get_decision_by_action(
        self, instinct_action_id: str, workspace_id: str | None = None
    ) -> DecisionStatus | None:
        """Fetch the decision row tied to an Instinct action id.

        The approve/reject delivery hook resolves the parked row this way: the
        Instinct Action's ``_customer_reply`` blob carries no DB handle, only the
        action id, which is the stable join key back to the parked row.

        ``workspace_id`` gives the lookup its own tenancy guard: when supplied,
        only a row in that workspace (or a legacy NULL/empty-workspace row that
        predates per-row tenancy) resolves — a row owned by another tenant
        returns ``None``. The decision row stores ``workspace_id`` as an empty
        string for rows created without one, so the scope matches
        ``workspace_id = ? OR workspace_id = '' OR workspace_id IS NULL``.
        ``None`` leaves the lookup unscoped (backward-compatible).
        """
        ws_cond, ws_params = _decision_workspace_scope(workspace_id)
        sql = "SELECT * FROM paw_bar_decisions WHERE instinct_action_id = ?"
        params: list[Any] = [instinct_action_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        sql += " LIMIT 1"
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
                return self._row_to_decision(row) if row else None

    async def set_decision(
        self,
        instinct_action_id: str,
        *,
        state: DecisionState,
        reply: str,
        decided_by: str,
        workspace_id: str | None = None,
    ) -> DecisionStatus | None:
        """Flip a parked decision to delivered/declined and record the answer.

        Idempotent on the action id. Returns the updated row, or ``None`` when no
        parked row matches (e.g. the proposal was raised before this slice
        shipped, or the row was never created — the approve hook degrades
        cleanly in that case).

        ``workspace_id``, when supplied, scopes BOTH the resolve and the UPDATE
        to the caller's tenant so a cross-tenant action id flips nothing — the
        delivery hook threads the workspace off the approved Action's blob.
        """
        existing = await self.get_decision_by_action(instinct_action_id, workspace_id=workspace_id)
        if existing is None:
            return None
        ws_cond, ws_params = _decision_workspace_scope(workspace_id)
        sql = (
            "UPDATE paw_bar_decisions"
            " SET state = ?, reply = ?, decided_by = ?, updated_at = ?"
            " WHERE instinct_action_id = ?"
        )
        params: list[Any] = [
            state.value,
            reply,
            decided_by,
            datetime.now().isoformat(),
            instinct_action_id,
        ]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(sql, params)
            await db.commit()
        return await self.get_decision_by_action(instinct_action_id, workspace_id=workspace_id)

    async def get_latest_decision(self, widget_id: str, customer_ref: str) -> DecisionStatus | None:
        """Return the most-recent decision for a (widget, customer) pair.

        This is the customer-surface poll: the rendered widget posted an event,
        then polls here to read "what did the owner decide about my request?".
        No owner credential is required — the row is scoped to the customer's own
        ``customer_ref`` on a specific widget, which is all the widget knows.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM paw_bar_decisions"
                " WHERE widget_id = ? AND customer_ref = ?"
                " ORDER BY created_at DESC LIMIT 1",
                (widget_id, customer_ref),
            ) as cur:
                row = await cur.fetchone()
                return self._row_to_decision(row) if row else None

    # ---------------- Helpers ----------------

    def _row_to_widget(self, row: Any) -> PawBarWidget:
        from pocketpaw.paw_bar.models import PawBarEventMapping

        raw_domains = json.loads(row["allowed_domains"]) if row["allowed_domains"] else []
        raw_mapping = json.loads(row["event_mapping"]) if row["event_mapping"] else {}
        mapping = {k: PawBarEventMapping.model_validate(v) for k, v in raw_mapping.items()}
        spec = PawBarSpec.model_validate_json(row["spec"])
        return PawBarWidget(
            id=row["id"],
            pocket_id=row["pocket_id"],
            owner=row["owner"],
            workspace_id=row["workspace_id"] or "",
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

    def _row_to_event(self, row: Any) -> PawBarEvent:
        return PawBarEvent(
            widget_id=row["widget_id"],
            type=row["type"],
            payload=json.loads(row["payload"]) if row["payload"] else {},
            customer_ref=row["customer_ref"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
        )

    def _row_to_decision(self, row: Any) -> DecisionStatus:
        return DecisionStatus(
            id=row["id"],
            widget_id=row["widget_id"],
            customer_ref=row["customer_ref"],
            event_type=row["event_type"] or "",
            instinct_action_id=row["instinct_action_id"] or "",
            workspace_id=row["workspace_id"] or "",
            state=DecisionState(row["state"]),
            reply=row["reply"] or "",
            decided_by=row["decided_by"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
