# tests/ee/sites/test_site_data_write.py — the write half of the Sites Data dashboard:
# ``sites.site_data.writes`` (coercion, SQL plans) and the write operations of
# ``sites.data_service`` / ``sites.data_router`` (insert, update with the conflict
# check, delete, duplicate, row history).
#
# D1 is a real in-memory SQLite (``d1_sqlite_fake.SqliteD1``) whose batches are
# atomic like live D1, so ``IS``, ``RETURNING``, affinity, FK enforcement and the
# all-or-nothing batch run for real. Audit rows go to a mongomock Beanie database
# (``beanie_test_db``) where a test reads them back; elsewhere ``record`` is stubbed.
#
# Covers TDD §11 "PR 3 (writes)": permission (member 403, admin, override), insert
# (required fields, auto columns), update (conflict 409 with the current row, NULL
# expected, missing row 404), delete (90-key chunks, >500 refused), login-table rules
# (masked column, read-only session, user delete cascading sessions + accounts in one
# batch), coercion per kind in both client modes, audit masking, row history, and the
# adversarial pass (masked column writes, another table, conflict bypass, >500 keys).
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError, NotFound  # noqa: E402
from pocketpaw_ee.sites import data_service  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.site_data import budget, d1, writes  # noqa: E402

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
CREATE TABLE orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER REFERENCES customers(id),
  note TEXT,
  total REAL DEFAULT 0,
  is_paid INTEGER DEFAULT 0,
  created_at INTEGER,
  shipped_at TEXT,
  meta_json TEXT,
  photo BLOB,
  total_x2 REAL GENERATED ALWAYS AS (total * 2) VIRTUAL
);
CREATE TABLE notes (body TEXT NOT NULL, tag TEXT);
CREATE TABLE skus (sku TEXT PRIMARY KEY, qty INTEGER NOT NULL);
CREATE TABLE pair (a TEXT, b TEXT, PRIMARY KEY (a, b));
CREATE TABLE "user" (id TEXT PRIMARY KEY, name TEXT, email TEXT, "emailVerified" INTEGER,
  image TEXT, "createdAt" INTEGER, "twoFactorSecret" TEXT);
CREATE TABLE session (id TEXT PRIMARY KEY, token TEXT, "userId" TEXT REFERENCES "user"(id));
CREATE TABLE account (id TEXT PRIMARY KEY, "userId" TEXT REFERENCES "user"(id),
  password TEXT, "accessToken" TEXT);
CREATE TABLE verification (id TEXT PRIMARY KEY, identifier TEXT, value TEXT);
INSERT INTO customers (id, name, email) VALUES (1, 'Ada', 'ada@x.io'), (2, 'Bob', NULL);
INSERT INTO orders (customer_id, note, total, is_paid, created_at, shipped_at)
  VALUES (1, 'first', 9.5, 1, 1767225600, '2026-01-01T10:00:00Z'),
         (2, NULL, 50, 0, 1767312000, '2026-01-02T10:00:00Z');
INSERT INTO notes (body, tag) VALUES ('hello', 'a'), ('bye', 'b');
INSERT INTO skus VALUES ('A-1', 4);
INSERT INTO "user" VALUES ('u1', 'Ann', 'ann@x.io', 1, NULL, 1767225600, 'totp-secret'),
                         ('u2', 'Ben', 'ben@x.io', 0, NULL, 1767225600, NULL);
