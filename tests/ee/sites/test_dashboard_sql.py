# tests/ee/sites/test_dashboard_sql.py — the Sites AI dashboard SQL gate, runner and
# result shaping (``site_data.dashboard_sql``) and the PII name classifier
# (``site_data.pii``).
#
# D1 is a real in-memory SQLite (``d1_sqlite_fake.SqliteD1``), so every statement the
# gate builds actually runs: a masking rewrite that leaks a credential, or a row cap
# that doesn't hold, fails here. Covers: refusals (writes, multi-statement, ATTACH,
# PRAGMA, DDL, EXPLAIN, recursion, internal tables, parameters, dangerous functions),
# masked-column reads through expressions / subqueries / CTEs / joins / compounds,
# row caps per viz, deps, the EXPLAIN-based heavy flag, batch runs with per-statement
# fallback, data shaping per viz, and an adversarial pass.
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import ValidationError  # noqa: E402
from pocketpaw_ee.sites.project_data import MASK  # noqa: E402
from pocketpaw_ee.sites.site_data import budget, d1, pii  # noqa: E402
from pocketpaw_ee.sites.site_data import dashboard_sql as ds  # noqa: E402
from pocketpaw_ee.sites.site_data.dashboard_dto import WidgetMapping  # noqa: E402

from tests.ee.sites.d1_sqlite_fake import SqliteD1  # noqa: E402

_DB = "11111111-2222-3333-4444-555555555555"

_DDL = """
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total INTEGER,
  status TEXT, created_at TEXT);
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT, plan TEXT);
CREATE TABLE notes (body TEXT NOT NULL, tag TEXT);
CREATE TABLE api_clients (id INTEGER PRIMARY KEY, label TEXT, api_key TEXT);
CREATE TABLE "user" (id TEXT PRIMARY KEY, name TEXT, email TEXT, "twoFactorSecret" TEXT,
  "createdAt" TEXT);
CREATE TABLE session (id TEXT PRIMARY KEY, token TEXT, "userId" TEXT REFERENCES "user"(id));
CREATE TABLE account (id TEXT PRIMARY KEY, "userId" TEXT REFERENCES "user"(id),
  password TEXT, "accessToken" TEXT);
CREATE TABLE user_sessions (id TEXT PRIMARY KEY, user_id TEXT REFERENCES "user"(id),
  expires_at INTEGER);
CREATE TABLE big (n INTEGER);
CREATE TABLE huge (id INTEGER PRIMARY KEY, v INTEGER);
CREATE TABLE _paw_migrations (name TEXT PRIMARY KEY);
INSERT INTO customers VALUES (1, 'Ada', 'ada@x.io', 'pro'), (2, 'Bob', NULL, 'free');
INSERT INTO orders VALUES (1, 1, 1200, 'paid', '2026-10-01'), (2, 1, 800, 'paid', '2026-10-02'),
  (3, 2, 300, 'refunded', '2026-10-02');
INSERT INTO notes VALUES ('hello', 'a'), ('bye', 'b');
INSERT INTO api_clients VALUES (1, 'ci', 'key-live-123');
INSERT INTO "user" VALUES ('u1', 'Ann', 'ann@x.io', 'totp-secret', '2026-10-01'),
  ('u2', 'Ben', 'ben@x.io', NULL, '2026-10-03');
INSERT INTO session VALUES ('s1', 'tok-1', 'u1');
INSERT INTO account VALUES ('a1', 'u1', 'scrypt$secret-hash', 'at-1');
INSERT INTO user_sessions VALUES ('sess-secret-1', 'u1', 1900000000);
"""
_SECRETS = ("scrypt", "secret-hash", "tok-1", "at-1", "totp", "key-live", "sess-secret")


@pytest.fixture(autouse=True)
def _clean():
    d1.clear_caches()
    budget.reset()
    yield
    d1.clear_caches()
    budget.reset()


@pytest.fixture
def db() -> SqliteD1:
    cf = SqliteD1(_DDL, database_id=_DB)
    cf.executemany("INSERT INTO big VALUES (?)", [(i,) for i in range(600)])
    cf.executemany("INSERT INTO huge VALUES (?, ?)", [(i, i % 7) for i in range(10_050)])
    return cf


