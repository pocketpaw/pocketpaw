# Tests for pushing Durable Object platform vars to a LIVE script without a redeploy
# (``durable_objects.set_platform_vars_live`` over the script settings API), and the
# three callers: the usage sweep's throttle flip, PAW_SITE_ORIGINS on deploys and
# drafts, and the origins refresh when a custom domain goes live or is removed.
# Cloudflare is faked; nothing real is called.
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import bundle_deploy, do_metering, draft_worker, preview_origin
from pocketpaw_ee.sites import durable_objects as dobj
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

from tests.ee.sites.test_do_metering import _plain, _UsageCF
from tests.ee.sites.test_do_metering import _seed as _seed_site
from tests.ee.sites.test_draft_worker import POCKET, FakeCF, Store, _build
from tests.ee.sites.test_durable_objects import _CF, _metadata, do_build  # noqa: F401
from tests.ee.sites.test_durable_objects_lifecycle import V1, _draft_manifest, _drafts  # noqa: F401
from tests.ee.sites.test_project_engine import _Records

ACCT = "acct_1"
DAY = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(dobj.FLAG_ENV, "1")
    monkeypatch.setenv(do_metering.DAILY_REQUESTS_ENV[False], "100")
    do_metering._reset()


LIVE_BINDINGS = [
    {"type": "d1", "name": "DB", "id": "db-1"},
    {"type": "durable_object_namespace", "name": "ROOM", "class_name": "Room"},
    {"type": "secret_text", "name": "API_KEY"},  # the GET leaves the value out
    {"type": "secret_text", "name": "STRIPE", "text": "redacted"},
    {"type": "secret_key", "name": "SIGNING", "algorithm": {"name": "HMAC"}},
    {"type": "plain_text", "name": "PAW_DO_THROTTLED", "text": "0"},
    {"type": "plain_text", "name": "OTHER", "text": "x"},
    {"type": "assets", "name": "ASSETS"},
]


class _SettingsCF:
    """GET / PATCH script settings, recorded."""

    def __init__(self, bindings=None, *, get_fails=False, patch_fails=False):
        self.bindings = [dict(b) for b in (bindings if bindings is not None else LIVE_BINDINGS)]
        self.get_fails, self.patch_fails = get_fails, patch_fails
        self.calls: list[tuple[str, Any]] = []

    async def get_script_settings(self, script_name, *, target):
        self.calls.append(("get", (script_name, target)))
        if self.get_fails:
            raise ValidationError("sites.cloudflare_error", "Cloudflare API 500")
        return {"bindings": [dict(b) for b in self.bindings], "compatibility_date": "2026-09-01"}

    async def patch_script_settings(self, script_name, settings, *, target):
        self.calls.append(("patch", (script_name, target, settings)))
        if self.patch_fails:
            raise ValidationError("sites.cloudflare_error", "Cloudflare API 500")
        return settings

    def patches(self) -> list[dict]:
        return [c[1][2] for c in self.calls if c[0] == "patch"]


# ----------------------------------------------------------------- helper


@pytest.mark.asyncio
async def test_live_push_keeps_every_binding_and_only_changes_the_var():
    cf = _SettingsCF()
    await dobj.set_platform_vars_live(cf, "s1", target="dispatch", values={"PAW_DO_THROTTLED": "1"})
    assert [c[0] for c in cf.calls] == ["get", "patch"]
    (settings,) = cf.patches()
    assert set(settings) == {"bindings"}  # nothing else is patched
    sent = settings["bindings"]
    assert sent == [
        {"type": "d1", "name": "DB", "id": "db-1"},
        {"type": "durable_object_namespace", "name": "ROOM", "class_name": "Room"},
        # Secrets are never re-sent: inherited from the live version, untouched.
        {"type": "inherit", "name": "API_KEY"},
        {"type": "inherit", "name": "STRIPE"},
        {"type": "inherit", "name": "SIGNING"},
        {"type": "plain_text", "name": "OTHER", "text": "x"},
        {"type": "assets", "name": "ASSETS"},
        {"type": "plain_text", "name": "PAW_DO_THROTTLED", "text": "1"},
    ]
    assert not any("redacted" in json.dumps(b) for b in sent)