INSERT INTO session VALUES ('s1', 'tok-1', 'u1'), ('s2', 'tok-2', 'u1'), ('s3', 'tok-3', 'u2');
INSERT INTO account VALUES ('a1', 'u1', 'scrypt$x', 'at-1'), ('a2', 'u2', 'scrypt$y', NULL);
INSERT INTO verification VALUES ('v1', 'ann@x.io', 'otp');
"""


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
    return SqliteD1(_DDL, database_id=_DB)


@pytest.fixture
def audit(monkeypatch):
    """Every audit event the writes record, without Mongo."""
    events: list[dict[str, Any]] = []

    async def _record(workspace_id, actor_id, action, **kw):
        events.append({"workspace_id": workspace_id, "actor_id": actor_id, "action": action, **kw})

    from pocketpaw_ee.cloud.audit import service as audit_service

    monkeypatch.setattr(audit_service, "record", _record)
    return events


class _pocket:  # noqa: N801 — reads like the patch it is
    """The pocket read (and, for writes, the pocket edit check) stubbed."""

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


def _kw(cf, table, **kw):
    return {
        "workspace_id": "ws1",
        "user_id": "u-admin",
        "pocket_id": "pk1",
        "table": table,
        "_cloudflare": cf,
        **kw,
    }


async def _insert(cf, table, values, wire=_PROJECT_WIRE):
    with _pocket(wire):
        return await data_service.insert_row(**_kw(cf, table, values=values))


async def _update(cf, table, key, set_, expected, wire=_PROJECT_WIRE):
    with _pocket(wire):
        return await data_service.update_row(**_kw(cf, table, key=key, set=set_, expected=expected))


async def _delete(cf, table, keys, wire=_PROJECT_WIRE):
    with _pocket(wire):
        return await data_service.delete_rows(**_kw(cf, table, keys=keys))


async def _duplicate(cf, table, key, values=None, wire=_PROJECT_WIRE):
    with _pocket(wire):
        return await data_service.duplicate_row(**_kw(cf, table, key=key, values=values))


async def _history(cf, table, key, wire=_PROJECT_WIRE):
    with _pocket(wire):
        return await data_service.row_history(**_kw(cf, table, key=key))


def _writes(cf: SqliteD1) -> list[str]:
    return [s for s, _ in cf.sql if s.split(" ", 1)[0] in ("INSERT", "UPDATE", "DELETE")]


async def _raises(code: str, status: int, coro) -> CloudError:
    with pytest.raises(CloudError) as exc:
        await coro
    assert exc.value.code == code, (exc.value.code, exc.value.message, exc.value.details)
    assert exc.value.status_code == status
    return exc.value


# --------------------------------------------------------------------------- insert


@pytest.mark.asyncio
async def test_insert_returns_the_row_with_its_key(site_doc, db, audit):
    res = await _insert(db, "customers", {"name": "Cy", "email": "cy@x.io"})
    assert res.row == {"id": 3, "name": "Cy", "email": "cy@x.io", "vip": 0, "__key": "3"}
    assert db.rows("SELECT name FROM customers WHERE id = 3") == [{"name": "Cy"}]
    assert any(s.startswith('INSERT INTO "customers"') and "RETURNING" in s for s in _writes(db))


@pytest.mark.asyncio
async def test_insert_required_field_missing_is_422_per_field(site_doc, db, audit):
    err = await _raises("sites.data_invalid_values", 422, _insert(db, "customers", {"email": "x"}))
    assert set(err.details["fields"]) == {"name"}
    err = await _raises("sites.data_invalid_values", 422, _insert(db, "skus", {}))
    assert set(err.details["fields"]) == {"sku", "qty"}
    err = await _raises("sites.data_invalid_values", 422, _insert(db, "customers", {"name": None}))
    assert set(err.details["fields"]) == {"name"}
    assert _writes(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("col", ["id", "total_x2"])
async def test_insert_auto_column_is_422(site_doc, db, audit, col):
    err = await _raises(
        "sites.data_invalid_values", 422, _insert(db, "orders", {col: 7, "note": "x"})
    )
    assert set(err.details["fields"]) == {col}
    assert _writes(db) == []


@pytest.mark.asyncio
async def test_insert_unknown_or_blob_column_is_422(site_doc, db, audit):
    err = await _raises(
        "sites.data_invalid_values", 422, _insert(db, "orders", {"nope": 1, "photo": "AA=="})
    )
    assert set(err.details["fields"]) == {"nope", "photo"}


@pytest.mark.asyncio
async def test_insert_rowid_table_addresses_by_rowid(site_doc, db, audit):
    res = await _insert(db, "notes", {"body": "new"})
    assert res.row["__key"] == "3" and res.row["body"] == "new"
    up = await _update(db, "notes", "3", {"tag": "z"}, {"tag": None})
    assert up.row["tag"] == "z"


@pytest.mark.asyncio
async def test_insert_text_key_table(site_doc, db, audit):
    res = await _insert(db, "skus", {"sku": "B-2", "qty": "7"})
    assert res.row == {"sku": "B-2", "qty": 7, "__key": "B-2"}


@pytest.mark.asyncio
async def test_unique_violation_is_a_clean_422(site_doc, db, audit):
    await _raises("sites.data_write_failed", 422, _insert(db, "skus", {"sku": "A-1", "qty": 1}))


@pytest.mark.asyncio
async def test_write_invalidates_the_inventory_count_cache(site_doc, db, audit):
    schema = await d1.introspect(budget.Budgeted(db, "ws1"), _DB)
    counts = await d1.count_tables(db, _DB, schema, ["customers"])
    assert counts == {"customers": 2}
    await _insert(db, "customers", {"name": "Cy"})
    counts = await d1.count_tables(db, _DB, schema, ["customers"])
    assert counts == {"customers": 3}


# --------------------------------------------------------------------------- coercion


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "true", 1, "1", "yes", "on"])
async def test_boolean_is_stored_as_integer_one_never_text(site_doc, db, audit, value):
    res = await _update(db, "orders", "2", {"is_paid": value}, {"is_paid": 0})
    assert res.row["is_paid"] == 1
    stored = db.rows("SELECT is_paid, typeof(is_paid) AS t FROM orders WHERE id = 2")
    assert stored == [{"is_paid": 1, "t": "integer"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, "false", 0, "0", "no", "off"])
async def test_boolean_false_is_zero(site_doc, db, audit, value):
    await _update(db, "orders", "1", {"is_paid": value}, {"is_paid": True})
    assert db.rows("SELECT is_paid AS v, typeof(is_paid) AS t FROM orders WHERE id = 1") == [
        {"v": 0, "t": "integer"}
    ]


@pytest.mark.asyncio
async def test_boolean_rejects_nonsense(site_doc, db, audit):
    err = await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "orders", "1", {"is_paid": "maybe"}, {"is_paid": 1}),
    )
    assert "is_paid" in err.details["fields"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("12", 12.0),  # string-params client mode
        (12, 12.0),  # typed-params client mode
        ("3.25", 3.25),
    ],
)
async def test_real_parses_numbers_in_both_client_modes(site_doc, db, audit, value, stored):
    await _update(db, "orders", "1", {"total": value}, {"total": 9.5})
    assert db.rows("SELECT total, typeof(total) AS t FROM orders WHERE id = 1") == [
        {"total": stored, "t": "real"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["12", 12, 12.0])
async def test_integer_parses_in_both_client_modes(site_doc, db, audit, value):
    await _update(db, "skus", "A-1", {"qty": value}, {"qty": "4"})
    assert db.rows("SELECT qty, typeof(qty) AS t FROM skus") == [{"qty": 12, "t": "integer"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["abc", "1.5", 1.5, [1], {"a": 1}])
async def test_integer_rejects_non_integers(site_doc, db, audit, value):
    await _raises(
        "sites.data_invalid_values", 422, _update(db, "skus", "A-1", {"qty": value}, {"qty": 4})
    )


@pytest.mark.asyncio
async def test_epoch_seconds_datetime_matches_the_stored_form(site_doc, db, audit):
    # created_at holds epoch seconds; an ISO string from the date picker stores seconds.
    await _update(
        db, "orders", "1", {"created_at": "2026-02-01T00:00:00Z"}, {"created_at": 1767225600}
    )
    assert db.rows("SELECT created_at AS v, typeof(created_at) AS t FROM orders WHERE id = 1") == [
        {"v": 1769904000, "t": "integer"}
    ]
    # Milliseconds sent to a seconds column are scaled down.
    await _update(db, "orders", "1", {"created_at": 1769904000123}, {"created_at": 1769904000})
    assert db.rows("SELECT created_at AS v FROM orders WHERE id = 1") == [{"v": 1769904000}]


@pytest.mark.asyncio
async def test_epoch_ms_datetime(site_doc, audit):
    cf = SqliteD1(
        "CREATE TABLE ev (id INTEGER PRIMARY KEY, created_at INTEGER);"
        "INSERT INTO ev VALUES (1, 1767225600000);",
        database_id=_DB,
    )
    await _update(cf, "ev", "1", {"created_at": "2026-02-01"}, {"created_at": 1767225600000})
    assert cf.rows("SELECT created_at AS v FROM ev") == [{"v": 1769904000000}]


@pytest.mark.asyncio
async def test_iso_datetime_keeps_the_stored_style(site_doc, db, audit):
    # shipped_at stores 'YYYY-MM-DDTHH:MM:SSZ'; a number or another ISO style follows it.
    await _update(
        db, "orders", "1", {"shipped_at": 1769904000}, {"shipped_at": "2026-01-01T10:00:00Z"}
    )
    assert db.rows("SELECT shipped_at AS v FROM orders WHERE id = 1") == [
        {"v": "2026-02-01T00:00:00Z"}
    ]
    await _update(
        db,
        "orders",
        "1",
        {"shipped_at": "2026-03-01 08:30:00+02:00"},
        {"shipped_at": "2026-02-01T00:00:00Z"},
    )
    assert db.rows("SELECT shipped_at AS v FROM orders WHERE id = 1") == [
        {"v": "2026-03-01T06:30:00Z"}
    ]


@pytest.mark.asyncio
async def test_bad_datetime_is_422(site_doc, db, audit):
    await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "orders", "1", {"shipped_at": "next tuesday"}, {"shipped_at": None}),
    )


@pytest.mark.asyncio
async def test_json_is_validated_and_normalised(site_doc, db, audit):
    await _update(db, "orders", "1", {"meta_json": {"a": [1, 2]}}, {"meta_json": None})
    assert db.rows("SELECT meta_json AS v FROM orders WHERE id = 1") == [{"v": '{"a": [1, 2]}'}]
    await _update(db, "orders", "1", {"meta_json": '{"b":true}'}, {"meta_json": '{"a": [1, 2]}'})
    assert db.rows("SELECT meta_json AS v FROM orders WHERE id = 1") == [{"v": '{"b": true}'}]
    await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "orders", "1", {"meta_json": "{not json"}, {"meta_json": '{"b": true}'}),
    )


@pytest.mark.asyncio
async def test_null_binds_as_null(site_doc, db, audit):
    await _update(db, "orders", "1", {"note": None}, {"note": "first"})
    assert db.rows("SELECT note IS NULL AS n FROM orders WHERE id = 1") == [{"n": 1}]


@pytest.mark.asyncio
async def test_not_null_column_cannot_be_cleared(site_doc, db, audit):
    err = await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "customers", "1", {"name": None}, {"name": "Ada"}),
    )
    assert set(err.details["fields"]) == {"name"}


def test_coerce_unit():
    assert writes.coerce("boolean", "", True) == 1
    assert writes.coerce("integer", "", True) == 1  # a JSON true never binds as text
    assert writes.coerce("text", "", 5) == "5"
    assert writes.coerce("text", "", None) is None
    assert writes.coerce("real", "", "1e3") == 1000.0
    assert writes.coerce("date", "iso", "2026-05-04T23:00:00Z") == "2026-05-04"
    assert writes.coerce("datetime", "iso", "2026-05-04T23:00:00Z") == "2026-05-04 23:00:00"
    with pytest.raises(ValueError):
        writes.coerce("real", "", "nan")
    with pytest.raises(ValueError):
        writes.coerce("blob", "", "x")


# --------------------------------------------------------------------------- update


@pytest.mark.asyncio
async def test_update_with_matching_expected_lands(site_doc, db, audit):
    res = await _update(db, "customers", "1", {"email": "new@x.io"}, {"email": "ada@x.io"})
    assert res.row["email"] == "new@x.io" and res.row["__key"] == "1"
    sql = next(s for s in _writes(db) if s.startswith("UPDATE"))
    assert '"email" IS ?' in sql and "RETURNING" in sql


@pytest.mark.asyncio
async def test_update_conflict_is_409_with_the_current_row(site_doc, db, audit):
    db.execute("UPDATE customers SET email = 'changed@x.io' WHERE id = 1")
    err = await _raises(
        "sites.data_conflict",
        409,
        _update(db, "customers", "1", {"email": "mine@x.io"}, {"email": "ada@x.io"}),
    )
    assert err.details["current"]["email"] == "changed@x.io"
    assert err.details["current"]["__key"] == "1"
    assert db.rows("SELECT email FROM customers WHERE id = 1") == [{"email": "changed@x.io"}]
    assert audit == []


@pytest.mark.asyncio
async def test_null_expected_matches_null(site_doc, db, audit):
    res = await _update(db, "customers", "2", {"email": "bob@x.io"}, {"email": None})
    assert res.row["email"] == "bob@x.io"
    # and NULL expected does NOT match a non-null value
    await _raises(
        "sites.data_conflict",
        409,
        _update(db, "customers", "2", {"email": "again"}, {"email": None}),
    )


@pytest.mark.asyncio
async def test_update_missing_row_is_404(site_doc, db, audit):
    with pytest.raises(NotFound):
        await _update(db, "customers", "99", {"email": "x"}, {"email": None})


@pytest.mark.asyncio
async def test_update_key_from_url_binds_as_the_key_type(site_doc, db, audit):
    await _update(db, "customers", "1", {"name": "Ada L"}, {"name": "Ada"})
    sql, params = next((s, p) for s, p in db.sql if s.startswith("UPDATE"))
    assert params[1] == 1 and isinstance(params[1], int)
    with pytest.raises(NotFound):  # a non-integer can never be an INTEGER PRIMARY KEY
        await _update(db, "customers", "1 OR 1=1", {"name": "x"}, {"name": "Ada L"})


@pytest.mark.asyncio
async def test_update_cannot_change_the_key_or_an_auto_column(site_doc, db, audit):
    err = await _raises(
        "sites.data_invalid_values", 422, _update(db, "skus", "A-1", {"sku": "Z"}, {"sku": "A-1"})
    )
    assert "sku" in err.details["fields"]
    await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "orders", "1", {"total_x2": 1}, {"total_x2": 19.0}),
    )


@pytest.mark.asyncio
async def test_update_without_expected_for_every_set_column_is_422(site_doc, db, audit):
    """Adversarial: dropping ``expected`` must not bypass the conflict check."""
    err = await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "customers", "1", {"email": "x", "name": "y"}, {"email": "ada@x.io"}),
    )
    assert set(err.details["fields"]) == {"name"}
    await _raises(
        "sites.data_invalid_values", 422, _update(db, "customers", "1", {"email": "x"}, {})
    )
    await _raises("sites.data_nothing_to_change", 422, _update(db, "customers", "1", {}, {}))
    assert _writes(db) == []


@pytest.mark.asyncio
async def test_expected_may_pin_more_columns_than_it_sets(site_doc, db, audit):
    await _raises(
        "sites.data_conflict",
        409,
        _update(db, "customers", "1", {"email": "x"}, {"email": "ada@x.io", "name": "Someone"}),
    )


@pytest.mark.asyncio
async def test_expected_unknown_column_is_422(site_doc, db, audit):
    err = await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "customers", "1", {"email": "x"}, {"email": "ada@x.io", "nope": 1}),
    )
    assert "nope" in err.details["fields"]


# --------------------------------------------------------------------------- delete


@pytest.mark.asyncio
async def test_delete_returns_count_and_keys(site_doc, db, audit):
    res = await _delete(db, "notes", [1, "2", 2])
    assert res.deleted == 2 and sorted(res.keys) == [1, 2]
    assert db.rows("SELECT COUNT(*) AS n FROM notes") == [{"n": 0}]
    # A bulk delete takes a restore point first: restoring it brings both rows back.
    assert res.restore_point
    await db.d1_restore(_DB, bookmark=res.restore_point)
    assert db.rows("SELECT COUNT(*) AS n FROM notes") == [{"n": 2}]


@pytest.mark.asyncio
async def test_single_row_delete_has_no_restore_point(site_doc, db, audit):
    res = await _delete(db, "notes", [1, "1"])
    assert res.deleted == 1 and res.restore_point is None
    assert db.restores == [] and db._snapshots == []


@pytest.mark.asyncio
async def test_bulk_delete_without_time_travel_still_deletes(site_doc, db, audit):
    db.version = "alpha"
    res = await _delete(db, "notes", [1, 2])
    assert res.deleted == 2 and res.restore_point is None


@pytest.mark.asyncio
async def test_bulk_delete_audits_its_restore_point(site_doc, db, audit):
    res = await _delete(db, "notes", [1, 2])
    assert [e["metadata"]["restore_point"] for e in audit] == [res.restore_point] * 2


@pytest.mark.asyncio
async def test_delete_chunks_at_90_keys_in_one_batch(site_doc, audit):
    cf = SqliteD1("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);", database_id=_DB)
    cf.executemany("INSERT INTO t (v) VALUES (?)", [("x",)] * 300)
    calls = cf.calls
    res = await _delete(cf, "t", list(range(1, 201)))
    assert res.deleted == 200
    deletes = [(s, p) for s, p in cf.sql if s.startswith("DELETE")]
    assert [len(p) for _, p in deletes] == [90, 90, 20]
    assert cf.rows("SELECT COUNT(*) AS n FROM t") == [{"n": 100}]
    # introspection (master + PRAGMA batch), the restore point and ONE delete batch
    assert cf.calls - calls == 4


@pytest.mark.asyncio
async def test_delete_more_than_500_keys_is_422(site_doc, db, audit):
    await _raises("sites.data_too_many_keys", 422, _delete(db, "notes", list(range(501))))
    await _raises("sites.data_invalid_values", 422, _delete(db, "notes", []))
    assert _writes(db) == []


@pytest.mark.asyncio
async def test_delete_500_distinct_keys_padded_with_duplicates_is_still_capped(site_doc, db, audit):
    """Adversarial: the cap is on what was sent, not on what's left after dedupe."""
    await _raises("sites.data_too_many_keys", 422, _delete(db, "notes", [1] * 600))


