# tests/ee/sites/test_site_data_import.py — CSV import and export of the Sites Data
# dashboard: ``sites.site_data.csv_io`` and the import / export operations of
# ``sites.data_service`` / ``sites.data_router``.
#
# D1 is a real in-memory SQLite (``d1_sqlite_fake.SqliteD1``) whose batches are
# atomic like live D1 and whose Time Travel stub snapshots the database, so a
# restore point taken before an import really restores the rows before it. Shared
# fixtures (the site doc, the pocket patch, the routed app) come from
# ``test_site_data_write``.
#
# Covers TDD §11 "PR 5" CSV and export: BOM, ``;`` delimiter, quoted newlines, the
# 5 MB / 20,000-row / 500-column caps, auto-match rules, per-row type errors, upsert
# without its key, credential / generated column mapping refused, login tables
# refused, another user's / site's / table's upload and an expired one 404, batch
# atomicity, the restore point and audit event; export masking, the formula guard,
# filters honoured and every page streamed; and the adversarial pass.
# ruff: noqa: F811 — the shared fixtures are imported from test_site_data_write
from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.sites import data_service  # noqa: E402
from pocketpaw_ee.sites.site_data import budget, csv_io  # noqa: E402

from tests.ee.sites.d1_sqlite_fake import SqliteD1  # noqa: E402
from tests.ee.sites.test_site_data_write import (  # noqa: E402,F401
    _DB,
    _DDL,
    _app,
    _call,
    _clean,
    _pocket,
    _raises,
    audit,
    db,
    routed,
    site_doc,
)

_EXTRA_DDL = """
CREATE TABLE api_clients (id INTEGER PRIMARY KEY, name TEXT, api_key TEXT);
INSERT INTO api_clients VALUES (1, 'shop', 'sk_live_123');
CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new');
INSERT INTO tickets VALUES (1, 'old', 'closed');
"""


@pytest.fixture(autouse=True)
def _uploads():
    csv_io.reset_uploads()
    yield
    csv_io.reset_uploads()


@pytest.fixture
def xdb() -> SqliteD1:
    return SqliteD1(_DDL + _EXTRA_DDL, database_id=_DB)


def _kw(cf, table, **kw):
    return {
        "workspace_id": "ws1",
        "user_id": "u-admin",
        "pocket_id": "pk1",
        "table": table,
        "_cloudflare": cf,
        **kw,
    }


async def _preview(cf, table, data: bytes | str, **kw):
    raw = data.encode("utf-8") if isinstance(data, str) else data
    with _pocket():
        return await data_service.import_preview(
            **_kw(cf, table, filename="in.csv", data=raw, **kw)
        )


async def _commit(cf, table, upload_id, mapping, mode="insert", **kw):
    with _pocket():
        return await data_service.import_commit(
            **_kw(cf, table, upload_id=upload_id, mapping=mapping, mode=mode, **kw)
        )


async def _import(cf, table, data, mapping=None, mode="insert"):
    p = await _preview(cf, table, data)
    return p, await _commit(cf, table, p.upload_id, mapping or p.mapping, mode)


async def _export(cf, table, **kw) -> tuple[str, str]:
    with _pocket():
        name, body = await data_service.export_csv(**_kw(cf, table, **kw))
        chunks = [c async for c in body]
    return name, b"".join(chunks).decode("utf-8")


# --------------------------------------------------------------------------- parsing


@pytest.mark.asyncio
async def test_bom_is_tolerated_and_header_auto_maps(site_doc, db, audit):
    p = await _preview(db, "customers", b"\xef\xbb\xbfname,Email\nCy,cy@x.io\n")
    assert p.header == ["name", "Email"]
    assert p.mapping == {"name": "name", "Email": "email"}
    assert p.rows == [["Cy", "cy@x.io"]] and p.total_rows == 1


@pytest.mark.asyncio
async def test_semicolon_delimiter(site_doc, db, audit):
    _p, res = await _import(db, "customers", "name;email;vip\nCy;cy@x.io;1\nDi;;0\n")
    assert res.inserted == 2 and res.failed == []
    assert db.rows("SELECT name, email, vip FROM customers WHERE id > 2 ORDER BY id") == [
        {"name": "Cy", "email": "cy@x.io", "vip": 1},
        {"name": "Di", "email": None, "vip": 0},
    ]


