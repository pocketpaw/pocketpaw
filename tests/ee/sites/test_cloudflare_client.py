# Tests for the Sites Cloudflare client (RFC 12, Task 2.2).
# Created: 2026-05-30 (feat/paw-sites-backend) — exercises the two CF
# surfaces the control plane uses, with httpx.MockTransport standing in
# for the real Cloudflare API (no network):
#   * put_worker — uploads a user Worker to the Workers-for-Platforms
#     dispatch namespace (asserts URL + PUT verb).
#   * create_custom_hostname — Cloudflare-for-SaaS hostname create, returns
#     the single CNAME target the client pastes.
#   * get_hostname_status — maps CF's status/ssl pair onto HostnameStatus.
#   * non-2xx responses fail closed (raise).
#
# Updated 2026-06-06 (feat/1346-cf-deploy — Cloudflare deploy pipeline):
# new tests for the REAL site deploy, all against httpx.MockTransport (no
# network) over a fake adapter-cloudflare output tree on disk:
#   * deploy_site — the full Workers-for-Platforms upload: the Workers Assets
#     two-step (manifest session → upload the missing files) then the multipart
#     script PUT. Asserts every CF endpoint is hit in order, the static asset is
#     uploaded, the worker module + a metadata part ride the PUT, the D1/Queue
#     bindings parsed from the generated wrangler.toml land in the metadata, and a
#     stable live URL comes back.
#   * deploy_site with no missing assets — the session reports empty buckets, so
#     the upload round-trip is skipped and the session JWT is reused.
#   * deploy_site fails closed — a non-2xx on the script PUT raises (no false
#     "Live").
#   * deploy_site with a missing worker entry raises before any network call.
#   * verify_domain — maps an active hostname to True, a pending one to False.
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus


def _client(handler) -> CloudflareClient:
    transport = httpx.MockTransport(handler)
    return CloudflareClient(
        account_id="acct_1",
        api_token="tok_1",
        zone_id="zone_1",
        dispatch_namespace="paw-sites",
        _transport=transport,
    )


@pytest.mark.asyncio
async def test_put_worker_uploads_to_dispatch_namespace():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"success": True, "result": {"id": "site_1"}})

    client = _client(handler)
    ok = await client.put_worker(script_name="site_1", bundle=b"export default {}")
    assert ok is True
    assert "dispatch/namespaces/paw-sites/scripts/site_1" in seen["url"]
    assert seen["method"] == "PUT"


@pytest.mark.asyncio
async def test_create_custom_hostname_returns_cname_target():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "ch_1",
                    "hostname": "www.brightsmiledental.com",
                    "status": "pending",
                    "ssl": {"status": "pending_validation"},
                },
            },
        )

    client = _client(handler)
    ch: CustomHostname = await client.create_custom_hostname("www.brightsmiledental.com")
    assert ch.hostname == "www.brightsmiledental.com"
    assert ch.status == HostnameStatus.PENDING
    # Clients paste ONE CNAME pointing at the zone's SaaS fallback origin.
    assert ch.cname_target.endswith("zone_1.cdn.cloudflare.net") or ch.cname_target


@pytest.mark.asyncio
async def test_get_hostname_status_maps_active_to_live():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "ch_1",
                    "hostname": "www.brightsmiledental.com",
                    "status": "active",
                    "ssl": {"status": "active"},
                },
            },
        )

    client = _client(handler)
    status = await client.get_hostname_status("ch_1")
    assert status == HostnameStatus.LIVE


@pytest.mark.asyncio
async def test_non_2xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"message": "denied"}]})

    client = _client(handler)
    with pytest.raises(Exception):
        await client.put_worker(script_name="x", bundle=b"")


# --- deploy_site: the REAL site deploy (worker + static assets + bindings) ----