@pytest.mark.asyncio
async def test_delete_is_atomic_across_chunks(site_doc, audit):
    cf = SqliteD1("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);", database_id=_DB)
    cf.executemany("INSERT INTO t (v) VALUES (?)", [("x",)] * 200)
    original = cf._run
    seen = {"n": 0}

    def _fail_third(sql, params):
        if sql.startswith("DELETE"):
            seen["n"] += 1
            if seen["n"] == 3:
                raise __import__("sqlite3").OperationalError("boom")
        return original(sql, params)

    cf._run = _fail_third  # type: ignore[method-assign]
    await _raises("sites.data_write_failed", 422, _delete(cf, "t", list(range(1, 200))))
    assert cf.rows("SELECT COUNT(*) AS n FROM t") == [{"n": 200}]
    assert audit == []


@pytest.mark.asyncio
async def test_delete_blocked_by_a_foreign_key_is_a_clean_422(site_doc, db, audit):
    await _raises("sites.data_write_failed", 422, _delete(db, "customers", [1]))
    assert db.rows("SELECT COUNT(*) AS n FROM customers") == [{"n": 2}]


# --------------------------------------------------------------------------- duplicate


@pytest.mark.asyncio
async def test_duplicate_drops_auto_and_key(site_doc, db, audit):
    res = await _duplicate(db, "orders", "1")
    row = res.row
    assert row["id"] == 3 and row["__key"] == "3"
    assert row["note"] == "first" and row["total"] == 9.5 and row["total_x2"] == 19.0
    assert db.rows("SELECT COUNT(*) AS n FROM orders") == [{"n": 3}]