@pytest.mark.asyncio
async def test_quoted_newlines_and_row_numbers(site_doc, db, audit):
    text = 'body,tag\n"line one\nline two",a\n,b\n'
    p = await _preview(db, "notes", text)
    assert p.rows[0] == ["line one\nline two", "a"]
    # body is NOT NULL without a default: the second record (file row 3) is required.
    assert [(e.row, e.column, e.error) for e in p.errors] == [(3, "body", "Required.")]
    res = await _commit(db, "notes", p.upload_id, p.mapping)
    assert res.inserted == 1 and [(f.row, f.error) for f in res.failed] == [(3, "body: Required.")]
    assert db.rows("SELECT body FROM notes WHERE tag = 'a' AND body LIKE 'line%'") == [
        {"body": "line one\nline two"}
    ]


@pytest.mark.asyncio
async def test_five_mb_cap_is_413(site_doc, db, audit):
    big = b"name\n" + b"x" * (csv_io.MAX_BYTES)
    await _raises("sites.data_csv_too_large", 413, _preview(db, "customers", big))
    assert db.restores == []


@pytest.mark.asyncio
async def test_five_mb_single_line_is_a_clean_422(site_doc, db, audit):
    """Adversarial: one enormous cell can't hang or 500 the parser."""
    line = b"name\n" + b"a" * (csv_io.MAX_BYTES - 10)
    await _raises("sites.data_csv_unreadable", 422, _preview(db, "customers", line))


@pytest.mark.asyncio
async def test_ten_thousand_columns_is_refused(site_doc, db, audit):
    header = ",".join(f"c{i}" for i in range(10_000))
    await _raises("sites.data_csv_too_wide", 422, _preview(db, "customers", header + "\n"))


@pytest.mark.asyncio
async def test_row_cap(site_doc, db, audit, monkeypatch):
    monkeypatch.setattr(csv_io, "MAX_ROWS", 3)
    await _raises("sites.data_csv_too_many_rows", 422, _preview(db, "notes", "body\na\nb\nc\nd\n"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "sites.data_csv_empty"),
        ("  \n\n", "sites.data_csv_empty"),
        ("body,body\nx,y\n", "sites.data_csv_bad_header"),
    ],
)
async def test_empty_and_repeated_headers(site_doc, db, audit, text, code):
    await _raises(code, 422, _preview(db, "notes", text))


# --------------------------------------------------------------------------- mapping


@pytest.mark.asyncio
async def test_auto_match_exact_then_case_insensitive_then_normalised(site_doc, db, audit):
    header = "ID,Customer Id,NOTE,Total,isPaid,created at,unknown"
    p = await _preview(db, "orders", header + "\n1,1,x,2,yes,2026-01-01,z\n")
    assert p.mapping == {
        "ID": "id",
        "Customer Id": "customer_id",
        "NOTE": "note",
        "Total": "total",
        "isPaid": "is_paid",
        "created at": "created_at",
        "unknown": None,
    }
    # generated, blob columns are not targets; the key is (and is the upsert key)
    names = {t.name for t in p.targets}
    assert "total_x2" not in names and "photo" not in names and "id" in names
    assert p.key == "id"


def test_auto_mapping_uses_each_table_column_once_exact_first():
    assert csv_io.auto_mapping(["Name", "name"], ["name"]) == {"Name": None, "name": "name"}
    assert csv_io.auto_mapping(["E-Mail", "email "], ["email"]) == {
        "E-Mail": None,
        "email ": "email",
    }
    assert csv_io.norm("orderTotal") == csv_io.norm("Order  Total") == "order_total"


@pytest.mark.asyncio
async def test_per_row_type_errors(site_doc, db, audit):
    text = "note,total,is_paid\nok,1.5,yes\nbad,abc,no\nworse,2,maybe\n"
    p = await _preview(db, "orders", text)
    assert [(e.row, e.column) for e in p.errors] == [(3, "total"), (4, "is_paid")]
    assert p.error_count == 2
    res = await _commit(db, "orders", p.upload_id, p.mapping)
    assert res.inserted == 1 and res.failed_count == 2
    assert [f.row for f in res.failed] == [3, 4]
    assert db.rows("SELECT COUNT(*) AS n FROM orders") == [{"n": 3}]