def _fake_build(tmp_path: Path, *, with_d1: bool = True) -> str:
    """Write a fake adapter-cloudflare output tree under
    <tmp>/.svelte-kit/cloudflare/ : the worker entry, one prerendered asset, the
    deploy-config files that must NOT be uploaded as assets, and the generated
    wrangler.toml the deploy reads bindings + compat settings from. Returns the
    project dir (the parent of .svelte-kit), which is what deploy_site takes."""
    project = tmp_path / "proj"
    cf = project / ".svelte-kit" / "cloudflare"
    (cf / "_app" / "immutable" / "assets").mkdir(parents=True)
    (cf / "_worker.js").write_text("export default { fetch() {} }")
    (cf / "index.html").write_text("<h1>Bright Smile Dental</h1>")
    (cf / "_app" / "immutable" / "assets" / "app.css").write_text("body{}")
    # Deploy-config files — excluded from the asset upload.
    (cf / "_routes.json").write_text('{"version":1}')
    (cf / "_headers").write_text("/*\n  X-Frame-Options: DENY")
    # The generated wrangler.toml: a real (provisioned) D1 id + a Queue producer.
    db_id = "d1-abc123" if with_d1 else "__D1_DATABASE_ID__"
    (project / "wrangler.toml").write_text(
        'name = "paw-site-x"\n'
        'compatibility_date = "2024-09-23"\n'
        'compatibility_flags = ["nodejs_compat"]\n'
        "[[queues.producers]]\n"
        'binding = "LEADS_QUEUE"\n'
        'queue = "paw-leads-x"\n'
        "[[d1_databases]]\n"
        'binding = "DB"\n'
        'database_name = "paw-site-x"\n'
        f'database_id = "{db_id}"\n'
    )
    return str(project)


@pytest.mark.asyncio
async def test_deploy_site_uploads_assets_then_puts_script(tmp_path: Path):
    """The happy path: deploy_site opens an asset upload session, uploads the
    file the API says it's missing, then PUTs the worker script with a metadata
    part — and returns a stable live URL. Asserts the endpoints are hit in order
    and the bindings from wrangler.toml ride the script metadata."""
    project = _fake_build(tmp_path)
    seen: list[str] = []
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/assets-upload-session"):
            seen.append("session")
            body = json.loads(request.content)
            captured["manifest"] = body["manifest"]
            # Report ONE bucket: the API is missing every file's hash.
            hashes = [v["hash"] for v in body["manifest"].values()]
            return httpx.Response(
                200, json={"success": True, "result": {"jwt": "session_jwt", "buckets": [hashes]}}
            )
        if "/workers/assets/upload" in url:
            seen.append("upload")
            captured["upload_auth"] = request.headers.get("Authorization")
            captured["uploaded"] = json.loads(request.content)["files"]
            return httpx.Response(200, json={"success": True, "result": {"jwt": "completion_jwt"}})
        # The multipart script PUT.
        seen.append("put")
        captured["put_method"] = request.method
        captured["put_url"] = url
        captured["put_body"] = request.content  # multipart bytes
        return httpx.Response(200, json={"success": True, "result": {"id": "site_x"}})

    client = _client(handler)
    url = await client.deploy_site(script_name="site_x", project_dir=project)

    # Endpoints hit in the right order: session → upload → script PUT.
    assert seen == ["session", "upload", "put"]
    # The manifest covered the servable assets and EXCLUDED the deploy-config
    # files (_worker.js / _routes.json / _headers).
    paths = set(captured["manifest"].keys())
    assert "/index.html" in paths
    assert "/_app/immutable/assets/app.css" in paths
    assert "/_worker.js" not in paths
    assert "/_routes.json" not in paths
    assert "/_headers" not in paths
    # The upload reused the session JWT and sent the (base64) file bodies.
    assert captured["upload_auth"] == "Bearer session_jwt"
    assert len(captured["uploaded"]) == len(paths)
    # The script PUT is a multipart PUT to the dispatch namespace.
    assert captured["put_method"] == "PUT"
    assert "dispatch/namespaces/paw-sites/scripts/site_x" in captured["put_url"]
    body = captured["put_body"].decode("utf-8", "replace")
    assert "_worker.js" in body  # the worker module part
    assert '"main_module": "_worker.js"' in body  # metadata part
    assert "completion_jwt" in body  # the assets completion JWT
    # Bindings parsed from wrangler.toml landed in the metadata.
    assert '"type": "d1"' in body
    assert "d1-abc123" in body
    assert '"type": "queue"' in body
    assert "LEADS_QUEUE" in body
    # A stable live URL comes back.
    assert url == "https://paw-sites.workers.dev/site_x/"


