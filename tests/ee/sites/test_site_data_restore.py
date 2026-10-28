# tests/ee/sites/test_site_data_restore.py — point-in-time restore of the Sites Data
# dashboard: ``data_service.restore_status`` / ``restore`` / ``restore_undo`` and
# their routes.
#
# D1 is the real in-memory SQLite fake with its Time Travel stub: each bookmark is a
# serialized copy of the database, so a restore really brings old rows back and the
# ``previous_bookmark`` really undoes it. Audit events go to a mongomock Beanie
# database (``beanie_test_db``), because undo checks the bookmark against the audit
# log this site's own restores wrote.
#
# Covers TDD §11 "PR 5" restore: not production / local / not published →
# unavailable, the window (``PAW_CF_D1_TT_DAYS``), confirm name mismatch → 422,
# member → 403, ``previous_bookmark`` audited, undo with a foreign or made-up
# bookmark → 422, undo with the recorded one → restored, the 3 per 10 minutes cap →
# 429, caches dropped after a restore, and ``changes_since``.
# ruff: noqa: F811 — the shared fixtures are imported from test_site_data_write
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.sites import data_service  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.site_data import budget, d1  # noqa: E402

from tests.ee.sites.d1_sqlite_fake import SqliteD1  # noqa: E402
from tests.ee.sites.test_site_data_write import (  # noqa: E402,F401
    _DB,
    _DDL,
    _PROJECT_WIRE,
    _app,
    _call,
    _clean,
    _pocket,
    _raises,
    site_doc,
)


@pytest.fixture
def tt() -> SqliteD1:
    """A database with a bookmark two hours ago, then both notes deleted."""
    cf = SqliteD1(_DDL, database_id=_DB)
    cf.mark(datetime.now(UTC) - timedelta(hours=2))
    cf.execute("DELETE FROM notes")
    return cf


def _ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat()


def _kw(cf, **kw):
    return {
        "workspace_id": "ws1",
        "user_id": "u-admin",
        "pocket_id": "pk1",
        "_cloudflare": cf,
        **kw,
    }


async def _status(cf, at=None, wire=_PROJECT_WIRE, restore_point=None, **kw):
    extra = {"restore_point": restore_point} if restore_point is not None else {}
    with _pocket(wire, **kw):
        return await data_service.restore_status(**_kw(cf, at=at, **extra))


async def _restore(cf, at, name="Shop", pocket_id="pk1"):
    with _pocket():
        return await data_service.restore(**_kw(cf, at=at, confirm_name=name, pocket_id=pocket_id))


async def _undo(cf, bookmark, pocket_id="pk1", name="Shop"):
    with _pocket():
        return await data_service.restore_undo(
            **_kw(cf, previous_bookmark=bookmark, pocket_id=pocket_id, confirm_name=name)
        )


async def _events(action: str):
    from pocketpaw_ee.cloud.models.audit_event import AuditEvent

    return await AuditEvent.find({"action": action}).to_list()


def _notes(cf) -> int:
    return cf.rows("SELECT COUNT(*) AS n FROM notes")[0]["n"]


# --------------------------------------------------------------------------- status


@pytest.mark.asyncio
async def test_status_reports_the_window_and_site_name(site_doc, tt, beanie_test_db):
    s = await _status(tt)
    assert s.available and s.reason == "" and s.site_name == "Shop"
    assert s.window_days == 30 and s.bookmark is None and s.changes_since is None
    earliest = datetime.fromisoformat(s.earliest)
    assert (
        timedelta(days=29, hours=23) < datetime.now(UTC) - earliest < timedelta(days=30, minutes=1)
    )


@pytest.mark.asyncio
async def test_status_at_gives_the_bookmark_and_changes_since(site_doc, tt, beanie_test_db):
    from pocketpaw_ee.cloud.audit import service as audit_service

    for action, target, ws in [
        ("site_data.row_update", "pk1:notes:1", "ws1"),
        ("site_data.import", "pk1:notes", "ws1"),
        ("site_data.row_update", "pk10:notes:1", "ws1"),  # another site, shared prefix
        ("site_data.row_update", "pk1:notes:1", "ws2"),  # another workspace
        ("pockets.update", "pk1", "ws1"),  # not a data change
    ]:
        await audit_service.record(
            ws,
            "u",
            action,
            target_type="site_data" if action.startswith("site_data") else "pocket",
            target_id=target,
        )
    s = await _status(tt, at=_ago(hours=1))
    assert s.bookmark == tt._snapshots[0][1]
    assert s.changes_since == 2 and s.visitor_writes_counted is False


