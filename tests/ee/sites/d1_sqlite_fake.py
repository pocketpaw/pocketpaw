# tests/ee/sites/d1_sqlite_fake.py — a D1 client double backed by a REAL in-memory
# SQLite database, for the Sites Data dashboard tests.
#
# It implements the two CloudflareClient methods the data dashboard calls
# (``query_d1`` and ``query_d1_batch``) by executing the SQL with ``sqlite3``, so
# quoting, PRAGMA output, ``instr``, the COUNT subquery and column affinity are tested
# for real rather than as string matches. Every statement is recorded in ``sql`` and
# every API call bumps ``calls``, so a test can assert what never reached SQL and how
# many Cloudflare requests a read cost. D1 refuses a statement with more than 100
# bound parameters, and so does this fake.
from __future__ import annotations

import sqlite3
from typing import Any

D1_MAX_PARAMS = 100


class SqliteD1:
    def __init__(self, ddl: str = "", *, database_id: str = "") -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        if ddl:
            self.conn.executescript(ddl)
        self.database_id = database_id
        self.sql: list[tuple[str, list[Any]]] = []
        self.calls = 0

    def execute(self, sql: str, params: list[Any] | tuple = ()) -> None:
        """Seed helper: run a statement directly, unrecorded."""
        self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.conn.executemany(sql, rows)

    def _run(self, sql: str, params: list[Any] | None) -> list[dict[str, Any]]:
        params = list(params or [])
        self.sql.append((sql, params))
        if len(params) > D1_MAX_PARAMS:
            raise AssertionError(f"D1 refuses {len(params)} bound parameters: {sql}")
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _check_db(self, database_id: str) -> None:
        if self.database_id:
            assert database_id == self.database_id, database_id

    async def query_d1(
        self, *, database_id: str, sql: str, params: list | None = None
    ) -> list[dict[str, Any]]:
        self._check_db(database_id)
        self.calls += 1
        return self._run(sql, params)

    async def query_d1_batch(self, *, database_id: str, statements) -> list[list[dict[str, Any]]]:
        self._check_db(database_id)
        self.calls += 1
        return [self._run(sql, params) for sql, params in statements]

    def statements(self, needle: str) -> list[str]:
        return [s for s, _ in self.sql if needle in s]