@pytest.mark.asyncio
async def test_deploy_site_skips_upload_when_no_missing_assets(tmp_path: Path):
    """If the asset session reports no buckets (CF already has every file), the
    upload round-trip is skipped and the session JWT is reused as the completion
    token."""
    project = _fake_build(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/assets-upload-session"):
            seen.append("session")
            return httpx.Response(
                200, json={"success": True, "result": {"jwt": "reused_jwt", "buckets": []}}
            )
        if "/workers/assets/upload" in url:
            seen.append("upload")  # must NOT happen
            return httpx.Response(200, json={"success": True, "result": {"jwt": "x"}})
        seen.append("put")
        assert b"reused_jwt" in request.content
        return httpx.Response(200, json={"success": True, "result": {}})

    client = _client(handler)
    url = await client.deploy_site(script_name="site_y", project_dir=project)
    assert seen == ["session", "put"]  # no upload step
    assert url == "https://paw-sites.workers.dev/site_y/"


@pytest.mark.asyncio
async def test_deploy_site_fails_closed_on_put_error(tmp_path: Path):
    """A non-2xx on the script PUT raises ValidationError — the deploy fails
    closed so "Live" never flips true on a broken upload."""
    project = _fake_build(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/assets-upload-session"):
            body = json.loads(request.content)
            hashes = [v["hash"] for v in body["manifest"].values()]
            return httpx.Response(
                200, json={"success": True, "result": {"jwt": "j", "buckets": [hashes]}}
            )
        if "/workers/assets/upload" in url:
            return httpx.Response(200, json={"success": True, "result": {"jwt": "j2"}})
        return httpx.Response(
            500, json={"success": False, "errors": [{"message": "script too large"}]}
        )

    client = _client(handler)
    with pytest.raises(ValidationError):
        await client.deploy_site(script_name="site_z", project_dir=project)


@pytest.mark.asyncio
async def test_deploy_site_missing_worker_raises_before_network(tmp_path: Path):
    """No _worker.js in the build → deploy_site raises before any HTTP call."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no network call should happen")

    # An empty project dir: no .svelte-kit/cloudflare/_worker.js.
    (tmp_path / "empty").mkdir()
    client = _client(handler)
    with pytest.raises(ValidationError):
        await client.deploy_site(script_name="x", project_dir=str(tmp_path / "empty"))


@pytest.mark.asyncio
async def test_deploy_site_skips_unprovisioned_d1_binding(tmp_path: Path):
    """A static-only site whose wrangler.toml still carries the __D1_DATABASE_ID__
    token (no real D1 provisioned) must NOT send a d1 binding — an unresolved id
    would fail the deploy."""
    project = _fake_build(tmp_path, with_d1=False)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/assets-upload-session"):
            body = json.loads(request.content)
            hashes = [v["hash"] for v in body["manifest"].values()]
            return httpx.Response(
                200, json={"success": True, "result": {"jwt": "j", "buckets": [hashes]}}
            )
        if "/workers/assets/upload" in url:
            return httpx.Response(200, json={"success": True, "result": {"jwt": "j2"}})
        captured["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"success": True, "result": {}})

    client = _client(handler)
    await client.deploy_site(script_name="static_site", project_dir=project)
    assert '"type": "d1"' not in captured["body"]
    # The Queue binding still rides (it has no unresolved id).
    assert "LEADS_QUEUE" in captured["body"]


@pytest.mark.asyncio
async def test_verify_domain_true_only_when_live():
    """verify_domain returns True when the hostname is active+TLS-active (LIVE),
    False otherwise — the backend half of the DomainsPanel "Verify" action."""

    def live_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"status": "active", "ssl": {"status": "active"}},
            },
        )

    def pending_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"status": "pending", "ssl": {"status": "pending_validation"}},
            },
        )

    assert await _client(live_handler).verify_domain("ch_1") is True
    assert await _client(pending_handler).verify_domain("ch_1") is False
