# Tests for the ``account`` deploy target of paw-build.json bundles: the interim home
# of ``project`` sites while the Cloudflare account has no Workers for Platforms.
# httpx.MockTransport stands in for Cloudflare. Covers the account-level upload
# sequence (session -> bucket upload -> PUT script) with exact URLs and metadata, the
# workers.dev toggle, target selection by mode and override, bindings / secrets / D1
# migrations on the account target, workers-mode serving parity (name, URL, route
# target, deploy_target), the WfP path and the other engines staying as they were,
# and that wrangler is never run for a project.
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from bson import ObjectId
from pocketpaw_ee.sites import bundle_deploy
from pocketpaw_ee.sites.bundle_deploy import ProvisionedResources
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

from tests.ee.sites.test_bundle_deploy import (
    ACCT,
    HEADERS,
    NS_URL,
    UPLOAD_URL,
    _FakeCloudflare,
    _parts,
    next_build,  # noqa: F401 - fixture
)
from tests.ee.sites.test_project_d1 import ENTRIES, OK, _KVFake, _publish, _setup, _source

API = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
SCRIPTS_URL = f"{API}/workers/scripts"


class _AccountFake(_KVFake):
    """``_KVFake`` (D1 + KV + dispatch PUT) plus the account-level Worker calls."""

    def __init__(self, *, subdomain: str = "acct-sub") -> None:
        super().__init__()
        self.subdomain = subdomain
        self.account_puts: list[str] = []
        self.enabled: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split(f"/accounts/{ACCT}", 1)[1]
        m = request.method
        if path.startswith("/workers/scripts"):
            request.read()
            self.requests.append(request)
            if path == "/workers/scripts" and m == "GET":
                return httpx.Response(200, json={**OK, "result": []})
            if path.endswith("/assets-upload-session"):
                return httpx.Response(200, json={**OK, "result": {"jwt": "jwt", "buckets": []}})
            if path.endswith("/subdomain") and m == "POST":
                self.enabled.append({"path": path, "body": json.loads(request.content)})
                return httpx.Response(
                    200, json={**OK, "result": {"enabled": True, "previews_enabled": False}}
                )
            if m == "PUT":
                self.account_puts.append(path)
                return httpx.Response(200, json={**OK, "result": {"id": "x"}})
        if path == "/workers/subdomain" and m == "GET":
            request.read()
            self.requests.append(request)
            return httpx.Response(200, json={**OK, "result": {"subdomain": self.subdomain}})
        return super().handler(request)


def _never_wrangler(monkeypatch) -> None:
    """Fail the test if anything tries to run wrangler (or any subprocess)."""
    import asyncio
    import subprocess

    from pocketpaw_ee.sites import _wrangler, workers_deploy

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a project deploy must never run wrangler or a subprocess")

    async def _aboom(*_a: Any, **_k: Any) -> Any:
        _boom()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _aboom)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _aboom)
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(_wrangler, "wrangler_argv", _boom)
    monkeypatch.setattr(workers_deploy, "deploy_workers", _aboom)


# ------------------------------------------------------------ wire sequence


@pytest.mark.asyncio
async def test_account_target_upload_sequence_uses_account_script_urls(
    next_build: Path,  # noqa: F811
):
    fake = _FakeCloudflare()
    result = await bundle_deploy.deploy_bundle(
        fake.client(), script_name="proj", build_dir=next_build, salt="ws_1", target="account"
    )
    assert result.modules == 1 and result.assets == 3

    session, upload, put = fake.requests
    assert session.method == "POST"
    assert str(session.url) == f"{SCRIPTS_URL}/proj/assets-upload-session"
    assert session.headers["authorization"] == "Bearer tok_1"
    assert str(upload.url) == UPLOAD_URL
    assert upload.headers["authorization"] == "Bearer session-jwt"
    assert put.method == "PUT" and str(put.url) == f"{SCRIPTS_URL}/proj"
    assert put.headers["authorization"] == "Bearer tok_1"
    meta, module = _parts(put)
    # Byte-for-byte the metadata the dispatch target sends.
    assert json.loads(meta["content"]) == {
        "main_module": "worker.js",
        "bindings": [{"name": "ASSETS", "type": "assets"}],
        "compatibility_date": "2026-09-01",
        "compatibility_flags": ["nodejs_compat", "global_fetch_strictly_public"],
        "assets": {"jwt": "completion-jwt", "config": {"_headers": HEADERS}},
    }
    assert module["name"] == "worker.js"
    assert module["content_type"] == "application/javascript+module"
    assert not any("/dispatch/" in str(r.url) for r in fake.requests)


