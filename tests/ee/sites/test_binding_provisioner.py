# Tests for the per-site KV / R2 binding provisioner
# (ee/pocketpaw_ee/sites/binding_provisioner.py), its CloudflareClient calls, the
# bundle-deploy mapping, and the delete cascade's ``bindings`` step. A stateful
# httpx.MockTransport stands in for Cloudflare's KV namespace and R2 bucket APIs.
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.entitlements.service import site_paid_backends_entitled
from pocketpaw_ee.sites import binding_provisioner as bp
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient
from pocketpaw_ee.sites.delete_cascade import (
    OUTCOME_DONE,
    OUTCOME_PARTIAL,
    OUTCOME_SKIPPED,
    STEP_BINDINGS,
    run_cascade,
)

ACCT = "acct_1"
API = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}"
OK = {"success": True, "errors": [], "messages": []}
SITE_ID = "65f0c0ffee0000000000abcd"


def _err(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"success": False, "errors": [{"code": code, "message": message}]}
    )


class _FakeCF:
    """KV namespaces and R2 buckets as in-memory state, plus the WfP upload calls."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.kv: dict[str, str] = {}  # id -> title
        self.buckets: dict[str, int] = {}  # name -> object count
        self.lifecycle: dict[str, dict] = {}
        self.fail: set[str] = set()  # "kv-delete:<id>", "r2-delete:<name>"
        self.next_id = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        path = request.url.path.split(f"/accounts/{ACCT}", 1)[1]
        m = request.method
        if path == "/storage/kv/namespaces" and m == "GET":
            rows = [{"id": i, "title": t} for i, t in self.kv.items()]
            return httpx.Response(200, json={**OK, "result": rows})
        if path == "/storage/kv/namespaces" and m == "POST":
            title = json.loads(request.content)["title"]
            if title in self.kv.values():
                return _err(400, 10014, "a namespace with this title already exists")
            self.next_id += 1
            ns_id = f"ns{self.next_id:030d}"
            self.kv[ns_id] = title
            return httpx.Response(200, json={**OK, "result": {"id": ns_id, "title": title}})
        if (km := re.fullmatch(r"/storage/kv/namespaces/([\w-]+)", path)) and m == "DELETE":
            ns_id = km.group(1)
            if f"kv-delete:{ns_id}" in self.fail:
                return _err(500, 10001, "internal error")
            if self.kv.pop(ns_id, None) is None:
                return _err(404, 10013, "namespace not found")
            return httpx.Response(200, json={**OK, "result": None})
        if path == "/r2/buckets" and m == "POST":
            name = json.loads(request.content)["name"]
            if name in self.buckets:
                return _err(409, 10004, "bucket already exists")
            self.buckets[name] = 0
            return httpx.Response(200, json={**OK, "result": {"name": name}})
        if rm := re.fullmatch(r"/r2/buckets/([a-z0-9-]+)(/lifecycle)?", path):
            name, lifecycle = rm.group(1), rm.group(2)
            if lifecycle and m == "PUT":
                self.lifecycle[name] = json.loads(request.content)
                return httpx.Response(200, json={**OK, "result": {}})
            if name not in self.buckets:
                return _err(404, 10006, "bucket does not exist")
            if m == "GET":
                return httpx.Response(200, json={**OK, "result": {"name": name}})
            if m == "DELETE":
                if f"r2-delete:{name}" in self.fail:
                    return _err(500, 10001, "internal error")
                if self.buckets[name]:
                    return _err(409, 10008, "The bucket you tried to delete is not empty")
                del self.buckets[name]
                return httpx.Response(200, json={**OK, "result": {}})
        if path.endswith("/assets-upload-session"):
            return httpx.Response(200, json={**OK, "result": {"jwt": "jwt", "buckets": []}})
        if "/workers/dispatch/namespaces/" in path and m == "PUT":
            return httpx.Response(200, json={**OK, "result": {"id": SITE_ID}})
        return _err(404, 7003, f"unrouted {m} {path}")

    def client(self) -> CloudflareClient:
        return CloudflareClient(
            account_id=ACCT,
            api_token="tok",
            zone_id="zone",
            dispatch_namespace="paw-sites",
            _transport=httpx.MockTransport(self.handler),
        )

    def writes(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests if r.method != "GET"]


class _Site:
    def __init__(self, **kw):
        self.id = SITE_ID
        self.workspace = "ws_1"
        self.plan_tier = "free"
        self.subscription_status = "none"
        self.d1_database_id = ""
        self.kv_namespaces: dict[str, str] = {}
        self.r2_buckets: dict[str, str] = {}
        self.delete_ledger: dict[str, str] = {}
        self.saved: list[tuple[dict, dict]] = []
        self.__dict__.update(kw)

    async def set(self, fields: dict) -> None:
        self.saved.append((dict(fields["kv_namespaces"]), dict(fields["r2_buckets"])))


async def _save(site):
    site.saved.append((dict(site.kv_namespaces), dict(site.r2_buckets)))


KV_REQ = {"type": "kv", "name": "CACHE", "id": "someone-elses"}
R2_REQ = {"type": "r2", "name": "FILES", "bucket_name": "someone-elses"}


# ---------------------------------------------------------------- names


def test_resource_names_are_valid_r2_names_and_distinct():
    names = {
        b: bp.resource_name(SITE_ID, b)
        for b in ("CACHE", "cache", "MY_KV", "my__kv", "A" * 64, "_x_")
    }
    for name in names.values():
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", name), name
        assert name.startswith(f"paw-{SITE_ID}-")
    assert len(set(names.values())) == len(names)
    assert bp.resource_name(SITE_ID, "CACHE") == bp.resource_name(SITE_ID, "CACHE")


# ------------------------------------------------------- create and reuse


@pytest.mark.asyncio
async def test_creates_records_then_reuses_without_cloudflare_calls():
    fake, site = _FakeCF(), _Site(plan_tier="site", subscription_status="active")
    cf = fake.client()

    res = await bp.ensure_bindings(site, [KV_REQ, R2_REQ], cloudflare=cf, save=_save, paid=True)

    ns_id = next(iter(fake.kv))
    bucket = bp.resource_name(SITE_ID, "FILES")
    assert fake.kv[ns_id] == bp.resource_name(SITE_ID, "CACHE")
    assert bucket in fake.buckets
    assert site.kv_namespaces == {"CACHE": ns_id} and site.r2_buckets == {"FILES": bucket}
    assert res.kv_namespaces == {"CACHE": ns_id} and res.r2_buckets == {"FILES": bucket}
    assert len(site.saved) == 2  # saved after each create, not once at the end

    fake.requests.clear()
    again = await bp.ensure_bindings(site, [KV_REQ, R2_REQ], cloudflare=cf, save=_save, paid=True)
    assert fake.requests == []
    assert again == res
    assert len(fake.kv) == 1 and len(fake.buckets) == 1


@pytest.mark.asyncio
async def test_unrecorded_resource_from_a_crashed_attempt_is_found_not_duplicated():
    fake, site = _FakeCF(), _Site(plan_tier="site", subscription_status="active")
    fake.kv["ns-old"] = bp.resource_name(SITE_ID, "CACHE")
    fake.buckets[bp.resource_name(SITE_ID, "FILES")] = 0

    await bp.ensure_bindings(
        site, [KV_REQ, R2_REQ], cloudflare=fake.client(), save=_save, paid=True
    )

    assert site.kv_namespaces == {"CACHE": "ns-old"}
    assert fake.writes() == []  # found both, created neither


@pytest.mark.asyncio
async def test_kv_on_free_is_allowed():
    fake, site = _FakeCF(), _Site()
    res = await bp.ensure_bindings(site, [KV_REQ], cloudflare=fake.client(), save=_save, paid=False)
    assert list(res.kv_namespaces) == ["CACHE"]


# ------------------------------------------------------------ plan gating


def test_paid_backends_predicate_uses_real_tiers():
    assert not site_paid_backends_entitled(plan_tier="free", subscription_status="active")
    assert not site_paid_backends_entitled(plan_tier="site", subscription_status="none")
    assert not site_paid_backends_entitled(plan_tier="nope", subscription_status="active")
    for tier in ("site", "staff", "pro", "business", "site_year", "staff_year"):
        assert site_paid_backends_entitled(plan_tier=tier, subscription_status="active")


@pytest.mark.asyncio
async def test_r2_on_free_is_refused_before_any_cloudflare_call():
    fake, site = _FakeCF(), _Site()
    with pytest.raises(ValidationError) as exc:
        await bp.ensure_bindings(
            site, [KV_REQ, R2_REQ], cloudflare=fake.client(), save=_save, paid=False
        )
    assert exc.value.code == "sites.binding_not_entitled"
    assert "Site plan or above" in exc.value.message and "FILES" in exc.value.message
    assert fake.requests == [] and site.kv_namespaces == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["do", "queues", "ai"])
async def test_do_queues_ai_are_not_supported_yet(kind: str):
    fake = _FakeCF()
    with pytest.raises(ValidationError, match="not supported on Paw Sites yet"):
        await bp.ensure_bindings(
            _Site(),
            [KV_REQ, {"type": kind, "name": "X"}],
            cloudflare=fake.client(),
            save=_save,
            paid=True,
        )
    assert fake.requests == []


@pytest.mark.asyncio
async def test_invalid_binding_name_is_refused():
    with pytest.raises(ValidationError, match="not a valid identifier"):
        await bp.ensure_bindings(
            _Site(),
            [{"type": "kv", "name": "bad-name"}],
            cloudflare=_FakeCF().client(),
            save=_save,
            paid=True,
        )


# ------------------------------------------------------------------ caps


@pytest.mark.asyncio
async def test_caps_count_recorded_and_requested(monkeypatch):
    two_kv = [KV_REQ, {"type": "kv", "name": "SESSIONS"}]
    fake = _FakeCF()
    with pytest.raises(ValidationError) as exc:  # free default: 1 KV
        await bp.ensure_bindings(_Site(), two_kv, cloudflare=fake.client(), save=_save, paid=False)
    assert exc.value.code == "sites.binding_cap" and "at most 1 KV" in exc.value.message
    assert fake.requests == []

    monkeypatch.setenv("PAW_SITES_MAX_KV_NAMESPACES_FREE", "2")
    res = await bp.ensure_bindings(
        _Site(), two_kv, cloudflare=fake.client(), save=_save, paid=False
    )
    assert set(res.kv_namespaces) == {"CACHE", "SESSIONS"}

    monkeypatch.setenv("PAW_SITES_MAX_R2_BUCKETS", "1")
    site = _Site(r2_buckets={"OLD": "paw-old"})  # an existing bucket counts
    with pytest.raises(ValidationError, match="at most 1 R2 buckets"):
        await bp.ensure_bindings(site, [R2_REQ], cloudflare=fake.client(), save=_save, paid=True)


# ------------------------------------------------ mapping into the upload


def _bundle(root: Path, requests: list[dict]) -> Path:
    (root / "w").mkdir()
    (root / "w" / "index.js").write_text("export default { fetch() {} };")
    manifest = {
        "workerModuleDir": "w",
        "mainModule": "index.js",
        "workerModules": ["w/index.js"],
        "compat": {"date": "2026-09-01"},
        "bindingRequests": requests,
    }
    (root / "paw-build.json").write_text(json.dumps(manifest))
    return root


def _metadata(fake: _FakeCF) -> dict:
    put = next(r for r in fake.requests if r.method == "PUT" and "/scripts/" in r.url.path)
    body = put.content
    start = body.index(b'name="metadata"')
    chunk = body[start:].split(b"\r\n\r\n", 1)[1]
    return json.loads(chunk[: chunk.index(b"\r\n--")])


@pytest.mark.asyncio
async def test_service_deploy_maps_provisioned_kv_and_r2_into_the_upload(tmp_path: Path):
    from pocketpaw_ee.sites import service as sites_service

    fake = _FakeCF()
    site = _Site(plan_tier="staff", subscription_status="active", d1_database_id="d1-ours")
    build = _bundle(tmp_path, [KV_REQ, R2_REQ, {"type": "d1", "name": "DB", "id": "theirs"}])

    await sites_service.deploy_bundle(site, build, cloudflare=fake.client())

    ns_id = site.kv_namespaces["CACHE"]
    assert _metadata(fake)["bindings"] == [
        {"type": "kv_namespace", "name": "CACHE", "namespace_id": ns_id},
        {"type": "r2_bucket", "name": "FILES", "bucket_name": bp.resource_name(SITE_ID, "FILES")},
        {"type": "d1", "name": "DB", "id": "d1-ours"},
    ]
    assert site.saved  # persisted through the doc's $set


@pytest.mark.asyncio
async def test_refused_bundle_creates_nothing_and_uploads_nothing(tmp_path: Path):
    from pocketpaw_ee.sites import service as sites_service

    fake = _FakeCF()
    build = _bundle(tmp_path, [KV_REQ, R2_REQ])
    with pytest.raises(ValidationError, match="Site plan or above"):
        await sites_service.deploy_bundle(_Site(), build, cloudflare=fake.client())
    assert fake.requests == []


# -------------------------------------------------------------- teardown


class _Deps:
    def __init__(self, cf):
        self.cloudflare = cf


async def _bindings_step(site, cf) -> str:
    # Pre-fill the ledger so only the bindings step runs.
    from pocketpaw_ee.sites.delete_cascade import CASCADE_STEPS

    site.delete_ledger = {s: OUTCOME_DONE for s in CASCADE_STEPS if s != STEP_BINDINGS}

    async def save(_site):
        return None

    await run_cascade(site=site, deps=_Deps(cf), save=save)
    return site.delete_ledger[STEP_BINDINGS]


@pytest.mark.asyncio
async def test_teardown_removes_everything_and_is_idempotent():
    fake, site = _FakeCF(), _Site(plan_tier="site", subscription_status="active")
    cf = fake.client()
    await bp.ensure_bindings(site, [KV_REQ, R2_REQ], cloudflare=cf, save=_save, paid=True)
    recorded = (dict(site.kv_namespaces), dict(site.r2_buckets))

    assert await _bindings_step(site, cf) == OUTCOME_DONE
    assert fake.kv == {} and fake.buckets == {}
    assert site.kv_namespaces == {} and site.r2_buckets == {}

    # Already gone at Cloudflare: 404s count as done.
    again = _Site(kv_namespaces=recorded[0], r2_buckets=recorded[1])
    assert await bp.teardown_bindings(again, cloudflare=cf) == []


@pytest.mark.asyncio
async def test_teardown_partial_failure_is_logged_and_does_not_stop(caplog):
    fake = _FakeCF()
    fake.kv = {"ns-a": "a", "ns-b": "b"}
    fake.buckets = {"paw-full": 3, "paw-empty": 0}
    fake.fail.add("kv-delete:ns-a")
    site = _Site(
        kv_namespaces={"A": "ns-a", "B": "ns-b"},
        r2_buckets={"FULL": "paw-full", "EMPTY": "paw-empty"},
    )

    with caplog.at_level(logging.WARNING):
        outcome = await _bindings_step(site, fake.client())

    assert outcome == OUTCOME_PARTIAL
    assert fake.kv == {"ns-a": "a"} and fake.buckets == {"paw-full": 3}
    assert site.kv_namespaces == {"A": "ns-a"} and site.r2_buckets == {"FULL": "paw-full"}
    rule = fake.lifecycle["paw-full"]["rules"][0]
    assert rule["conditions"] == {"prefix": ""} and rule["enabled"] is True
    assert rule["deleteObjectsTransition"]["condition"] == {"type": "Age", "maxAge": 86400}
    text = caplog.text
    assert "ns-a" in text and "paw-full is not empty" in text and "kv:A, r2:FULL" in text


@pytest.mark.asyncio
async def test_site_without_bindings_skips_the_step():
    assert await _bindings_step(_Site(), _FakeCF().client()) == OUTCOME_SKIPPED
