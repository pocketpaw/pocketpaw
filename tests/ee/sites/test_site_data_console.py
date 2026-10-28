# tests/ee/sites/test_site_data_console.py — the Sites Data SQL console and schema view:
# ``sites.site_data.console`` (classify / plan / shape) and ``data_service.run_sql`` /
# ``get_schema`` with their routes.
#
# D1 is a real in-memory SQLite (``d1_sqlite_fake.SqliteD1``), so every statement the
# console builds actually runs: a masking rewrite that SQLite rejects, or one that
# leaks a credential, fails here. Audit events are captured by stubbing ``record``.
#
# Covers TDD §11 "PR 9": the classification table (reads, CTE prefixes, writes, every
# DDL and transaction keyword, comments and strings hiding statements, the PRAGMA
# allowlist, internal tables, unbounded writes), execution (500-row cap, masking by
# name and by login table, big integers, confirm flow, restore point before a write,
# D1 errors, permissions, the per-user busy claim, audit), the schema route, and the
# adversarial pass (DDL / second statements smuggled every way we know, credential
# reads through expressions, compounds, CTE renames and filters, internal tables).
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.sites import data_service  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.project_data import MASK  # noqa: E402
from pocketpaw_ee.sites.site_data import budget, console, d1  # noqa: E402

from tests.ee.sites.d1_sqlite_fake import SqliteD1  # noqa: E402

_PATCH_GET = "pocketpaw_ee.cloud.pockets.service.get"
_PATCH_EDIT = "pocketpaw_ee.cloud.pockets.service.has_edit_access"
_DB = "11111111-2222-3333-4444-555555555555"

_PROJECT_WIRE = {
    "name": "Shop",
    "workspace": "ws1",
    "engine": "project",
    "siteMeta": {"project": {"template": "astro", "recipes": ["d1-drizzle", "better-auth"]}},
    "source": {"package.json": "{}"},
}

_DDL = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT,
  vip INTEGER DEFAULT 0);
CREATE INDEX customers_email ON customers (email, name);
CREATE TABLE notes (body TEXT NOT NULL, tag TEXT);
CREATE TABLE big (n INTEGER);
CREATE TABLE api_clients (id INTEGER PRIMARY KEY, label TEXT, api_key TEXT UNIQUE);
CREATE TABLE "user" (id TEXT PRIMARY KEY, name TEXT, email TEXT, "emailVerified" INTEGER,
  "twoFactorSecret" TEXT);
CREATE TABLE session (id TEXT PRIMARY KEY, token TEXT, "userId" TEXT REFERENCES "user"(id));
CREATE TABLE account (id TEXT PRIMARY KEY, "userId" TEXT REFERENCES "user"(id),
  password TEXT, "accessToken" TEXT);
CREATE TABLE user_sessions (id TEXT PRIMARY KEY, user_id TEXT REFERENCES "user"(id),
  expires_at INTEGER);
CREATE TABLE _paw_migrations (name TEXT PRIMARY KEY);
INSERT INTO customers (id, name, email) VALUES (1, 'Ada', 'ada@x.io'), (2, 'Bob', NULL);
INSERT INTO notes (body, tag) VALUES ('hello', 'a'), ('bye', 'b');
INSERT INTO api_clients VALUES (1, 'ci', 'key-live-123');
INSERT INTO "user" VALUES ('u1', 'Ann', 'ann@x.io', 1, 'totp-secret'),
                         ('u2', 'Ben', 'ben@x.io', 0, NULL);
INSERT INTO session VALUES ('s1', 'tok-1', 'u1');
INSERT INTO account VALUES ('a1', 'u1', 'scrypt$secret-hash', 'at-1'),
                           ('a2', 'u2', 'scrypt$other-hash', NULL);
