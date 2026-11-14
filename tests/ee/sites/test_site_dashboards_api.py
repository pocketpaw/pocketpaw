# tests/ee/sites/test_site_dashboards_api.py — Sites AI dashboards: the routes in
# ``sites.dashboard_router`` and ``sites.dashboard_service`` behind them.
#
# D1 is a real in-memory SQLite (``d1_sqlite_fake.SqliteD1``) and Mongo is mongomock
# (``beanie_test_db``), so saves, compare-and-set, revisions, runs, the shared cache and
# drift are exercised end to end. Covers: CRUD and permissions (member views, admin
# manages), If-Match / 409 / 428, the save-time gate (422 vs needs_fix), origin
# tracking, the heavy flag, runs (shaping, cache, epoch bump on Data-page writes,
# force, the dashboards' own bucket, 429 / 503), widget tests, health, revisions and
# restore, tenancy, audit, and the compiled Ripple spec.
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import dashboard_service as svc  # noqa: E402
from pocketpaw_ee.sites import data_service  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.site_data import budget, d1  # noqa: E402
from pocketpaw_ee.sites.site_data.dashboard_dto import WidgetMapping  # noqa: E402
from pocketpaw_ee.sites.site_data.dashboard_spec import compile_widget  # noqa: E402

from tests.ee.sites.d1_sqlite_fake import SqliteD1  # noqa: E402

_PATCH_GET = "pocketpaw_ee.cloud.pockets.service.get"
_PATCH_EDIT = "pocketpaw_ee.cloud.pockets.service.has_edit_access"
_DB = "11111111-2222-3333-4444-555555555555"
_WIRE = {
    "name": "Shop",
    "workspace": "ws1",
    "engine": "project",
    "siteMeta": {"project": {"template": "astro", "recipes": ["d1-drizzle"]}},
    "source": {"package.json": "{}"},
}
_DDL = """
CREATE TABLE orders (id INTEGER PRIMARY KEY, total INTEGER, status TEXT, created_at TEXT);
CREATE TABLE api_clients (id INTEGER PRIMARY KEY, label TEXT, api_key TEXT);
CREATE TABLE huge (id INTEGER PRIMARY KEY, v INTEGER);
INSERT INTO orders VALUES (1, 1200, 'paid', '2026-10-01'), (2, 800, 'paid', '2026-10-02'),
  (3, 300, 'refunded', '2026-10-02');
INSERT INTO api_clients VALUES (1, 'ci', 'key-live-123');
"""
_LAYOUT = {"x": 0, "y": 0, "w": 4, "h": 2}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    d1.clear_caches()
    budget.reset()
    svc.reset()
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_1")
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    yield
    d1.clear_caches()
    budget.reset()
    svc.reset()


@pytest.fixture
def db(monkeypatch) -> SqliteD1:
    cf = SqliteD1(_DDL, database_id=_DB)
    cf.executemany("INSERT INTO huge VALUES (?, ?)", [(i, i % 5) for i in range(10_050)])
    doc = SimpleNamespace(d1_database_id=_DB, kv_namespaces={}, r2_buckets={}, name="Shop")

    async def _doc(workspace_id, pocket_id):
        return doc

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _doc)
    monkeypatch.setattr(sites_service, "_data_cf_client", lambda: cf)
    monkeypatch.setattr(sites_service, "_cf_client", lambda: cf)
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
    def __init__(self, wire=_WIRE, *, can_edit: bool = True) -> None:
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


# --------------------------------------------------------------------------- app


class _Membership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


class _User:
    def __init__(self, role: str) -> None:
        self.id = "u1"
        self.active_workspace = "ws1"
        self.workspaces = [_Membership("ws1", role)]


def _app(role: str = "admin") -> FastAPI:
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


async def _call(app: FastAPI, method: str, path: str = "", body: Any = None, headers=None):
    base = "/api/v1/sites/by-pocket/pk1/data/dashboards"
    with _pocket():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(method, base + path, json=body, headers=headers or {})


def _w(sql: str, viz: str = "metric", **kw: Any) -> dict[str, Any]:
    return {"title": kw.pop("title", "W"), "viz": viz, "sql": sql, "layout": _LAYOUT, **kw}