async def _schema(cf: SqliteD1) -> d1.Schema:
    return await d1.introspect(budget.Budgeted(cf, "ws1"), _DB)


def _reason(sql: str) -> str:
    try:
        ds.check(sql)
    except ds.SqlRejected as exc:
        assert exc.code == "sql_rejected" and exc.status_code == 422
        assert exc.details == {"reason": exc.reason}
        return exc.reason
    return "ok"


async def _result(cf: SqliteD1, sql: str, viz: str = "table", mapping=None):
    q = ds.gate(sql, viz, await _schema(cf))
    raw = await ds.run_one(cf, _DB, q)
    return ds.to_result(q, viz, mapping, raw)


# --------------------------------------------------------------------------- check

REFUSED = [
    # writes, however they are dressed
    ("INSERT INTO notes (body) VALUES ('x')", "not_a_select"),
    ("REPLACE INTO notes (body) VALUES ('x')", "not_a_select"),
    ("UPDATE notes SET tag = 'x' WHERE 1", "not_a_select"),
    ("DELETE FROM notes WHERE 1", "not_a_select"),
    ("WITH x AS (SELECT 1) DELETE FROM notes WHERE 1", "not_a_select"),
    ("WITH x AS (SELECT 1) INSERT INTO notes (body) SELECT 1 FROM x", "not_a_select"),
    ("SELECT * FROM notes WHERE body IN (DELETE FROM notes RETURNING body)", "ddl_refused"),
    ("INSERT INTO notes SELECT * FROM notes RETURNING *", "not_a_select"),
    # more than one statement
    ("SELECT 1; SELECT 2", "sql_multiple"),
    ("SELECT 1; DELETE FROM notes", "sql_multiple"),
    ("SELECT 1 /* x */ ; DROP TABLE notes", "sql_multiple"),
    ("SELECT 1 -- c\n; DROP TABLE notes", "sql_multiple"),
    ("SELECT 'a';DROP TABLE notes", "sql_multiple"),
    # ATTACH / DETACH
    ("ATTACH DATABASE 'file:x' AS evil", "ddl_refused"),
    ("SELECT 1 FROM notes WHERE 1 = 1 ATTACH 'x' AS y", "ddl_refused"),
    ("DETACH DATABASE evil", "ddl_refused"),
    # PRAGMA in every form
    ("PRAGMA table_info(notes)", "not_a_select"),
    ("PRAGMA writable_schema = 1", "ddl_refused"),
    ("SELECT * FROM pragma_table_info('notes')", "pragma"),
    ("SELECT name FROM pragma_table_xinfo('account')", "pragma"),
    ("SELECT * FROM pragma_database_list", "sql_internal_table"),
    ("SELECT 1 FROM notes WHERE 1 PRAGMA foreign_keys", "ddl_refused"),
    # DDL and transactions
    ("CREATE TABLE x (a)", "ddl_refused"),
    ("CREATE TEMP VIEW v AS SELECT * FROM account", "ddl_refused"),
    ("DROP TABLE notes", "ddl_refused"),
    ("ALTER TABLE notes ADD COLUMN x", "ddl_refused"),
    ("SELECT 1 WHERE 0 AND 'x' = 'y' OR 1 VACUUM", "ddl_refused"),
    ("BEGIN", "ddl_refused"),
    ("SELECT * FROM notes; COMMIT", "sql_multiple"),
    ("REINDEX notes", "ddl_refused"),
    ("ANALYZE", "ddl_refused"),
    # other non-SELECT reads
    ("EXPLAIN SELECT * FROM notes", "not_a_select"),
    ("EXPLAIN QUERY PLAN SELECT * FROM account", "not_a_select"),
    (
        "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT n FROM c",
        "recursive",
    ),
    # internal tables and functions, anywhere
    ("SELECT * FROM sqlite_master", "sql_internal_table"),
    ("SELECT sql FROM sqlite_schema", "sql_internal_table"),
    ('SELECT * FROM "sqlite_temp_master"', "sql_internal_table"),
    ("SELECT * FROM _cf_KV", "sql_internal_table"),
    ("SELECT * FROM d1_migrations", "sql_internal_table"),
    ("SELECT * FROM _paw_migrations", "sql_internal_table"),
    ("SELECT * FROM __drizzle_migrations", "sql_internal_table"),
    ("SELECT sqlite_version()", "sql_internal_table"),
    ("SELECT * FROM notes WHERE tag IN (SELECT name FROM sqlite_master)", "sql_internal_table"),
    # parameters and junk
    ("SELECT * FROM notes WHERE tag = ?", "sql_error"),
    ("SELECT * FROM notes WHERE tag = :t", "sql_error"),
    ("SELECT * FROM notes WHERE tag = @t", "sql_error"),
    ("SELECT 'unterminated", "sql_error"),
    ("SELECT (1", "sql_error"),
    ("SELECT 1\x00", "sql_error"),
    ("", "sql_empty"),
    ("   ;; ", "sql_empty"),
    (None, "sql_empty"),
    ("SELECT '" + "x" * 11_000 + "'", "sql_too_long"),
    # functions D1 must never be asked to run, or that only burn memory
    ("SELECT load_extension('evil')", "function"),
    ("SELECT length(randomblob(1000000000))", "function"),
    ("SELECT zeroblob(1000000000)", "function"),
    ("SELECT readfile('/etc/passwd')", "function"),
]


