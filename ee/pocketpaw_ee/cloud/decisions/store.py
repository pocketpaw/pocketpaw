# store.py — SQLite-backed materialized store for the decision graph.
# Created: 2026-05-25 (RFC 07 Slice 1) — implements the DDL from RFC 07
#   § "The materialized store" (lines 278-348). Four tables:
#     - decisions          (one row per Decision)
#     - decision_edges     (five edge kinds; the graph)
#     - decision_inputs    (denormalized for fast input filtering)
#     - decision_approvers (denormalized for "approved by X" queries)
#
#   WAL mode + 64 MB page cache per the RFC's perf budgets. The six
#   indexes the RFC lists are created on bootstrap. JSON1 is used at
#   read time for scope-tag overlap (`scope_tags ?| ?` — JSON array
#   containment) so a per-request scope filter is one query.
#
#   The store lives at `~/.soul/decisions.db` per RFC § Architecture —
#   sibling to the journal at `~/.soul/journal.db`. The path is
#   overridable via `set_db_path` so tests write to a tmp file instead
#   of the real home directory.
#
#   Why SQLite (not a graph engine): five edge kinds, depth-3 walks,
#   bounded fanout. The RFC's Open Q "should we use Neo4j" answers "no"
#   — SQLite + the right indexes wins on operator simplicity. See RFC
#   § Performance + scale.
#
#   Multi-tenancy: one decisions.db per org, but the SAME db can hold
#   decisions from multiple scope tags within the org. Every read
#   filters by scope BEFORE counting (the scope-filter-post-count
#   invariant from RFC § Privacy + audit). Calls in service.py guard
#   this — store.py exposes raw reads + filtered reads, service.py
#   always uses the filtered variant.
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pocketpaw_ee.cloud.decisions.domain import (
    ApproverRef,
    Decision,
    DecisionEdgeRecord,
    DecisionRef,
    InputRef,
    OutcomeRef,
)

logger = logging.getLogger(__name__)


# Module-level db path. Default is the canonical `~/.soul/decisions.db`;
# tests call ``set_db_path(tmp_path / "decisions.db")``.
_DB_PATH: Path = Path.home() / ".soul" / "decisions.db"


def set_db_path(path: str | Path) -> None:
    """Override the decisions store path (test seam)."""
    global _DB_PATH
    _DB_PATH = Path(path)


def get_db_path() -> Path:
    """Return the current decisions store path."""
    return _DB_PATH