@pytest.mark.asyncio
async def test_row_errors_are_capped_at_100(site_doc, db, audit):
    text = "note,total\n" + "".join(f"n{i},x\n" for i in range(150))
    p = await _preview(db, "orders", text)
    assert len(p.errors) == 100 and p.error_count == 150


@pytest.mark.asyncio
async def test_upsert_without_the_key_mapped_is_422(site_doc, db, audit):
    p = await _preview(db, "customers", "name\nCy\n")
    await _raises(
        "sites.data_import_needs_key",
        422,
        _commit(db, "customers", p.upload_id, p.mapping, "upsert"),
    )
    await _raises(
        "sites.data_import_needs_key",
        422,
        _commit(
            db,
            "notes",
            (await _preview(db, "notes", "body\nx\n")).upload_id,
            {"body": "body"},
            "upsert",
        ),
    )
    assert db.statements("INSERT") == []


@pytest.mark.asyncio
async def test_upsert_updates_matches_and_inserts_the_rest(site_doc, db, audit):
    text = "id,name,email\n1,Ada L,ada@new.io\n7,Gus,\n7,Gus 2,g@x.io\n"
    _p, res = await _import(db, "customers", text, mode="upsert")
    assert (res.inserted, res.updated, res.failed) == (1, 2, [])
    assert db.rows("SELECT id, name, email FROM customers ORDER BY id") == [
        {"id": 1, "name": "Ada L", "email": "ada@new.io"},
        {"id": 2, "name": "Bob", "email": None},
        {"id": 7, "name": "Gus 2", "email": "g@x.io"},
    ]


@pytest.mark.asyncio
async def test_empty_cell_in_a_not_null_column_with_a_default(site_doc, xdb, audit):
    _p, res = await _import(xdb, "tickets", "id,title,status\n1,old2,\n2,new,\n", mode="upsert")
    assert (res.inserted, res.updated) == (1, 1)
    assert xdb.rows("SELECT id, title, status FROM tickets ORDER BY id") == [
        {"id": 1, "title": "old2", "status": "closed"},  # kept: the cell was empty
        {"id": 2, "title": "new", "status": "new"},  # the column default
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "mapping", "field", "message"),
    [
        ("api_clients", {"name": "name", "secret": "api_key"}, "secret", "Credentials"),
        ("orders", {"note": "note", "x2": "total_x2"}, "x2", "Filled in"),
        ("orders", {"note": "note", "x2": "photo"}, "x2", "Files"),
        ("orders", {"note": "note", "x2": "nope"}, "x2", "no column"),
        ("orders", {"note": "note", "x2": "note"}, "x2", "already mapped"),
        ("orders", {"note": "note", "ghost": "total"}, "ghost", "no such column"),
    ],
)
async def test_bad_mappings_are_422(site_doc, xdb, audit, table, mapping, field, message):
    p = await _preview(xdb, table, "name,secret,note,x2\nshop,k,a,b\n")
    err = await _raises(
        "sites.data_import_bad_mapping", 422, _commit(xdb, table, p.upload_id, mapping)
    )
    assert message in err.details["fields"][field]
    assert xdb.statements("INSERT") == []


@pytest.mark.asyncio
async def test_masked_column_is_never_auto_mapped(site_doc, xdb, audit):
    p = await _preview(xdb, "api_clients", "name,api_key\nx,leak\n")
    assert p.mapping == {"name": "name", "api_key": None}
    assert "api_key" not in {t.name for t in p.targets}


@pytest.mark.asyncio
async def test_required_column_left_unmapped_is_422(site_doc, db, audit):
    p = await _preview(db, "customers", "email\nx@y.io\n")
    err = await _raises(
        "sites.data_import_bad_mapping", 422, _commit(db, "customers", p.upload_id, p.mapping)
    )
    assert err.details["fields"] == {"name": "Required: map a column to it."}


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["user", "session", "account", "verification"])
async def test_login_tables_refuse_import(site_doc, db, audit, table):
    err = await _raises("sites.data_read_only", 403, _preview(db, table, "name\nx\n"))
    assert err.details["reason"] == "login_table"
    assert db.statements("INSERT") == []