@pytest.mark.asyncio
async def test_duplicate_text_key_needs_a_new_key(site_doc, db, audit):
    err = await _raises("sites.data_invalid_values", 422, _duplicate(db, "skus", "A-1"))
    assert set(err.details["fields"]) == {"sku"}
    res = await _duplicate(db, "skus", "A-1", {"sku": "A-2"})
    assert res.row == {"sku": "A-2", "qty": 4, "__key": "A-2"}


@pytest.mark.asyncio
async def test_duplicate_missing_row_is_404(site_doc, db, audit):
    with pytest.raises(NotFound):
        await _duplicate(db, "orders", "99")


# --------------------------------------------------------------------------- read-only tables


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["session", "account", "verification"])
async def test_session_account_verification_are_read_only(site_doc, db, audit, table):
    for coro in (
        _insert(db, table, {"id": "x"}),
        _update(db, table, "s1", {"id": "y"}, {"id": "s1"}),
        _delete(db, table, ["s1"]),
        _duplicate(db, table, "s1"),
    ):
        err = await _raises("sites.data_read_only", 403, coro)
        assert err.details["reason"] == "login_table"
    assert _writes(db) == []


@pytest.mark.asyncio
async def test_composite_key_table_is_read_only(site_doc, db, audit):
    err = await _raises("sites.data_read_only", 422, _insert(db, "pair", {"a": "1", "b": "2"}))
    assert err.details["reason"] == "no_key"


@pytest.mark.asyncio
async def test_unknown_table_is_404_before_any_write_sql(site_doc, db, audit):
    for coro in (
        _insert(db, "nope", {"a": 1}),
        _update(db, "nope", "1", {"a": 1}, {"a": 1}),
        _delete(db, "nope", [1]),
        _duplicate(db, "nope", "1"),
        _insert(db, "_paw_migrations", {"name": "x"}),
        _insert(db, 'customers"; DROP TABLE customers; --', {"name": "x"}),
    ):
        with pytest.raises(NotFound):
            await coro
    assert _writes(db) == []


# --------------------------------------------------------------------------- user table


@pytest.mark.asyncio
async def test_user_table_edits_only_profile_columns(site_doc, db, audit):
    res = await _update(db, "user", "u1", {"name": "Ann B"}, {"name": "Ann"})
    assert res.row["name"] == "Ann B"
    assert res.row["twoFactorSecret"] == "•••"  # the returned row is masked
    for col, val in (("id", "u9"), ("createdAt", 1), ("emailVerified", 1)):
        err = await _raises(
            "sites.data_invalid_values", 422, _update(db, "user", "u1", {col: val}, {col: None})
        )
        assert col in err.details["fields"]


@pytest.mark.asyncio
async def test_masked_column_is_never_writable(site_doc, db, audit):
    """Adversarial: a credential column, as a set target, an expected value, or an
    insert / duplicate value, is refused before any SQL."""
    for coro in (
        _update(db, "user", "u1", {"twoFactorSecret": "mine"}, {"twoFactorSecret": "totp-secret"}),
        _update(db, "user", "u1", {"name": "x"}, {"name": "Ann", "twoFactorSecret": "totp-secret"}),
        _update(db, "user", "u1", {"name": "x"}, {"name": "Ann", "twoFactorSecret": "•••"}),
    ):
        err = await _raises("sites.data_invalid_values", 422, coro)
        assert "twoFactorSecret" in err.details["fields"]
    assert _writes(db) == []
    assert db.rows('SELECT "twoFactorSecret" AS v FROM "user" WHERE id = ?', ["u1"]) == [
        {"v": "totp-secret"}
    ]


@pytest.mark.asyncio
async def test_users_cannot_be_added_or_duplicated(site_doc, db, audit):
    for coro in (_insert(db, "user", {"name": "x"}), _duplicate(db, "user", "u1")):
        err = await _raises("sites.data_read_only", 403, coro)
        assert err.details["reason"] == "login_table"


@pytest.mark.asyncio
async def test_user_delete_cascades_sessions_and_accounts_in_one_batch(site_doc, db, audit):
    calls = db.calls
    res = await _delete(db, "user", ["u1"])
    assert res.deleted == 1
    assert db.rows('SELECT id FROM "user"') == [{"id": "u2"}]
    assert db.rows("SELECT id FROM session ORDER BY id") == [{"id": "s3"}]
    assert db.rows("SELECT id FROM account") == [{"id": "a2"}]
    assert db.calls - calls == 3  # master, PRAGMA batch, one write batch
    deletes = [s for s in _writes(db)]
    assert deletes[-1].startswith('DELETE FROM "user"')
    assert {d.split('"')[1] for d in deletes[:-1]} == {"session", "account"}


@pytest.mark.asyncio
async def test_user_delete_cascade_is_atomic(site_doc, db, audit):
    db.fail_on = 'DELETE FROM "user"'
    await _raises("sites.data_write_failed", 422, _delete(db, "user", ["u1"]))
    assert db.rows("SELECT COUNT(*) AS n FROM session") == [{"n": 3}]
    assert db.rows("SELECT COUNT(*) AS n FROM account") == [{"n": 2}]


@pytest.mark.asyncio
async def test_user_delete_cascade_with_snake_case_and_plural_tables(site_doc, audit):
    cf = SqliteD1(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, password_hash TEXT);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id INTEGER);"
        "INSERT INTO users VALUES (1, 'a', 'h'), (2, 'b', 'h');"
        "INSERT INTO sessions VALUES ('x', 1), ('y', 2);",
        database_id=_DB,
    )
    await _delete(cf, "users", ["1"])
    assert cf.rows("SELECT id FROM sessions") == [{"id": "y"}]


@pytest.mark.asyncio
async def test_login_table_schema_flags_match_the_write_rules(site_doc, db, audit):
    with _pocket():
        user = await data_service.get_table_schema(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="user", _cloudflare=db
        )
        session = await data_service.get_table_schema(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="session", _cloudflare=db
        )
        orders = await data_service.get_table_schema(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table="orders", _cloudflare=db
        )
    writable = {c.name for c in user.columns if c.writable}
    assert writable == {"name", "email", "image"}
    assert user.editable is True and user.insertable is False
    assert session.editable is False and not any(c.writable for c in session.columns)
    assert session.insertable is False
    assert orders.insertable is True


# --------------------------------------------------------------------------- availability


@pytest.mark.asyncio
async def test_local_mode_writes_are_409_unavailable(site_doc, monkeypatch, audit):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    with _pocket():
        with pytest.raises(CloudError) as exc:
            await data_service.insert_row(
                workspace_id="ws1",
                user_id="u1",
                pocket_id="pk1",
                table="customers",
                values={"name": "x"},
            )
    assert exc.value.status_code == 409 and exc.value.code == "sites.data_unavailable"
    assert exc.value.details == {"reason": "live_on_cloudflare_only"}


@pytest.mark.asyncio
async def test_not_published_writes_are_409(monkeypatch, db, audit):
    async def _no_doc(workspace_id, pocket_id):
        return None

    async def _no_db(workspace_id, pocket_id, pocket):
        return ""

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _no_doc)
    monkeypatch.setattr(sites_service, "_project_database_id", _no_db)
    err = await _raises("sites.data_unavailable", 409, _delete(db, "notes", [1]))
    assert err.details == {"reason": "not_published"}
    assert db.calls == 0