@pytest.mark.asyncio
async def test_dispatch_target_is_unchanged(next_build: Path):  # noqa: F811
    fake = _FakeCloudflare()
    await bundle_deploy.deploy_bundle(
        fake.client(), script_name="site_1", build_dir=next_build, salt="ws_1"
    )
    session, _upload, put = fake.requests
    assert str(session.url) == f"{NS_URL}/site_1/assets-upload-session"
    assert str(put.url) == f"{NS_URL}/site_1"


@pytest.mark.asyncio
async def test_unknown_target_is_refused_before_any_call(next_build: Path):  # noqa: F811
    fake = _FakeCloudflare()
    with pytest.raises(Exception, match="unknown deploy target"):
        await bundle_deploy.deploy_bundle(
            fake.client(), script_name="s", build_dir=next_build, salt="w", target="zone"
        )
    assert fake.requests == []


@pytest.mark.asyncio
async def test_bindings_and_secrets_map_the_same_on_the_account_target(
    next_build: Path,  # noqa: F811
):
    manifest = json.loads((next_build / "paw-build.json").read_text())
    manifest["bindingRequests"] += [
        {"type": "d1", "name": "DB", "id": "author-id"},
        {"type": "kv", "name": "CACHE", "id": "author-kv"},
        {"type": "service", "name": "OTHER", "service": "someone-else"},
        {"type": "dispatch_namespace", "name": "NS", "namespace": "x"},
        {"type": "secret", "name": "API_KEY", "required": True},
    ]
    (next_build / "paw-build.json").write_text(json.dumps(manifest))
    provisioned = ProvisionedResources(
        d1_database_id="d1-ours",
        kv_namespaces={"CACHE": "kv-ours"},
        secrets={"API_KEY": "s3cret"},
    )
    seen: list[list[dict]] = []

    async def _before(bindings: list[dict]) -> None:
        seen.append(bindings)

    fake = _FakeCloudflare()
    result = await bundle_deploy.deploy_bundle(
        fake.client(),
        script_name="proj",
        build_dir=next_build,
        salt="ws_1",
        provisioned=provisioned,
        before_upload=_before,
        target="account",
    )
    put = fake.requests[-1]
    assert str(put.url) == f"{SCRIPTS_URL}/proj"
    bindings = json.loads(_parts(put)[0]["content"])["bindings"]
    assert bindings == [
        {"type": "assets", "name": "ASSETS"},
        {"type": "d1", "name": "DB", "id": "d1-ours"},
        {"type": "kv_namespace", "name": "CACHE", "namespace_id": "kv-ours"},
        {"type": "secret_text", "name": "API_KEY", "text": "s3cret"},
    ]
    assert seen == [bindings]  # the migration hook ran with the mapped bindings
    assert any("service:OTHER" in w for w in result.warnings)
    assert any("dispatch_namespace:NS" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_workers_dev_toggle_and_subdomain_lookup():
    fake = _AccountFake(subdomain="my-sub")
    client = CloudflareClient(
        account_id=ACCT,
        api_token="tok",
        zone_id="",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(fake.handler),
    )
    await client.enable_workers_dev("proj")
    assert await client.workers_dev_subdomain() == "my-sub"
    toggle, lookup = fake.requests
    assert toggle.method == "POST" and str(toggle.url) == f"{SCRIPTS_URL}/proj/subdomain"
    assert json.loads(toggle.content) == {"enabled": True, "previews_enabled": False}
    assert lookup.method == "GET" and str(lookup.url) == f"{API}/workers/subdomain"


# ---------------------------------------------------------------- selection


@pytest.mark.parametrize(
    ("mode", "override", "expected"),
    [
        ("workers", None, "account"),
        ("wfp", None, "dispatch"),
        (None, None, "dispatch"),
        ("workers", "dispatch", "dispatch"),
        ("wfp", "account", "account"),
        ("workers", " ACCOUNT ", "account"),
        ("workers", "bogus", "account"),
        ("wfp", "bogus", "dispatch"),
    ],
)
def test_project_target_follows_the_mode_unless_overridden(monkeypatch, mode, override, expected):
    if override is None:
        monkeypatch.delenv(bundle_deploy.PROJECT_TARGET_ENV, raising=False)
    else:
        monkeypatch.setenv(bundle_deploy.PROJECT_TARGET_ENV, override)
    assert bundle_deploy.project_deploy_target(mode) == expected


# ------------------------------------------------------------ publish path


@pytest.mark.asyncio
async def test_workers_mode_project_publish_is_an_account_worker_served_like_other_sites(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.workers_deploy import site_worker_name

    source = _source(**{"0001_entries.sql": ENTRIES})
    pocket_id, site_id = await _setup(monkeypatch, source)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    _never_wrangler(monkeypatch)
    fake = _AccountFake()

    doc = await _publish(pocket_id, site_id, source, fake.client())

    # Named, served and stamped like a wrangler workers-mode site.
    name = site_worker_name(doc)
    assert name == doc.worker_name == "proj"  # the slug claimed from the site name
    assert doc.deployed is True and doc.deploy_target == "workers"
    assert doc.url == f"https://{name}.acct-sub.workers.dev"
    assert sites_service._route_target(doc) == name  # custom domains route to it
    assert fake.account_puts == [f"/workers/scripts/{name}"]
    assert fake.enabled == [
        {
            "path": f"/workers/scripts/{name}/subdomain",
            "body": {"enabled": True, "previews_enabled": False},
        }
    ]
    assert fake.puts == []  # nothing went to the dispatch namespace
    assert not any("/dispatch/" in r.url.path for r in fake.requests)

    # D1 provisioned + migrated, and bound with our ids, exactly as on WfP.
    (uuid,) = fake.dbs
    assert doc.d1_database_id == uuid
    assert "entries" in {
        r[0] for r in fake.conn(uuid).execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    put = next(r for r in fake.requests if r.method == "PUT")
    bindings = {b["name"]: b for b in json.loads(_parts(put)[0]["content"])["bindings"]}
    assert bindings["DB"] == {"type": "d1", "name": "DB", "id": uuid}
    assert bindings["CACHE"]["type"] == "kv_namespace"
    # No pageview counter on a bundle Worker.
    assert doc.analytics_since is None

    # A republish keeps the same Worker and address.
    fake.account_puts.clear()
    again = await _publish(pocket_id, site_id, source, fake.client())
    assert fake.account_puts == [f"/workers/scripts/{name}"]
    assert again.url == doc.url and again.deploy_target == "workers"


@pytest.mark.asyncio
async def test_configured_workers_subdomain_skips_the_lookup(beanie_test_db, monkeypatch):
    source = _source()
    pocket_id, site_id = await _setup(monkeypatch, source)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.setenv("PAW_CF_WORKERS_SUBDOMAIN", "configured")
    _never_wrangler(monkeypatch)
    fake = _AccountFake()

    doc = await _publish(pocket_id, site_id, source, fake.client())

    assert doc.url == f"https://{doc.worker_name}.configured.workers.dev"
    assert not any(r.url.path.endswith("/workers/subdomain") for r in fake.requests)


@pytest.mark.asyncio
async def test_override_dispatch_in_workers_mode_takes_the_wfp_path(beanie_test_db, monkeypatch):
    source = _source()
    pocket_id, site_id = await _setup(monkeypatch, source)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.setenv(bundle_deploy.PROJECT_TARGET_ENV, "dispatch")
    monkeypatch.setenv("PAW_CF_SITES_DOMAIN", "sites.paw.test")
    _never_wrangler(monkeypatch)
    fake = _AccountFake()

    doc = await _publish(pocket_id, site_id, source, fake.client())

    assert [p["path"] for p in fake.puts] == [
        f"/workers/dispatch/namespaces/paw-sites/scripts/{site_id}"
    ]
    assert fake.account_puts == [] and fake.enabled == []
    assert doc.deploy_target == "wfp" and doc.url == f"https://{site_id}.sites.paw.test"


@pytest.mark.asyncio
async def test_wfp_mode_project_publish_is_unchanged(beanie_test_db, monkeypatch):
    source = _source()
    pocket_id, site_id = await _setup(monkeypatch, source)  # PAW_CF_DEPLOY_MODE=wfp
    _never_wrangler(monkeypatch)
    fake = _AccountFake()

    doc = await _publish(pocket_id, site_id, source, fake.client())

    assert [p["path"] for p in fake.puts] == [
        f"/workers/dispatch/namespaces/paw-sites/scripts/{site_id}"
    ]
    assert fake.account_puts == [] and fake.enabled == []
    assert doc.deploy_target == "wfp"


@pytest.mark.asyncio
async def test_a_failed_account_upload_leaves_the_site_undeployed(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.sites import service as sites_service

    source = _source()
    pocket_id, site_id = await _setup(monkeypatch, source)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    _never_wrangler(monkeypatch)

    class _Refusing(_AccountFake):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PUT" and "/workers/scripts/" in request.url.path:
                request.read()
                return httpx.Response(
                    403, json={"success": False, "errors": [{"code": 10000, "message": "no"}]}
                )
            return super().handler(request)

    fake = _Refusing()
    with pytest.raises(CloudError):
        await _publish(pocket_id, site_id, source, fake.client())
    doc = await sites_service._SiteDoc.find_one({"_id": ObjectId(site_id)})
    assert doc is not None and doc.deployed is False and doc.url == ""
    assert fake.enabled == []


@pytest.mark.asyncio
async def test_other_engines_in_workers_mode_still_deploy_through_wrangler_path(
    beanie_test_db, monkeypatch, tmp_path: Path
):
    """The override names project bundles only: an html site in workers mode still goes
    through ``workers_deploy`` and never touches the bundle path."""
    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "workers")
    monkeypatch.setenv(bundle_deploy.PROJECT_TARGET_ENV, "account")
    (tmp_path / "index.html").write_text("<html><body><h1>Hi</h1></body></html>")

    async def _no_bundle(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a non-project site must not take the bundle path")

    monkeypatch.setattr(bundle_deploy, "deploy_bundle", _no_bundle)
    deployed: list[dict] = []

    async def _workers(site_id: str, project_dir: str, **kw: Any) -> str:
        deployed.append({"site_id": site_id, **kw})
        return f"https://{kw['worker_name']}.sub.workers.dev"

    site_id = str(ObjectId())
    doc = await sites_service._deploy_site_doc(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk-html",
        site_id=site_id,
        signed_key="k",
        site_name="Plain",
        ripple_spec=None,
        theme={},
        engine="html",
        source={"index.html": "<h1>Hi</h1>"},
        pattern="landing",
        workers_deploy=_workers,
        prebuilt_project_dir=str(tmp_path),
    )
    assert len(deployed) == 1 and deployed[0]["engine"] == "html"
    assert doc.deploy_target == "workers"
