# tests/ee/sites/test_project_d1_export.py — a project site's own D1 is exported as a
# SQL dump before a delete drops it.
#
# A project site (the d1-drizzle recipe) declares no ripple ``objects``, so the export
# used to treat it as static: ``tables`` came back empty, the export read ``ready``, and
# the cascade then deleted a database full of rows nobody kept. These pin the fix: the
# D1 export API's SQL dump rides in the export bundle, an export that cannot take it
# fails (and so blocks the delete), and only an explicit ``force`` deletes anyway.
# Cloudflare is mocked at its boundary (httpx.MockTransport for the client, a stub with
# ``export_d1_sql`` for the service).
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_export import SiteExport
from pocketpaw_ee.sites import delete_job as delete_job_mod
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

ACCT = "acct_1"
DB = "6f1e4b1e-0000-4000-8000-000000000001"
SIGNED = "https://d1-exports.example.r2.dev/dump.sql?X-Amz-Signature=abc"
DUMP = (
    "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);\nINSERT INTO notes VALUES(1,'keep');\n"
)
OK = {"success": True, "errors": [], "messages": []}


def _client(handler) -> CloudflareClient:
    return CloudflareClient(
        account_id=ACCT,
        api_token="tok_secret",
        zone_id="zone",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(handler),
    )


# ── the client: D1 export API, polling, signed-url download ─────────────────────


@pytest.mark.asyncio
async def test_export_d1_sql_polls_until_complete_and_downloads_without_the_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        if request.url.host == "api.cloudflare.com":
            assert request.method == "POST"
            assert request.url.path == f"/client/v4/accounts/{ACCT}/d1/database/{DB}/export"
            body = json.loads(request.content)
            assert body["output_format"] == "polling"
            if "current_bookmark" not in body:
                return httpx.Response(
                    200, json={**OK, "result": {"at_bookmark": "bm-1", "status": "active"}}
                )
            assert body["current_bookmark"] == "bm-1"
            return httpx.Response(
                200,
                json={
                    **OK,
                    "result": {
                        "at_bookmark": "bm-1",
                        "status": "complete",
                        "result": {"filename": "dump.sql", "signed_url": SIGNED},
                    },
                },
            )
        assert str(request.url) == SIGNED
        return httpx.Response(200, content=DUMP.encode())

    sql = await _client(handler).export_d1_sql(DB, poll_interval=0)

    assert sql == DUMP
    download = seen[-1]
    # The signed URL is a different host: the account token must never go there.
    assert "authorization" not in {k.lower() for k in download.headers}
    assert [r.url.host for r in seen].count("api.cloudflare.com") == 2


@pytest.mark.asyncio
async def test_export_d1_sql_raises_on_an_export_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={**OK, "result": {"at_bookmark": "b", "status": "error", "error": "boom"}},
        )

    with pytest.raises(ValidationError) as exc:
        await _client(handler).export_d1_sql(DB, poll_interval=0)
    assert exc.value.code == "sites.d1_export_failed"


@pytest.mark.asyncio
async def test_export_d1_sql_gives_up_after_the_poll_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**OK, "result": {"at_bookmark": "b", "status": "active"}})

    with pytest.raises(ValidationError) as exc:
        await _client(handler).export_d1_sql(DB, poll_interval=0, max_polls=3)
    assert exc.value.code == "sites.d1_export_failed"


@pytest.mark.asyncio
async def test_export_d1_sql_refuses_a_non_https_download_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **OK,
                "result": {
                    "status": "complete",
                    "result": {"filename": "d.sql", "signed_url": "http://169.254.169.254/x"},
                },
            },
        )

    with pytest.raises(ValidationError) as exc:
        await _client(handler).export_d1_sql(DB, poll_interval=0)
    assert exc.value.code == "sites.d1_export_failed"


# ── the service: a project site's export carries the dump, or fails ─────────────


WS = "ws_export"
OWNER = "u_owner"


class _Adapter:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        body = b""
        async for chunk in stream:
            body += chunk
        self.objects[key] = body


class _CF:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.exported: list[str] = []

    async def export_d1_sql(self, database_id: str) -> str:
        self.exported.append(database_id)
        if self.fails:
            raise ValidationError("sites.d1_export_failed", "Cloudflare said no")
        return DUMP


@pytest.fixture
def adapter(monkeypatch) -> _Adapter:
    store = _Adapter()

    async def _not_dynamic(**_kw: Any) -> Any:
        raise ValidationError("sites.not_dynamic", "no objects")

    monkeypatch.setattr(sites_service, "_dynamic_pocket_objects", _not_dynamic)
    monkeypatch.setattr(sites_service, "_export_adapter", lambda: store)
    return store


async def _site(d1: str = DB, *, workspace: str = WS) -> Site:
    doc = Site(
        workspace=workspace,
        pocket_id="pk-project",
        owner=OWNER,
        name="Notes",
        script_name="paw-site-notes",
        d1_database_id=d1,
    )
    await doc.insert()
    return doc