@pytest.mark.asyncio
async def test_live_push_adds_a_var_the_script_does_not_have_yet():
    cf = _SettingsCF([{"type": "d1", "name": "DB", "id": "db-1"}])
    await dobj.set_platform_vars_live(
        cf, "s1", target="account", values={"PAW_SITE_ORIGINS": "https://a.example"}
    )
    (settings,) = cf.patches()
    assert settings["bindings"][-1] == {
        "type": "plain_text",
        "name": "PAW_SITE_ORIGINS",
        "text": "https://a.example",
    }


@pytest.mark.asyncio
async def test_live_push_raises_when_the_read_fails_and_patches_nothing():
    cf = _SettingsCF(get_fails=True)
    with pytest.raises(ValidationError):
        await dobj.set_platform_vars_live(cf, "s1", target="account", values={"X_VAR": "1"})
    assert cf.patches() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,path",
    [
        ("dispatch", "/workers/dispatch/namespaces/paw-sites/scripts/s1/settings"),
        ("account", "/workers/scripts/s1/settings"),
    ],
)
async def test_client_settings_get_and_multipart_patch(target, path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return httpx.Response(
            200, json={"success": True, "result": {"bindings": [{"type": "d1", "name": "DB"}]}}
        )

    client = CloudflareClient(
        account_id=ACCT,
        api_token="tok",
        zone_id="z",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(handler),
    )
    got = await client.get_script_settings("s1", target=target)
    assert got["bindings"] == [{"type": "d1", "name": "DB"}]
    await client.patch_script_settings("s1", {"bindings": []}, target=target)
    get, patch = seen
    assert get.method == "GET" and get.url.path.endswith(path)
    assert patch.method == "PATCH" and patch.url.path.endswith(path)
    assert patch.headers["content-type"].startswith("multipart/form-data")
    boundary = re.search(r"boundary=(.+)$", patch.headers["content-type"]).group(1)
    part = next(c for c in patch.content.split(boundary.encode()) if b'name="settings"' in c)
    assert json.loads(part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]) == {"bindings": []}


# ------------------------------------------------------------- the sweep


class _SweepCF(_SettingsCF, _UsageCF):
    def __init__(self, *, requests, patch_fails=False):
        _UsageCF.__init__(self, requests=requests)
        _SettingsCF.__init__(self, patch_fails=patch_fails)


@pytest.mark.asyncio
async def test_a_throttle_flip_reaches_the_live_script_at_once(beanie_test_db):
    from pocketpaw_ee.cloud.models.site import Site

    site = await _seed_site("site-a")
    cf = _SweepCF(requests={"ns-a": 500})
    await do_metering.sweep_do_usage(cf=cf, now=DAY)
    site = await Site.get(site.id)
    assert site.do_throttled is True
    assert [c[0] for c in cf.calls] == ["get", "patch"]
    assert cf.calls[0][1] == ("site-a", "dispatch")
    sent = {b["name"]: b for b in cf.patches()[0]["bindings"]}
    assert sent["PAW_DO_THROTTLED"] == {
        "type": "plain_text",
        "name": "PAW_DO_THROTTLED",
        "text": "1",
    }

    # Back under the ceiling (a new day): pushed again, as "0".
    do_metering._reset()
    cf2 = _SweepCF(requests={"ns-a": 1})
    await do_metering.sweep_do_usage(cf=cf2, now=DAY + timedelta(days=1))
    assert (await Site.get(site.id)).do_throttled is False
    assert {b["name"]: b for b in cf2.patches()[0]["bindings"]}["PAW_DO_THROTTLED"]["text"] == "0"


@pytest.mark.asyncio
async def test_no_settings_call_when_the_flag_does_not_change(beanie_test_db):
    await _seed_site("site-a")
    cf = _SweepCF(requests={"ns-a": 1})
    await do_metering.sweep_do_usage(cf=cf, now=DAY)
    assert cf.calls == []