@pytest.mark.parametrize(("sql", "reason"), REFUSED)
def test_refusals(sql, reason):
    assert _reason(sql) == reason, sql


ALLOWED = [
    "SELECT 1",
    "select count(*) as n from orders;",
    "VALUES (1, 2)",
    "WITH paid AS (SELECT * FROM orders WHERE status = 'paid') SELECT count(*) FROM paid",
    "SELECT date(created_at) AS d, sum(total) AS t FROM orders GROUP BY 1 ORDER BY 1",
    "SELECT json_extract('{\"a\":1}', '$.a') AS a",
    "SELECT key, value FROM json_each('[1,2,3]')",
    "SELECT replace(status, 'paid', 'ok') AS s FROM orders",
    "SELECT 'DROP TABLE notes; DELETE' AS harmless_text",
    "SELECT o.id, c.plan FROM orders o JOIN customers c ON c.id = o.customer_id",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    assert _reason(sql) == "ok", sql


def test_unknown_viz_is_refused(db):
    schema = d1.Schema(fingerprint="x")
    with pytest.raises(ds.SqlRejected) as exc:
        ds.gate("SELECT 1", "html", schema)
    assert exc.value.reason == "bad_viz"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT * FROM nope", "sql_unknown_table"),
        ("SELECT * FROM notes JOIN nope ON 1", "sql_unknown_table"),
        ("WITH account AS (SELECT * FROM account) SELECT * FROM account", "sql_ambiguous"),
        ("WITH session AS (SELECT 1 AS token) SELECT * FROM session", "sql_ambiguous"),
        ("SELECT account FROM customers", "sql_ambiguous"),
    ],
)
async def test_plan_refusals(db, sql, reason):
    with pytest.raises(ds.SqlRejected) as exc:
        ds.gate(sql, "table", await _schema(db))
    assert exc.value.reason == reason


# --------------------------------------------------------------------------- masking