@pytest.mark.asyncio
async def test_alpha_database_is_unavailable(site_doc, tt, beanie_test_db):
    tt.version = "alpha"
    s = await _status(tt, at=_ago(hours=1))
    assert (s.available, s.reason, s.bookmark) == (False, "not_production", None)
    err = await _raises("sites.data_unavailable", 409, _restore(tt, _ago(hours=1)))
    assert err.details["reason"] == "not_production"
    assert tt.restores == []


@pytest.mark.asyncio
async def test_local_mode_and_not_published_are_unavailable(site_doc, monkeypatch, beanie_test_db):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    with _pocket():
        s = await data_service.restore_status(workspace_id="ws1", user_id="u", pocket_id="pk1")
    assert (s.available, s.reason) == (False, "live_on_cloudflare_only")
    monkeypatch.delenv("PAW_SITES_LOCAL")

    async def _no_db(workspace_id, pocket_id, pocket):
        return ""

    monkeypatch.setattr(sites_service, "_project_database_id", _no_db)
    cf = SqliteD1(_DDL, database_id=_DB)
    s = await _status(cf)
    assert (s.available, s.reason) == (False, "not_published")
    await _raises("sites.data_unavailable", 409, _restore(cf, _ago(hours=1)))
    assert cf.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("at", "code"),
    [
        ("yesterday", "sites.data_restore_bad_time"),
        ("31 days", "sites.data_restore_bad_time"),
    ],
)
async def test_bad_time_is_422(site_doc, tt, beanie_test_db, at, code):
    await _raises(code, 422, _status(tt, at=at))


@pytest.mark.asyncio
async def test_outside_the_window_is_422(site_doc, tt, beanie_test_db, monkeypatch):
    await _raises("sites.data_restore_out_of_window", 422, _status(tt, at=_ago(days=31)))
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await _raises("sites.data_restore_out_of_window", 422, _restore(tt, future))
    monkeypatch.setenv("PAW_CF_D1_TT_DAYS", "7")
    assert (await _status(tt)).window_days == 7
    await _raises("sites.data_restore_out_of_window", 422, _restore(tt, _ago(days=8)))
    assert tt.restores == []


def test_window_days_env(monkeypatch):
    for raw, days in [("", 30), ("7", 7), ("0", 1), ("90", 30), ("x", 30)]:
        monkeypatch.setenv("PAW_CF_D1_TT_DAYS", raw)
        assert data_service.window_days() == days


@pytest.mark.asyncio
async def test_no_edit_access_is_403(site_doc, tt, beanie_test_db):
    with pytest.raises(CloudError) as exc:
        await _status(tt, can_edit=False)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- restore


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "Shopp", "Sho", "shop site", "pk1"])
async def test_confirm_name_mismatch_is_422_before_any_cloudflare_call(
    site_doc, tt, beanie_test_db, name
):
    calls = tt.calls
    await _raises("sites.data_restore_confirm_mismatch", 422, _restore(tt, _ago(hours=1), name))
    assert tt.restores == [] and tt.calls == calls


@pytest.mark.asyncio
async def test_restore_brings_old_rows_back_and_audits_the_undo_bookmark(
    site_doc, tt, beanie_test_db
):
    assert _notes(tt) == 0
    out = await _restore(tt, _ago(hours=1), name="  shop ")  # case and spaces ignored
    assert _notes(tt) == 2
    assert out.bookmark == tt._snapshots[0][1] and out.previous_bookmark
    (event,) = await _events("site_data.restore")
    assert event.workspace == "ws1" and event.target_type == "site_data"
    assert event.target_id == "pk1" and event.actor_id == "u-admin"
    assert event.metadata["previous_bookmark"] == out.previous_bookmark
    assert event.metadata["database_id"] == _DB and event.metadata["at"].endswith("Z")