_DYNAMIC_WIRE = {
    "name": "Guestbook",
    "workspace": "ws1",
    "pattern": "dynamic",
    "rippleSpec": {
        "type": "container",
        "objects": [
            {
                "name": "order",
                "fields": {"id": "text", "who": "string", "paid": "bool", "at": "datetime"},
                "primaryKey": "id",
            },
            {"name": "rsvp", "fields": {"id": "text", "guests": "int"}, "primaryKey": "id"},
        ],
    },
}


@pytest.mark.asyncio
async def test_dynamic_lane_writes_spec_tables_only(site_doc, audit):
    cf = SqliteD1(
        'CREATE TABLE "order" (id TEXT PRIMARY KEY, who TEXT, paid INTEGER, at TEXT);'
        "CREATE TABLE undeclared (id INTEGER PRIMARY KEY, v TEXT);",
        database_id=_DB,
    )
    res = await _insert(cf, "order", {"id": "o1", "who": "Ada", "paid": True}, wire=_DYNAMIC_WIRE)
    assert res.row["paid"] == 1
    with pytest.raises(NotFound):
        await _insert(cf, "undeclared", {"v": "x"}, wire=_DYNAMIC_WIRE)
    err = await _raises(
        "sites.data_read_only", 422, _insert(cf, "rsvp", {"id": "r"}, wire=_DYNAMIC_WIRE)
    )
    assert err.details["reason"] == "not_created"


@pytest.mark.asyncio
async def test_foreign_workspace_write_is_404(site_doc, db, audit):
    foreign = {**_PROJECT_WIRE, "workspace": "ws-other"}
    with pytest.raises(NotFound):
        await _insert(db, "customers", {"name": "x"}, wire=foreign)
    assert db.calls == 0


@pytest.mark.asyncio
async def test_writes_spend_the_data_budget(site_doc, db, audit, monkeypatch):
    monkeypatch.setattr(budget, "WORKSPACE_LIMIT", (2, 60))
    with pytest.raises(budget.DataBusy):
        await _insert(db, "customers", {"name": "x"})  # master + PRAGMA take both
    assert _writes(db) == []


# --------------------------------------------------------------------------- audit


@pytest.mark.asyncio
async def test_one_audit_event_per_write_with_masked_before_after(site_doc, db, audit):
    await _update(db, "user", "u1", {"name": "Ann B"}, {"name": "Ann"})
    (ev,) = audit
    assert ev["action"] == "site_data.row_update"
    assert ev["workspace_id"] == "ws1" and ev["actor_id"] == "u-admin"
    assert ev["target_type"] == "site_data" and ev["target_id"] == "pk1:user:u1"
    meta = ev["metadata"]
    assert meta["table"] == "user" and meta["keys"] == ["u1"]
    assert meta["before"]["name"] == "Ann" and meta["after"]["name"] == "Ann B"
    for side in (meta["before"], meta["after"]):
        assert "twoFactorSecret" not in side and "__key" not in side
    assert "totp-secret" not in repr(ev)


@pytest.mark.asyncio
async def test_audit_on_insert_and_per_row_delete(site_doc, db, audit):
    res = await _insert(db, "customers", {"name": "Cy", "email": "c" * 5000})
    (ins,) = audit
    assert ins["action"] == "site_data.row_insert" and ins["metadata"]["before"] is None
    assert len(ins["metadata"]["after"]["email"]) <= 1025  # truncated to 1 KB
    assert ins["target_id"] == f"pk1:customers:{res.row['__key']}"
    audit.clear()
    await _delete(db, "notes", [1, 2])
    assert sorted(e["target_id"] for e in audit) == ["pk1:notes:1", "pk1:notes:2"]
    assert all(e["action"] == "site_data.row_delete" for e in audit)
    assert {e["metadata"]["before"]["body"] for e in audit} == {"hello", "bye"}
    assert all(e["metadata"]["after"] is None for e in audit)


@pytest.mark.asyncio
async def test_user_delete_audit_masks_credentials(site_doc, db, audit):
    await _delete(db, "user", ["u1"])
    (ev,) = audit
    assert "twoFactorSecret" not in ev["metadata"]["before"]
    assert "totp-secret" not in repr(ev)


@pytest.mark.asyncio
async def test_a_failing_audit_never_fails_the_write(site_doc, db, monkeypatch):
    from pocketpaw_ee.cloud.audit import service as audit_service

    async def _boom(*a, **k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(audit_service, "record", _boom)
    res = await _insert(db, "customers", {"name": "Cy"})
    assert res.row["name"] == "Cy"


# --------------------------------------------------------------------------- history


@pytest.mark.asyncio
async def test_history_returns_only_this_rows_events_newest_first(site_doc, db, beanie_test_db):
    await _update(db, "customers", "1", {"name": "Ada 2"}, {"name": "Ada"})
    await _update(db, "customers", "2", {"name": "Bob 2"}, {"name": "Bob"})
    await _update(db, "customers", "1", {"name": "Ada 3"}, {"name": "Ada 2"})
    await _insert(db, "skus", {"sku": "1", "qty": 1})  # same key text, other table
    hist = await _history(db, "customers", "1")
    assert [e.after["name"] for e in hist.events] == ["Ada 3", "Ada 2"]
    assert [e.before["name"] for e in hist.events] == ["Ada 2", "Ada"]
    assert all(e.action == "site_data.row_update" for e in hist.events)
    assert hist.events[0].actor_id == "u-admin" and hist.events[0].at


@pytest.mark.asyncio
async def test_history_includes_the_delete_and_skips_other_workspaces(site_doc, db, beanie_test_db):
    from pocketpaw_ee.cloud.audit import service as audit_service

    await _delete(db, "notes", [1])
    await audit_service.record(
        "ws-other",
        "x",
        "site_data.row_update",
        target_type="site_data",
        target_id="pk1:notes:1",
        metadata={"table": "notes"},
    )
    hist = await _history(db, "notes", "1")
    assert [e.action for e in hist.events] == ["site_data.row_delete"]
    assert hist.events[0].before["body"] == "hello"


@pytest.mark.asyncio
async def test_history_is_capped_at_50(site_doc, db, beanie_test_db):
    from pocketpaw_ee.cloud.audit import service as audit_service

    for i in range(55):
        await audit_service.record(
            "ws1",
            "u",
            "site_data.row_update",
            target_type="site_data",
            target_id="pk1:customers:1",
            metadata={"table": "customers", "after": {"i": i}},
        )
    hist = await _history(db, "customers", "1")
    assert len(hist.events) == 50 and hist.events[0].after == {"i": 54}


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


_WRITE_CALLS = [
    ("POST", "/tables/customers/rows", {"values": {"name": "Cy"}}),
    ("PATCH", "/tables/customers/rows/1", {"set": {"name": "A"}, "expected": {"name": "Ada"}}),
    ("POST", "/tables/notes/rows:delete", {"keys": [1]}),
    ("POST", "/tables/orders/rows:duplicate", {"key": 1}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), _WRITE_CALLS)
async def test_member_cannot_write(routed, method, path, body):
    resp = await _call(_app("member"), method, path, body)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "sites.data_write_forbidden"
    assert _writes(routed) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), _WRITE_CALLS)