@pytest.mark.asyncio
async def test_a_failed_push_keeps_the_stored_flag_so_the_next_sweep_retries(beanie_test_db):
    from pocketpaw_ee.cloud.models.site import Site

    site = await _seed_site("site-a")
    cf = _SweepCF(requests={"ns-a": 500}, patch_fails=True)
    out = await do_metering.sweep_do_usage(cf=cf, now=DAY)
    site = await Site.get(site.id)
    assert site.do_throttled is False  # unchanged: the live script was not told
    assert site.do_usage["2026-10-08"]["requests"] == 500  # usage is still recorded
    assert out["throttled"] == 0

    do_metering._reset()
    ok = _SweepCF(requests={"ns-a": 500})
    out = await do_metering.sweep_do_usage(cf=ok, now=DAY + timedelta(hours=2))
    assert (await Site.get(site.id)).do_throttled is True and out["throttled"] == 1


# ---------------------------------------------------------- PAW_SITE_ORIGINS


@pytest.mark.asyncio
async def test_a_do_deploy_sets_site_origins_from_a_list_or_a_callable(do_build):  # noqa: F811
    cf = _CF(tag="v1")
    await bundle_deploy.deploy_bundle(
        cf.client(),
        script_name="s",
        build_dir=do_build,
        salt="ws",
        site_origins=["https://a.example", "https://b.example"],
    )
    meta = _metadata(next(r for r in cf.requests if r.method == "PUT"))
    assert _plain(meta)["PAW_SITE_ORIGINS"] == "https://a.example,https://b.example"

    calls = []

    async def origins():
        calls.append(1)
        return ["https://c.example"]

    cf = _CF(tag="v1")
    await bundle_deploy.deploy_bundle(
        cf.client(), script_name="s", build_dir=do_build, salt="ws", site_origins=origins
    )
    meta = _metadata(next(r for r in cf.requests if r.method == "PUT"))
    assert _plain(meta)["PAW_SITE_ORIGINS"] == "https://c.example" and calls == [1]