_LEAKS = [
    # expressions
    "SELECT password AS p FROM account",
    "SELECT substr(password, 1, 6) AS x FROM account",
    "SELECT hex(password) AS h FROM account",
    "SELECT unicode(password) AS u FROM account",
    "SELECT length(password) || password AS l FROM account",
    "SELECT json_object('p', password) AS j FROM account",
    "SELECT group_concat(password) AS g FROM account",
    'SELECT max(password) AS m, min("accessToken") AS n FROM account',
    "SELECT CASE WHEN password LIKE 'scrypt%' THEN 'scrypt-yes' END AS c FROM account",
    "SELECT api_key AS k FROM api_clients",
    "SELECT lower(api_key) AS k FROM api_clients",
    'SELECT "twoFactorSecret" AS s FROM "user"',
    "SELECT token AS t FROM session",
    "SELECT id AS sid FROM user_sessions",
    # subqueries
    "SELECT x.p FROM (SELECT password AS p FROM account) x",
    "SELECT (SELECT password FROM account WHERE id = 'a1') AS p",
    "VALUES ((SELECT password FROM account LIMIT 1))",
    "SELECT * FROM notes WHERE tag IN (SELECT token FROM session) OR 1",
    "SELECT * FROM json_each((SELECT json_group_array(password) FROM account))",
    "SELECT * FROM (SELECT * FROM (SELECT * FROM account))",
    # CTEs
    "WITH c(a, b, c, d) AS (SELECT * FROM account) SELECT * FROM c",
    "WITH s AS (SELECT token FROM session) SELECT token AS t FROM s",
    "WITH a AS (SELECT * FROM account), b AS (SELECT password AS p FROM a) SELECT p FROM b",
    "WITH k AS MATERIALIZED (SELECT api_key FROM api_clients) SELECT * FROM k",
    # joins
    'SELECT u.email, a.password AS name FROM "user" u JOIN account a ON a."userId" = u.id',
    "SELECT * FROM notes, account",
    "SELECT * FROM notes NATURAL JOIN account",
    'SELECT * FROM notes LEFT OUTER JOIN "main"."account" ON 1',
    "SELECT * FROM notes CROSS JOIN session",
    "SELECT * FROM notes, (account a)",
    'SELECT s.token FROM "user" u, session s WHERE s."userId" = u.id',
    # compounds and quoting
    "SELECT 1, 2, 3, 4 UNION ALL SELECT * FROM account",
    "SELECT 'x' AS k UNION SELECT token FROM session",
    "SELECT * FROM main.account",
    'SELECT * FROM "account"',
    "SELECT * FROM [account]",
    "SELECT * FROM `account`",
    "SELECT * FROM 'account'",
    "SELECT * FROM account ORDER BY 3",
    # table-valued functions and window functions over a masked column
    "SELECT j.value FROM account, json_each(account.password) j",
    "SELECT j.value FROM account a, json_each(json_array(a.password)) j",
    "SELECT first_value(password) OVER (ORDER BY id) AS f FROM account",
    "SELECT (SELECT group_concat(token) FROM session s2) AS x FROM notes",
    "SELECT * FROM account a WHERE a.password IS NOT NULL",
    # an error oracle: the overflow only fires if the filter matched a real value
    "SELECT abs(-9223372036854775807 - 1) AS x FROM account WHERE password LIKE 'scrypt%'",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", _LEAKS)
async def test_no_expression_reads_a_credential(db, sql):
    res = await _result(db, sql)
    blob = repr(res.rows) + repr(res.data)
    assert not any(s in blob for s in _SECRETS), (sql, blob)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) AS value FROM account WHERE password LIKE 'scrypt%'",
        "SELECT count(*) AS value FROM session WHERE token = 'tok-1'",
        "SELECT count(*) AS value FROM api_clients WHERE substr(api_key, 1, 1) = 'k'",
    ],
)
async def test_filters_on_credentials_only_see_the_mask(db, sql):
    res = await _result(db, sql, "metric")
    assert res.ok and res.data == {"value": 0}


@pytest.mark.asyncio
async def test_login_tables_still_chart(db):
    """Counting sign-ups from the user table is a fine widget; its secrets stay masked."""
    res = await _result(
        db,
        'SELECT "createdAt" AS day, count(*) AS n FROM "user" GROUP BY 1 ORDER BY 1',
        "chart",
    )
    assert res.ok
    assert res.data == [{"label": "2026-10-01", "value": 1}, {"label": "2026-10-03", "value": 1}]
    res = await _result(db, 'SELECT * FROM "user"')
    assert res.rows[0]["twoFactorSecret"] == MASK and res.rows[0]["email"] == "ann@x.io"


# --------------------------------------------------------------------------- caps and deps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("viz", "cap"),
    [("metric", 1), ("stat", 1), ("gauge", 1), ("funnel", 50), ("table", 100), ("chart", 366)],
)
async def test_row_caps(db, viz, cap):
    q = ds.gate("SELECT n AS label, n AS value FROM big", viz, await _schema(db))
    assert q.sql.endswith(f"LIMIT {cap + 1}") and q.row_cap == cap
    res = ds.to_result(q, viz, None, await ds.run_one(db, _DB, q))
    assert len(res.rows) == cap and res.truncated