INSERT INTO user_sessions VALUES ('sess-secret-1', 'u1', 1900000000);
"""
_SECRETS = (
    "scrypt",
    "secret-hash",
    "other-hash",
    "tok-1",
    "at-1",
    "totp",
    "key-live",
    "sess-secret",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    d1.clear_caches()
    budget.reset()
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_1")
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    yield
    d1.clear_caches()
    budget.reset()


@pytest.fixture
def site_doc(monkeypatch):
    doc = SimpleNamespace(d1_database_id=_DB, kv_namespaces={}, r2_buckets={})

    async def _doc(workspace_id, pocket_id):
        return doc

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _doc)
    return doc


@pytest.fixture
def db() -> SqliteD1:
    cf = SqliteD1(_DDL, database_id=_DB)
    cf.executemany("INSERT INTO big VALUES (?)", [(i,) for i in range(600)])
    return cf


@pytest.fixture
def audit(monkeypatch):
    events: list[dict[str, Any]] = []

    async def _record(workspace_id, actor_id, action, **kw):
        events.append({"workspace_id": workspace_id, "actor_id": actor_id, "action": action, **kw})

    from pocketpaw_ee.cloud.audit import service as audit_service

    monkeypatch.setattr(audit_service, "record", _record)
    return events


class _pocket:  # noqa: N801 — reads like the patch it is
    def __init__(self, wire=_PROJECT_WIRE, *, can_edit: bool = True) -> None:
        self._patches = [
            patch(_PATCH_GET, new=AsyncMock(return_value=wire)),
            patch(_PATCH_EDIT, new=AsyncMock(return_value=can_edit)),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


async def _run(cf, sql, *, confirm=None, can_write=True, can_edit=True):
    with _pocket(can_edit=can_edit):
        return await data_service.run_sql(
            workspace_id="ws1",
            user_id="u-admin",
            pocket_id="pk1",
            sql=sql,
            confirm=confirm,
            can_write=can_write,
            _cloudflare=cf,
        )


async def _raises(code: str, status: int, coro) -> CloudError:
    with pytest.raises(CloudError) as exc:
        await coro
    assert exc.value.code == code, (exc.value.code, exc.value.message, exc.value.details)
    assert exc.value.status_code == status
    return exc.value


def _code(sql: str) -> str:
    try:
        stmt = console.classify(sql)
    except CloudError as exc:
        return exc.code
    return stmt.kind + (":unbounded" if stmt.unbounded else "")


def _console_sql(cf: SqliteD1) -> list[str]:
    """What the console sent through the raw endpoint (introspection excluded)."""
    introspection = ("PRAGMA table_xinfo", "PRAGMA foreign_key_list", d1.LIST_SQL)
    return [s for s, _ in cf.sql if not s.startswith(introspection)]


# --------------------------------------------------------------------------- classification

READ, WRITE, UNBOUNDED = "read", "write", "write:unbounded"
DDL = "sites.data_ddl_refused"
MULTI = "sites.data_sql_multiple"
EMPTY = "sites.data_sql_empty"
INTERNAL = "sites.data_sql_internal_table"
ERROR = "sites.data_sql_error"

CASES = [
    # reads
    ("SELECT 1", READ),
    ("select * from customers;", READ),
    ("  SELECT 1 ;;  ", READ),
    ("VALUES (1, 2)", READ),
    ("WITH x AS (SELECT 1) SELECT * FROM x", READ),
    (
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) "
        "SELECT n FROM c",
        READ,
    ),
    (
        "WITH a AS MATERIALIZED (SELECT 1), b AS NOT MATERIALIZED (SELECT 2) SELECT * FROM a, b",
        READ,
    ),
    ("EXPLAIN SELECT 1", READ),
    ("EXPLAIN QUERY PLAN SELECT * FROM customers", READ),
    ("EXPLAIN QUERY PLAN WITH x AS (SELECT 1) SELECT * FROM x", READ),
    ("SELECT ';DROP TABLE customers;--' AS s", READ),
    ("SELECT 'DELETE FROM customers; CREATE TABLE x(a)' AS s", READ),
    ("-- a note\nSELECT 1", READ),
    ("/* DROP TABLE notes */ SELECT 1", READ),
    ("SELECT 1 -- ; DROP TABLE notes", READ),
    ("SELECT 1 /* ; DROP TABLE notes */", READ),
    ("SELECT 1 /* never closed ; DROP TABLE notes", READ),
    ("SELECT replace(name, 'a', 'b') FROM customers", READ),
    ("SELECT x'00ff' AS b", READ),
    ("PRAGMA table_info(customers)", READ),
    ("PRAGMA table_xinfo('customers')", READ),
    ("PRAGMA index_list(customers)", READ),
    ("PRAGMA index_info(customers_email)", READ),
    ('PRAGMA foreign_key_list("account")', READ),
    ("pragma TABLE_INFO", READ),
    # writes
    ("INSERT INTO notes (body) VALUES ('x')", WRITE),
    ("REPLACE INTO notes (body) VALUES ('x')", WRITE),
    ("INSERT OR REPLACE INTO notes (body) VALUES ('x')", WRITE),
    ("INSERT INTO notes (body) VALUES ('x') ON CONFLICT DO NOTHING", WRITE),
    ("UPDATE customers SET vip = 1 WHERE id = 1", WRITE),
    ("UPDATE OR ROLLBACK customers SET vip = 1 WHERE id = 1", WRITE),
    ("DELETE FROM notes WHERE tag = 'a'", WRITE),
    ("DELETE FROM notes WHERE tag = 'a' RETURNING *", WRITE),
    ("DELETE FROM notes WHERE rowid IN (SELECT rowid FROM notes WHERE tag = 'a')", WRITE),
    (
        "WITH old AS (SELECT id FROM customers WHERE vip = 0) DELETE FROM notes WHERE tag IN old",
        WRITE,
    ),
    ("UPDATE customers SET vip = 1", UNBOUNDED),
    ("DELETE FROM notes", UNBOUNDED),
    ("delete from notes;", UNBOUNDED),
    (
        "UPDATE customers SET vip = (SELECT 1 FROM notes WHERE notes.tag = customers.name)",
        UNBOUNDED,
    ),
    ("WITH x AS (SELECT 1 WHERE 1) DELETE FROM notes", UNBOUNDED),
    ("DELETE FROM notes -- WHERE tag = 'a'", UNBOUNDED),
    ("UPDATE customers SET name = 'where' ", UNBOUNDED),
    # DDL and every other verb
    ("CREATE TABLE x (a)", DDL),
    ("CREATE INDEX i ON notes (tag)", DDL),
    ("CREATE VIEW v AS SELECT 1", DDL),
    ("CREATE TEMP TABLE t (a)", DDL),
    ("CREATE VIRTUAL TABLE t USING fts5(a)", DDL),
    ("CREATE TRIGGER t AFTER INSERT ON notes BEGIN DELETE FROM customers; END", DDL),
    ("ALTER TABLE notes ADD COLUMN x TEXT", DDL),
    ("ALTER TABLE notes RENAME TO n2", DDL),
    ("DROP TABLE notes", DDL),
    ("DROP INDEX customers_email", DDL),
    ("DROP VIEW v", DDL),
    ("DROP TRIGGER t", DDL),
    ("ATTACH DATABASE 'x.db' AS other", DDL),
    ("DETACH other", DDL),
    ("VACUUM", DDL),
    ("REINDEX", DDL),
    ("ANALYZE", DDL),
    ("BEGIN", DDL),
    ("BEGIN TRANSACTION", DDL),
    ("COMMIT", DDL),
    ("END", DDL),
    ("ROLLBACK", DDL),
    ("SAVEPOINT a", DDL),
    ("RELEASE a", DDL),
    ("TRUNCATE notes", DDL),
    ("hello", DDL),
    ("PRAGMA foreign_keys = OFF", DDL),
    ("PRAGMA writable_schema = 1", DDL),
    ("PRAGMA table_info = notes", DDL),
    ("PRAGMA table_info(notes) = 1", DDL),
    ("PRAGMA main.table_info(notes)", DDL),
    ("PRAGMA journal_mode", DDL),
    ("PRAGMA database_list", DDL),
    ("PRAGMA defer_foreign_keys(1)", DDL),
    ("EXPLAIN DELETE FROM notes", DDL),
    ("EXPLAIN PRAGMA table_info(notes)", DDL),
    ("WITH x AS (SELECT 1) CREATE TABLE y (a)", DDL),
    ("WITH x AS (SELECT 1)", DDL),
    ("WITH x (SELECT 1) SELECT 1", DDL),
    ("SELECT 1 FROM (SELECT 1) WHERE 1 = (CREATE TABLE t (a))", DDL),
    ("SELECT * FROM notes WHERE EXISTS (DELETE FROM notes)", DDL),
    ("SELECT 1 UNION ALL SELECT 1 FROM notes; UPDATE notes SET tag = 1", MULTI),
    ("DROP TABLE notes", DDL),
    ("SELECT 1", DDL),
    # more than one statement
    ("SELECT 1; SELECT 2", MULTI),
    ("SELECT 1;DROP TABLE notes", MULTI),
    ("SELECT 1; -- note\n DROP TABLE notes", MULTI),
    ("SELECT 1; /* note */ DROP TABLE notes", MULTI),
    ("SELECT 1 --> statement-breakpoint\nDROP TABLE notes", MULTI),
    ('SELECT "a;b" FROM notes', MULTI),
    ("SELECT [a;b] FROM notes", MULTI),
    ("INSERT INTO notes (body) VALUES ('x'); DELETE FROM notes", MULTI),
    # empty
    ("", EMPTY),
    ("   ", EMPTY),
    ("-- only a comment\n", EMPTY),
    (";", EMPTY),
    ("/* */ ;", EMPTY),
    # internal tables, any spelling
    ("SELECT * FROM sqlite_master", INTERNAL),
    ("SELECT * FROM SQLITE_MASTER", INTERNAL),
    ('SELECT * FROM "sqlite_schema"', INTERNAL),
    ("SELECT * FROM main.sqlite_master", INTERNAL),
    ("SELECT * FROM [_cf_KV]", INTERNAL),
    ("SELECT * FROM `d1_migrations`", INTERNAL),
    ("SELECT * FROM __drizzle_migrations", INTERNAL),
    ("SELECT * FROM 'sqlite_master'", INTERNAL),
    ("SELECT * FROM _paw_migrations", INTERNAL),
    ("DELETE FROM _paw_migrations WHERE 1", INTERNAL),
    ("REPLACE INTO sqlite_master VALUES ('table', 'x', 'x', 0, 'CREATE TABLE x(a)')", INTERNAL),
    ("SELECT * FROM pragma_table_info('sqlite_master')", INTERNAL),
    ("SELECT * FROM pragma_database_list", INTERNAL),
    ("SELECT sqlite_version()", INTERNAL),
    ("PRAGMA table_info(sqlite_sequence)", INTERNAL),
    # malformed
    ("SELECT ?", ERROR),
    ("SELECT :name", ERROR),
    ("SELECT @x", ERROR),
    ("SELECT (1", ERROR),
    ("SELECT 1)", ERROR),
    ("SELECT 'never closed", ERROR),
    ("SELECT 1\x00; DROP TABLE notes", ERROR),
    ("SELECT 1 # 2", ERROR),
    # comment and whitespace tricks
    ("SELECT 1 /*/ DROP TABLE notes /*/", READ),
    ("SELECT 1 -- \r DROP TABLE notes", READ),
    ("SELECT 1 \u2028 ; DROP TABLE notes", MULTI),
    ("SELECT 1 \\; DROP TABLE notes", MULTI),
    ("SELECT * FROM (WITH t AS (SELECT 1) SELECT * FROM t) WHERE (CREATE TRIGGER x)", DDL),
    ("WITH t AS (SELECT 1) SELECT * FROM t; CREATE TRIGGER x BEGIN SELECT 1; END", MULTI),
    ("SeLeCt 1", READ),
    ("INSERT INTO notes (body) VALUES ('x') RETURNING (SELECT 1 FROM notes)", WRITE),
]


@pytest.mark.parametrize(("sql", "expected"), CASES)
def test_classification_table(sql, expected):
    assert _code(sql) == expected, sql


def test_classification_table_is_big():
    assert len(CASES) >= 40


def test_too_long_is_refused():
    assert _code("SELECT '" + "x" * 20_001 + "'") == "sites.data_sql_too_long"
    assert _code("SELECT '" + "x" * 19_000 + "'") == READ


def test_returning_and_verbs():
    stmt = console.classify("DELETE FROM notes WHERE 1 RETURNING *")
    assert stmt.verb == "DELETE" and stmt.returning and stmt.targets == {"notes"}
    stmt = console.classify("WITH a AS (SELECT 1) INSERT INTO notes (body) SELECT * FROM a")
    assert stmt.verb == "INSERT" and stmt.targets == {"notes"}
    assert console.classify("EXPLAIN SELECT 1").wrap is False
    assert console.classify("PRAGMA table_info(notes)").wrap is False
    assert console.classify("VALUES (1)").wrap is True


# --------------------------------------------------------------------------- plan


async def _schema(cf: SqliteD1) -> d1.Schema:
    return await d1.introspect(budget.Budgeted(cf, "ws1"), _DB)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql", "code", "status"),
    [
        ("UPDATE \"user\" SET name = 'x' WHERE id = 'u1'", "sites.data_read_only", 403),
        ("DELETE FROM session WHERE id = 's1'", "sites.data_read_only", 403),
        ("DELETE FROM user_sessions WHERE 1", "sites.data_read_only", 403),
        ("INSERT INTO account (id) VALUES ('a9')", "sites.data_read_only", 403),
        ("INSERT INTO notes (body) SELECT password FROM account", "sites.data_read_only", 403),
        ("INSERT INTO notes (body) SELECT token FROM session", "sites.data_read_only", 403),
        (
            "UPDATE notes SET tag = 'x' WHERE body IN (SELECT email FROM \"user\")",
            "sites.data_read_only",
            403,
        ),
        ("DELETE FROM notes WHERE tag IN account", "sites.data_read_only", 403),
        ("UPDATE api_clients SET api_key = 'x' WHERE id = 1", "sites.data_sql_masked_column", 422),
        (
            "UPDATE api_clients SET label = 'x' WHERE api_key LIKE 'key%'",
            "sites.data_sql_masked_column",
            422,
        ),
        ("INSERT INTO api_clients VALUES (3, 'l', 'k')", "sites.data_sql_masked_column", 422),
        ("SELECT * FROM nope", "sites.data_sql_unknown_table", 422),
        ("DELETE FROM nope WHERE 1", "sites.data_sql_unknown_table", 422),
        (
            "WITH account AS (SELECT * FROM account) SELECT * FROM account",
            "sites.data_sql_ambiguous",
            422,
        ),
        ("SELECT account FROM customers", "sites.data_sql_ambiguous", 422),
        (
            "INSERT INTO notes (body) VALUES ((SELECT password FROM account))",
            "sites.data_read_only",
            403,
        ),
        (
            "INSERT INTO api_clients (id, label) SELECT id + 10, api_key FROM api_clients",
            "sites.data_sql_masked_column",
            422,
        ),
        (
            "UPDATE api_clients SET label = 'x' RETURNING upper(api_key)",
            "sites.data_sql_masked_column",
            422,
        ),
        (
            "DELETE FROM notes WHERE body IN (SELECT id FROM user_sessions)",
            "sites.data_read_only",
            403,
        ),
    ],
)
async def test_plan_refusals(db, sql, code, status):
    schema = await _schema(db)
    with pytest.raises(CloudError) as exc:
        console.plan(console.classify(sql), schema)
    assert (exc.value.code, exc.value.status_code) == (code, status), exc.value.message


@pytest.mark.asyncio
async def test_plan_wraps_reads_and_parameterises_semicolon_strings(db):
    schema = await _schema(db)
    p = console.plan(console.classify("SELECT ';' AS s, 'plain' AS t -- c"), schema)
    assert p.sql == "SELECT * FROM (SELECT ? AS s , 'plain' AS t) LIMIT 501"
    assert p.params == [";"] and ";" not in p.sql
    p = console.plan(console.classify("PRAGMA table_info(notes)"), schema)
    assert p.sql == "PRAGMA table_info ( notes )" and p.params == []


# --------------------------------------------------------------------------- execution


@pytest.mark.asyncio
async def test_read_caps_at_500_and_flags_truncation(site_doc, db, audit):
    res = await _run(db, "SELECT * FROM big")
    assert res.kind == "read" and res.columns == ["n"]
    assert len(res.rows) == 500 and res.truncated is True and res.changes is None
    assert any("LIMIT 501" in s for s in _console_sql(db))
    res = await _run(db, "SELECT * FROM big ORDER BY n DESC LIMIT 10")
    assert len(res.rows) == 10 and not res.truncated and res.rows[0] == {"n": 599}


@pytest.mark.asyncio
async def test_pragma_and_explain_run_unwrapped(site_doc, db, audit):
    res = await _run(db, "PRAGMA table_info(customers)")
    assert [r["name"] for r in res.rows] == ["id", "name", "email", "vip"]
    res = await _run(db, "PRAGMA index_list(customers)")
    assert res.rows[0]["name"] == "customers_email"
    res = await _run(db, "EXPLAIN QUERY PLAN SELECT * FROM customers WHERE email = 'x'")
    assert res.kind == "read" and res.rows


@pytest.mark.asyncio
async def test_strings_holding_keywords_and_semicolons_are_values(site_doc, db, audit):
    res = await _run(db, "SELECT ';DROP TABLE notes;--' AS s, 'a''b' AS q")
    assert res.rows == [{"s": ";DROP TABLE notes;--", "q": "a'b"}]
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 2}]
    res = await _run(db, "SELECT count(*) AS n FROM notes WHERE body LIKE '%;%'")
    assert res.rows == [{"n": 0}]


@pytest.mark.asyncio
async def test_comments_never_reach_d1(site_doc, db, audit):
    await _run(db, "SELECT 1 AS one -- ; DROP TABLE notes")
    await _run(db, "SELECT 2 AS two /* never closed ; DROP TABLE notes")
    sent = _console_sql(db)
    assert all("DROP" not in s and "--" not in s and "/*" not in s for s in sent)
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 2}]


@pytest.mark.asyncio
async def test_unicode_space_glues_words_like_sqlite(site_doc, db, audit):
    """A non-breaking space is an identifier character in SQLite: FROM<nbsp>x is one
    name (a column alias), never a FROM clause, so no internal table is reached."""
    res = await _run(db, "SELECT 1 FROM sqlite_master")
    assert res.columns == ["FROM sqlite_master"] and res.rows == [{"FROM sqlite_master": 1}]


@pytest.mark.asyncio
async def test_big_integers_go_out_as_strings(site_doc, db, audit):
    res = await _run(db, "SELECT 9007199254740993 AS big, 42 AS small")
    assert res.rows == [{"big": "9007199254740993", "small": 42}]


@pytest.mark.asyncio
async def test_duplicate_column_names_survive(site_doc, db, audit):
    res = await _run(db, "PRAGMA index_info(customers_email)")
    assert res.columns == ["seqno", "cid", "name"]
    assert console.labels(["id", "id", "id"]) == ["id", "id:1", "id:2"]


@pytest.mark.asyncio
async def test_credential_named_columns_are_masked_on_any_table(site_doc, db, audit):
    res = await _run(
        db, "SELECT name AS password, email AS api_token, id FROM customers WHERE id = 1"
    )
    assert res.rows[0] == {"password": MASK, "api_token": MASK, "id": 1}
    res = await _run(db, "SELECT * FROM api_clients")
    assert res.rows == [{"id": 1, "label": "ci", "api_key": MASK}]


@pytest.mark.asyncio
async def test_select_star_from_a_login_table_is_masked(site_doc, db, audit):
    res = await _run(db, "SELECT * FROM account ORDER BY id")
    assert res.columns == ["id", "userId", "password", "accessToken"]
    assert res.rows[0] == {"id": "a1", "userId": "u1", "password": MASK, "accessToken": MASK}
    assert res.rows[1]["accessToken"] is None  # null stays null, as in the grid
    res = await _run(db, 'SELECT "twoFactorSecret", email FROM "user" WHERE id = \'u1\'')
    assert res.rows == [{"twoFactorSecret": MASK, "email": "ann@x.io"}]
    res = await _run(db, "SELECT id FROM user_sessions")
    assert res.rows == [{"id": MASK}]


_LEAKS = [
    "SELECT password AS p FROM account",
    "SELECT substr(password, 1, 6) AS x FROM account",
    "SELECT hex(password) AS h FROM account",
    "SELECT json_object('p', password) AS j FROM account",
    "SELECT group_concat(password) AS g FROM account",
    "SELECT a.password || '' AS pw FROM account a",
    'SELECT u.email, a.password AS name FROM "user" u JOIN account a ON a."userId" = u.id',
    "SELECT * FROM notes, account",
    "SELECT * FROM notes n JOIN customers c ON 1, account",
    "SELECT * FROM (account)",
    "SELECT * FROM main.account",
    'SELECT * FROM "account"',
    "SELECT * FROM [account]",
    "SELECT * FROM 'account'",
    "SELECT x.p FROM (SELECT password AS p FROM account) x",
    "WITH c(a, b, c, d) AS (SELECT * FROM account) SELECT * FROM c",
    "SELECT 1, 2, 3, 4 UNION ALL SELECT * FROM account",
    "SELECT 'x' AS k UNION SELECT token FROM session",
    "SELECT max(password) AS m FROM account",
    'SELECT "accessToken" AS t FROM account WHERE "accessToken" IS NOT NULL',
    "SELECT token AS t FROM session",
    "SELECT id AS sid FROM user_sessions",
    "SELECT api_key AS k FROM api_clients",
    "SELECT lower(api_key) AS k FROM api_clients",
    'SELECT "twoFactorSecret" AS s FROM "user"',
    "VALUES ((SELECT password FROM account LIMIT 1))",
    "SELECT (SELECT password FROM account WHERE id = 'a1') AS p",
    "SELECT * FROM notes WHERE tag IN (SELECT token FROM session) OR 1",
    "SELECT * FROM notes NATURAL JOIN account",
    'SELECT * FROM notes LEFT OUTER JOIN "main"."account" ON 1',
    'SELECT * FROM notes AS "x", account',
    "SELECT * FROM notes, (account a)",
    "SELECT * FROM json_each((SELECT json_group_array(password) FROM account))",
    "SELECT * FROM (SELECT 1), account",
    "EXPLAIN SELECT password FROM account",
    "SELECT * FROM account ORDER BY 3",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", _LEAKS)
async def test_no_expression_reads_a_credential(site_doc, db, audit, sql):
    res = await _run(db, sql)
    blob = repr(res.rows)
    assert not any(s in blob for s in _SECRETS), (sql, blob)


_ORACLES = [
    ("SELECT id FROM account WHERE password LIKE 'scrypt%'", []),
    ("SELECT id FROM account WHERE substr(password, 1, 1) = 's'", []),
    ("SELECT count(*) AS n FROM session WHERE token = 'tok-1'", [{"n": 0}]),
    ("SELECT label FROM api_clients WHERE api_key = 'key-live-123'", []),
    (
        "SELECT 1 AS hit WHERE EXISTS (SELECT 1 FROM account WHERE password GLOB 'scrypt*')",
        [],
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("sql", "rows"), _ORACLES)
async def test_filters_on_credentials_see_only_the_mask(site_doc, db, audit, sql, rows):
    """A WHERE over a masked column is evaluated against the mask, so row presence
    can't be used to guess the value a character at a time."""
    res = await _run(db, sql)
    assert res.rows == rows