@pytest.mark.asyncio
async def test_undo_with_the_recorded_bookmark_goes_back(site_doc, tt, beanie_test_db):
    out = await _restore(tt, _ago(hours=1))
    assert _notes(tt) == 2
    undo = await _undo(tt, out.previous_bookmark)
    assert _notes(tt) == 0
    assert tt.restores[-1]["bookmark"] == out.previous_bookmark
    (event,) = await _events("site_data.restore_undo")
    assert event.metadata["undoes"] == out.previous_bookmark
    assert event.metadata["previous_bookmark"] == undo.previous_bookmark
    # and the undo itself can be undone (it is a restore this site recorded)
    await _undo(tt, undo.previous_bookmark)
    assert _notes(tt) == 2


@pytest.mark.asyncio
async def test_undo_refuses_a_bookmark_this_site_never_recorded(site_doc, tt, beanie_test_db):
    """Adversarial: undo can't be used to restore to an arbitrary bookmark."""
    real = tt._snapshots[0][1]  # a real bookmark of this database, never a restore's
    for bookmark in (real, "00000001-deadbeef", "x" * 200, "a'b", "$ne"):
        await _raises("sites.data_restore_unknown_bookmark", 422, _undo(tt, bookmark))
    assert tt.restores == []


@pytest.mark.asyncio
async def test_undo_refuses_another_sites_or_workspaces_bookmark(site_doc, tt, beanie_test_db):
    from pocketpaw_ee.cloud.audit import service as audit_service

    stolen = tt.mark()
    for ws, target in [("ws1", "pk2"), ("ws2", "pk1")]:
        await audit_service.record(
            ws,
            "u",
            "site_data.restore",
            target_type="site_data",
            target_id=target,
            metadata={"previous_bookmark": stolen, "database_id": _DB},
        )
    await _raises("sites.data_restore_unknown_bookmark", 422, _undo(tt, stolen))
    # recorded for this site but against another database (re-provisioned) is refused too
    await audit_service.record(
        "ws1",
        "u",
        "site_data.restore",
        target_type="site_data",
        target_id="pk1",
        metadata={"previous_bookmark": stolen, "database_id": "other-db"},
    )
    await _raises("sites.data_restore_unknown_bookmark", 422, _undo(tt, stolen))
    assert tt.restores == []


@pytest.mark.asyncio
async def test_three_restores_per_ten_minutes(site_doc, tt, beanie_test_db):
    out = None
    for _ in range(3):
        out = await _restore(tt, _ago(hours=1))
    err = await _raises("sites.data_restore_busy", 429, _restore(tt, _ago(hours=1)))
    assert err.details["retry_after"] > 0
    await _raises("sites.data_restore_busy", 429, _undo(tt, out.previous_bookmark))
    assert len(tt.restores) == 3


@pytest.mark.asyncio
async def test_restore_spends_the_data_budget(site_doc, tt, beanie_test_db, monkeypatch):
    monkeypatch.setattr(budget, "WORKSPACE_LIMIT", (1, 60))
    await _raises("sites.data_busy", 429, _restore(tt, _ago(hours=1)))
    assert tt.restores == []


@pytest.mark.asyncio
async def test_restore_drops_cached_schema_and_counts(site_doc, tt, beanie_test_db):
    with _pocket():
        before = await data_service.get_inventory(**_kw(tt))
    assert next(t.row_count for t in before.tables if t.name == "notes") == 0
    await _restore(tt, _ago(hours=1))
    assert not [k for k in d1._counts_cache if k[0] == _DB]
    assert not [k for k in d1._schema_cache if k[0] == _DB]
    with _pocket():
        after = await data_service.get_inventory(**_kw(tt))
    assert next(t.row_count for t in after.tables if t.name == "notes") == 2


# --------------------------------------------------------------------------- routes


@pytest.fixture
def routed_tt(site_doc, monkeypatch, tt):
    monkeypatch.setattr(sites_service, "_data_cf_client", lambda: tt)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: tt)
    return tt