async def test_admin_can_write(routed, method, path, body):
    resp = await _call(_app("admin"), method, path, body)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_member_with_an_override_can_write(routed, monkeypatch):
    from pocketpaw_ee.guards import deps as guard_deps

    async def _grant(workspace_id, user_id, action):
        return action == "sites.data_write"

    monkeypatch.setattr(guard_deps, "_has_action_override", _grant)
    method, path, body = _WRITE_CALLS[0]
    resp = await _call(_app("member"), method, path, body)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_route_shapes(routed):
    app = _app("admin")
    ins = await _call(app, "POST", "/tables/customers/rows", {"values": {"name": "Cy"}})
    assert ins.json() == {"row": {"id": 3, "name": "Cy", "email": None, "vip": 0, "__key": "3"}}
    up = await _call(
        app, "PATCH", "/tables/customers/rows/3", {"set": {"vip": True}, "expected": {"vip": 0}}
    )
    assert up.json()["row"]["vip"] == 1
    conflict = await _call(
        app, "PATCH", "/tables/customers/rows/3", {"set": {"vip": False}, "expected": {"vip": 0}}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "sites.data_conflict"
    assert conflict.json()["error"]["details"]["current"]["vip"] == 1
    dup = await _call(app, "POST", "/tables/customers/rows:duplicate", {"key": 3})
    assert dup.json()["row"]["__key"] == "4" and dup.json()["row"]["name"] == "Cy"
    gone = await _call(app, "POST", "/tables/customers/rows:delete", {"keys": [3, 4]})
    assert gone.json()["deleted"] == 2 and gone.json()["keys"] == [3, 4]
    assert gone.json()["restore_point"]
    bad = await _call(app, "POST", "/tables/customers/rows", {"values": {"email": "x"}})
    assert bad.status_code == 422
    assert bad.json()["error"]["details"]["fields"] == {"name": "Required."}


@pytest.mark.asyncio
async def test_route_rejects_extra_body_fields(routed):
    """Adversarial: a body can't smuggle another table or a raw WHERE."""
    app = _app("admin")
    resp = await _call(
        app,
        "PATCH",
        "/tables/customers/rows/1",
        {"set": {"name": "x"}, "expected": {"name": "Ada"}, "table": "user", "where": "1=1"},
    )
    assert resp.status_code == 422
    assert _writes(routed) == []
    resp = await _call(app, "POST", "/tables/notes/rows:delete", {"keys": list(range(501))})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "sites.data_too_many_keys"


@pytest.mark.asyncio
async def test_route_key_with_slashes_and_history(routed, beanie_test_db):
    app = _app("admin")
    routed.execute("INSERT INTO skus VALUES ('a/b', 1)")
    up = await _call(
        app, "PATCH", "/tables/skus/rows/a%2Fb", {"set": {"qty": 2}, "expected": {"qty": 1}}
    )
    assert up.status_code == 200, up.text
    member = _app("member")
    hist = await _call(member, "GET", "/tables/skus/rows/a%2Fb/history")
    assert hist.status_code == 200, hist.text
    assert set(hist.json()) == {"events"}


@pytest.mark.asyncio
async def test_route_history_shape(routed, monkeypatch):
    from pocketpaw_ee.cloud.audit import service as audit_service
    from pocketpaw_ee.cloud.audit.domain import AuditEventDomain

    async def _events(workspace_id, *, target_type, target_id, limit):
        assert (workspace_id, target_type, target_id, limit) == (
            "ws1",
            "site_data",
            "pk1:customers:1",
            50,
        )
        return [
            AuditEventDomain(
                id="e1",
                workspace_id="ws1",
                actor_id="u9",
                action="site_data.row_update",
                target_type="site_data",
                target_id=target_id,
                metadata={"table": "customers", "keys": [1], "before": {"a": 1}, "after": {"a": 2}},
                ip=None,
                user_agent=None,
                at=datetime(2026, 10, 28, tzinfo=UTC),
            )
        ]

    async def _names(workspace_id, ids):
        return {"u9": "Nia"}

    monkeypatch.setattr(audit_service, "list_target_events", _events)
    monkeypatch.setattr(audit_service, "actor_names", _names)
    resp = await _call(_app("member"), "GET", "/tables/customers/rows/1/history")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "events": [
            {
                "action": "site_data.row_update",
                "actor_id": "u9",
                "actor_name": "Nia",
                "at": "2026-10-28T00:00:00+00:00",
                "before": {"a": 1},
                "after": {"a": 2},
            }
        ]
    }


def test_every_write_route_requires_sites_data_write():
    from pocketpaw_ee.sites.data_router import router

    for route in router.routes:
        methods = getattr(route, "methods", set())
        guards = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        is_restore = "/restore" in route.path
        is_write = bool(methods & {"POST", "PATCH", "PUT", "DELETE"}) and not is_restore
        assert ("require_action_sites_data_write" in guards) == is_write, route.path
        assert ("require_action_sites_data_restore" in guards) == is_restore, route.path


# --------------------------------------------------------------------------- adversarial


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ['name" = 1; DROP TABLE customers; --', "email, vip", '"vip"', "rowid", "__key"],
)
async def test_column_names_never_reach_sql_unless_introspected(site_doc, db, audit, name):
    err = await _raises("sites.data_invalid_values", 422, _insert(db, "customers", {name: 1}))
    assert name in err.details["fields"]
    err = await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "customers", "1", {"email": "x"}, {"email": "ada@x.io", name: 1}),
    )
    assert name in err.details["fields"]
    assert _writes(db) == []


@pytest.mark.asyncio
async def test_a_text_key_is_bound_never_spliced(site_doc, db, audit):
    with pytest.raises(NotFound):
        await _update(db, "skus", "A-1' OR '1'='1", {"qty": 9}, {"qty": 4})
    assert db.rows("SELECT qty FROM skus") == [{"qty": 4}]
    res = await _delete(db, "skus", ["x' OR 1=1 --"])
    assert res.deleted == 0 and db.rows("SELECT COUNT(*) AS n FROM skus") == [{"n": 1}]


@pytest.mark.asyncio
async def test_rowid_delete_keys_must_be_integers(site_doc, db, audit):
    err = await _raises("sites.data_invalid_values", 422, _delete(db, "notes", ["1 OR 1=1"]))
    assert "keys" in err.details["fields"]
    assert _writes(db) == []


@pytest.mark.asyncio
async def test_route_refuses_non_scalar_keys(routed):
    app = _app("admin")
    for body in ({"keys": [[1, 2]]}, {"keys": [{"$gt": 0}]}, {"keys": "1"}):
        resp = await _call(app, "POST", "/tables/notes/rows:delete", body)
        assert resp.status_code == 422, body
    resp = await _call(app, "POST", "/tables/orders/rows:duplicate", {"key": [1]})
    assert resp.status_code == 422
    assert _writes(routed) == []


@pytest.mark.asyncio
async def test_duplicate_cannot_copy_into_or_override_a_masked_column(site_doc, audit):
    """A non-login table never has masked columns, and a user table refuses copies;
    an override naming a generated column is refused like an insert."""
    cf = SqliteD1(_DDL, database_id=_DB)
    err = await _raises(
        "sites.data_invalid_values", 422, _duplicate(cf, "orders", "1", {"total_x2": 5})
    )
    assert "total_x2" in err.details["fields"]
    await _raises("sites.data_read_only", 403, _duplicate(cf, "user", "u1", {"name": "x"}))
    assert _writes(cf) == []


def test_a_masked_column_is_never_writable_even_where_the_table_allows_it():
    """The masked rule stands on its own, not only behind the user-table allowlist
    and the login-table lock that cover it on today's tables."""
    col = d1.Column(
        name="email",
        sql_type="text",
        notnull=False,
        default=None,
        pk=0,
        hidden=0,
        references=None,
    )
    for name in ("notes", "user"):
        table = d1.Table(
            name=name,
            columns=(col,),
            without_rowid=False,
            key="rowid",
            key_sql="rowid",
            order_sql=("rowid",),
            masked=frozenset({"email"}),
            read_only_reason="",
        )
        assert d1.column_writable(table, col, "text") is False
        unmasked = d1.Table(**{**table.__dict__, "masked": frozenset()})
        assert d1.column_writable(unmasked, col, "text") is True


# ------------------------------------------------- review: credentials everywhere