@pytest.mark.asyncio
async def test_login_table_reads_still_work(site_doc, db, audit):
    res = await _run(
        db,
        'SELECT u.email, count(a.id) AS n FROM "user" u LEFT JOIN account a '
        'ON a."userId" = u.id GROUP BY u.id ORDER BY u.id',
    )
    assert res.rows == [{"email": "ann@x.io", "n": 1}, {"email": "ben@x.io", "n": 1}]


@pytest.mark.asyncio
async def test_a_write_copies_only_the_mask(site_doc, db, audit):
    """A write may READ a masked ordinary table, but only through the rewrite."""
    res = await _run(
        db, "INSERT INTO notes (body) SELECT api_key FROM api_clients", confirm="write"
    )
    assert res.changes == 1
    assert db.rows("SELECT body FROM notes WHERE rowid = 3") == [{"body": MASK}]


@pytest.mark.asyncio
async def test_write_needs_confirm(site_doc, db, audit):
    err = await _raises(
        "sites.data_sql_confirm_needed",
        422,
        _run(db, "UPDATE customers SET vip = 1 WHERE id = 1"),
    )
    assert err.details == {"kind": "write", "unbounded": False}
    assert db.rows("SELECT vip FROM customers WHERE id = 1") == [{"vip": 0}]
    res = await _run(db, "UPDATE customers SET vip = 1 WHERE id = 1", confirm="write")
    assert res.kind == "write" and res.changes == 1 and res.rows == []
    assert res.restore_point and res.columns == []
    assert db.rows("SELECT vip FROM customers WHERE id = 1") == [{"vip": 1}]