_RESTORE_CALLS = [
    ("GET", "/restore", None),
    ("POST", "/restore", {"at": "2026-10-27T10:00:00Z", "confirm_name": "Shop"}),
    ("POST", "/restore", {"restore_point": "00000001-ab", "confirm_name": "Shop"}),
    ("GET", "/restore?restore_point=00000001-ab", None),
    ("POST", "/restore:undo", {"previous_bookmark": "00000001-ab", "confirm_name": "Shop"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "body"), _RESTORE_CALLS)
async def test_member_cannot_restore(routed_tt, beanie_test_db, method, path, body):
    resp = await _call(_app("member"), method, path, body)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "sites.data_restore_forbidden"
    assert routed_tt.restores == []


@pytest.mark.asyncio
async def test_route_shapes(routed_tt, beanie_test_db):
    app = _app("admin")
    at = _ago(hours=1)
    status = await _call(app, "GET", f"/restore?at={at.replace('+', '%2B')}")
    assert status.status_code == 200, status.text
    assert set(status.json()) == {
        "pocket_id",
        "available",
        "reason",
        "site_name",
        "window_days",
        "earliest",
        "at",
        "bookmark",
        "changes_since",
        "visitor_writes_counted",
    }
    done = await _call(app, "POST", "/restore", {"at": at, "confirm_name": "shop"})
    assert done.status_code == 200, done.text
    assert set(done.json()) == {"bookmark", "previous_bookmark"}
    undo = await _call(
        app,
        "POST",
        "/restore:undo",
        {"previous_bookmark": done.json()["previous_bookmark"], "confirm_name": "Shop"},
    )
    assert undo.status_code == 200, undo.text
    bad = await _call(app, "POST", "/restore", {"at": at})
    assert bad.status_code == 422  # restore without a confirm name
    extra = await _call(
        app, "POST", "/restore", {"at": at, "confirm_name": "Shop", "bookmark": "x"}
    )
    assert extra.status_code == 422


# --------------------------------------------------------------------------- review fixes


@pytest.mark.asyncio
async def test_earliest_is_raised_to_the_databases_creation(site_doc, tt, beanie_test_db):
    tt.mark(datetime.now(UTC) - timedelta(hours=5))  # a bookmark exists that far back
    created = datetime.now(UTC) - timedelta(hours=3)
    tt.created_at = created.isoformat()
    s = await _status(tt)
    assert abs(datetime.fromisoformat(s.earliest) - created) < timedelta(seconds=1)
    await _raises("sites.data_restore_out_of_window", 422, _status(tt, at=_ago(hours=4)))
    await _raises("sites.data_restore_out_of_window", 422, _restore(tt, _ago(hours=4)))
    assert tt.restores == []


@pytest.mark.asyncio
async def test_a_time_before_the_databases_history_is_out_of_window(site_doc, tt, beanie_test_db):
    """Live D1 answers code 7400 (the client raises out_of_window) for a moment with
    no bookmark: before the database existed, or past retention."""
    await _raises("sites.data_restore_out_of_window", 422, _status(tt, at=_ago(hours=5)))
    await _raises("sites.data_restore_out_of_window", 422, _restore(tt, _ago(hours=5)))
    assert tt.restores == []


@pytest.mark.asyncio
@pytest.mark.parametrize("at", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
async def test_extreme_times_are_bad_time_not_a_crash(site_doc, tt, beanie_test_db, at):
    await _raises("sites.data_restore_bad_time", 422, _status(tt, at=at))
    await _raises("sites.data_restore_bad_time", 422, _restore(tt, at))


@pytest.mark.asyncio
async def test_undo_needs_the_confirm_name(site_doc, tt, beanie_test_db):
    out = await _restore(tt, _ago(hours=1))
    await _raises(
        "sites.data_restore_confirm_mismatch", 422, _undo(tt, out.previous_bookmark, name="nope")
    )
    assert len(tt.restores) == 1


@pytest.mark.asyncio
async def test_undo_accepts_only_the_newest_restore(site_doc, tt, beanie_test_db):
    first = await _restore(tt, _ago(hours=1))
    second = await _restore(tt, _ago(minutes=90))
    await _raises("sites.data_restore_unknown_bookmark", 422, _undo(tt, first.previous_bookmark))
    await _undo(tt, second.previous_bookmark)
    assert tt.restores[-1]["bookmark"] == second.previous_bookmark


@pytest.mark.asyncio
async def test_an_undo_is_consumed(site_doc, tt, beanie_test_db):
    out = await _restore(tt, _ago(hours=1))
    await _undo(tt, out.previous_bookmark)
    await _raises("sites.data_restore_unknown_bookmark", 422, _undo(tt, out.previous_bookmark))
    assert len(tt.restores) == 2


@pytest.mark.asyncio
async def test_parallel_undos_run_once(site_doc, tt, beanie_test_db, monkeypatch):
    import asyncio

    from pocketpaw_ee.cloud.audit import service as audit_service

    out = await _restore(tt, _ago(hours=1))
    # Both requests read the audit log before either restores: a real race.
    real = audit_service.find_target_event
    arrived: list[int] = []
    both = asyncio.Event()

    async def _racing(*a, **kw):
        found = await real(*a, **kw)
        arrived.append(1)
        if len(arrived) >= 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return found

    monkeypatch.setattr(audit_service, "find_target_event", _racing)
    results = await asyncio.gather(
        _undo(tt, out.previous_bookmark), _undo(tt, out.previous_bookmark), return_exceptions=True
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, CloudError)]
    assert len(ok) == 1 and len(refused) == 1
    assert refused[0].code == "sites.data_restore_unknown_bookmark"
    assert len(tt.restores) == 2


@pytest.mark.asyncio
async def test_undo_expires_after_24_hours(site_doc, tt, beanie_test_db):
    from pocketpaw_ee.cloud.models.audit_event import AuditEvent

    stale = tt.mark()
    await AuditEvent(
        workspace="ws1",
        actor_id="u",
        action="site_data.restore",
        target_type="site_data",
        target_id="pk1",
        metadata={"previous_bookmark": stale, "database_id": _DB},
        at=datetime.now(UTC) - timedelta(hours=25),
    ).insert()
    await _raises("sites.data_restore_unknown_bookmark", 422, _undo(tt, stale))
    assert tt.restores == []


@pytest.mark.asyncio
async def test_a_cached_schema_follows_a_restore_done_by_another_worker(site_doc, beanie_test_db):
    """Another worker's restore can't be seen in this process's caches, but the
    schema cache is keyed by a fingerprint of ``sqlite_master``, which every read
    fetches first: a restore that changed the schema misses the cache at once, and
    one that didn't leaves a cache that is still true."""
    cf = SqliteD1("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT);", database_id=_DB)
    old = cf.mark(datetime.now(UTC) - timedelta(hours=2))
    cf.execute("ALTER TABLE t ADD COLUMN b TEXT")
    with _pocket():
        before = await data_service.get_table_schema(**_kw(cf, table="t"))
    assert [c.name for c in before.columns] == ["id", "a", "b"]
    await cf.d1_restore(_DB, bookmark=old)  # "another worker": no cache was dropped here
    with _pocket():
        after = await data_service.get_table_schema(**_kw(cf, table="t"))
    assert [c.name for c in after.columns] == ["id", "a"]


# --------------------------------------------------------------------------- by restore point
#
# The UI's "Restore to before this import / bulk delete" sends the restore_point the
# server took just before the first write, never the browser's clock.


async def _import_two(cf) -> str:
    """Import two notes; returns the import's restore point."""
    with _pocket():
        p = await data_service.import_preview(
            **_kw(cf, table="notes", filename="n.csv", data=b"body\nnew1\nnew2\n")
        )
        res = await data_service.import_commit(
            **_kw(cf, table="notes", upload_id=p.upload_id, mapping=p.mapping)
        )
    assert res.inserted == 2 and res.restore_point
    return res.restore_point


async def _restore_rp(cf, restore_point, name="Shop", pocket_id="pk1", **extra):
    with _pocket():
        return await data_service.restore(
            **_kw(cf, restore_point=restore_point, confirm_name=name, pocket_id=pocket_id, **extra)
        )


@pytest.mark.asyncio
async def test_restore_to_an_imports_restore_point(site_doc, tt, beanie_test_db):
    rp = await _import_two(tt)
    tt.execute("INSERT INTO notes (body) VALUES ('after')")  # a later change, also undone
    assert _notes(tt) == 3
    out = await _restore_rp(tt, rp, name=" shop ")
    assert _notes(tt) == 0  # exactly the moment before the import's first write
    assert tt.restores[-1] == {"bookmark": rp, "timestamp": None, "to": rp}
    (event,) = await _events("site_data.restore")
    assert event.metadata["restore_point"] == rp
    assert event.metadata["previous_bookmark"] == out.previous_bookmark
    assert event.metadata["database_id"] == _DB
    await _undo(tt, out.previous_bookmark)  # undo works as for a timestamp restore
    assert _notes(tt) == 3


@pytest.mark.asyncio
async def test_restore_to_a_bulk_deletes_restore_point(site_doc, tt, beanie_test_db):
    tt.execute("INSERT INTO notes (body) VALUES ('a'), ('b')")
    ids = [r["rowid"] for r in tt.rows("SELECT rowid FROM notes")]
    with _pocket():
        res = await data_service.delete_rows(**_kw(tt, table="notes", keys=ids))
    assert res.restore_point and _notes(tt) == 0
    await _restore_rp(tt, res.restore_point)
    assert _notes(tt) == 2


@pytest.mark.asyncio
async def test_import_and_bulk_delete_events_record_their_restore_point(
    site_doc, tt, beanie_test_db
):
    rp = await _import_two(tt)
    (imp,) = await _events("site_data.import")
    assert imp.metadata["restore_point"] == rp and imp.metadata["database_id"] == _DB
    keys = [r["rowid"] for r in tt.rows("SELECT rowid FROM notes")]
    with _pocket():
        res = await data_service.delete_rows(**_kw(tt, table="notes", keys=keys))
    deletes = await _events("site_data.row_delete")
    assert len(deletes) == 2
    assert {e.metadata.get("restore_point") for e in deletes} == {res.restore_point}
    assert {e.metadata.get("database_id") for e in deletes} == {_DB}


@pytest.mark.asyncio
async def test_a_foreign_pockets_restore_point_is_refused(site_doc, tt, beanie_test_db):
    from pocketpaw_ee.cloud.audit import service as audit_service

    stolen = tt.mark()
    for ws, target in [("ws1", "pk2:notes"), ("ws2", "pk1:notes"), ("ws1", "pk10:notes")]:
        await audit_service.record(
            ws,
            "u",
            "site_data.import",
            target_type="site_data",
            target_id=target,
            metadata={"restore_point": stolen, "database_id": _DB},
        )
    await _raises("sites.data_restore_unknown_bookmark", 422, _restore_rp(tt, stolen))
    # this pocket, but a restore point of another (re-provisioned) database
    await audit_service.record(
        "ws1",
        "u",
        "site_data.import",
        target_type="site_data",
        target_id="pk1:notes",
        metadata={"restore_point": stolen, "database_id": "other-db"},
    )
    await _raises("sites.data_restore_unknown_bookmark", 422, _restore_rp(tt, stolen))
    assert tt.restores == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bookmark", ["00000001-deadbeef", "x" * 201, "a'b", "$ne", "a/b", ""])
async def test_a_made_up_restore_point_is_refused(
    site_doc, tt, beanie_test_db, bookmark, monkeypatch
):
    from pocketpaw_ee.cloud.audit import service as audit_service

    real = tt._snapshots[0][1]  # a real bookmark of this database, no event recorded it
    await _raises("sites.data_restore_unknown_bookmark", 422, _restore_rp(tt, real))
    queried: list[dict] = []
    real_find = audit_service.find_target_event

    async def _spy(*a, **kw):
        queried.append(kw)
        return await real_find(*a, **kw)

    monkeypatch.setattr(audit_service, "find_target_event", _spy)
    await _raises("sites.data_restore_unknown_bookmark", 422, _restore_rp(tt, bookmark))
    if bookmark != "00000001-deadbeef":  # a bad shape never reaches the audit query
        assert queried == []
    assert tt.restores == []


@pytest.mark.asyncio
async def test_a_restore_or_undo_bookmark_is_not_a_restore_point(site_doc, tt, beanie_test_db):
    rp = await _import_two(tt)
    out = await _restore_rp(tt, rp)
    undo = await _undo(tt, out.previous_bookmark)
    for bookmark in (out.previous_bookmark, out.bookmark, undo.previous_bookmark):
        if bookmark == rp:
            continue  # the import's own point stays valid
        await _raises("sites.data_restore_unknown_bookmark", 422, _restore_rp(tt, bookmark))
    # Only import / bulk-delete events vouch for a restore point, even when a restore
    # or undo event carries one in its metadata.
    from pocketpaw_ee.cloud.audit import service as audit_service

    planted = tt.mark()
    for action in ("site_data.restore", "site_data.restore_undo", "site_data.row_update"):
        await audit_service.record(
            "ws1",
            "u",
            action,
            target_type="site_data",
            target_id="pk1",
            metadata={"restore_point": planted, "database_id": _DB},
        )
    await _raises("sites.data_restore_unknown_bookmark", 422, _restore_rp(tt, planted))
    assert len(tt.restores) == 2


@pytest.mark.asyncio
async def test_a_restore_point_outside_the_window_is_refused(site_doc, tt, beanie_test_db):
    from pocketpaw_ee.cloud.models.audit_event import AuditEvent

    old = tt.mark()
    await AuditEvent(
        workspace="ws1",
        actor_id="u",
        action="site_data.import",
        target_type="site_data",
        target_id="pk1:notes",
        metadata={"restore_point": old, "database_id": _DB},
        at=datetime.now(UTC) - timedelta(days=31),
    ).insert()
    await _raises("sites.data_restore_unknown_bookmark", 422, _restore_rp(tt, old))
    with pytest.raises(CloudError) as exc:
        await _status(tt, restore_point=old)
    assert exc.value.code == "sites.data_restore_unknown_bookmark"
    assert tt.restores == []


@pytest.mark.asyncio
async def test_both_or_neither_of_at_and_restore_point_is_a_bad_request(
    site_doc, tt, beanie_test_db
):
    rp = await _import_two(tt)
    with _pocket():
        for kw in ({"at": _ago(hours=1), "restore_point": rp}, {}):
            with pytest.raises(CloudError) as exc:
                await data_service.restore(**_kw(tt, confirm_name="Shop", **kw))
            assert (exc.value.code, exc.value.status_code) == (
                "sites.data_restore_bad_request",
                422,
            )
    with pytest.raises(CloudError) as exc:
        await _status(tt, at=_ago(hours=1), restore_point=rp)
    assert exc.value.code == "sites.data_restore_bad_request"
    assert tt.restores == []


@pytest.mark.asyncio
async def test_restore_point_confirm_mismatch(site_doc, tt, beanie_test_db):
    rp = await _import_two(tt)
    await _raises("sites.data_restore_confirm_mismatch", 422, _restore_rp(tt, rp, name="Shopp"))
    assert tt.restores == []


@pytest.mark.asyncio
async def test_status_for_a_restore_point(site_doc, tt, beanie_test_db):
    from pocketpaw_ee.cloud.audit import service as audit_service

    rp = await _import_two(tt)
    (imp,) = await _events("site_data.import")
    await audit_service.record(
        "ws1", "u", "site_data.row_update", target_type="site_data", target_id="pk1:notes:1"
    )
    s = await _status(tt, restore_point=rp)
    assert s.available and s.bookmark == rp
    at = imp.at if imp.at.tzinfo else imp.at.replace(tzinfo=UTC)
    assert abs(datetime.fromisoformat(s.at) - at) < timedelta(seconds=1)
    assert s.changes_since == 2  # the import itself and the update after it


@pytest.mark.asyncio
async def test_route_restore_by_restore_point(routed_tt, beanie_test_db):
    app = _app("admin")
    rp = await _import_two(routed_tt)
    status = await _call(app, "GET", f"/restore?restore_point={rp}")
    assert status.status_code == 200, status.text
    assert status.json()["bookmark"] == rp
    done = await _call(app, "POST", "/restore", {"restore_point": rp, "confirm_name": "Shop"})
    assert done.status_code == 200, done.text
    assert _notes(routed_tt) == 0
    both = await _call(
        app, "POST", "/restore", {"restore_point": rp, "at": _ago(hours=1), "confirm_name": "Shop"}
    )
    assert both.status_code == 422
    assert both.json()["error"]["code"] == "sites.data_restore_bad_request"
    neither = await _call(app, "POST", "/restore", {"confirm_name": "Shop"})
    assert neither.json()["error"]["code"] == "sites.data_restore_bad_request"
