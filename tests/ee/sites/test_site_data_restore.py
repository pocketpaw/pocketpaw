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


async def _status(cf, at=None, wire=_PROJECT_WIRE, **kw):
    with _pocket(wire, **kw):
        return await data_service.restore_status(**_kw(cf, at=at))


async def _restore(cf, at, name="Shop", pocket_id="pk1"):
    with _pocket():
        return await data_service.restore(**_kw(cf, at=at, confirm_name=name, pocket_id=pocket_id))


async def _undo(cf, bookmark, pocket_id="pk1"):
    with _pocket():
        return await data_service.restore_undo(
            **_kw(cf, previous_bookmark=bookmark, pocket_id=pocket_id)
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
    ("POST", "/restore:undo", {"previous_bookmark": "00000001-ab"}),
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
        app, "POST", "/restore:undo", {"previous_bookmark": done.json()["previous_bookmark"]}
    )
    assert undo.status_code == 200, undo.text
    bad = await _call(app, "POST", "/restore", {"at": at})
    assert bad.status_code == 422  # restore without a confirm name
    extra = await _call(
        app, "POST", "/restore", {"at": at, "confirm_name": "Shop", "bookmark": "x"}
    )
    assert extra.status_code == 422