@pytest.mark.asyncio
async def test_unbounded_write_needs_the_stronger_confirm(site_doc, db, audit):
    err = await _raises(
        "sites.data_sql_confirm_needed", 422, _run(db, "DELETE FROM notes", confirm="write")
    )
    assert err.details == {"kind": "write", "unbounded": True}
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 2}]
    res = await _run(db, "DELETE FROM notes", confirm="unbounded")
    assert res.changes == 2
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 0}]


@pytest.mark.asyncio
async def test_restore_point_is_taken_before_the_write(site_doc, db, audit):
    res = await _run(db, "DELETE FROM notes", confirm="unbounded")
    await db.d1_restore(_DB, bookmark=res.restore_point)
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 2}]


@pytest.mark.asyncio
async def test_returning_rows_are_masked_and_capped(site_doc, db, audit):
    res = await _run(
        db, "UPDATE api_clients SET label = 'x' WHERE id = 1 RETURNING *", confirm="write"
    )
    assert res.rows == [{"id": 1, "label": "x", "api_key": MASK}] and res.changes == 1
    res = await _run(db, "DELETE FROM big WHERE n >= 0 RETURNING n", confirm="write")
    assert len(res.rows) == 500 and res.truncated and res.changes == 600


@pytest.mark.asyncio
async def test_d1_errors_are_422_without_ids(site_doc, db, audit):
    err = await _raises("sites.data_sql_error", 422, _run(db, "SELECT nope FROM notes"))
    assert "no such column" in err.message
    assert _DB not in err.message and "Cloudflare API" not in err.message
    assert audit[-1]["metadata"]["error"] == err.message
    assert console.clean_error("acct 0123456789abcdef0123456789abcdef x") == "acct … x"