_CRED_DDL = """
CREATE TABLE "twoFactor" (id TEXT PRIMARY KEY, secret TEXT, "backupCodes" TEXT,
  "userId" TEXT);
CREATE TABLE apikey (id TEXT PRIMARY KEY, key TEXT, name TEXT);
CREATE TABLE jwks (id TEXT PRIMARY KEY, "publicKey" TEXT, "privateKey" TEXT);
CREATE TABLE passkey (id TEXT PRIMARY KEY, "publicKey" TEXT, name TEXT);
CREATE TABLE auth_user (id TEXT PRIMARY KEY, name TEXT, email TEXT, password TEXT);
CREATE TABLE admins (id INTEGER PRIMARY KEY, name TEXT, password TEXT, api_key TEXT,
  "resetToken" TEXT, pin_hash TEXT, key TEXT, primary_key TEXT);
INSERT INTO "twoFactor" VALUES ('t1', 'totp', 'codes', 'u1');
INSERT INTO apikey VALUES ('k1', 'sk_live_x', 'ci');
INSERT INTO jwks VALUES ('j1', 'pub', 'priv');
INSERT INTO auth_user VALUES ('u1', 'Ann', 'ann@x.io', 'scrypt$z');
INSERT INTO admins VALUES (1, 'root', 'hunter2', 'ak_1', 'rt_1', 'h', 'theme', 'pk');
"""


@pytest.fixture
def creds() -> SqliteD1:
    return SqliteD1(_CRED_DDL, database_id=_DB)


async def _schema_of(cf, table):
    with _pocket():
        return await data_service.get_table_schema(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table=table, _cloudflare=cf
        )


async def _rows_of(cf, table, **kw):
    with _pocket():
        return await data_service.get_rows(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", table=table, _cloudflare=cf, **kw
        )


def _masked_names(schema) -> set[str]:
    return {c.name for c in schema.columns if c.masked}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "secrets"),
    [
        ("twoFactor", {"secret", "backupCodes"}),
        ("apikey", {"key"}),
        ("jwks", {"privateKey"}),
        ("passkey", {"publicKey"}),
    ],
)
async def test_auth_plugin_tables_are_masked_and_read_only(site_doc, creds, audit, table, secrets):
    s = await _schema_of(creds, table)
    assert s.editable is False and s.read_only_reason == "login_table"
    assert _masked_names(s) >= secrets
    page = await _rows_of(creds, table)
    for row in page.rows:
        for col in secrets:
            assert row[col] in ("•••", None)
    err = await _raises("sites.data_read_only", 403, _delete(creds, table, ["x"]))
    assert err.details["reason"] == "login_table"


@pytest.mark.asyncio
async def test_a_renamed_user_table_is_a_user_table(site_doc, creds, audit):
    s = await _schema_of(creds, "auth_user")
    assert {c.name for c in s.columns if c.writable} == {"name", "email"}
    assert s.insertable is False
    assert "password" in _masked_names(s)
    page = await _rows_of(creds, "auth_user")
    assert page.rows[0]["password"] == "•••"


@pytest.mark.asyncio
async def test_credential_columns_on_any_table_are_masked_and_locked(site_doc, creds, audit):
    s = await _schema_of(creds, "admins")
    assert s.editable is True
    assert _masked_names(s) == {"password", "api_key", "resetToken", "pin_hash"}
    assert {c.name for c in s.columns if c.writable} == {"name", "key", "primary_key"}
    page = await _rows_of(creds, "admins")
    row = page.rows[0]
    assert row["password"] == "•••" and row["api_key"] == "•••" and row["resetToken"] == "•••"
    assert row["key"] == "theme" and row["primary_key"] == "pk"
    assert (await _rows_of(creds, "admins", q="hunter2")).rows == []  # not searched
    with pytest.raises(CloudError):
        await _rows_of(creds, "admins", f='[{"col": "password", "op": "eq", "value": "x"}]')
    for col in ("password", "api_key"):
        err = await _raises(
            "sites.data_invalid_values", 422, _update(creds, "admins", "1", {col: "x"}, {col: None})
        )
        assert col in err.details["fields"]
    await _update(creds, "admins", "1", {"name": "admin"}, {"name": "root"})
    (ev,) = audit
    for side in (ev["metadata"]["before"], ev["metadata"]["after"]):
        assert not {"password", "api_key", "resetToken", "pin_hash"} & set(side)
    assert "hunter2" not in repr(ev) and "ak_1" not in repr(ev)


def test_credential_pattern_is_narrow():
    from pocketpaw_ee.sites.project_data import masked_columns

    cols = [
        "key",
        "primary_key",
        "settings_key",
        "keyboard",
        "name",
        "hash",
        "pinHash",
        "passwd",
        "clientSecret",
        "refresh_token",
        "privateKey",
        "apiKey",
        "backup_codes",
        "hashtag",
    ]
    assert masked_columns("anything", cols) == {
        "hash",
        "pinHash",
        "passwd",
        "clientSecret",
        "refresh_token",
        "privateKey",
        "apiKey",
        "backup_codes",
    }


# --------------------------------------------------------------------------- review: edit access


@pytest.mark.asyncio
async def test_writes_need_edit_access_to_the_pocket(site_doc, db, audit):
    for coro_of in (
        lambda: data_service.insert_row(**_kw(db, "customers", values={"name": "x"})),
        lambda: data_service.delete_rows(**_kw(db, "notes", keys=[1])),
    ):
        with _pocket(can_edit=False):
            with pytest.raises(CloudError) as exc:
                await coro_of()
        assert exc.value.status_code == 403 and exc.value.code == "pocket.access_denied"
    assert _writes(db) == [] and audit == []


# --------------------------------------------------------------------------- review: email takeover


@pytest.mark.asyncio
async def test_changing_a_users_email_clears_email_verified(site_doc, db, audit):
    res = await _update(db, "user", "u1", {"email": "new@x.io"}, {"email": "ann@x.io"})
    assert res.row["email"] == "new@x.io" and res.row["emailVerified"] == 0
    updates = [s for s in _writes(db) if s.startswith("UPDATE")]
    assert len(updates) == 1 and '"emailVerified" = ?' in updates[0]
    # an unchanged email leaves the flag alone
    db.execute('UPDATE "user" SET "emailVerified" = 1 WHERE id = ?', ["u1"])
    await _update(db, "user", "u1", {"email": "new@x.io"}, {"email": "new@x.io"})
    assert db.rows('SELECT "emailVerified" AS v FROM "user" WHERE id = ?', ["u1"]) == [{"v": 1}]


@pytest.mark.asyncio
async def test_email_verified_snake_case_is_cleared_too(site_doc, audit):
    cf = SqliteD1(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, email_verified INTEGER);"
        "INSERT INTO users VALUES (1, 'a@x.io', 1);",
        database_id=_DB,
    )
    await _raises(
        "sites.data_invalid_values",
        422,
        _update(cf, "users", "1", {"email_verified": 1}, {"email_verified": 1}),
    )
    await _update(cf, "users", "1", {"email": "b@x.io"}, {"email": "a@x.io"})
    assert cf.rows("SELECT email_verified AS v FROM users") == [{"v": 0}]


# --------------------------------------------------------------------------- review: no 500s


@pytest.mark.asyncio
async def test_deeply_nested_json_is_422(site_doc, db, audit):
    await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "orders", "1", {"meta_json": "[" * 100_000}, {"meta_json": None}),
    )
    nested: Any = []
    for _ in range(100_000):
        nested = [nested]
    await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "orders", "1", {"meta_json": nested}, {"meta_json": None}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [10**400, "1" * 39, -(10**30), 1e300])
async def test_huge_datetime_is_422(site_doc, db, audit, value):
    await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "orders", "1", {"created_at": value}, {"created_at": 1767225600}),
    )


