# Agent ledger store — append-only, agent-keyed SQLite for the value board.
# Created: 2026-07-31 (AL-1, ledger spine) — the table that makes "what did my
#   agents do for me?" a query instead of a manual reconciliation across three
#   stores. Follows the InstinctStore / PawBarStore shape exactly (module-level
#   SCHEMA_SQL, lazy _ensure_schema, a connection per call, an aclose() the
#   workspace-keyed factory runs on LRU eviction) so the wiring is familiar and
#   the per-workspace factory in pocketpaw/stores.py can drive it unchanged.
#
# Three properties this store is built around:
#
#   * APPEND-ONLY. There is no update and no delete. A value board that can be
#     edited is not evidence, and the reconcile alarm (AL-4) can only compare
#     two counts if one of them cannot be quietly revised.
#
#   * UNIQUE(kind, ref) IS THE IDEMPOTENCY GUARD, enforced by the database
#     rather than by every caller remembering. Approvals replay (a retried
#     request, a re-swept run, the reject path which has no CAS today), and a
#     replay must not double-count. ``append`` uses ON CONFLICT DO NOTHING and
#     reports whether a row actually landed, so a caller that cares can tell
#     "first time" from "already recorded" without a pre-read race.
#
#   * WAL. Emitters fire on hot paths — a visitor's turn, an approval click, a
#     billing sweep — while the board reads the same file. WAL keeps a reader
#     from blocking those writers.
#
# Tenancy is BOTH physical (the factory gives each workspace its own file under
# ~/.pocketpaw/workspaces/<id>/agent_ledger.db) and in-row (workspace_id on
# every row, filtered on read). The in-row filter is STRICT — it does not match
# ''/NULL the way the paw_bar and instinct scopes do — because those stores
# carry legacy rows written before their tenancy column existed and this table
# has none: it is new, the column is NOT NULL, and a permissive OR '' would turn
# the single-tenant OSS file's rows into every tenant's rows the moment a shared
# deployment queried it.
#
# What this store must NEVER hold: tokens, cost, latency, model mix, traces. See
# the models module header — the row model has no field for them by design.

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from pocketpaw.agent_ledger.models import LedgerRow

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_ledger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The actor. THE key of the whole design: the agent owns its outcomes.
    -- Empty means "unattributed" — a legal, visible state, not an error.
    agent_id TEXT NOT NULL,
    -- In-row tenancy, layered on top of the per-workspace file.
    workspace_id TEXT NOT NULL,
    -- paw_bar | chat | belt | deep_work | growth | instinct | <new surface>.
    surface TEXT NOT NULL,
    kind TEXT NOT NULL,
    -- OutcomeStatus value on verdict rows, NULL everywhere else.
    outcome TEXT,
    -- Attributed value in minor units + its currency, when genuinely known.
    value_cents INTEGER,
    currency TEXT,
    -- The producing record's stable id — half of the dedupe key.
    ref TEXT NOT NULL,
    actor TEXT NOT NULL,
    -- Flat JSON, OTel GenAI attribute names where one exists.
    attrs TEXT NOT NULL DEFAULT '{}',
    -- Aware-UTC ISO-8601. Sorts lexicographically == chronologically, which is
    -- what lets the window filter compare strings instead of parsing rows.
    ts TEXT NOT NULL,
    -- Replay/dedupe guard: one row per (beat, producing record). An approve
    -- that fires twice, a re-swept run, a reject race — all absorbed here.
    UNIQUE (kind, ref)
);