async def _new(app: FastAPI, name: str = "Sales") -> dict[str, Any]:
    resp = await _call(app, "POST", "", {"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _patch(app, dash, widgets=None, *, version=None, name=None):
    body: dict[str, Any] = {}
    if widgets is not None:
        body["widgets"] = widgets
    if name is not None:
        body["name"] = name
    v = dash["version"] if version is None else version
    return await _call(app, "PATCH", f"/{dash['id']}", body, {"If-Match": str(v)})


# --------------------------------------------------------------------------- CRUD + permissions


@pytest.mark.asyncio
async def test_create_get_list(beanie_test_db, db, audit):
    app = _app("admin")
    dash = await _new(app)
    assert dash["version"] == 1 and dash["widgets"] == [] and dash["can_manage"] is True
    assert dash["schema_fingerprint"] and dash["generated"] is None
    assert dash["created_at"].endswith("Z")
    got = (await _call(app, "GET", f"/{dash['id']}")).json()
    assert got["id"] == dash["id"] and got["name"] == "Sales"
    listed = (await _call(app, "GET")).json()
    assert listed["unavailable_reason"] is None and listed["can_manage"] is True
    assert listed["dashboards"] == [
        {
            "id": dash["id"],
            "name": "Sales",
            "widget_count": 0,
            "updated_at": dash["updated_at"],
            "needs_fix_count": 0,
        }
    ]
    assert [e["action"] for e in audit] == ["site_dashboard.create"]
    assert audit[0]["target_type"] == "site_dashboard"


@pytest.mark.asyncio
async def test_member_views_but_cannot_manage(beanie_test_db, db, audit):
    dash = await _new(_app("admin"))
    member = _app("member")
    got = await _call(member, "GET", f"/{dash['id']}")
    assert got.status_code == 200 and got.json()["can_manage"] is False
    listed = (await _call(member, "GET")).json()
    assert listed["can_manage"] is False and len(listed["dashboards"]) == 1
    assert (await _call(member, "POST", f"/{dash['id']}/run", {})).status_code == 200
    for method, path, body in [
        ("POST", "", {"name": "x"}),
        ("PATCH", f"/{dash['id']}", {"name": "x"}),
        ("DELETE", f"/{dash['id']}", None),
        ("POST", f"/{dash['id']}/widgets:test", {"sql": "SELECT 1", "viz": "metric"}),
        ("POST", f"/{dash['id']}/revisions/1:restore", None),
    ]:
        resp = await _call(member, method, path, body, {"If-Match": "1"})
        assert resp.status_code == 403, (method, path, resp.text)
        assert resp.json()["error"]["code"] == "sites.dashboard_manage_forbidden"


def test_the_manage_permission_is_admin():
    from pocketpaw_ee.guards.actions import ACTIONS
    from pocketpaw_ee.guards.rbac import WorkspaceRole

    rule = ACTIONS["sites.dashboard_manage"]
    assert rule.minimum == WorkspaceRole.ADMIN


@pytest.mark.asyncio
async def test_manage_needs_edit_access_to_the_site(beanie_test_db, db, audit):
    with _pocket(can_edit=False):
        with pytest.raises(Exception) as exc:
            await svc.create_dashboard(workspace_id="ws1", user_id="u1", pocket_id="pk1", name="x")
    assert getattr(exc.value, "code", "") == "pocket.access_denied"


@pytest.mark.asyncio
async def test_another_workspace_or_site_is_404(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    with _pocket(wire={**_WIRE, "workspace": "ws2"}):
        with pytest.raises(Exception) as exc:
            await svc.get_dashboard(
                workspace_id="ws1", user_id="u1", pocket_id="pk1", dashboard_id=dash["id"]
            )
    assert getattr(exc.value, "status_code", 0) == 404
    with _pocket():
        with pytest.raises(Exception) as exc:
            await svc.get_dashboard(
                workspace_id="ws1", user_id="u1", pocket_id="pk2", dashboard_id=dash["id"]
            )
    assert getattr(exc.value, "status_code", 0) == 404
    assert (await _call(app, "GET", "/not-an-id")).status_code == 404


@pytest.mark.asyncio
async def test_delete(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    await _patch(app, dash, name="Renamed")
    assert (await _call(app, "DELETE", f"/{dash['id']}")).status_code == 204
    assert (await _call(app, "GET", f"/{dash['id']}")).status_code == 404
    from pocketpaw_ee.cloud.models.site_dashboard import SiteDashboardRevision

    assert await SiteDashboardRevision.find({"dashboard_id": dash["id"]}).count() == 0
    assert audit[-1]["action"] == "site_dashboard.delete"


# --------------------------------------------------------------------------- saving


@pytest.mark.asyncio
async def test_patch_saves_widgets_with_deps_status_and_spec(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    resp = await _patch(
        app,
        dash,
        [
            _w("SELECT count(*) AS n FROM orders", title="Orders"),
            _w(
                "SELECT status, sum(total) AS t FROM orders GROUP BY 1",
                "chart",
                title="By status {state.secret}",
                mapping={"label_col": "status", "value_col": "t", "format": "currency"},
            ),
        ],
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["version"] == 2
    a, b = out["widgets"]
    assert a["id"].startswith("w_") and a["origin"] == "user" and a["status"] == "ok"
    assert a["deps"] == ["orders"] and a["chart_type"] is None
    assert a["spec"]["ui"]["type"] == "metric"
    assert b["chart_type"] == "bar" and b["deps"] == ["orders", "orders.status", "orders.total"]
    assert b["spec"]["ui"]["props"]["data"] == "{state.data}"
    assert "{" not in b["spec"]["ui"]["props"]["title"]  # a title is never an expression
    assert audit[-1]["action"] == "site_dashboard.update"


@pytest.mark.asyncio
async def test_stale_if_match_is_409_with_the_current_dashboard(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    assert (await _patch(app, dash, name="One")).status_code == 200
    resp = await _patch(app, dash, name="Two")  # still sends version 1
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"] == "version_conflict" and body["error"]["code"] == "version_conflict"
    assert body["current"]["name"] == "One" and body["current"]["version"] == 2


@pytest.mark.asyncio
async def test_patch_without_if_match_is_428(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    resp = await _call(app, "PATCH", f"/{dash['id']}", {"name": "x"})
    assert resp.status_code == 428 and resp.json()["error"]["code"] == "precondition_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DELETE FROM orders WHERE 1", "not_a_select"),
        ("SELECT 1; DROP TABLE orders", "sql_multiple"),
        ("ATTACH 'x' AS y", "ddl_refused"),
        ("PRAGMA table_info(orders)", "not_a_select"),
        ("SELECT * FROM sqlite_master", "sql_internal_table"),
    ],
)
async def test_a_refused_widget_saves_nothing(beanie_test_db, db, audit, sql, reason):
    app = _app()
    dash = await _new(app)
    resp = await _patch(app, dash, [_w("SELECT 1"), _w(sql)])
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "sql_rejected" and err["details"]["reason"] == reason
    assert err["details"]["widget_index"] == 1
    got = (await _call(app, "GET", f"/{dash['id']}")).json()
    assert got["version"] == 1 and got["widgets"] == []
    assert db.rows("SELECT count(*) AS n FROM orders") == [{"n": 3}]


@pytest.mark.asyncio
async def test_an_unplannable_widget_saves_as_needs_fix(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    out = (await _patch(app, dash, [_w("SELECT count(*) FROM gone")])).json()
    w = out["widgets"][0]
    assert w["status"] == "needs_fix" and "gone" in w["status_detail"].lower()
    listed = (await _call(app, "GET")).json()
    assert listed["dashboards"][0]["needs_fix_count"] == 1


@pytest.mark.asyncio
async def test_origin_tracks_edits(beanie_test_db, db, audit):
    ai = {
        "id": "w_ai1",
        "title": "Orders",
        "viz": "metric",
        "chart_type": None,
        "sql": "SELECT count(*) AS n FROM orders",
        "mapping": {},
        "layout": _LAYOUT,
        "refresh": "open",
        "interval_s": 300,
        "origin": "ai",
        "deps": ["orders"],
        "status": "ok",
        "status_detail": None,
    }
    with _pocket():
        doc = await svc.create_dashboard(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", name="Gen", widgets=[ai]
        )
    app = _app()
    dash = (await _call(app, "GET", f"/{doc.id}")).json()
    moved = {**dash["widgets"][0], "layout": {"x": 4, "y": 0, "w": 4, "h": 2}}
    dash = (await _patch(app, dash, [moved])).json()
    assert dash["widgets"][0]["origin"] == "ai"  # moving isn't editing
    edited = {**dash["widgets"][0], "title": "All orders", "origin": "ai"}  # a client lies
    dash = (await _patch(app, dash, [edited, _w("SELECT 2")])).json()
    assert [w["origin"] for w in dash["widgets"]] == ["ai_edited", "user"]
    assert dash["widgets"][0]["id"] == "w_ai1"


@pytest.mark.asyncio
async def test_a_heavy_widget_gets_a_long_interval(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    out = (
        await _patch(
            app, dash, [_w("SELECT v, count(*) AS n FROM huge GROUP BY v", "chart", interval_s=60)]
        )
    ).json()
    w = out["widgets"][0]
    assert w["status"] == "heavy" and "huge" in w["status_detail"] and w["interval_s"] >= 900
    # Saving it again unchanged doesn't EXPLAIN it again.
    before = len(db.statements("EXPLAIN"))
    await _patch(app, out, out["widgets"])
    assert len(db.statements("EXPLAIN")) == before


@pytest.mark.asyncio
async def test_concurrent_creates_never_pass_the_cap(beanie_test_db, db, audit, monkeypatch):
    import asyncio

    monkeypatch.setattr(svc, "MAX_DASHBOARDS", 3)
    with _pocket():
        await svc.create_dashboard(workspace_id="ws1", user_id="u1", pocket_id="pk1", name="a")
        results = await asyncio.gather(
            *[
                svc.create_dashboard(workspace_id="ws1", user_id="u1", pocket_id="pk1", name=n)
                for n in "bcde"
            ],
            return_exceptions=True,
        )
    from pocketpaw_ee.cloud.models.site_dashboard import SiteDashboard

    assert await SiteDashboard.find({"site_pocket_id": "pk1"}).count() == 3
    refused = [r for r in results if isinstance(r, Exception)]
    assert refused and all(getattr(r, "code", "") == "sites.dashboard_limit" for r in refused)


@pytest.mark.asyncio
async def test_widget_count_is_capped(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    resp = await _patch(app, dash, [_w("SELECT 1")] * 41)
    assert resp.status_code == 422


# --------------------------------------------------------------------------- runs


async def _dash_with(app, widgets):
    dash = await _new(app)
    return (await _patch(app, dash, widgets)).json()


@pytest.mark.asyncio
async def test_run_shapes_every_widget_in_one_batch(beanie_test_db, db, audit):
    app = _app()
    dash = await _dash_with(
        app,
        [
            _w("SELECT count(*) AS n FROM orders"),
            _w("SELECT status, count(*) AS n FROM orders GROUP BY 1 ORDER BY 1", "chart"),
            _w("SELECT label, api_key FROM api_clients", "table"),
        ],
    )
    batches = len([s for s in db.sql if s[0].startswith("SELECT * FROM (")])
    resp = await _call(app, "POST", f"/{dash['id']}/run", {})
    assert resp.status_code == 200, resp.text
    res = resp.json()["results"]
    m, c, t = (res[w["id"]] for w in dash["widgets"])
    assert m["ok"] and m["data"] == {"value": 3} and m["cached_at"].endswith("Z")
    assert c["data"] == [{"label": "paid", "value": 2}, {"label": "refunded", "value": 1}]
    assert t["rows"] == [{"label": "ci", "api_key": "•••"}]
    assert "key-live" not in resp.text
    ran = [s for s in db.sql if s[0].startswith("SELECT * FROM (")]
    assert len(ran) - batches == 3


@pytest.mark.asyncio
async def test_runs_share_the_cache_until_the_owner_writes(beanie_test_db, db, audit):
    app = _app()
    dash = await _dash_with(app, [_w("SELECT count(*) AS n FROM orders")])
    wid = dash["widgets"][0]["id"]
    first = (await _call(app, "POST", f"/{dash['id']}/run", {})).json()["results"][wid]
    calls = db.calls
    again = (await _call(app, "POST", f"/{dash['id']}/run", {})).json()["results"][wid]
    assert db.calls == calls and again["cached_at"] == first["cached_at"]
    # A Data-page write bumps the epoch: the next run reads D1 again.
    with _pocket():
        await data_service.insert_row(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            table="orders",
            values={"total": 5, "status": "paid", "created_at": "2026-10-03"},
            _cloudflare=db,
        )
    fresh = (await _call(app, "POST", f"/{dash['id']}/run", {})).json()["results"][wid]
    assert fresh["data"] == {"value": 4}


@pytest.mark.asyncio
async def test_force_is_for_managers_and_has_a_floor(beanie_test_db, db, audit, monkeypatch):
    app = _app()
    dash = await _dash_with(app, [_w("SELECT count(*) AS n FROM orders")])
    wid = dash["widgets"][0]["id"]
    await _call(app, "POST", f"/{dash['id']}/run", {})
    db.execute("INSERT INTO orders (total) VALUES (1)")  # behind Paw's back
    cached = (await _call(app, "POST", f"/{dash['id']}/run", {})).json()["results"][wid]
    assert cached["data"] == {"value": 3}
    # A forced refresh right after a read still gets the shared result (15 s floor).
    soon = (await _call(app, "POST", f"/{dash['id']}/run", {"force": True})).json()
    assert soon["results"][wid]["data"] == {"value": 3}
    monkeypatch.setattr(svc, "FORCE_FLOOR_S", 0)
    member = (await _call(_app("member"), "POST", f"/{dash['id']}/run", {"force": True})).json()
    assert member["results"][wid]["data"] == {"value": 3}  # a viewer's force is ignored
    forced = (await _call(app, "POST", f"/{dash['id']}/run", {"force": True})).json()
    assert forced["results"][wid]["data"] == {"value": 4}


def test_results_are_shared_for_the_widget_interval():
    assert svc.refresh_floor({"interval_s": 60}) == 60
    assert svc.refresh_floor({"interval_s": 3600}) == 3600
    assert svc.refresh_floor({"interval_s": 60, "status": "heavy"}) == 900
    assert svc.refresh_floor({}) == 300


@pytest.mark.asyncio
async def test_a_cached_result_outlives_a_minute(beanie_test_db, db, audit, monkeypatch):
    """The TTL is the widget's interval, not min(interval, 60 s)."""
    from pocketpaw_ee.sites.site_data import dashboard_cache

    app = _app()
    dash = await _dash_with(app, [_w("SELECT count(*) AS n FROM orders", interval_s=1800)])
    await _call(app, "POST", f"/{dash['id']}/run", {})
    import time as _time

    (expires, _value) = next(
        v for k, v in dashboard_cache._local.items() if k.startswith("dash:res:")
    )
    assert expires - _time.monotonic() > 1700


@pytest.mark.asyncio
async def test_a_widget_batch_costs_one_token_per_statement(beanie_test_db, db, audit):
    app = _app()
    dash = await _dash_with(app, [_w(f"SELECT {i} AS n") for i in range(3)])
    budget.reset()
    svc.reset()
    d1.clear_caches()
    await _call(app, "POST", f"/{dash['id']}/run", {})
    # introspection: 2 calls; the widget batch: 3 statements.
    assert budget._local["cf:data:dash:ws:ws1"][0] == 5
    assert budget._local["cf:data:global"][0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("use_redis", [False, True])
async def test_statement_costs_are_atomic(use_redis, monkeypatch):
    from fakeredis import FakeAsyncRedis

    if use_redis:
        budget.use_redis(FakeAsyncRedis(decode_responses=True))
    monkeypatch.setattr(budget, "DASHBOARD_LIMIT", (4, 60))
    await budget.acquire("d1", "ws9", dashboard=True, statements=3)
    with pytest.raises(budget.DataBusy):
        await budget.acquire("d1", "ws9", dashboard=True, statements=2)  # 3 + 2 > 4
    await budget.acquire("d1", "ws9", dashboard=True)  # nothing was charged by the refusal
    with pytest.raises(budget.DataBusy):
        await budget.acquire("d1", "ws9", dashboard=True)


@pytest.mark.asyncio
async def test_run_selected_widgets_and_failures_per_widget(beanie_test_db, db, audit):
    app = _app()
    dash = await _dash_with(
        app,
        [
            _w("SELECT count(*) AS n FROM orders"),
            _w("SELECT nope FROM orders"),
            _w("SELECT count(*) FROM gone"),
        ],
    )
    ids = [w["id"] for w in dash["widgets"]]
    res = (await _call(app, "POST", f"/{dash['id']}/run", {})).json()["results"]
    assert res[ids[0]]["ok"] and not res[ids[1]]["ok"] and not res[ids[2]]["ok"]
    assert _DB not in res[ids[1]]["error"]
    one = (await _call(app, "POST", f"/{dash['id']}/run", {"widget_ids": [ids[0]]})).json()
    assert list(one["results"]) == [ids[0]]


@pytest.mark.asyncio
async def test_runs_spend_the_dashboard_bucket_not_the_workspace_one(beanie_test_db, db, audit):
    app = _app()
    dash = await _dash_with(app, [_w("SELECT 1 AS n")])
    budget.reset()
    svc.reset()
    await _call(app, "POST", f"/{dash['id']}/run", {})
    assert "cf:data:dash:ws:ws1" in budget._local and "cf:data:ws:ws1" not in budget._local


@pytest.mark.asyncio
async def test_a_spent_bucket_is_429_rate_limited(beanie_test_db, db, audit, monkeypatch):
    app = _app()
    dash = await _dash_with(app, [_w("SELECT 1 AS n")])
    svc.reset()
    monkeypatch.setattr(budget, "DASHBOARD_LIMIT", (0, 60))
    resp = await _call(app, "POST", f"/{dash['id']}/run", {"force": True})
    assert resp.status_code == 429
    err = resp.json()["error"]
    assert err["code"] == "rate_limited" and err["details"]["retry_after"] > 0


@pytest.mark.asyncio
async def test_an_unpublished_site_is_503(beanie_test_db, db, audit, monkeypatch):
    app = _app()
    dash = await _new(app)
    monkeypatch.setattr(sites_service, "_local_mode", lambda: True)
    resp = await _call(app, "POST", f"/{dash['id']}/run", {})
    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["code"] == "unavailable" and err["details"]["reason"] == "live_on_cloudflare_only"
    listed = (await _call(app, "GET")).json()
    assert listed["unavailable_reason"] == "live_on_cloudflare_only"


@pytest.mark.asyncio
async def test_an_empty_database_lists_no_tables(beanie_test_db, monkeypatch, audit):
    cf = SqliteD1("", database_id=_DB)
    doc = SimpleNamespace(d1_database_id=_DB, kv_namespaces={}, r2_buckets={}, name="Shop")

    async def _doc(workspace_id, pocket_id):
        return doc

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _doc)
    monkeypatch.setattr(sites_service, "_data_cf_client", lambda: cf)
    listed = (await _call(_app(), "GET")).json()
    assert listed == {"dashboards": [], "unavailable_reason": "no_tables", "can_manage": True}


# --------------------------------------------------------------------------- test widget


@pytest.mark.asyncio
async def test_widget_test_runs_fresh(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    resp = await _call(
        app,
        "POST",
        f"/{dash['id']}/widgets:test",
        {"sql": "SELECT status, total FROM orders", "viz": "table"},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "ok" and out["result"]["columns"] == ["status", "total"]
    assert [c["accessorKey"] for c in out["spec"]["ui"]["props"]["columns"]] == ["status", "total"]
    bad = await _call(
        app, "POST", f"/{dash['id']}/widgets:test", {"sql": "DROP TABLE orders", "viz": "table"}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "sql_rejected"
    miss = (
        await _call(
            app,
            "POST",
            f"/{dash['id']}/widgets:test",
            {"sql": "SELECT 1 AS a", "viz": "metric", "mapping": {"value_col": "b"}},
        )
    ).json()
    assert miss["status"] == "needs_fix" and "'b'" in miss["status_detail"]
    runs = [e for e in audit if e["action"] == "site_data.sql"]
    assert len(runs) == 2 and runs[0]["target_type"] == "site_query"
    assert runs[0]["metadata"]["source"] == "dashboard_test"
    assert runs[0]["metadata"]["sql"] == "SELECT status, total FROM orders"


# --------------------------------------------------------------------------- health


@pytest.mark.asyncio
async def test_health_reports_drift(beanie_test_db, db, audit):
    app = _app()
    dash = await _dash_with(
        app,
        [
            _w("SELECT count(*) AS n FROM orders"),
            _w("SELECT status, count(*) AS n FROM orders GROUP BY status", "chart"),
            _w("SELECT label FROM api_clients", "table"),
        ],
    )
    healthy = (await _call(app, "GET", f"/{dash['id']}/health")).json()
    assert healthy == {"schema_changed": False, "broken": [], "new_tables": []}
    db.execute("ALTER TABLE orders DROP COLUMN status")
    db.execute("DROP TABLE api_clients")
    db.execute("CREATE TABLE signups (id INTEGER PRIMARY KEY, at TEXT)")
    db.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, name TEXT)')
    out = (await _call(app, "GET", f"/{dash['id']}/health")).json()
    assert out["schema_changed"] is True and out["new_tables"] == ["signups"]
    broken = {b["widget_id"]: b["reason"] for b in out["broken"]}
    ids = [w["id"] for w in dash["widgets"]]
    assert ids[0] not in broken
    assert "orders.status" in broken[ids[1]] and "api_clients" in broken[ids[2]].lower()


# --------------------------------------------------------------------------- revisions


@pytest.mark.asyncio
async def test_revisions_and_restore(beanie_test_db, db, audit):
    app = _app()
    dash = await _new(app)
    v2 = (await _patch(app, dash, [_w("SELECT 1 AS a", title="One")])).json()
    v3 = (await _patch(app, v2, [_w("SELECT 2 AS b", title="Two")])).json()
    revs = (await _call(app, "GET", f"/{dash['id']}/revisions")).json()["revisions"]
    assert [r["version"] for r in revs] == [3, 2, 1]
    assert revs[0]["actor"] == "u1" and revs[0]["reason"] == "edit"
    assert revs[2]["reason"] == "create"
    resp = await _call(app, "POST", f"/{dash['id']}/revisions/2:restore")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["version"] == 4 and [w["title"] for w in out["widgets"]] == ["One"]
    assert out["widgets"][0]["id"] == v2["widgets"][0]["id"]
    assert v3["version"] == 3
    assert (await _call(app, "POST", f"/{dash['id']}/revisions/99:restore")).status_code == 404
    assert audit[-1]["action"] == "site_dashboard.restore"


@pytest.mark.asyncio
async def test_only_the_last_20_revisions_are_kept(beanie_test_db, db, audit):
    from pocketpaw_ee.cloud.models.site_dashboard import SiteDashboardRevision

    app = _app()
    dash = await _new(app)
    for i in range(25):
        dash = (await _patch(app, dash, name=f"n{i}")).json()
    assert await SiteDashboardRevision.find({"dashboard_id": dash["id"]}).count() == 20
    revs = (await _call(app, "GET", f"/{dash['id']}/revisions")).json()["revisions"]
    assert revs[0]["version"] == 26 and len(revs) == 21


# --------------------------------------------------------------------------- spec


@pytest.mark.parametrize(
    "viz", ["metric", "stat", "chart", "table", "sparkline", "gauge", "funnel"]
)
def test_compiled_specs_are_valid_ripple(viz):
    from pocketpaw_ee.cloud.ripple_validator import validate_ripple_spec

    spec = compile_widget(viz, "line", "Revenue {state.x}", WidgetMapping(), ["a", "b{c}"])
    assert validate_ripple_spec(spec) == []
    assert spec["ui"]["type"] in {"metric", "stat", "chart", "table", "gauge", "funnel"}
    flat = repr(spec)
    assert "{state.x}" not in flat and "b{c}" not in flat


@pytest.mark.asyncio
async def test_a_big_save_survives_a_spent_bucket(beanie_test_db, db, audit, monkeypatch):
    """The EXPLAIN heavy check is advice: when the dashboards' bucket runs out mid-save,
    the save still lands (unchecked widgets read ``ok``)."""
    app = _app()
    dash = await _new(app)
    budget.reset()
    monkeypatch.setattr(budget, "DASHBOARD_LIMIT", (2, 60))
    widgets = [_w(f"SELECT {i} AS n FROM orders") for i in range(6)]
    resp = await _patch(app, dash, widgets)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["widgets"]) == 6