@pytest.mark.asyncio
async def test_no_edit_access_is_403(site_doc, db, audit):
    with _pocket(can_edit=False):
        with pytest.raises(CloudError) as exc:
            await data_service.import_preview(
                **_kw(db, "notes", filename="x.csv", data=b"body\nx\n")
            )
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- uploads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "who",
    [{"user_id": "u-thief"}, {"pocket_id": "pk2"}, {"workspace_id": "ws2"}, {"table": "skus"}],
)
async def test_someone_elses_upload_is_404(site_doc, db, audit, who):
    p = await _preview(db, "notes", "body\nx\n")
    kw = _kw(db, who.get("table", "notes"), upload_id=p.upload_id, mapping={"body": "body"})
    kw.update({k: v for k, v in who.items() if k != "table"})
    wire = {
        "name": "Shop",
        "workspace": kw["workspace_id"],
        "engine": "project",
        "siteMeta": {"project": {"template": "astro", "recipes": ["d1-drizzle"]}},
    }
    with _pocket(wire):
        with pytest.raises(CloudError) as exc:
            await data_service.import_commit(**kw)
    assert exc.value.status_code == 404, exc.value.code
    assert db.statements("INSERT") == []


@pytest.mark.asyncio
async def test_expired_or_unknown_upload_is_404(site_doc, db, audit, monkeypatch):
    monkeypatch.setattr(csv_io, "UPLOAD_TTL_S", -1)
    p = await _preview(db, "notes", "body\nx\n")
    for upload_id in (p.upload_id, "A" * 32, "../../etc"):
        with pytest.raises(CloudError) as exc:
            await _commit(db, "notes", upload_id, {"body": "body"})
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_an_upload_is_used_once(site_doc, db, audit):
    p, res = await _import(db, "notes", "body\nx\n")
    assert res.inserted == 1
    with pytest.raises(CloudError) as exc:
        await _commit(db, "notes", p.upload_id, p.mapping)
    assert exc.value.status_code == 404