@pytest.mark.asyncio
async def test_write_needs_permission_and_edit_access(site_doc, db, audit):
    await _raises(
        "sites.data_write_forbidden",
        403,
        _run(db, "DELETE FROM notes WHERE 1", confirm="write", can_write=False),
    )
    await _raises(
        "pocket.access_denied",
        403,
        _run(db, "DELETE FROM notes WHERE 1", confirm="write", can_edit=False),
    )
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 2}]
    res = await _run(db, "SELECT count(*) AS n FROM notes", can_write=False, can_edit=False)
    assert res.rows == [{"n": 2}]


@pytest.mark.asyncio
async def test_one_run_per_user_at_a_time(site_doc, db, audit):
    assert await budget.claim_once("site_data:sql:ws1:u-admin", 35)
    await _raises("sites.data_sql_busy", 429, _run(db, "SELECT 1"))
    await budget.release("site_data:sql:ws1:u-admin")
    await _run(db, "SELECT 1")
    await _raises("sites.data_sql_error", 422, _run(db, "SELECT nope"))
    await _run(db, "SELECT 2")  # the claim is released after a failure too


@pytest.mark.asyncio
async def test_spent_budget_is_429(site_doc, db, audit, monkeypatch):
    monkeypatch.setattr(budget, "WORKSPACE_LIMIT", (1, 60))
    await _raises("sites.data_busy", 429, _run(db, "SELECT 1"))


