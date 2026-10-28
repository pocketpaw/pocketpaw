# tests/ee/sites/d1_sqlite_fake.py — a D1 client double backed by a REAL in-memory
# SQLite database, for the Sites Data dashboard tests.
#
# It implements the two CloudflareClient methods the data dashboard calls
# (``query_d1`` and ``query_d1_batch``) by executing the SQL with ``sqlite3``, so
# quoting, PRAGMA output, ``instr``, ``IS``, ``RETURNING``, the COUNT subquery and
# column affinity are tested for real rather than as string matches. Every statement
# is recorded in ``sql`` and every API call bumps ``calls``, so a test can assert
# what never reached SQL and how many Cloudflare requests an operation cost.
#
# It behaves like live D1 where the dashboard depends on it (spikes S1-S3):
#   * a statement with more than 100 bound parameters is refused;
#   * a bound boolean is stored as the TEXT "true" / "false", as D1 stores a JSON
#     ``true`` (spike S1), so a missing 0 / 1 coercion shows up as text;
#   * a batch runs in one transaction: any failing statement rolls the WHOLE batch
#     back, and nothing of it lands;
#   * a failing statement surfaces as ``ValidationError("sites.cloudflare_error")``,
#     what ``CloudflareClient`` raises for a ``success: false`` result;
#   * foreign keys are enforced, as D1 enforces them (``foreign_keys=False`` to
#     seed a database that breaks them).
#
# Time Travel (``d1_info`` / ``d1_bookmark`` / ``d1_restore``) is a minimal stub with
# real semantics: every bookmark is a full ``serialize()`` copy of the database at
# that moment, so a restore really puts the old rows back and its
# ``previous_bookmark`` really undoes it. ``version`` (default ``production``) is what
# ``d1_info`` reports; anything else refuses bookmarks like an alpha database.
# ``mark(at)`` takes a bookmark stamped ``at`` for a test to restore to by time;
# ``restores`` records every restore call.
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError

D1_MAX_PARAMS = 100


class SqliteD1:
    def __init__(self, ddl: str = "", *, database_id: str = "", foreign_keys: bool = True):
        # Autocommit: every statement outside a batch lands at once, like D1.
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if foreign_keys:
            self.conn.execute("PRAGMA foreign_keys = ON")
        if ddl:
            self.conn.executescript(ddl)
        self.database_id = database_id
        self.sql: list[tuple[str, list[Any]]] = []
        self.calls = 0
        self.fail_on: str | None = None  # a statement containing this text fails
        self.version = "production"
        self.restores: list[dict[str, Any]] = []
        self._snapshots: list[tuple[datetime, str, bytes]] = []

    def execute(self, sql: str, params: list[Any] | tuple = ()) -> None:
        """Seed helper: run a statement directly, unrecorded."""
        self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.conn.executemany(sql, rows)

    def rows(self, sql: str, params: list[Any] | tuple = ()) -> list[dict[str, Any]]:
        """Inspect helper: read rows directly, unrecorded."""
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _run(self, sql: str, params: list[Any] | None) -> list[dict[str, Any]]:
        params = list(params or [])
        self.sql.append((sql, params))
        params = [("true" if p else "false") if isinstance(p, bool) else p for p in params]
        if len(params) > D1_MAX_PARAMS:
            raise AssertionError(f"D1 refuses {len(params)} bound parameters: {sql}")
        if self.fail_on and self.fail_on in sql:
            raise sqlite3.OperationalError(f"injected failure: {self.fail_on}")
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _check_db(self, database_id: str) -> None:
        if self.database_id:
            assert database_id == self.database_id, database_id

    async def query_d1(
        self, *, database_id: str, sql: str, params: list | None = None
    ) -> list[dict[str, Any]]:
        self._check_db(database_id)
        self.calls += 1
        try:
            return self._run(sql, params)
        except sqlite3.Error as exc:
            raise ValidationError("sites.cloudflare_error", "D1 query statement failed") from exc

    async def query_d1_batch(self, *, database_id: str, statements) -> list[list[dict[str, Any]]]:
        self._check_db(database_id)
        self.calls += 1
        statements = list(statements)
        self.conn.execute("BEGIN")
        out: list[list[dict[str, Any]]] = []
        for index, (sql, params) in enumerate(statements):
            try:
                out.append(self._run(sql, params))
            except sqlite3.Error as exc:
                self.conn.execute("ROLLBACK")
                raise ValidationError(
                    "sites.cloudflare_error",
                    f"D1 batch statement {index + 1} of {len(statements)} failed",
                ) from exc
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
        self.conn.execute("COMMIT")
        return out

    def statements(self, needle: str) -> list[str]:
        return [s for s, _ in self.sql if needle in s]

    # ----------------------------------------------------------------- Time Travel

    def mark(self, at: datetime | None = None) -> str:
        """A bookmark of the database as it is now, stamped ``at`` (default now)."""
        bookmark = f"{len(self._snapshots) + 1:08x}-0000001a-00004c6d-{id(self) % 10**12:012x}"
        self._snapshots.append((at or datetime.now(UTC), bookmark, self.conn.serialize()))
        return bookmark

    def _production(self) -> None:
        if self.version != "production":
            raise ValidationError("sites.cloudflare_error", "Time Travel needs a production D1")

    def _at_or_before(self, timestamp: str) -> tuple[datetime, str, bytes]:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        found = [s for s in self._snapshots if s[0] <= moment]
        if not found:
            raise ValidationError("sites.cloudflare_error", "No bookmark that far back")
        return max(found, key=lambda s: s[0])

    async def d1_info(self, database_id: str) -> dict[str, Any]:
        self._check_db(database_id)
        self.calls += 1
        return {"uuid": database_id, "version": self.version, "file_size": 4096}

    async def d1_bookmark(self, database_id: str, timestamp: str | None = None) -> str:
        self._check_db(database_id)
        self.calls += 1
        self._production()
        return self._at_or_before(timestamp)[1] if timestamp else self.mark()

    async def d1_restore(
        self, database_id: str, *, bookmark: str | None = None, timestamp: str | None = None
    ) -> dict[str, Any]:
        self._check_db(database_id)
        self.calls += 1
        self._production()
        assert bool(bookmark) != bool(timestamp)
        if timestamp:
            target = self._at_or_before(timestamp)
        else:
            target = next((s for s in self._snapshots if s[1] == bookmark), None)
            if target is None:
                raise ValidationError("sites.cloudflare_error", "Unknown bookmark")
        previous = self.mark()
        self.conn.deserialize(target[2])
        self.restores.append({"bookmark": bookmark, "timestamp": timestamp, "to": target[1]})
        return {"bookmark": target[1], "previous_bookmark": previous, "message": "Restored"}