@pytest.mark.asyncio
async def test_a_short_result_is_not_truncated(db):
    res = await _result(db, "SELECT * FROM notes")
    assert len(res.rows) == 2 and not res.truncated


@pytest.mark.asyncio
async def test_a_caller_limit_cannot_raise_the_cap(db):
    q = ds.gate("SELECT n FROM big LIMIT 100000", "table", await _schema(db))
    res = ds.to_result(q, "table", None, await ds.run_one(db, _DB, q))
    assert len(res.rows) == 100 and res.truncated


@pytest.mark.asyncio
async def test_deps_name_tables_and_columns(db):
    q = ds.gate(
        "SELECT c.plan, sum(o.total) AS t FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE o.status = 'paid' GROUP BY 1",
        "chart",
        await _schema(db),
    )
    assert q.deps == (
        "customers",
        "customers.id",
        "customers.plan",
        "orders",
        "orders.customer_id",
        "orders.id",  # a bare name counts for every table that has it: drift errs safe
        "orders.status",
        "orders.total",
    )
    # A CTE is not a table dependency.
    q = ds.gate("WITH x AS (SELECT total FROM orders) SELECT * FROM x", "table", await _schema(db))
    assert q.deps == ("orders", "orders.total")


# --------------------------------------------------------------------------- heavy


@pytest.mark.asyncio
async def test_a_full_scan_of_a_big_table_is_heavy(db):
    cf = budget.Budgeted(db, "ws1")
    schema = await _schema(db)
    q = ds.gate("SELECT v, count(*) AS n FROM huge GROUP BY v", "chart", schema)
    why = await ds.heavy(cf, _DB, q, schema)
    assert why is not None and "huge" in why


@pytest.mark.asyncio
async def test_an_indexed_lookup_or_a_small_table_is_not_heavy(db):
    cf = budget.Budgeted(db, "ws1")
    schema = await _schema(db)
    for sql in ("SELECT v FROM huge WHERE id = 5", "SELECT count(*) FROM big", "SELECT 1"):
        assert await ds.heavy(cf, _DB, ds.gate(sql, "metric", schema), schema) is None, sql


@pytest.mark.asyncio
async def test_a_failing_explain_is_not_heavy(db):
    schema = await _schema(db)
    q = ds.gate("SELECT v FROM huge", "table", schema)
    db.fail_on = "EXPLAIN"
    assert await ds.heavy(budget.Budgeted(db, "ws1"), _DB, q, schema) is None


# --------------------------------------------------------------------------- running


@pytest.mark.asyncio
async def test_batch_runs_once_and_keeps_column_order(db):
    schema = await _schema(db)
    qs = [
        ds.gate("SELECT status, count(*) AS n FROM orders GROUP BY 1 ORDER BY 1", "chart", schema),
        ds.gate("SELECT count(*) AS n FROM customers", "metric", schema),
    ]
    calls = db.calls
    raws = await ds.run_batch(db, _DB, qs)
    assert db.calls == calls + 1
    res = ds.to_result(qs[0], "chart", None, raws[0])
    assert res.columns == ["status", "n"]
    assert res.data == [{"label": "paid", "value": 2}, {"label": "refunded", "value": 1}]
    assert ds.to_result(qs[1], "metric", None, raws[1]).data == {"value": 2}


@pytest.mark.asyncio
async def test_one_bad_widget_does_not_blank_the_rest(db):
    schema = await _schema(db)
    qs = [
        ds.gate("SELECT count(*) AS n FROM orders", "metric", schema),
        ds.gate("SELECT nope FROM orders", "metric", schema),  # fails in D1, not the gate
        ds.gate("SELECT count(*) AS n FROM notes", "metric", schema),
    ]
    raws = await ds.run_batch(db, _DB, qs)
    results = [ds.to_result(q, "metric", None, r) for q, r in zip(qs, raws, strict=True)]
    assert [r.ok for r in results] == [True, False, True]
    assert results[0].data == {"value": 3} and results[2].data == {"value": 2}
    assert "nope" in results[1].error and _DB not in results[1].error
    assert results[1].error.endswith("SQLITE_ERROR")