@pytest.mark.asyncio
async def test_every_run_is_audited(site_doc, db, audit):
    await _run(db, "SELECT 1 AS one")
    res = await _run(db, "DELETE FROM notes WHERE tag = 'a'", confirm="write")
    long_sql = "SELECT '" + "x" * 3_000 + "' AS s"
    await _run(db, long_sql)
    events = [e for e in audit if e["action"] == "site_data.sql"]
    assert [e["metadata"]["kind"] for e in events] == ["read", "write", "read"]
    write = events[1]
    assert write["target_type"] == "site_data" and write["target_id"] == "pk1"
    assert write["metadata"]["sql"] == "DELETE FROM notes WHERE tag = 'a'"
    assert write["metadata"]["changes"] == 1
    assert write["metadata"]["restore_point"] == res.restore_point
    assert write["metadata"]["database_id"] == _DB
    assert len(events[2]["metadata"]["sql"]) == 2_048
    # Reads change nothing: they stay out of a restore's "changes since" count.
    assert [e["target_type"] for e in events] == ["site_query", "site_data", "site_query"]
    await _raises(
        "sites.data_sql_error", 422, _run(db, "DELETE FROM notes WHERE nope = 1", confirm="write")
    )
    assert audit[-1]["target_type"] == "site_query"  # a failed write changed nothing