CREATE INDEX IF NOT EXISTS idx_agent_ledger_agent_ts
    ON agent_ledger_events(agent_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent_ledger_workspace_ts
    ON agent_ledger_events(workspace_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent_ledger_kind_ts
    ON agent_ledger_events(kind, ts DESC);
"""

# Ceiling on any single read. The board pages; nothing on this store should ever
# load a whole tenant's history into memory because a caller forgot a limit.
MAX_QUERY_LIMIT = 1000


def _normalize_ts(value: Any) -> str:
    """Coerce a timestamp to the aware-UTC ISO form every row is stored in.

    Load-bearing, not cosmetic: the window filter compares ``ts`` as a STRING,
    which is only equivalent to comparing instants when every row shares one
    offset. A naive stamp (or one written at +05:30) would sort into the wrong
    window forever. Anything unparseable becomes "now" rather than raising — a
    bookkeeping row with a suspect clock is worth more than a lost emit.
    """
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return datetime.now(UTC).isoformat()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


class AgentLedgerStore:
    """Append-only async SQLite store for agent-keyed value events."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._initialized = False

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            # WAL is a persistent per-file property, so this is a one-time cost
            # that survives every later connection. Emitters write on hot paths
            # while the board reads; without WAL a read holds them up.
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(SCHEMA_SQL)
            await db.commit()
        self._initialized = True

    def _conn(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self._db_path)

    async def aclose(self) -> None:
        """Release on-disk resources (the workspace-keyed factory calls this).

        Verbatim posture from ``InstinctStore.aclose``: the store holds no
        long-lived connection, so the only thing to clean up is the WAL sidecar
        an idle tenant would otherwise leave growing behind an evicted handle.
        Best-effort and idempotent — eviction must never raise.
        """
        self._initialized = False
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 — eviction cleanup is best-effort
            logger.debug("AgentLedgerStore.aclose checkpoint skipped", exc_info=True)

    # ---------------- Write ----------------

    async def append(self, row: LedgerRow) -> bool:
        """Append one row. Returns True when a NEW row landed.

        ``False`` means ``(kind, ref)`` was already recorded — a replay, not a
        failure, and the single most useful thing a caller can learn here. The
        dedupe is done by the UNIQUE constraint via ``ON CONFLICT DO NOTHING``
        rather than by a read-then-write, so two concurrent emitters for the
        same beat cannot both see "absent" and both insert.

        This method does NOT swallow errors. Fail-soft is the EMITTER's job
        (each wraps its own call), because a store that silently eats every
        write would also hide a broken schema from the tests that exist to
        catch it.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            cur = await db.execute(
                "INSERT INTO agent_ledger_events"
                " (agent_id, workspace_id, surface, kind, outcome, value_cents,"
                "  currency, ref, actor, attrs, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (kind, ref) DO NOTHING",
                (
                    row.agent_id,
                    row.workspace_id,
                    row.surface,
                    row.kind,
                    row.outcome,
                    row.value_cents,
                    row.currency,
                    row.ref,
                    row.actor,
                    json.dumps(row.attrs or {}, sort_keys=True),
                    _normalize_ts(row.ts),
                ),
            )
            await db.commit()
            return (cur.rowcount or 0) > 0

    # ---------------- Read ----------------

    @staticmethod
    def _filters(
        *,
        agent_id: str | None,
        workspace_id: str | None,
        since: str | None,
        kinds: list[str] | tuple[str, ...] | None,
        surface: str | None,
    ) -> tuple[str, list[Any]]:
        """Build the shared WHERE clause every read goes through.

        One place, so the row list, the counts, and the value sum can never
        disagree about which rows are in the window — the failure mode that
        produced the chart-vs-wallet bug in the first place.
        """
        conditions: list[str] = []
        params: list[Any] = []
        # An empty-string agent_id is a MEANINGFUL filter (the unattributed
        # bucket), so the check is `is not None`, not truthiness.
        if agent_id is not None:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if workspace_id is not None:
            conditions.append("workspace_id = ?")
            params.append(workspace_id)
        if since:
            conditions.append("ts >= ?")
            params.append(since)
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            conditions.append(f"kind IN ({placeholders})")
            params.extend(kinds)
        if surface:
            conditions.append("surface = ?")
            params.append(surface)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, params

    async def query(
        self,
        *,
        agent_id: str | None = None,
        workspace_id: str | None = None,
        since: str | None = None,
        kinds: list[str] | tuple[str, ...] | None = None,
        surface: str | None = None,
        limit: int = 100,
    ) -> list[LedgerRow]:
        """Rows matching the filters, newest first.

        ``since`` is an aware-UTC ISO string (see
        :func:`pocketpaw.agent_ledger.models.window_start`). ``limit`` is capped
        at :data:`MAX_QUERY_LIMIT`.
        """
        where, params = self._filters(
            agent_id=agent_id,
            workspace_id=workspace_id,
            since=since,
            kinds=kinds,
            surface=surface,
        )
        params.append(max(1, min(int(limit), MAX_QUERY_LIMIT)))
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM agent_ledger_events {where} ORDER BY ts DESC, id DESC LIMIT ?",
                params,
            ) as cur:
                return [self._row_to_model(row) async for row in cur]

    async def counts_by_kind(
        self,
        *,
        agent_id: str | None = None,
        workspace_id: str | None = None,
        since: str | None = None,
        kinds: list[str] | tuple[str, ...] | None = None,
        surface: str | None = None,
    ) -> dict[str, int]:
        """``{kind: count}`` over the window — the value board's spine.

        Aggregated in SQL, not by loading rows, so the board stays cheap as a
        workspace's ledger grows (v1 deliberately ships without rollups).
        """
        where, params = self._filters(
            agent_id=agent_id,
            workspace_id=workspace_id,
            since=since,
            kinds=kinds,
            surface=surface,
        )
        await self._ensure_schema()
        async with self._conn() as db:
            async with db.execute(
                f"SELECT kind, COUNT(*) FROM agent_ledger_events {where} GROUP BY kind",
                params,
            ) as cur:
                return {row[0]: row[1] async for row in cur}

    async def counts_by_outcome(
        self,
        *,
        agent_id: str | None = None,
        workspace_id: str | None = None,
        since: str | None = None,
        surface: str | None = None,
    ) -> dict[str, int]:
        """``{outcome: count}`` over the verdict rows in the window.

        Rows with a NULL outcome are excluded — they carry no verdict, and
        folding them into an "unknown" bucket here would make the solved-ratio
        denominator silently include every approval that was never verified.
        """
        where, params = self._filters(
            agent_id=agent_id,
            workspace_id=workspace_id,
            since=since,
            kinds=None,
            surface=surface,
        )
        clause = f"{where} AND outcome IS NOT NULL" if where else "WHERE outcome IS NOT NULL"
        await self._ensure_schema()
        async with self._conn() as db:
            async with db.execute(
                f"SELECT outcome, COUNT(*) FROM agent_ledger_events {clause} GROUP BY outcome",
                params,
            ) as cur:
                return {row[0]: row[1] async for row in cur}

    async def value_by_currency(
        self,
        *,
        agent_id: str | None = None,
        workspace_id: str | None = None,
        since: str | None = None,
        surface: str | None = None,
    ) -> dict[str, int]:
        """``{currency: summed minor units}`` over the window.

        Grouped by currency rather than returning one total on purpose: adding
        cents to pence produces a number that is wrong in a way nobody can see.
        A caller that wants a single headline figure picks the currency itself
        and is forced to notice when there is more than one.
        """
        where, params = self._filters(
            agent_id=agent_id,
            workspace_id=workspace_id,
            since=since,
            kinds=None,
            surface=surface,
        )
        clause = (
            f"{where} AND value_cents IS NOT NULL" if where else "WHERE value_cents IS NOT NULL"
        )
        await self._ensure_schema()
        async with self._conn() as db:
            async with db.execute(
                f"SELECT COALESCE(currency, ''), SUM(value_cents)"
                f" FROM agent_ledger_events {clause} GROUP BY COALESCE(currency, '')",
                params,
            ) as cur:
                return {row[0]: int(row[1] or 0) async for row in cur}

    @staticmethod
    def _row_to_model(row: Any) -> LedgerRow:
        """Rebuild a :class:`LedgerRow` from a DB row.

        ``attrs`` decodes permissively: a corrupt JSON blob yields an empty dict
        rather than breaking a whole page of the board over one bad row. The
        ``kind`` validator still runs — a row whose kind left the vocabulary
        between write and read SHOULD be loud, because that means the closed
        core moved under a deployed store.
        """
        try:
            attrs = json.loads(row["attrs"]) if row["attrs"] else {}
        except (TypeError, ValueError):
            attrs = {}
        if not isinstance(attrs, dict):
            attrs = {}
        return LedgerRow(
            id=row["id"],
            agent_id=row["agent_id"] or "",
            workspace_id=row["workspace_id"] or "",
            surface=row["surface"],
            kind=row["kind"],
            outcome=row["outcome"],
            value_cents=row["value_cents"],
            currency=row["currency"],
            ref=row["ref"],
            actor=row["actor"],
            attrs=attrs,
            ts=row["ts"],
        )
