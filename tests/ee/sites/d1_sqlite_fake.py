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
#   * ``foreign_keys=True`` turns on FK enforcement (D1 enforces foreign keys).
from __future__ import annotations

import sqlite3
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError

D1_MAX_PARAMS = 100


class SqliteD1:
    def __init__(self, ddl: str = "", *, database_id: str = "", foreign_keys: bool = False):
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