@pytest.mark.asyncio
async def test_cloudflare_429_raises_busy(db):
    schema = await _schema(db)
    q = ds.gate("SELECT 1", "metric", schema)

    async def _busy(**_kw: Any):
        raise ValidationError("sites.cloudflare_error", "Cloudflare API 429: Too many requests")

    db.query_d1_raw = _busy  # type: ignore[method-assign]
    with pytest.raises(budget.DataBusy):
        await ds.run_one(db, _DB, q)


@pytest.mark.asyncio
async def test_an_outage_is_a_generic_widget_error(db):
    schema = await _schema(db)
    q = ds.gate("SELECT 1", "metric", schema)

    async def _down(**_kw: Any):
        raise ValidationError("sites.cloudflare_error", "Cloudflare API 503: acct 1234 down")

    db.query_d1_raw = _down  # type: ignore[method-assign]
    assert await ds.run_one(db, _DB, q) == ds.UNREACHABLE


# --------------------------------------------------------------------------- shaping

_COLS = ["day", "paid", "refunded", "delta"]
_ROWS = [
    {"day": "2026-10-01", "paid": 3, "refunded": 1, "delta": 2},
    {"day": "2026-10-02", "paid": 5, "refunded": 0, "delta": -1},
]


def test_chart_series():
    data, lossy = ds.shape_data(
        "chart", WidgetMapping(label_col="day", series_cols=["paid", "refunded"]), _COLS, _ROWS
    )
    assert not lossy
    assert data == [
        {"label": "2026-10-01", "series": {"paid": 3, "refunded": 1}},
        {"label": "2026-10-02", "series": {"paid": 5, "refunded": 0}},
    ]


def test_chart_defaults_to_first_two_columns():
    data, _ = ds.shape_data("chart", None, _COLS, _ROWS)
    assert data == [{"label": "2026-10-01", "value": 3}, {"label": "2026-10-02", "value": 5}]


def test_metric_takes_the_first_row_and_delta():
    data, _ = ds.shape_data("metric", WidgetMapping(value_col="paid"), _COLS, _ROWS)
    assert data == {"value": 3, "delta": 2}
    assert ds.shape_data("metric", None, ["n"], []) == ({"value": None}, False)


def test_gauge_carries_max():
    data, _ = ds.shape_data("gauge", None, ["v", "max"], [{"v": 4, "max": 10}])
    assert data == {"value": 4, "max": 10}


def test_table_keeps_the_chosen_columns_in_order():
    data, _ = ds.shape_data("table", WidgetMapping(columns=["delta", "day"]), _COLS, _ROWS)
    assert data == [{"delta": 2, "day": "2026-10-01"}, {"delta": -1, "day": "2026-10-02"}]


@pytest.mark.parametrize(
    ("viz", "mapping"),
    [
        ("chart", WidgetMapping(label_col="nope")),
        ("chart", WidgetMapping(value_col="nope")),
        ("chart", WidgetMapping(series_cols=["paid", "nope"])),
        ("metric", WidgetMapping(value_col="nope")),
        ("table", WidgetMapping(columns=["day", "nope"])),
    ],
)
def test_a_missing_mapping_column_is_an_error(viz, mapping):
    with pytest.raises(ds.MappingError):
        ds.shape_data(viz, mapping, _COLS, _ROWS)


def test_chart_values_become_numbers():
    rows = [
        {"l": 1, "v": "12.5"},
        {"l": 2, "v": "abc"},
        {"l": 3, "v": MASK},
        {"l": 4, "v": None},
        {"l": 5, "v": True},
        {"l": None, "v": float("nan")},
    ]
    data, lossy = ds.shape_data("chart", None, ["l", "v"], rows)
    assert [p["value"] for p in data] == [12.5, None, None, None, 1, None]
    assert [p["label"] for p in data] == ["1", "2", "3", "4", "5", ""]
    assert not lossy


@pytest.mark.asyncio
async def test_big_integers_flag_precision(db):
    db.execute("INSERT INTO big VALUES (?)", (2**60,))
    res = await _result(db, "SELECT 'x' AS l, max(n) AS v FROM big", "chart")
    assert res.precision_loss and res.data == [{"label": "x", "value": float(2**60)}]
    res = await _result(db, "SELECT max(n) AS v FROM big", "metric")
    assert res.data == {"value": str(2**60)}  # metrics show the exact digits