@pytest.mark.asyncio
async def test_console_reads_do_not_count_as_changes(site_doc, db, beanie_test_db):
    from pocketpaw_ee.cloud.audit import service as audit_service

    since = datetime.now(UTC).replace(microsecond=0)
    await _run(db, "SELECT 1")
    await _run(db, "DELETE FROM notes WHERE tag = 'a'", confirm="write")
    count = await audit_service.count_target_events(
        "ws1", target_type="site_data", target_root="pk1", since=since, action_prefix="site_data."
    )
    assert count == 1


@pytest.mark.asyncio
async def test_a_console_restore_point_is_one_restore_accepts(site_doc, db, beanie_test_db):
    """The audit event a console write records lets restore name its bookmark."""
    res = await _run(db, "DELETE FROM notes", confirm="unbounded")
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 0}]
    with _pocket():
        await data_service.restore(
            workspace_id="ws1",
            user_id="u-admin",
            pocket_id="pk1",
            restore_point=res.restore_point,
            confirm_name="Shop",
            _cloudflare=db,
        )
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 2}]


@pytest.mark.asyncio
async def test_refused_statements_never_reach_d1(site_doc, db, audit):
    for sql in ("DROP TABLE notes", "SELECT 1; DROP TABLE notes", "SELECT * FROM sqlite_master"):
        with pytest.raises(CloudError):
            await _run(db, sql, confirm="unbounded")
    assert db.calls == 0 and audit == []


@pytest.mark.asyncio
async def test_local_and_unpublished_are_409(site_doc, db, audit, monkeypatch):
    async def _no_db(workspace_id, pocket_id, pocket):
        return ""

    monkeypatch.setattr(sites_service, "_project_database_id", _no_db)
    err = await _raises("sites.data_unavailable", 409, _run(db, "SELECT 1"))
    assert err.details == {"reason": "not_published"}
    assert db.calls == 0


# --------------------------------------------------------------------------- schema


@pytest.mark.asyncio
async def test_schema_view(site_doc, db):
    with _pocket():
        out = await data_service.get_schema(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", _cloudflare=db
        )
    assert out.available is True
    names = [t.name for t in out.tables]
    assert "_paw_migrations" not in names and "account" in names
    customers = next(t for t in out.tables if t.name == "customers")
    assert customers.create_sql.startswith("CREATE TABLE customers")
    assert [i.model_dump() for i in customers.indexes] == [
        {"name": "customers_email", "unique": False, "columns": ["email", "name"]}
    ]
    vip = next(c for c in customers.columns if c.name == "vip")
    assert vip.model_dump() == {
        "name": "vip",
        "sql_type": "integer",
        "kind": "integer",
        "nullable": True,
        "primary_key": False,
        "default": "0",
        "references": None,
        "masked": False,
    }
    account = next(t for t in out.tables if t.name == "account")
    assert {c.name for c in account.columns if c.masked} == {"password", "accessToken"}
    assert "password TEXT" in account.create_sql
    assert next(c for c in account.columns if c.name == "userId").references == "user.id"
    api = next(t for t in out.tables if t.name == "api_clients")
    assert any(i.unique and i.columns == ["api_key"] for i in api.indexes)