@pytest.mark.asyncio
async def test_a_project_site_export_carries_its_d1_sql_dump(beanie_test_db, adapter):
    site = await _site()
    cf = _CF()

    out = await sites_service.create_site_export(
        workspace_id=WS, user_id=OWNER, site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "ready"
    assert out.database_dump == "sql"
    assert cf.exported == [DB]
    (payload,) = adapter.objects.values()
    bundle = json.loads(payload)
    assert bundle["database"] == {"format": "sql", "database_id": DB, "sql": DUMP}
    (key,) = adapter.objects
    assert key.startswith(f"site-exports/{WS}/")
    assert out.expires_at is not None


@pytest.mark.asyncio
async def test_a_failed_d1_dump_fails_the_export(beanie_test_db, adapter):
    site = await _site()

    with pytest.raises(ValidationError) as exc:
        await sites_service.create_site_export(
            workspace_id=WS, user_id=OWNER, site_id=str(site.id), _cloudflare=_CF(fails=True)
        )

    assert exc.value.code == "sites.export_unavailable"
    (row,) = await SiteExport.find_all().to_list()
    assert row.status == "failed"
    assert adapter.objects == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("d1", ["", "placeholder"])
async def test_a_site_without_a_created_database_exports_no_dump(beanie_test_db, adapter, d1):
    if d1 == "placeholder":
        d1 = sites_service._derive_d1_database_id(WS, "pk-project")
    site = await _site(d1)
    cf = _CF(fails=True)

    out = await sites_service.create_site_export(
        workspace_id=WS, user_id=OWNER, site_id=str(site.id), _cloudflare=cf
    )

    assert out.status == "ready"
    assert out.database_dump == ""
    assert cf.exported == []


@pytest.mark.asyncio
async def test_another_workspace_cannot_export_the_site(beanie_test_db, adapter):
    from pocketpaw_ee.cloud._core.errors import NotFound

    site = await _site()
    cf = _CF()

    with pytest.raises(NotFound):
        await sites_service.create_site_export(
            workspace_id="ws_other", user_id=OWNER, site_id=str(site.id), _cloudflare=cf
        )
    assert cf.exported == []


# ── the delete: refused on a failed export unless forced ─────────────────────────


class _Deps:
    def __init__(self) -> None:
        self.calls: list[str] = []
        outer = self

        class _CFRec:
            def __getattr__(self, name: str) -> Any:
                async def _call(*_a: Any, **_kw: Any) -> None:
                    outer.calls.append(name)

                return _call

        self.cloudflare = _CFRec()
        self.assets = None

    async def purge_records(self, *, workspace_id: str, site_id: str) -> None:
        self.calls.append("purge_records")


def _failing_export(monkeypatch) -> None:
    async def _fail(**_kw: Any) -> Any:
        raise ValidationError("sites.export_unavailable", "could not export")

    monkeypatch.setattr(sites_service, "create_site_export", _fail)


@pytest.mark.asyncio
async def test_a_failed_export_refuses_the_delete(beanie_test_db, monkeypatch):
    site = await _site()
    _failing_export(monkeypatch)
    deps = _Deps()

    await delete_job_mod.run_site_delete({}, WS, str(site.id), _deps=deps)

    row = await Site.get(site.id)
    assert row is not None
    assert row.delete_status == "failed"
    assert row.delete_reason == "export:sites_export_unavailable"
    assert deps.calls == []


@pytest.mark.asyncio
async def test_a_forced_delete_goes_ahead_without_the_export(beanie_test_db, monkeypatch):
    site = await _site()
    _failing_export(monkeypatch)
    deps = _Deps()

    await delete_job_mod.run_site_delete({}, WS, str(site.id), True, _deps=deps)

    assert await Site.get(site.id) is None
    assert "delete_database" in deps.calls


@pytest.mark.asyncio
async def test_force_reaches_the_job(beanie_test_db):
    site = await _site()
    enqueued: list[tuple] = []

    class _Pool:
        async def enqueue_job(self, *args: Any, **_kw: Any) -> Any:
            enqueued.append(args)
            return object()

    await delete_job_mod.enqueue_site_delete(site, force=True, _pool_override=_Pool())

    (args,) = enqueued
    assert args == (delete_job_mod.SITE_DELETE_FUNCTION_NAME, WS, str(site.id), True)


@pytest.mark.asyncio
async def test_start_site_delete_threads_force(beanie_test_db, monkeypatch):
    site = await _site()
    seen: dict[str, Any] = {}

    async def _enqueue(doc: Any, **kw: Any) -> str:
        seen.update(kw)
        return "job-1"

    monkeypatch.setattr(delete_job_mod, "enqueue_site_delete", _enqueue)

    await sites_service.start_site_delete(
        workspace_id=WS, user_id=OWNER, site_id=str(site.id), force=True
    )

    assert seen == {"force": True}