@pytest.mark.asyncio
async def test_huge_rowid_keys_are_422(site_doc, db, audit):
    huge = "1" * 5000
    await _raises("sites.data_bad_key", 422, _update(db, "notes", huge, {"tag": "x"}, {"tag": "a"}))
    await _raises("sites.data_invalid_values", 422, _delete(db, "notes", [huge]))
    await _raises(
        "sites.data_bad_key", 422, _update(db, "notes", str(2**63), {"tag": "x"}, {"tag": "a"})
    )
    await _raises("sites.data_invalid_values", 422, _delete(db, "notes", [2**63]))
    assert _writes(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2**63, -(2**63) - 1, "9" * 41, 10**400])
async def test_integers_outside_int64_are_422(site_doc, db, audit, value):
    err = await _raises(
        "sites.data_invalid_values", 422, _update(db, "skus", "A-1", {"qty": value}, {"qty": 4})
    )
    assert "qty" in err.details["fields"]


@pytest.mark.asyncio
async def test_int64_bounds_are_fine(site_doc, db, audit):
    await _update(db, "skus", "A-1", {"qty": 2**63 - 1}, {"qty": 4})
    assert db.rows("SELECT qty FROM skus") == [{"qty": 2**63 - 1}]


@pytest.mark.asyncio
async def test_out_of_range_expected_and_untyped_values_are_422(site_doc, audit):
    cf = SqliteD1(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v);INSERT INTO t VALUES (1, 1);",
        database_id=_DB,
    )
    await _raises("sites.data_invalid_values", 422, _update(cf, "t", "1", {"v": 2**64}, {"v": 1}))
    await _raises("sites.data_invalid_values", 422, _update(cf, "t", "1", {"v": 2}, {"v": 2**64}))


# --------------------------------------------------------------------------- review: cascade


@pytest.mark.asyncio
async def test_cascade_without_fk_ignores_plurality(site_doc, audit):
    cf = SqliteD1(
        "CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT);"
        'CREATE TABLE session (id TEXT PRIMARY KEY, "userId" TEXT);'
        "INSERT INTO users VALUES ('u1', 'a');"
        "INSERT INTO session VALUES ('s1', 'u1'), ('s2', 'u1');",
        database_id=_DB,
    )
    await _delete(cf, "users", ["u1"])
    assert cf.rows("SELECT COUNT(*) AS n FROM session") == [{"n": 0}]
    (ev,) = audit
    assert ev["metadata"]["cascade"] == {"session": 2}


@pytest.mark.asyncio
async def test_cascade_matches_fk_parent_case_insensitively(site_doc, audit):
    cf = SqliteD1(
        'CREATE TABLE "user" (id TEXT PRIMARY KEY, name TEXT);'
        'CREATE TABLE sessions (id TEXT PRIMARY KEY, "userId" TEXT REFERENCES "User"(id));'
        "INSERT INTO \"user\" VALUES ('u1', 'a');"
        "INSERT INTO sessions VALUES ('s1', 'u1');",
        database_id=_DB,
    )
    await _delete(cf, "user", ["u1"])
    assert cf.rows("SELECT COUNT(*) AS n FROM sessions") == [{"n": 0}]


@pytest.mark.asyncio
async def test_user_delete_audit_records_cascade_counts(site_doc, db, audit):
    await _delete(db, "user", ["u1", "u2"])
    by_key = {e["target_id"]: e["metadata"]["cascade"] for e in audit}
    assert by_key == {
        "pk1:user:u1": {"session": 2, "account": 1},
        "pk1:user:u2": {"session": 1, "account": 1},
    }


# --------------------------------------------------------------------------- review: size caps


@pytest.mark.asyncio
async def test_expected_values_are_size_capped(site_doc, db, audit):
    too_big = "x" * (writes.MAX_VALUE_BYTES + 1)
    err = await _raises(
        "sites.data_invalid_values",
        422,
        _update(db, "customers", "1", {"email": "x"}, {"email": too_big}),
    )
    assert "email" in err.details["fields"]


@pytest.mark.asyncio
async def test_text_keys_are_capped_at_512(site_doc, db, audit):
    await _raises("sites.data_bad_key", 422, _update(db, "skus", "k" * 513, {"qty": 1}, {"qty": 4}))
    await _raises("sites.data_invalid_values", 422, _delete(db, "skus", ["k" * 513]))
    with pytest.raises(NotFound):  # 512 is a legal key that just doesn't exist
        await _update(db, "skus", "k" * 512, {"qty": 1}, {"qty": 4})


@pytest.mark.asyncio
async def test_error_map_is_capped_at_20(site_doc, db, audit):
    err = await _raises(
        "sites.data_invalid_values",
        422,
        _insert(db, "customers", {f"c{i}": 1 for i in range(200)}),
    )
    assert len(err.details["fields"]) == 20 and err.details["truncated"] is True


@pytest.mark.asyncio
async def test_write_bodies_over_1mb_are_refused(routed):
    big = {"set": {"name": "x" * 600_000}, "expected": {"name": "y" * 600_000}}
    resp = await _call(_app("admin"), "PATCH", "/tables/customers/rows/1", big)
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "sites.data_body_too_large"
    assert _writes(routed) == []


@pytest.mark.asyncio
async def test_deeply_nested_body_is_not_a_500(routed):
    app = _app("admin")
    body = '{"values": {"name": ' + "[" * 100_000 + "]" * 100_000 + "}}"
    base = "/api/v1/sites/by-pocket/pk1/data"
    with _pocket():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post(
                base + "/tables/customers/rows",
                content=body,
                headers={"content-type": "application/json"},
            )
    assert 400 <= resp.status_code < 500, resp.status_code


def test_the_fake_enforces_foreign_keys_by_default():
    cf = SqliteD1(
        "CREATE TABLE p (id INTEGER PRIMARY KEY); CREATE TABLE c (p INTEGER REFERENCES p(id));"
    )
    with pytest.raises(sqlite3.IntegrityError):
        cf.execute("INSERT INTO c VALUES (9)")


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["two_factor", "TwoFactors", "api_keys", "auth_session"])
async def test_auth_tables_are_read_only_whatever_their_spelling(site_doc, audit, table):
    from pocketpaw_ee.sites.project_data import quote_ident

    cf = SqliteD1(f"CREATE TABLE {quote_ident(table)} (id TEXT PRIMARY KEY, label TEXT);")
    err = await _raises("sites.data_read_only", 403, _insert(cf, table, {"id": "x"}))
    assert err.details["reason"] == "login_table"


@pytest.mark.asyncio
async def test_a_long_numeric_key_is_refused_even_when_its_value_is_small(site_doc, db, audit):
    """The 40-character cap stands on its own: ``int()`` on a huge digit string is
    slow (and refused past Python's digit limit), whatever value it would yield."""
    padded = "0" * 50 + "1"
    await _raises(
        "sites.data_bad_key", 422, _update(db, "notes", padded, {"tag": "x"}, {"tag": "a"})
    )
    assert _writes(db) == []


@pytest.mark.asyncio
async def test_big_integers_round_trip_as_digit_strings(site_doc, db, audit):
    """A value past 2**53 - 1 reaches the browser as a digit string; sending that
    string back as ``expected`` (and as a new value) still matches and stores the
    exact integer, never a rounded one."""
    big = 12_345_678_901_234_567
    res = await _insert(db, "skus", {"sku": "snow", "qty": str(big)})
    assert res.row["qty"] == str(big) and res.row["__key"] == "snow"
    up = await _update(db, "skus", "snow", {"qty": str(big + 2)}, {"qty": str(big)})
    assert up.row["qty"] == str(big + 2)
    assert db.rows("SELECT qty, typeof(qty) t FROM skus WHERE sku = 'snow'") == [
        {"qty": big + 2, "t": "integer"}
    ]