@pytest.mark.asyncio
async def test_origins_are_never_computed_for_a_bundle_without_dos(do_build):  # noqa: F811
    manifest = json.loads((do_build / "paw-build.json").read_text())
    manifest.pop("durableObjects")
    manifest["bindingRequests"] = []
    (do_build / "paw-build.json").write_text(json.dumps(manifest))

    async def origins():
        raise AssertionError("not for a bundle without DOs")

    cf = _CF()
    await bundle_deploy.deploy_bundle(
        cf.client(), script_name="s", build_dir=do_build, salt="ws", site_origins=origins
    )
    assert "PAW_SITE_ORIGINS" not in _plain(
        _metadata(next(r for r in cf.requests if r.method == "PUT"))
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_draft_allows_only_its_own_preview_origin():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    token = store.tokens[(POCKET, "h1")]
    put = cf.named("put_worker")[-1]
    plain = {b["name"]: b["text"] for b in put["bindings"] if b["type"] == "plain_text"}
    assert plain["PAW_SITE_ORIGINS"] == f"https://{token}.preview.paw.test"
    assert preview_origin.preview_url_for(token).startswith(plain["PAW_SITE_ORIGINS"])


@pytest.mark.asyncio
async def test_publish_sets_the_public_origin_and_live_custom_domains(beanie_test_db, monkeypatch):
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import SiteDomain
    from pocketpaw_ee.sites import service as sites_service

    from tests.ee.sites.test_durable_objects_lifecycle import (
        DO_BASE,
        _do_manifest,
        _DOFake,
        _first,
    )
    from tests.ee.sites.test_project_d1 import _publish

    monkeypatch.setenv("PAW_CF_SITES_DOMAIN", "sites.paw.test")
    (pocket_id, site_id), source = await _first(monkeypatch, _do_manifest(DO_BASE, V1))
    cf = _DOFake()
    await _publish(pocket_id, site_id, source, cf.client())
    assert _plain(cf.metadata[-1])["PAW_SITE_ORIGINS"] == f"https://{site_id}.sites.paw.test"

    doc = await sites_service._SiteDoc.find_one({"_id": ObjectId(site_id)})
    await doc.set(
        {
            # Server code on a custom domain needs the Site plan.
            "plan_tier": "site",
            "subscription_status": "active",
            "domains": [
                SiteDomain(hostname="shop.example.com", status="live").model_dump(),
                SiteDomain(hostname="new.example.com", status="pending").model_dump(),
            ],
        }
    )
    await _publish(pocket_id, site_id, source, cf.client())
    assert _plain(cf.metadata[-1])["PAW_SITE_ORIGINS"] == (
        f"https://{site_id}.sites.paw.test,https://shop.example.com"
    )


class _DomainCF(_SettingsCF):
    def __init__(self, status: str = "live", **kw):
        super().__init__(**kw)
        self.status = status

    async def get_hostname_status(self, _id):
        from pocketpaw_ee.sites.domain import HostnameStatus

        return HostnameStatus(self.status)

    async def delete_worker_route(self, _id):
        return None

    async def delete_custom_hostname(self, _id):
        return None


async def _domain_site(**kw):
    from pocketpaw_ee.cloud.models.site import SiteDomain

    return await _seed_site(
        "site-d",
        domains=[SiteDomain(hostname="shop.example.com", cf_hostname_id="h1", status="verifying")],
        **kw,
    )


@pytest.mark.asyncio
async def test_a_domain_going_live_refreshes_the_origins_without_a_redeploy(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setenv("PAW_CF_SITES_DOMAIN", "sites.paw.test")
    site = await _domain_site()
    cf = _DomainCF("live")
    await sites_service.domain_status(
        workspace_id="ws1", site_id=str(site.id), hostname="shop.example.com", _cloudflare=cf
    )
    sent = {b["name"]: b for b in cf.patches()[0]["bindings"]}
    assert sent["PAW_SITE_ORIGINS"]["text"] == (
        f"https://{site.id}.sites.paw.test,https://shop.example.com"
    )
    assert sent["API_KEY"] == {"type": "inherit", "name": "API_KEY"}
    # Polling again with no change: no settings call.
    cf2 = _DomainCF("live")
    await sites_service.domain_status(
        workspace_id="ws1", site_id=str(site.id), hostname="shop.example.com", _cloudflare=cf2
    )
    assert cf2.calls == []


@pytest.mark.asyncio
async def test_removing_a_domain_refreshes_the_origins(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site import SiteDomain
    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setenv("PAW_CF_SITES_DOMAIN", "sites.paw.test")
    site = await _seed_site(
        "site-d",
        domains=[SiteDomain(hostname="shop.example.com", cf_hostname_id="h1", status="live")],
    )
    cf = _DomainCF()
    await sites_service.remove_domain(
        workspace_id="ws1", site_id=str(site.id), hostname="shop.example.com", _cloudflare=cf
    )
    sent = {b["name"]: b for b in cf.patches()[0]["bindings"]}
    assert sent["PAW_SITE_ORIGINS"]["text"] == f"https://{site.id}.sites.paw.test"


@pytest.mark.asyncio
async def test_a_failed_origins_refresh_never_fails_the_domain_call(beanie_test_db):
    from pocketpaw_ee.sites import service as sites_service

    site = await _domain_site()
    cf = _DomainCF("live", patch_fails=True)
    out = await sites_service.domain_status(
        workspace_id="ws1", site_id=str(site.id), hostname="shop.example.com", _cloudflare=cf
    )
    assert out.status == "live"


@pytest.mark.asyncio
async def test_a_site_without_dos_gets_no_settings_call(beanie_test_db):
    from pocketpaw_ee.sites import service as sites_service

    site = await _domain_site(do_classes=[])
    cf = _DomainCF("live")
    await sites_service.domain_status(
        workspace_id="ws1", site_id=str(site.id), hostname="shop.example.com", _cloudflare=cf
    )
    assert cf.calls == []