class _FakeRedis:
    """Strings with a TTL flag; ``eval`` fails so the budget uses its own buckets."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.data[key], self.ttl[key] = value, ex

    async def get(self, key):
        v = self.data.get(key)
        return v.encode() if isinstance(v, str) else v

    async def delete(self, key):
        self.data.pop(key, None)

    async def eval(self, *a, **k):
        raise ConnectionError("no scripts here")


@pytest.mark.asyncio
async def test_uploads_live_in_redis_with_a_15_minute_ttl(site_doc, db, audit):
    redis = _FakeRedis()
    budget.use_redis(redis)
    p = await _preview(db, "notes", "body\nx\n")
    key = f"site_data:csv_upload:{p.upload_id}"
    assert redis.ttl[key] == 900 and p.expires_in == 900
    assert json.loads(redis.data[key])["owner"] == ["ws1", "pk1", "u-admin", "notes"]
    assert csv_io._local_uploads == {}
    res = await _commit(db, "notes", p.upload_id, p.mapping)
    assert res.inserted == 1 and key not in redis.data


# --------------------------------------------------------------------------- writes


@pytest.mark.asyncio
async def test_rows_per_statement_keep_under_90_params(site_doc, audit):
    cf = SqliteD1(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT, c TEXT);", database_id=_DB
    )
    text = "a,b,c\n" + "".join(f"x{i},y,z\n" for i in range(100))
    _p, res = await _import(cf, "t", text)
    assert res.inserted == 100
    inserts = [p for s, p in cf.sql if s.startswith("INSERT")]
    assert [len(p) for p in inserts] == [90, 90, 90, 30]
    assert cf.rows("SELECT COUNT(*) AS n FROM t") == [{"n": 100}]


@pytest.mark.asyncio
async def test_a_refused_batch_lands_nothing_and_the_rest_go_on(site_doc, db, audit, monkeypatch):
    monkeypatch.setattr(csv_io, "PARAMS_PER_STATEMENT", 2)  # one row per statement
    monkeypatch.setattr(csv_io, "STATEMENTS_PER_BATCH", 2)  # two rows per batch
    text = "sku,qty\nB-1,1\nA-1,2\nC-1,3\nD-1,4\n"  # A-1 exists: its batch fails whole
    _p, res = await _import(db, "skus", text)
    assert (res.inserted, res.failed_count) == (2, 2)
    assert [f.row for f in res.failed] == [2, 3]
    assert [r["sku"] for r in db.rows("SELECT sku FROM skus ORDER BY sku")] == [
        "A-1",
        "C-1",
        "D-1",
    ]
    assert db.rows("SELECT qty FROM skus WHERE sku = 'A-1'") == [{"qty": 4}]


@pytest.mark.asyncio
async def test_restore_point_is_taken_before_the_first_write(site_doc, db, audit):
    _p, res = await _import(db, "notes", "body,tag\nnew1,x\nnew2,y\n")
    assert res.restore_point
    first_insert = next(i for i, (s, _) in enumerate(db.sql) if s.startswith("INSERT"))
    assert first_insert > 0
    await db.d1_restore(_DB, bookmark=res.restore_point)
    assert db.rows("SELECT COUNT(*) AS n FROM notes") == [{"n": 2}]  # back to before


@pytest.mark.asyncio
async def test_no_time_travel_still_imports_without_a_restore_point(site_doc, db, audit):
    db.version = "alpha"
    _p, res = await _import(db, "notes", "body\nx\n")
    assert res.inserted == 1 and res.restore_point is None


@pytest.mark.asyncio
async def test_one_audit_event_without_row_values(site_doc, db, audit):
    _p, res = await _import(db, "customers", "name,email\nCy,cy@secret.io\n")
    events = [e for e in audit if e["action"] == "site_data.import"]
    assert len(events) == 1 and len(audit) == 1
    e = events[0]
    assert e["target_type"] == "site_data" and e["target_id"] == "pk1:customers"
    meta = e["metadata"]
    assert meta["mapping"] == {"name": "name", "email": "email"}
    assert (meta["inserted"], meta["updated"], meta["failed"]) == (1, 0, 0)
    assert meta["restore_point"] == res.restore_point
    assert "cy@secret.io" not in json.dumps(meta) and "Cy" not in json.dumps(meta)


@pytest.mark.asyncio
async def test_import_drops_cached_counts(site_doc, db, audit):
    with _pocket():
        before = await data_service.get_inventory(
            workspace_id="ws1", user_id="u", pocket_id="pk1", _cloudflare=db
        )
    await _import(db, "notes", "body\nx\n")
    with _pocket():
        after = await data_service.get_inventory(
            workspace_id="ws1", user_id="u", pocket_id="pk1", _cloudflare=db
        )
    count = lambda inv: next(t.row_count for t in inv.tables if t.name == "notes")  # noqa: E731
    assert (count(before), count(after)) == (2, 3)


# --------------------------------------------------------------------------- export


@pytest.mark.asyncio
async def test_export_leaves_credentials_out_entirely(site_doc, xdb, audit):
    _name, text = await _export(xdb, "api_clients")
    assert text.splitlines()[0] == "﻿id,name"
    assert "sk_live_123" not in text and "api_key" not in text


@pytest.mark.asyncio
async def test_export_of_a_login_table_masks_its_secrets(site_doc, db, audit):
    _name, text = await _export(db, "account")
    assert "scrypt" not in text and "at-1" not in text and "password" not in text


@pytest.mark.asyncio
async def test_export_formula_guard(site_doc, audit):
    cf = SqliteD1("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT, n INTEGER);", database_id=_DB)
    payloads = ['=HYPERLINK("http://x","y")', "+1", "-2", "@SUM(A1)", "\tx", "\rx", "ok"]
    for i, v in enumerate(payloads, 1):
        cf.execute("INSERT INTO t VALUES (?, ?, ?)", (i, v, -5))
    _name, text = await _export(cf, "t", sort="id", dir="asc")
    import csv as _csv
    import io as _io

    rows = list(_csv.reader(_io.StringIO(text.lstrip("﻿"), newline="")))
    assert [r[1] for r in rows[1:]] == ["'" + p for p in payloads[:-1]] + ["ok"]
    assert {r[2] for r in rows[1:]} == {"-5"}  # a stored number is not prefixed


@pytest.mark.asyncio
async def test_export_honours_filter_search_and_sort(site_doc, db, audit):
    f = json.dumps([{"col": "tag", "op": "eq", "value": "a"}])
    _name, text = await _export(db, "notes", f=f)
    assert text.splitlines()[1:] == ["hello,a"]
    _name, text = await _export(db, "notes", q="by")
    assert text.splitlines()[1:] == ["bye,b"]
    _name, text = await _export(db, "notes", sort="body", dir="desc")
    assert text.splitlines()[1:] == ["hello,a", "bye,b"]


@pytest.mark.asyncio
async def test_export_streams_every_page(site_doc, audit):
    cf = SqliteD1("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);", database_id=_DB)
    cf.executemany("INSERT INTO t (v) VALUES (?)", [("x",)] * 1203)
    _name, text = await _export(cf, "t")
    assert len(text.splitlines()) == 1204
    pages = [p for s, p in cf.sql if s.startswith('SELECT "id"')]
    assert [p[-2:] for p in pages] == [[500, 0], [500, 500], [500, 1000]]


@pytest.mark.asyncio
async def test_export_cap(site_doc, audit, monkeypatch):
    monkeypatch.setattr(csv_io, "EXPORT_MAX_ROWS", 3)
    monkeypatch.setattr(csv_io, "EXPORT_PAGE", 2)
    cf = SqliteD1("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);", database_id=_DB)
    cf.executemany("INSERT INTO t (v) VALUES (?)", [("x",)] * 10)
    _name, text = await _export(cf, "t")
    assert len(text.splitlines()) == 4


@pytest.mark.asyncio
async def test_export_masked_sort_is_422_before_streaming(site_doc, xdb, audit):
    await _raises("sites.data_bad_sort", 422, _export(xdb, "api_clients", sort="api_key"))


def test_export_filename_is_header_safe():
    from datetime import date

    assert csv_io.export_filename("orders", date(2026, 10, 28)) == "orders-2026-10-28.csv"
    assert csv_io.export_filename('a"b\r\n;c', date(2026, 1, 2)) == "a_b_c-2026-01-02.csv"


# --------------------------------------------------------------------------- routes


@pytest.mark.asyncio
async def test_route_preview_commit_and_export(routed):
    from httpx import ASGITransport, AsyncClient

    app = _app("admin")
    base = "/api/v1/sites/by-pocket/pk1/data"
    with _pocket():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            prev = await c.post(
                base + "/tables/customers/import:preview",
                files={"file": ("people.csv", b"Name,EMAIL\nCy,cy@x.io\n", "text/csv")},
            )
            assert prev.status_code == 200, prev.text
            body = prev.json()
            assert body["filename"] == "people.csv" and body["mapping"] == {
                "Name": "name",
                "EMAIL": "email",
            }
            done = await c.post(
                base + "/tables/customers/import",
                json={"upload_id": body["upload_id"], "mapping": body["mapping"]},
            )
            assert done.status_code == 200, done.text
            assert {k: done.json()[k] for k in ("inserted", "updated", "failed")} == {
                "inserted": 1,
                "updated": 0,
                "failed": [],
            }
            exp = await c.get(base + "/tables/customers/export.csv")
            assert exp.status_code == 200
            assert exp.headers["content-type"].startswith("text/csv")
            assert exp.headers["content-disposition"].startswith('attachment; filename="customers-')
            assert exp.content.startswith(b"\xef\xbb\xbfid,name,email,vip\r\n")
            big = await c.post(
                base + "/tables/customers/import:preview",
                files={"file": ("x.csv", b"name\n" + b"x" * csv_io.MAX_BYTES, "text/csv")},
            )
            assert big.status_code == 413
            assert big.json()["error"]["code"] == "sites.data_csv_too_large"


@pytest.mark.asyncio
async def test_member_cannot_import_but_can_export(routed):
    from httpx import ASGITransport, AsyncClient

    app = _app("member")
    base = "/api/v1/sites/by-pocket/pk1/data"
    with _pocket():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            prev = await c.post(
                base + "/tables/notes/import:preview",
                files={"file": ("x.csv", b"body\nx\n", "text/csv")},
            )
            assert prev.status_code == 403
            assert prev.json()["error"]["code"] == "sites.data_write_forbidden"
            commit = await c.post(
                base + "/tables/notes/import", json={"upload_id": "A" * 32, "mapping": {}}
            )
            assert commit.status_code == 403
            exp = await c.get(base + "/tables/notes/export.csv")
            assert exp.status_code == 200


@pytest.mark.asyncio
async def test_route_import_body_refuses_extra_fields_and_bad_mode(routed):
    app = _app("admin")
    for body in (
        {"upload_id": "A" * 32, "mapping": {}, "table": "user"},
        {"upload_id": "A" * 32, "mapping": {}, "mode": "replace"},
    ):
        resp = await _call(app, "POST", "/tables/notes/import", body)
        assert resp.status_code == 422, resp.text