@pytest.mark.asyncio
async def test_schema_unavailable(site_doc, db, monkeypatch):
    async def _no_db(workspace_id, pocket_id, pocket):
        return ""

    monkeypatch.setattr(sites_service, "_project_database_id", _no_db)
    with _pocket():
        out = await data_service.get_schema(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", _cloudflare=db
        )
    assert (out.available, out.reason, out.tables) == (False, "not_published", [])


# --------------------------------------------------------------------------- routes


class _Membership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _User:
    def __init__(self, role: str) -> None:
        self.id = "u1"
        self.active_workspace = "ws1"
        self.workspaces = [_Membership("ws1", role)]


def _app(role: str = "member") -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    user = _User(role)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id="ws1",
            request_id="t",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: "ws1"
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest.fixture
def routed(site_doc, monkeypatch, audit):
    cf = SqliteD1(_DDL, database_id=_DB)
    monkeypatch.setattr(sites_service, "_data_cf_client", lambda: cf)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
    return cf


async def _call(app: FastAPI, method: str, path: str, body: Any = None):
    base = "/api/v1/sites/by-pocket/pk1/data"
    with _pocket():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(method, base + path, json=body)


@pytest.mark.asyncio
async def test_route_member_reads_but_cannot_write(routed):
    app = _app("member")
    resp = await _call(app, "POST", "/sql", {"sql": "SELECT name FROM customers ORDER BY id"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "pocket_id",
        "kind",
        "columns",
        "rows",
        "truncated",
        "changes",
        "duration_ms",
        "restore_point",
    }
    assert body["kind"] == "read" and body["rows"] == [{"name": "Ada"}, {"name": "Bob"}]
    assert body["changes"] is None and body["restore_point"] is None
    write = {"sql": "DELETE FROM notes WHERE 1", "confirm": "write"}
    resp = await _call(app, "POST", "/sql", write)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "sites.data_write_forbidden"
    assert routed.rows("SELECT count(*) AS n FROM notes") == [{"n": 2}]


@pytest.mark.asyncio
async def test_route_admin_writes_with_confirm(routed):
    app = _app("admin")
    resp = await _call(app, "POST", "/sql", {"sql": "DELETE FROM notes WHERE tag = 'a'"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "sites.data_sql_confirm_needed"
    assert resp.json()["error"]["details"] == {"kind": "write", "unbounded": False}
    resp = await _call(
        app, "POST", "/sql", {"sql": "DELETE FROM notes WHERE tag = 'a'", "confirm": "write"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "write" and resp.json()["changes"] == 1
    assert resp.json()["restore_point"]


@pytest.mark.asyncio
async def test_route_refuses_ddl_and_bad_bodies(routed):
    app = _app("admin")
    resp = await _call(app, "POST", "/sql", {"sql": "DROP TABLE notes", "confirm": "unbounded"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "sites.data_ddl_refused"
    resp = await _call(app, "POST", "/sql", {"sql": "SELECT 1", "confirm": "yes"})
    assert resp.status_code == 422
    resp = await _call(app, "POST", "/sql", {"sql": "SELECT 1", "database_id": "other"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_route_schema(routed):
    resp = await _call(_app("member"), "GET", "/schema")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True and body["pocket_id"] == "pk1"
    table = next(t for t in body["tables"] if t["name"] == "customers")
    assert set(table) == {"name", "columns", "indexes", "create_sql"}


# --------------------------------------------------------------------------- client wire


@pytest.mark.asyncio
async def test_client_raw_endpoint_wire_shape():
    import json

    import httpx
    from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if body["sql"] == "bad":
            return httpx.Response(
                400,
                json={"success": False, "errors": [{"code": 7500, "message": "near bad"}]},
            )
        result = {
            "results": {"columns": ["id", "id"], "rows": [[1, 2]]},
            "success": True,
            "meta": {"changes": 3},
        }
        return httpx.Response(200, json={"success": True, "result": [result]})

    cf = CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="",
        dispatch_namespace="",
        _transport=httpx.MockTransport(handler),
    )
    out = await cf.query_d1_raw(database_id=_DB, sql="SELECT ?", params=[";"])
    assert out == {"columns": ["id", "id"], "rows": [[1, 2]], "changes": 3}
    assert str(seen[0].url).endswith(f"/accounts/acct_1/d1/database/{_DB}/raw")
    assert json.loads(seen[0].content) == {"sql": "SELECT ?", "params": [";"]}
    with pytest.raises(CloudError) as exc:
        await cf.query_d1_raw(database_id=_DB, sql="bad")
    assert exc.value.code == "sites.cloudflare_error" and "near bad" in exc.value.message


def test_deep_nesting_classifies_in_linear_time():
    import time

    sql = "SELECT " + "abs(" * 3_000 + "1" + ")" * 3_000
    started = time.monotonic()
    assert console.classify(sql).kind == "read"
    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_sqlite_folds_only_ascii_case(db):
    """SQLite treats "\u00c4" and "\u00e4" as different names; so does the planner."""
    db.execute('CREATE TABLE "\u00c4" (id INTEGER PRIMARY KEY, secret TEXT)')
    db.execute('CREATE TABLE "\u00e4" (id INTEGER PRIMARY KEY, label TEXT)')
    schema = await _schema(db)
    p = console.plan(console.classify('SELECT secret AS s FROM "\u00c4"'), schema)
    assert MASK in p.sql
    p = console.plan(console.classify('SELECT label FROM "\u00e4"'), schema)
    assert MASK not in p.sql