_DDL = """
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,                  -- UUID v4
    ts TEXT NOT NULL,                     -- ISO-8601 UTC
    decided_by_kind TEXT NOT NULL,        -- agent | user | system | root
    decided_by_id TEXT NOT NULL,
    decided_by_scope_context TEXT NOT NULL, -- JSON array
    scope_kind TEXT NOT NULL,             -- workspace | org | pocket | team
    pocket_id TEXT,
    intent TEXT NOT NULL,
    action TEXT NOT NULL,
    instinct_policy TEXT,
    instinct_policy_passed INTEGER,       -- 0|1|NULL
    outcome_id TEXT,
    outcome_status TEXT,                  -- pending|landed|rejected|abandoned
    outcome_landed_at TEXT,
    outcome_metered INTEGER,              -- 0|1|NULL
    payload TEXT NOT NULL,                -- JSON blob
    correlation_id TEXT,
    hash_link TEXT NOT NULL,
    last_seq INTEGER NOT NULL,
    scope_tags TEXT NOT NULL              -- JSON array of journal scope strings
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_decisions_actor_ts ON decisions(decided_by_id, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_pocket_ts ON decisions(pocket_id, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_scope_ts ON decisions(scope_kind, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_policy_ts ON decisions(instinct_policy, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_outcome_ts ON decisions(outcome_status, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_correlation ON decisions(correlation_id);

CREATE TABLE IF NOT EXISTS decision_edges (
    src_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,               -- precedent | input | approval | outcome
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src_id, target_id, relation),
    FOREIGN KEY (src_id) REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_edges_src_rel ON decision_edges(src_id, relation);
CREATE INDEX IF NOT EXISTS idx_edges_tgt_rel ON decision_edges(target_id, relation);

CREATE TABLE IF NOT EXISTS decision_inputs (
    decision_id TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- fabric_object | dataref | decision
    input_id TEXT NOT NULL,
    label TEXT,
    point_in_time TEXT,
    PRIMARY KEY (decision_id, kind, input_id),
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_inputs_id ON decision_inputs(input_id);

CREATE TABLE IF NOT EXISTS decision_approvers (
    decision_id TEXT NOT NULL,
    approver_kind TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    approver_scope_context TEXT NOT NULL, -- JSON array
    approved_at TEXT NOT NULL,
    position INTEGER NOT NULL,            -- order; first approver = 0
    PRIMARY KEY (decision_id, position),
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_approvers_id ON decision_approvers(approver_id);

CREATE TABLE IF NOT EXISTS decision_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DecisionStore:
    """SQLite-backed store for the materialized Decision graph.

    One instance per process. Thread-safe for one writer + many readers
    via SQLite's WAL mode. The projection (single-writer) calls `upsert`
    + `add_edge` inside a transaction; the Python API (`service.py`)
    calls the read methods.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None else _DB_PATH
        self._lock = threading.Lock()  # serialize writes; SQLite WAL handles reads
        self._conn: sqlite3.Connection | None = None
        self._bootstrap()

    # --- lifecycle -----------------------------------------------------------

    def _bootstrap(self) -> None:
        """Open the connection, set pragmas, create the schema if absent."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False`` is needed because the projection's
        # `apply()` may be called from the FastAPI request thread while
        # `DecisionGraph.find()` reads from another. Writes are serialized
        # by ``self._lock``; reads rely on SQLite WAL concurrency.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL mode + 64 MB page cache per RFC perf budget. ``synchronous
        # = normal`` is the documented WAL choice — durable enough, fast
        # for the high write rate the projection can produce on rebuild.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA cache_size = -64000")  # 64 MB
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Apply DDL — IF NOT EXISTS clauses make this idempotent.
        self._conn.executescript(_DDL)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                logger.warning("error closing decisions store", exc_info=True)
            self._conn = None

    def reset(self) -> None:
        """Drop all rows. Used by ``DecisionProjection.rebuild(since_seq=0)``
        and by tests. Does NOT drop the schema — just truncates."""
        with self._lock:
            assert self._conn is not None
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM decision_approvers")
                self._conn.execute("DELETE FROM decision_inputs")
                self._conn.execute("DELETE FROM decision_edges")
                self._conn.execute("DELETE FROM decisions")
                self._conn.execute("DELETE FROM decision_meta")
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    # --- writes --------------------------------------------------------------

    def upsert_decision(
        self,
        decision: Decision,
        *,
        edges: Iterable[DecisionEdgeRecord] = (),
    ) -> None:
        """Insert (or update) a Decision + its edges in a single transaction.

        The whole write is one transaction so a partial-failure can't leave
        a Decision visible without its edges. On `INSERT OR REPLACE` we
        re-stamp every row from the Decision; the older rows are dropped
        in the same transaction.
        """
        with self._lock:
            assert self._conn is not None
            self._conn.execute("BEGIN")
            try:
                self._write_decision_row(decision)
                self._write_input_rows(decision)
                self._write_approver_rows(decision)
                for edge in edges:
                    self._write_edge_row(edge)
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    def update_outcome(self, decision_id: UUID, outcome: OutcomeRef) -> None:
        """Update only the outcome columns on an existing Decision row.

        Called when ``decision.outcome_attached`` lands. Outcome is the
        ONLY post-emit mutation the projection makes; it intentionally
        does NOT touch hash_link (the chain stays valid).
        """
        with self._lock:
            assert self._conn is not None
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """
                    UPDATE decisions
                       SET outcome_id = ?,
                           outcome_status = ?,
                           outcome_landed_at = ?,
                           outcome_metered = ?
                     WHERE id = ?
                    """,
                    (
                        str(outcome.outcome_id),
                        outcome.status,
                        outcome.landed_at.isoformat() if outcome.landed_at else None,
                        1 if outcome.metered else 0,
                        str(decision_id),
                    ),
                )
                # Also write an "outcome" edge so trace queries can surface
                # the link to the Outcome object.
                self._write_edge_row(
                    DecisionEdgeRecord(
                        src_id=decision_id,
                        target_id=str(outcome.outcome_id),
                        relation="outcome",
                        weight=1.0,
                    )
                )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    def set_cursor(self, seq: int) -> None:
        """Persist the projection cursor so restarts can resume."""
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "INSERT INTO decision_meta(key, value) VALUES('cursor', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(seq),),
            )

    def get_cursor(self) -> int:
        assert self._conn is not None
        row = self._conn.execute("SELECT value FROM decision_meta WHERE key = 'cursor'").fetchone()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    # --- low-level write helpers --------------------------------------------

    def _write_decision_row(self, d: Decision) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT OR REPLACE INTO decisions (
                id, ts,
                decided_by_kind, decided_by_id, decided_by_scope_context,
                scope_kind, pocket_id, intent, action,
                instinct_policy, instinct_policy_passed,
                outcome_id, outcome_status, outcome_landed_at, outcome_metered,
                payload, correlation_id, hash_link, last_seq, scope_tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(d.id),
                d.ts.isoformat(),
                d.decided_by.kind,
                d.decided_by.id,
                json.dumps(list(d.decided_by.scope_context)),
                d.scope_kind,
                d.pocket_id,
                d.intent,
                d.action,
                d.instinct_policy,
                None if d.instinct_policy_passed is None else int(bool(d.instinct_policy_passed)),
                str(d.outcome.outcome_id) if d.outcome else None,
                d.outcome.status if d.outcome else None,
                d.outcome.landed_at.isoformat() if d.outcome and d.outcome.landed_at else None,
                (1 if d.outcome.metered else 0) if d.outcome else None,
                json.dumps(d.payload, default=str),
                str(d.correlation_id) if d.correlation_id else None,
                d.hash_link,
                d.last_seq,
                json.dumps(list(d.scope)),
            ),
        )
        # Re-stamp input/approver rows on every upsert — drop old ones first
        # so the row set is canonical.
        self._conn.execute("DELETE FROM decision_inputs WHERE decision_id = ?", (str(d.id),))
        self._conn.execute("DELETE FROM decision_approvers WHERE decision_id = ?", (str(d.id),))

    def _write_input_rows(self, d: Decision) -> None:
        assert self._conn is not None
        for inp in d.inputs:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO decision_inputs (
                    decision_id, kind, input_id, label, point_in_time
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(d.id),
                    inp.kind,
                    inp.id,
                    inp.label,
                    inp.point_in_time.isoformat() if inp.point_in_time else None,
                ),
            )

    def _write_approver_rows(self, d: Decision) -> None:
        assert self._conn is not None
        for app in d.approvers:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO decision_approvers (
                    decision_id, approver_kind, approver_id,
                    approver_scope_context, approved_at, position
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(d.id),
                    app.actor.kind,
                    app.actor.id,
                    json.dumps(list(app.actor.scope_context)),
                    app.approved_at.isoformat(),
                    app.position,
                ),
            )

    def _write_edge_row(self, edge: DecisionEdgeRecord) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT OR REPLACE INTO decision_edges (
                src_id, target_id, relation, weight
            ) VALUES (?, ?, ?, ?)
            """,
            (str(edge.src_id), edge.target_id, edge.relation, edge.weight),
        )

    # --- reads ---------------------------------------------------------------

    def get_decision(self, decision_id: UUID) -> Decision | None:
        """Fetch one Decision (unfiltered — caller must scope-filter)."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (str(decision_id),)
        ).fetchone()
        if row is None:
            return None
        return self._hydrate_decision(row)

    def iter_decisions(
        self,
        *,
        actor: str | None = None,
        pocket_id: str | None = None,
        policy: str | None = None,
        outcome_status: str | None = None,
        scope_kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        input_id: str | None = None,
        correlation_id: UUID | None = None,
    ) -> Iterable[Decision]:
        """Index-driven multi-axis filter. The most selective axis is
        picked by SQLite's planner via the indexes the RFC lists. Scope
        filter is applied by the caller (see service.py) — this method
        returns the *unfiltered* set so the post-filter total invariant
        can be honored in one place.

        ``input_id`` narrows via a join on decision_inputs; the
        ``idx_inputs_id`` index keeps this O(matches).
        """
        assert self._conn is not None

        clauses: list[str] = []
        params: list[object] = []

        if input_id is not None:
            base = (
                "SELECT d.* FROM decisions d "
                "JOIN decision_inputs di ON di.decision_id = d.id "
                "WHERE di.input_id = ?"
            )
            params.append(input_id)
        else:
            base = "SELECT * FROM decisions WHERE 1=1"

        if actor is not None:
            clauses.append("decided_by_id = ?")
            params.append(actor)
        if pocket_id is not None:
            clauses.append("pocket_id = ?")
            params.append(pocket_id)
        if policy is not None:
            clauses.append("instinct_policy = ?")
            params.append(policy)
        if outcome_status is not None:
            if outcome_status == "pending":
                clauses.append("outcome_status IS NULL")
            else:
                clauses.append("outcome_status = ?")
                params.append(outcome_status)
        if scope_kind is not None:
            clauses.append("scope_kind = ?")
            params.append(scope_kind)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until.isoformat())
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(str(correlation_id))

        sql = base
        if clauses:
            joiner = " AND " if "WHERE" in sql else " WHERE "
            sql = sql + joiner + " AND ".join(clauses)
        sql = sql + " ORDER BY ts DESC, id DESC"

        for row in self._conn.execute(sql, params):
            yield self._hydrate_decision(row)

    def edges_from(
        self,
        src_id: UUID,
        *,
        relation: str | None = None,
    ) -> list[DecisionEdgeRecord]:
        assert self._conn is not None
        if relation is not None:
            rows = self._conn.execute(
                "SELECT * FROM decision_edges WHERE src_id = ? AND relation = ?",
                (str(src_id), relation),
            )
        else:
            rows = self._conn.execute(
                "SELECT * FROM decision_edges WHERE src_id = ?", (str(src_id),)
            )
        return [self._hydrate_edge(r) for r in rows]

    def edges_to(
        self,
        target_id: str,
        *,
        relation: str | None = None,
    ) -> list[DecisionEdgeRecord]:
        assert self._conn is not None
        if relation is not None:
            rows = self._conn.execute(
                "SELECT * FROM decision_edges WHERE target_id = ? AND relation = ?",
                (target_id, relation),
            )
        else:
            rows = self._conn.execute(
                "SELECT * FROM decision_edges WHERE target_id = ?", (target_id,)
            )
        return [self._hydrate_edge(r) for r in rows]

    def count(self) -> int:
        """Total decisions in the store (smoke / debug)."""
        assert self._conn is not None
        row = self._conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()
        return int(row["n"])

    # --- hydration -----------------------------------------------------------

    def _hydrate_decision(self, row: sqlite3.Row) -> Decision:
        """Re-build a Decision domain object from a sqlite Row."""
        # Local imports keep the module dep-free of the Actor type at
        # write time (the row gets the kind+id columns directly).
        from soul_protocol.spec.journal import Actor

        decided_by = Actor(
            kind=row["decided_by_kind"],
            id=row["decided_by_id"],
            scope_context=json.loads(row["decided_by_scope_context"]) or [],
        )

        inputs = self._read_inputs(UUID(row["id"]))
        approvers = self._read_approvers(UUID(row["id"]))
        precedents = self._read_precedents(UUID(row["id"]))

        outcome: OutcomeRef | None = None
        if row["outcome_id"] is not None:
            landed_at_raw = row["outcome_landed_at"]
            metered_raw = row["outcome_metered"]
            outcome = OutcomeRef(
                outcome_id=UUID(row["outcome_id"]),
                status=row["outcome_status"],
                landed_at=_parse_iso(landed_at_raw),
                metered=bool(metered_raw) if metered_raw is not None else False,
            )

        policy_passed = row["instinct_policy_passed"]
        return Decision(
            id=UUID(row["id"]),
            ts=_parse_iso(row["ts"]) or datetime.now(),
            decided_by=decided_by,
            scope=json.loads(row["scope_tags"]) or ["org:unscoped"],
            scope_kind=row["scope_kind"],
            intent=row["intent"],
            action=row["action"],
            inputs=inputs,
            approvers=approvers,
            instinct_policy=row["instinct_policy"],
            instinct_policy_passed=bool(policy_passed) if policy_passed is not None else None,
            precedents=precedents,
            outcome=outcome,
            payload=json.loads(row["payload"]) if row["payload"] else {},
            pocket_id=row["pocket_id"],
            correlation_id=UUID(row["correlation_id"]) if row["correlation_id"] else None,
            hash_link=row["hash_link"],
            last_seq=int(row["last_seq"]),
        )

    def _read_inputs(self, decision_id: UUID) -> list[InputRef]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM decision_inputs WHERE decision_id = ?",
            (str(decision_id),),
        )
        return [
            InputRef(
                kind=r["kind"],
                id=r["input_id"],
                label=r["label"] or "",
                point_in_time=_parse_iso(r["point_in_time"]),
            )
            for r in rows
        ]

    def _read_approvers(self, decision_id: UUID) -> list[ApproverRef]:
        from soul_protocol.spec.journal import Actor

        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM decision_approvers WHERE decision_id = ? ORDER BY position ASC",
            (str(decision_id),),
        )
        return [
            ApproverRef(
                actor=Actor(
                    kind=r["approver_kind"],
                    id=r["approver_id"],
                    scope_context=json.loads(r["approver_scope_context"]) or [],
                ),
                approved_at=_parse_iso(r["approved_at"]) or datetime.now(),
                position=int(r["position"]),
            )
            for r in rows
        ]

    def _read_precedents(self, decision_id: UUID) -> list[DecisionRef]:
        """Read precedent edges from `decision_edges` for this Decision."""
        edges = self.edges_from(decision_id, relation="precedent")
        out: list[DecisionRef] = []
        for e in edges:
            try:
                out.append(
                    DecisionRef(
                        decision_id=UUID(e.target_id),
                        relation="precedent",
                        weight=e.weight,
                    )
                )
            except ValueError:
                # Target wasn't a UUID — skip (shouldn't happen for precedents).
                continue
        return out

    def _hydrate_edge(self, row: sqlite3.Row) -> DecisionEdgeRecord:
        return DecisionEdgeRecord(
            src_id=UUID(row["src_id"]),
            target_id=row["target_id"],
            relation=row["relation"],
            weight=float(row["weight"]),
        )


def _parse_iso(v: object) -> datetime | None:
    """Parse an ISO-8601 string back to a datetime; tolerate None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


__all__ = [
    "DecisionStore",
    "get_db_path",
    "set_db_path",
]