@pytest.mark.asyncio
async def test_long_cells_and_blobs_are_clipped(db):
    res = await _result(db, "SELECT '" + "y" * 3000 + "' AS t, x'00ff' AS b")
    assert len(res.rows[0]["t"]) == ds.CELL_MAX + 1 and res.rows[0]["b"] == "(binary)"


@pytest.mark.asyncio
async def test_a_mapping_error_is_a_failed_result_with_columns(db):
    res = await _result(db, "SELECT 1 AS a", "metric", WidgetMapping(value_col="b"))
    assert not res.ok and res.columns == ["a"] and "'b'" in res.error


def test_an_error_text_is_a_failed_result():
    q = ds.GatedQuery("SELECT 1", [], frozenset(), frozenset(), (), 1)
    res = ds.to_result(q, "metric", None, "boom")
    assert not res.ok and res.error == "boom" and res.data is None


# --------------------------------------------------------------------------- adversarial

_ADVERSARIAL = [
    # second statements smuggled through comments, strings and odd whitespace
    "SELECT 1 /*/ ; DROP TABLE notes /*/",
    "SELECT 1 ; DROP TABLE notes",
    "SELECT '\\'; DROP TABLE notes; --'",
    'SELECT "a;b" FROM notes',
    "SELECT 1 --\rDROP TABLE notes",
    # writes hidden in CTEs, subqueries and RETURNING
    "WITH d AS (DELETE FROM notes RETURNING *) SELECT * FROM d",
    "SELECT * FROM (INSERT INTO notes (body) VALUES ('x') RETURNING *)",
    "SELECT 1 UNION SELECT changes() FROM (UPDATE notes SET tag = 'x' RETURNING 1)",
    # sneaking at the schema
    "SELECT * FROM main.sqlite_master",
    "SELECT * FROM temp.sqlite_master",
    'SELECT * FROM "main"."_cf_KV"',
    "SELECT name FROM pragma_table_list",
    "SELECT * FROM notes INDEXED BY sqlite_autoindex_x",
    # ATTACH under another case and spelling
    "attach 'x' as y",
    "SeLeCt 1; AtTaCh 'x' AS y",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", _ADVERSARIAL)
async def test_adversarial_statements_never_run(db, sql):
    before = len(db.sql)
    schema = await _schema(db)
    try:
        q = ds.gate(sql, "table", schema)
    except ds.SqlRejected:
        assert len(db.sql) == before or all(
            s.startswith(("PRAGMA", "SELECT type")) for s, _ in db.sql[before:]
        )
        return
    # Whatever passed is a harmless read: it runs, and the tables are untouched.
    await ds.run_one(db, _DB, q)
    assert db.rows("SELECT count(*) AS n FROM notes") == [{"n": 2}]
    assert ";" not in q.sql


# --------------------------------------------------------------------------- pii


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("orders", "email"),
        ("orders", "customerEmail"),
        ("orders", "billing_address"),
        ("orders", "phone_number"),
        ("orders", "zip"),
        ("orders", "postal_code"),
        ("orders", "ip_address"),
        ("orders", "dateOfBirth"),
        ("orders", "first_name"),
        ("orders", "lastName"),
        ("orders", "card_number"),
        ("orders", "iban"),
        ("orders", "ssn"),
        ("orders", "latitude"),
        ("customers", "name"),
        ("users", "username"),
        ("bookings", "name"),
        ("contact_submissions", "name"),
        ("auth_user", "display_name"),
    ],
)
def test_pii_names(table, column):
    assert pii.is_pii(table, column)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("products", "name"),
        ("orders", "total"),
        ("orders", "status"),
        ("orders", "created_at"),
        ("events", "kind"),
        ("posts", "title"),
        ("plans", "display_name"),
    ],
)
def test_not_pii(table, column):
    assert not pii.is_pii(table, column)


def test_sensitive_covers_masked_columns():
    assert pii.sensitive("api_clients", "api_key", frozenset({"api_key"}))
    assert pii.sensitive("orders", "email")
    assert not pii.sensitive("orders", "total", frozenset({"api_key"}))
