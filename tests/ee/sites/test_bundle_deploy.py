# Tests for paw-build.json bundle deploys into the WfP dispatch namespace
# (ee/pocketpaw_ee/sites/bundle_deploy.py + CloudflareClient.upload_assets and the
# multi-module put_worker form). httpx.MockTransport stands in for Cloudflare.
#
# The Next fixture replays the request sequence captured from wrangler's real WfP
# deploy in the 2026-10-07 next-on-wfp spike (session -> base64 bucket upload ->
# multipart PUT) and asserts the exact parts, metadata and auth headers. The rest
# pins the trust boundary: the compat-flag allow-list, binding mapping onto OUR
# resources only, the size/count refusals, path containment, the worker settings
# (observability, plan-tiered limits, opt-in Smart Placement) and that the legacy
# single-module path is unchanged.
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import bundle_deploy
from pocketpaw_ee.sites.bundle_deploy import ProvisionedResources, map_bindings, resolve_compat
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient, asset_hash

ACCT = "acct_1"
NS_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/workers/dispatch/namespaces/paw-sites/scripts"
_SETTINGS_ENV = (
    "PAW_SITES_SMART_PLACEMENT",
    "PAW_SITES_OBSERVABILITY",
    "PAW_SITES_OBSERVABILITY_SAMPLE",
    "PAW_SITES_CPU_MS_FREE",
    "PAW_SITES_CPU_MS_PAID",
    "PAW_SITES_SUBREQUESTS_FREE",
    "PAW_SITES_SUBREQUESTS_PAID",
)


@pytest.fixture(autouse=True)
def _default_worker_settings(monkeypatch):
    """Every test sees the shipped defaults unless it sets an env itself."""
    for key in _SETTINGS_ENV:
        monkeypatch.delenv(key, raising=False)


OBSERVABILITY_DEFAULT = {
    "enabled": True,
    "head_sampling_rate": 0.1,
    "logs": {"enabled": True, "invocation_logs": True},
    "traces": {"enabled": True, "head_sampling_rate": 0.1},
}
FREE_LIMITS = {"cpu_ms": 50, "subrequests": 50}
UPLOAD_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/workers/assets/upload?base64=true"
)
HEADERS = "/_next/static/*\n  Cache-Control: public,max-age=31536000,immutable\n"


def _parts(request: httpx.Request) -> list[dict]:
    """Ordered multipart parts: name, filename, content-type, content."""
    ct = request.headers["content-type"]
    boundary = re.search(r"boundary=(.+)$", ct).group(1).strip('"').encode()
    out = []
    for chunk in request.content.split(b"--" + boundary)[1:-1]:
        head, _, body = chunk[2:].partition(b"\r\n\r\n")
        head_text = head.decode()
        fname = re.search(r'filename="([^"]*)"', head_text)
        ctype = re.search(r"Content-Type: (.+)", head_text)
        out.append(
            {
                "name": re.search(r'name="([^"]+)"', head_text).group(1),
                "filename": fname.group(1) if fname else None,
                "content_type": ctype.group(1).strip() if ctype else None,
                "content": body[:-2],  # trailing CRLF before the next boundary
            }
        )
    return out


class _FakeCloudflare:
    """Records every request; answers like the spike's mock (mock-cf.mjs)."""

    def __init__(self, *, buckets: str = "all"):
        self.requests: list[httpx.Request] = []
        self.buckets = buckets

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        url = str(request.url)
        ok = {"success": True, "errors": [], "messages": []}
        if url.endswith("/assets-upload-session"):
            manifest = json.loads(request.content)["manifest"]
            hashes = sorted({m["hash"] for m in manifest.values()})
            buckets = [hashes] if self.buckets == "all" else []
            return httpx.Response(
                200, json={**ok, "result": {"jwt": "session-jwt", "buckets": buckets}}
            )
        if "/workers/assets/upload" in url:
            return httpx.Response(201, json={**ok, "result": {"jwt": "completion-jwt"}})
        if request.method == "PUT":
            return httpx.Response(200, json={**ok, "result": {"id": "site_1"}})
        return httpx.Response(404, json={"success": False, "errors": [{"code": 1, "message": "?"}]})

    def client(self) -> CloudflareClient:
        return CloudflareClient(
            account_id=ACCT,
            api_token="tok_1",
            zone_id="zone_1",
            dispatch_namespace="paw-sites",
            _transport=httpx.MockTransport(self.handler),
        )


def _write(root: Path, rel: str, content: bytes | str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)


@pytest.fixture
def next_build(tmp_path: Path) -> Path:
    """The spike's Next.js build, shrunk: one bundled worker module + assets."""
    _write(tmp_path, ".paw/worker/worker.js", "export default { fetch() {} };\n")
    _write(tmp_path, ".paw/worker/worker.js.map", "{}")
    _write(tmp_path, ".paw/worker/README.md", "wrangler dry-run output")
    _write(tmp_path, ".open-next/assets/BUILD_ID", "VlQfQVnrjqvGOCOo6x_LA")
    _write(tmp_path, ".open-next/assets/paw.png", b"\x89PNG\r\n\x1a\n\x00binary")
    _write(tmp_path, ".open-next/assets/_next/static/chunks/a.js", "console.log(1)")
    _write(tmp_path, ".open-next/assets/_headers", HEADERS)
    manifest = {
        "slug": "next",
        "assetsDir": ".open-next/assets",
        "workerEntry": ".paw/worker/worker.js",
        "workerModuleDir": ".paw/worker",
        "mainModule": "worker.js",
        "workerModules": [".paw/worker/worker.js"],
        "assetsConfig": {"_headers": HEADERS, "_redirects": None},
        "compat": {
            "date": "2026-09-01",
            "flags": ["nodejs_compat", "global_fetch_strictly_public"],
        },
        "bindingRequests": [{"type": "assets", "name": "ASSETS"}],
        "droppedBindings": [],
        "framework": "next",
        "sizes": {"assetFiles": 3, "workerModules": 1, "workerBytes": 31},
        "startup": {"cpuMs": 51.5},
    }
    _write(tmp_path, "paw-build.json", json.dumps(manifest))
    return tmp_path


# ------------------------------------------------------------ spike replay


@pytest.mark.asyncio
async def test_next_bundle_replays_the_spike_request_sequence(next_build: Path):
    fake = _FakeCloudflare()
    result = await bundle_deploy.deploy_bundle(
        fake.client(), script_name="site_1", build_dir=next_build, salt="ws_1"
    )
    assert result.modules == 1 and result.assets == 3 and result.warnings == []

    session, upload, put = fake.requests
    # 1. assets-upload-session with the manifest; _headers is NOT an asset.
    assert session.method == "POST"
    assert str(session.url) == f"{NS_URL}/site_1/assets-upload-session"
    assert session.headers["authorization"] == "Bearer tok_1"
    manifest = json.loads(session.content)["manifest"]
    files = {
        "/BUILD_ID": b"VlQfQVnrjqvGOCOo6x_LA",
        "/paw.png": b"\x89PNG\r\n\x1a\n\x00binary",
        "/_next/static/chunks/a.js": b"console.log(1)",
    }
    assert manifest == {
        path: {"hash": asset_hash(content, path, "ws_1"), "size": len(content)}
        for path, content in files.items()
    }
    assert all(re.fullmatch(r"[0-9a-f]{32}", m["hash"]) for m in manifest.values())

    # 2. one base64 part per hash, authorised by the SESSION jwt, not our token.
    assert upload.method == "POST"
    assert str(upload.url) == UPLOAD_URL
    assert upload.headers["authorization"] == "Bearer session-jwt"
    by_hash = {m["hash"]: path for path, m in manifest.items()}
    parts = _parts(upload)
    assert sorted(p["name"] for p in parts) == sorted(by_hash)
    for part in parts:
        assert part["filename"] == part["name"]
        assert base64.b64decode(part["content"]) == files[by_hash[part["name"]]]
    types = {by_hash[p["name"]]: p["content_type"] for p in parts}
    assert types["/paw.png"] == "image/png"
    assert types["/BUILD_ID"] == "application/octet-stream"

    # 3. multipart PUT: metadata, then the module, exactly as wrangler sent it
    # (minus wrangler-only code_update_strategy / package_dependencies telemetry).
    assert put.method == "PUT"
    assert str(put.url) == f"{NS_URL}/site_1"
    assert put.headers["authorization"] == "Bearer tok_1"
    assert put.headers["content-type"].startswith("multipart/form-data; boundary=")
    meta, module = _parts(put)
    assert meta["name"] == "metadata" and meta["filename"] is None
    assert json.loads(meta["content"]) == {
        "main_module": "worker.js",
        "bindings": [{"name": "ASSETS", "type": "assets"}],
        "compatibility_date": "2026-09-01",
        "compatibility_flags": ["nodejs_compat", "global_fetch_strictly_public"],
        "assets": {"jwt": "completion-jwt", "config": {"_headers": HEADERS}},
        "observability": OBSERVABILITY_DEFAULT,
        "limits": FREE_LIMITS,
    }
    assert module["name"] == "worker.js" and module["filename"] == "worker.js"
    assert module["content_type"] == "application/javascript+module"
    assert module["content"] == b"export default { fetch() {} };\n"


@pytest.mark.asyncio
async def test_multi_module_parts_are_named_by_path_and_typed_by_extension(tmp_path: Path):
    """The spike's ``mm`` capture (index.js + lib.js + late.js), plus the module
    kinds other frameworks emit. No assets -> no session call at all."""
    mods = {
        "index.js": "import './lib.js'",
        "lib.js": "export const x = 1",
        "late.js": "export const y = 2",
        "chunks/deep.mjs": "export {}",
        "legacy.cjs": "module.exports = 1",
        "og.wasm": b"\x00asm\x01\x00\x00\x00",
        "data.json": "{}",
        "notes.txt": "hi",
        "blob.bin": b"\x01\x02",
    }
    for name, content in mods.items():
        _write(tmp_path, f"dist/server/{name}", content)
    _write(
        tmp_path,
        "paw-build.json",
        json.dumps(
            {
                "workerEntry": "dist/server/index.js",
                "workerModules": [f"dist/server/{n}" for n in mods],
                "compat": {"date": "2026-09-01", "flags": []},
            }
        ),
    )
    fake = _FakeCloudflare()
    await bundle_deploy.deploy_bundle(
        fake.client(), script_name="mm", build_dir=tmp_path, salt="ws"
    )

    (put,) = fake.requests
    parts = _parts(put)
    assert json.loads(parts[0]["content"]) == {
        "main_module": "index.js",
        "bindings": [],
        "compatibility_date": "2026-09-01",
        "compatibility_flags": [],
        "observability": OBSERVABILITY_DEFAULT,
        "limits": FREE_LIMITS,
    }
    got = {p["name"]: (p["filename"], p["content_type"]) for p in parts[1:]}
    assert got == {
        "index.js": ("index.js", "application/javascript+module"),
        "lib.js": ("lib.js", "application/javascript+module"),
        "late.js": ("late.js", "application/javascript+module"),
        "chunks/deep.mjs": ("chunks/deep.mjs", "application/javascript+module"),
        "legacy.cjs": ("legacy.cjs", "application/javascript"),
        "og.wasm": ("og.wasm", "application/wasm"),
        "data.json": ("data.json", "application/json"),
        "notes.txt": ("notes.txt", "text/plain"),
        "blob.bin": ("blob.bin", "application/octet-stream"),
    }


@pytest.mark.asyncio
async def test_astro_shaped_multi_module_bundle_with_routing_options(tmp_path: Path):
    """paw-sites' final worker shape for Astro: many modules in the dry-run outdir
    (nested chunks, a wasm, a .bin), and assets routing options that must reach
    assets.config or SPA fallback and /api routing break."""
    modules = {
        "entry.mjs": "import './chunks/a.mjs'",
        "chunks/a.mjs": "export {}",
        "chunks/b.mjs": "export {}",
        "chunks/pages/index.mjs": "export {}",
        "chunks/sharp.wasm": b"\x00asm\x01\x00\x00\x00",
        "manifest.bin": b"\x01",
    }
    for name, content in modules.items():
        _write(tmp_path, f".paw/worker/{name}", content)
    _write(tmp_path, "dist/client/favicon.svg", "<svg/>")
    _write(tmp_path, "dist/client/_astro/app.css", "body{}")
    astro_headers = "/_astro/*\n  Cache-Control: immutable\n"
    _write(
        tmp_path,
        "paw-build.json",
        json.dumps(
            {
                "slug": "astro",
                "assetsDir": "dist/client",
                "workerEntry": ".paw/worker/entry.mjs",
                "workerModuleDir": ".paw/worker",
                "mainModule": "entry.mjs",
                "workerModules": [f".paw/worker/{n}" for n in sorted(modules)],
                "assetsConfig": {
                    "_headers": astro_headers,
                    "_redirects": None,
                    "html_handling": "drop-trailing-slash",
                    "not_found_handling": "single-page-application",
                    "run_worker_first": ["/api/*"],
                },
                "compat": {"date": "2026-09-28", "flags": ["nodejs_compat"]},
                "bindingRequests": [{"type": "assets", "name": "ASSETS"}],
                "droppedBindings": [
                    {"type": "services", "name": "SELF"},
                    {"type": "vars", "name": "PUBLIC_URL"},
                ],
                "framework": "astro",
                "sizes": {"workerModules": len(modules)},
                "startup": {"cpuMs": 54},
            }
        ),
    )
    fake = _FakeCloudflare()
    result = await bundle_deploy.deploy_bundle(
        fake.client(), script_name="astro_1", build_dir=tmp_path, salt="ws"
    )
    assert result.modules == len(modules)
    assert result.warnings == [
        "binding services:SELF was dropped at build time",
        "binding vars:PUBLIC_URL was dropped at build time",
    ]
    session, _upload, put = fake.requests
    assert set(json.loads(session.content)["manifest"]) == {"/favicon.svg", "/_astro/app.css"}
    parts = _parts(put)
    assert json.loads(parts[0]["content"]) == {
        "main_module": "entry.mjs",
        "bindings": [{"type": "assets", "name": "ASSETS"}],
        "compatibility_date": "2026-09-28",
        "compatibility_flags": ["nodejs_compat"],
        "assets": {
            "jwt": "completion-jwt",
            "config": {
                "_headers": astro_headers,
                "html_handling": "drop-trailing-slash",
                "not_found_handling": "single-page-application",
                "run_worker_first": ["/api/*"],
            },
        },
        "observability": OBSERVABILITY_DEFAULT,
        "limits": FREE_LIMITS,
    }
    assert {p["name"]: p["content_type"] for p in parts[1:]} == {
        "chunks/a.mjs": "application/javascript+module",
        "chunks/b.mjs": "application/javascript+module",
        "chunks/pages/index.mjs": "application/javascript+module",
        "chunks/sharp.wasm": "application/wasm",
        "entry.mjs": "application/javascript+module",
        "manifest.bin": "application/octet-stream",
    }


@pytest.mark.asyncio
async def test_assets_upload_without_an_assets_binding(tmp_path: Path):
    """TanStack Start has assets but its config sets no assets.binding: the assets
    still upload and ride assets.jwt, with no binding in the metadata."""
    _write(tmp_path, ".paw/worker/index.js", "export default {}")
    _write(tmp_path, "dist/client/assets/main.js", "console.log(1)")
    _write(tmp_path, "dist/client/robots.txt", "User-agent: *")
    _write(
        tmp_path,
        "paw-build.json",
        json.dumps(
            {
                "slug": "tanstack-start",
                "assetsDir": "dist/client",
                "workerEntry": ".paw/worker/index.js",
                "workerModuleDir": ".paw/worker",
                "mainModule": "index.js",
                "workerModules": [".paw/worker/index.js"],
                "assetsConfig": {"_headers": None, "_redirects": None},
                "compat": {"date": "2026-09-30", "flags": ["nodejs_compat"]},
                "bindingRequests": [],
                "droppedBindings": [],
            }
        ),
    )
    fake = _FakeCloudflare()
    await bundle_deploy.deploy_bundle(
        fake.client(), script_name="ts_1", build_dir=tmp_path, salt="ws"
    )
    session, _upload, put = fake.requests
    assert set(json.loads(session.content)["manifest"]) == {"/assets/main.js", "/robots.txt"}
    meta = json.loads(_parts(put)[0]["content"])
    assert meta["bindings"] == []
    assert meta["assets"] == {"jwt": "completion-jwt", "config": {}}


def test_nested_main_module_names_parts_from_the_module_dir(tmp_path: Path):
    """With no_bundle, wrangler keeps the entry's path inside the outdir, so
    mainModule has a slash and workerEntry's directory is NOT the module dir."""
    _write(tmp_path, ".paw/worker/server/index.js", "import '../shared/x.js'")
    _write(tmp_path, ".paw/worker/shared/x.js", "export {}")
    _write(
        tmp_path,
        "paw-build.json",
        json.dumps(
            {
                "workerEntry": ".paw/worker/server/index.js",
                "workerModuleDir": ".paw/worker",
                "mainModule": "server/index.js",
                "workerModules": [".paw/worker/server/index.js", ".paw/worker/shared/x.js"],
            }
        ),
    )
    bundle = bundle_deploy.load_bundle(tmp_path, ProvisionedResources())
    assert bundle.main_module == "server/index.js"
    assert [m.name for m in bundle.modules] == ["server/index.js", "shared/x.js"]


def test_assetsignore_matches_are_not_uploaded(tmp_path: Path):
    _write(tmp_path, "public/index.html", "<h1>hi</h1>")
    _write(tmp_path, "public/drafts/a.html", "draft")
    _write(tmp_path, "public/notes.md", "x")
    _write(tmp_path, "public/sub/notes.md", "x")
    _write(tmp_path, "public/.assetsignore", "# comment\n/drafts\n*.md\n")
    _write(tmp_path, "paw-build.json", json.dumps({"assetsDir": "public", "workerModules": []}))
    bundle = bundle_deploy.load_bundle(tmp_path, ProvisionedResources())
    assert list(bundle.assets) == ["/index.html"]
    assert bundle.modules == [] and bundle.main_module is None


@pytest.mark.asyncio
async def test_assets_only_target_puts_no_main_module(tmp_path: Path):
    _write(tmp_path, "dist/index.html", "<h1>hi</h1>")
    _write(
        tmp_path,
        "paw-build.json",
        json.dumps(
            {
                "slug": "static",
                "assetsDir": "dist",
                "workerModules": [],
                "compat": {"date": None, "flags": []},
                "bindingRequests": [],
            }
        ),
    )
    fake = _FakeCloudflare()
    await bundle_deploy.deploy_bundle(fake.client(), script_name="s", build_dir=tmp_path, salt="ws")
    parts = _parts(fake.requests[-1])
    assert len(parts) == 1
    meta = json.loads(parts[0]["content"])
    assert "main_module" not in meta
    assert meta["assets"]["jwt"] == "completion-jwt"


@pytest.mark.asyncio
async def test_earlier_manifest_shape_is_still_accepted(tmp_path: Path):
    """The pre-dry-run manifest: root-relative modules, no mainModule or
    workerModuleDir, no assetsConfig. The entry names the main module;
    _headers/_redirects are lifted out of the assets dir into assets.config."""
    _write(tmp_path, "dist/server/index.js", "export default {}")
    _write(tmp_path, "dist/server/chunks/a.js", "export {}")
    _write(tmp_path, "dist/client/index.html", "<h1>hi</h1>")
    _write(tmp_path, "dist/client/_redirects", "/old /new 301\n")
    _write(tmp_path, "dist/client/.assetsignore", "secret\n")
    _write(
        tmp_path,
        "paw-build.json",
        json.dumps(
            {
                "slug": "tanstack",
                "assetsDir": "dist/client",
                "workerEntry": "dist/server/index.js",
                "workerModules": ["dist/server/index.js", "dist/server/chunks/a.js"],
                "compat": {"date": "2026-09-30", "flags": ["nodejs_compat"]},
                "bindingRequests": [{"type": "assets", "name": "ASSETS"}],
                "framework": "tanstack-start",
                "sizes": {"assetFiles": 1},
                "someFutureField": {"ignored": True},
            }
        ),
    )
    bundle = bundle_deploy.load_bundle(tmp_path, ProvisionedResources())
    assert bundle.main_module == "index.js"
    assert [m.name for m in bundle.modules] == ["index.js", "chunks/a.js"]
    assert list(bundle.assets) == ["/index.html"]
    assert bundle.assets_config == {"_redirects": "/old /new 301\n"}


@pytest.mark.asyncio
async def test_already_uploaded_assets_use_the_session_jwt_and_skip_upload(next_build: Path):
    fake = _FakeCloudflare(buckets="none")
    await bundle_deploy.deploy_bundle(
        fake.client(), script_name="site_1", build_dir=next_build, salt="ws_1"
    )
    assert [r.method for r in fake.requests] == ["POST", "PUT"]
    meta = json.loads(_parts(fake.requests[1])[0]["content"])
    assert meta["assets"]["jwt"] == "session-jwt"


def test_asset_hashes_are_salted_per_tenant_and_content_addressed():
    a = asset_hash(b"same", "/x.js", "ws_a")
    assert a == asset_hash(b"same", "/other/x.js", "ws_a")  # content-addressed
    assert a != asset_hash(b"same", "/x.js", "ws_b")  # tenant salt
    assert a != asset_hash(b"same", "/x.txt", "ws_a")  # extension matters


@pytest.mark.asyncio
async def test_upload_assets_refuses_without_a_salt():
    fake = _FakeCloudflare()
    with pytest.raises(ValidationError):
        await fake.client().upload_assets(script_name="s", assets={"/a": b"x"}, salt="")
    assert fake.requests == []


# ------------------------------------------------------------- compat


def test_flags_outside_the_allow_list_are_dropped():
    date_, flags, warnings = resolve_compat(
        {
            "date": "2026-09-01",
            "flags": [
                "nodejs_compat",
                "unsafe_eval",
                "global_fetch_strictly_public",
                "experimental",
            ],
        }
    )
    assert date_ == "2026-09-01"
    assert flags == ["nodejs_compat", "global_fetch_strictly_public"]
    assert len(warnings) == 2 and "unsafe_eval" in warnings[0]


def test_old_date_is_bumped_for_nodejs_compat_only():
    assert resolve_compat({"date": "2024-01-01", "flags": ["nodejs_compat"]})[0] == "2024-09-23"
    assert resolve_compat({"date": "2024-01-01", "flags": []})[0] == "2024-01-01"


def test_missing_bad_and_future_dates():
    from datetime import date

    assert resolve_compat({})[0] == bundle_deploy.DEFAULT_COMPAT_DATE
    assert resolve_compat({"date": "not-a-date"})[0] == bundle_deploy.DEFAULT_COMPAT_DATE
    today = date(2026, 10, 7)
    assert resolve_compat({"date": "2030-01-01"}, today=today)[0] == "2026-10-07"
    # wrangler key names are tolerated
    assert resolve_compat(
        {"compatibility_date": "2026-01-01", "compatibility_flags": ["nodejs_als"]}
    )[:2] == ("2026-01-01", ["nodejs_als"])


# ----------------------------------------------------------- bindings


def test_d1_maps_to_our_database_and_ignores_author_ids():
    bindings, warnings = map_bindings(
        [
            {"type": "assets", "name": "ASSETS"},
            {"type": "d1", "name": "DB", "id": "someone-elses-db", "database_id": "x"},
        ],
        [],
        ProvisionedResources(d1_database_id="our-d1"),
        has_assets=True,
    )
    assert bindings == [
        {"type": "assets", "name": "ASSETS"},
        {"type": "d1", "name": "DB", "id": "our-d1"},
    ]
    assert warnings == []


@pytest.mark.parametrize("kind", ["d1", "kv", "r2", "do", "ai", "queues"])
def test_unprovisioned_backends_are_refused(kind: str):
    with pytest.raises(ValidationError, match="not provisioned"):
        map_bindings([{"type": kind, "name": "X"}], [], ProvisionedResources(), has_assets=False)


def test_provisioned_backends_map_to_our_resources():
    res = ProvisionedResources(
        kv_namespaces={"CACHE": "kv1"}, r2_buckets={"FILES": "b1"}, queue_name="q1", ai=True
    )
    bindings, _ = map_bindings(
        [
            {"type": "kv", "name": "CACHE", "id": "theirs"},
            {"type": "r2", "name": "FILES", "bucket_name": "theirs"},
            {"type": "queues", "name": "JOBS", "queue": "theirs"},
            {"type": "ai", "name": "AI"},
        ],
        [],
        res,
        has_assets=False,
    )
    assert bindings == [
        {"type": "kv_namespace", "name": "CACHE", "namespace_id": "kv1"},
        {"type": "r2_bucket", "name": "FILES", "bucket_name": "b1"},
        {"type": "queue", "name": "JOBS", "queue_name": "q1"},
        {"type": "ai", "name": "AI"},
    ]


def test_services_dispatch_and_tail_are_never_forwarded():
    bindings, warnings = map_bindings(
        [
            {"type": "service", "name": "WORKER_SELF_REFERENCE", "service": "paw-sites-dispatch"},
            {"type": "dispatch_namespaces", "name": "NS", "namespace": "paw-sites"},
            {"type": "tail_consumers", "name": "T", "service": "internal"},
            {"type": "images", "name": "IMAGES"},
            {"type": "unknown", "name": "FROM_TOML"},
            {"type": "hyperdrive", "name": "PG", "id": "x"},
        ],
        ["service:WORKER_SELF_REFERENCE", {"type": "images", "name": "IMAGES"}],
        ProvisionedResources(d1_database_id="d"),
        has_assets=True,
    )
    assert bindings == []
    assert len(warnings) == 8
    assert "dropped at build time" in warnings[0]


def test_binding_refusals():
    res = ProvisionedResources(d1_database_id="d")
    for requests in (
        [{"type": "d1", "name": "A"}, {"type": "d1", "name": "B"}],  # one D1 per site
        [{"type": "assets", "name": "X"}, {"type": "d1", "name": "X"}],  # duplicate name
        [{"type": "d1", "name": "bad-name"}],  # not an identifier
        [{"type": "secret", "name": "API_KEY", "required": True}],  # unset required secret
    ):
        with pytest.raises(ValidationError):
            map_bindings(requests, [], res, has_assets=True)


def test_secrets_come_from_our_store_and_optional_ones_are_skipped():
    bindings, warnings = map_bindings(
        [
            {"type": "secret", "name": "API_KEY", "required": True},
            {"type": "secret", "name": "OPTIONAL", "required": False},
        ],
        [],
        ProvisionedResources(secrets={"API_KEY": "s3cret"}),
        has_assets=False,
    )
    assert bindings == [{"type": "secret_text", "name": "API_KEY", "text": "s3cret"}]
    assert len(warnings) == 1


def test_assets_binding_without_assets_is_dropped():
    bindings, warnings = map_bindings(
        [{"type": "assets", "name": "ASSETS"}], [], ProvisionedResources(), has_assets=False
    )
    assert bindings == [] and len(warnings) == 1


# ------------------------------------------------------- limits / paths


@pytest.mark.asyncio
async def test_oversized_worker_is_refused_before_any_upload(next_build: Path, monkeypatch):
    monkeypatch.setattr(bundle_deploy, "MAX_WORKER_BYTES", 10)
    fake = _FakeCloudflare()
    with pytest.raises(ValidationError, match="64 MiB"):
        await bundle_deploy.deploy_bundle(
            fake.client(), script_name="site_1", build_dir=next_build, salt="ws"
        )
    assert fake.requests == []


def test_module_count_and_asset_caps(next_build: Path, monkeypatch):
    monkeypatch.setattr(bundle_deploy, "MAX_WORKER_MODULES", 0)
    with pytest.raises(ValidationError, match="module cap"):
        bundle_deploy.load_bundle(next_build, ProvisionedResources())
    monkeypatch.setattr(bundle_deploy, "MAX_WORKER_MODULES", 1000)
    monkeypatch.setattr(bundle_deploy, "MAX_ASSET_FILE_BYTES", 5)
    with pytest.raises(ValidationError, match="25 MiB"):
        bundle_deploy.load_bundle(next_build, ProvisionedResources())


@pytest.mark.parametrize(
    "field,value",
    [
        ("workerModules", ["../outside.js"]),
        ("workerEntry", "/etc/passwd"),
        ("assetsDir", "C:/Windows"),
        ("mainModule", "a/../../b.js"),
    ],
)
def test_paths_outside_the_build_are_refused(tmp_path: Path, field: str, value):
    _write(tmp_path, "w/index.js", "export default {}")
    manifest = {"workerEntry": "w/index.js", "workerModules": ["w/index.js"], field: value}
    _write(tmp_path, "paw-build.json", json.dumps(manifest))
    with pytest.raises(ValidationError, match="inside the build"):
        bundle_deploy.load_bundle(tmp_path, ProvisionedResources())


def test_empty_or_broken_manifest_is_refused(tmp_path: Path):
    _write(tmp_path, "paw-build.json", "{not json")
    with pytest.raises(ValidationError, match="valid JSON"):
        bundle_deploy.load_bundle(tmp_path, ProvisionedResources())
    _write(tmp_path, "paw-build.json", "{}")
    with pytest.raises(ValidationError, match="neither"):
        bundle_deploy.load_bundle(tmp_path, ProvisionedResources())


# ------------------------------------------------------- worker settings


def _with_requests(build: Path, requests: list[dict]) -> Path:
    manifest = json.loads((build / "paw-build.json").read_text())
    manifest["bindingRequests"] = [{"type": "assets", "name": "ASSETS"}, *requests]
    (build / "paw-build.json").write_text(json.dumps(manifest))
    return build


async def _put_metadata(
    build: Path, provisioned: ProvisionedResources, target: str, *, paid: bool = False
) -> dict:
    fake = _FakeCloudflare()
    await bundle_deploy.deploy_bundle(
        fake.client(),
        script_name="site_1",
        build_dir=build,
        salt="ws_1",
        provisioned=provisioned,
        target=target,
        paid=paid,
    )
    put = fake.requests[-1]
    assert put.method == "PUT"
    return json.loads(_parts(put)[0]["content"])


def _assets_only_build(root: Path) -> Path:
    _write(root, "dist/index.html", "<h1>hi</h1>")
    _write(
        root,
        "paw-build.json",
        json.dumps(
            {
                "assetsDir": "dist",
                "workerModules": [],
                "compat": {"date": "2026-09-01", "flags": []},
                "bindingRequests": [{"type": "d1", "name": "DB"}],
            }
        ),
    )
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["account", "dispatch"])
async def test_smart_placement_is_off_by_default_even_with_d1(next_build: Path, target: str):
    build = _with_requests(next_build, [{"type": "d1", "name": "DB"}])
    meta = await _put_metadata(build, ProvisionedResources(d1_database_id="our-d1"), target)
    assert "placement" not in meta
    assert {"type": "d1", "name": "DB", "id": "our-d1"} in meta["bindings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["account", "dispatch"])
@pytest.mark.parametrize(
    ("requests", "provisioned"),
    [
        ([{"type": "d1", "name": "DB"}], ProvisionedResources(d1_database_id="our-d1")),
        ([{"type": "r2", "name": "FILES"}], ProvisionedResources(r2_buckets={"FILES": "paw-b"})),
    ],
)
async def test_opted_in_smart_placement_covers_regional_backends_on_both_targets(
    next_build: Path, monkeypatch, target: str, requests, provisioned
):
    monkeypatch.setenv("PAW_SITES_SMART_PLACEMENT", "1")
    meta = await _put_metadata(_with_requests(next_build, requests), provisioned, target)
    assert meta["placement"] == {"mode": "smart"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requests", "provisioned"),
    [
        ([], ProvisionedResources()),  # assets binding only
        ([{"type": "kv", "name": "CACHE"}], ProvisionedResources(kv_namespaces={"CACHE": "k"})),
    ],
)
async def test_opted_in_placement_skips_workers_without_a_regional_backend(
    next_build: Path, monkeypatch, requests, provisioned
):
    monkeypatch.setenv("PAW_SITES_SMART_PLACEMENT", "true")
    meta = await _put_metadata(_with_requests(next_build, requests), provisioned, "account")
    assert "placement" not in meta


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "false", "", "maybe"])
async def test_only_a_truthy_value_opts_into_placement(next_build: Path, monkeypatch, value):
    monkeypatch.setenv("PAW_SITES_SMART_PLACEMENT", value)
    build = _with_requests(next_build, [{"type": "d1", "name": "DB"}])
    meta = await _put_metadata(build, ProvisionedResources(d1_database_id="our-d1"), "account")
    assert "placement" not in meta


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["account", "dispatch"])
async def test_observability_is_on_by_default_at_ten_percent(next_build: Path, target: str):
    meta = await _put_metadata(next_build, ProvisionedResources(), target)
    assert meta["observability"] == OBSERVABILITY_DEFAULT


@pytest.mark.asyncio
async def test_observability_sample_rate_comes_from_env(next_build: Path, monkeypatch):
    monkeypatch.setenv("PAW_SITES_OBSERVABILITY_SAMPLE", "0.25")
    obs = (await _put_metadata(next_build, ProvisionedResources(), "account"))["observability"]
    assert obs["head_sampling_rate"] == 0.25
    assert obs["traces"] == {"enabled": True, "head_sampling_rate": 0.25}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["2", "-0.1", "lots"])
async def test_an_invalid_sample_rate_falls_back_to_the_default(
    next_build: Path, monkeypatch, value: str
):
    monkeypatch.setenv("PAW_SITES_OBSERVABILITY_SAMPLE", value)
    obs = (await _put_metadata(next_build, ProvisionedResources(), "account"))["observability"]
    assert obs == OBSERVABILITY_DEFAULT


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "off", "FALSE"])
async def test_observability_opt_out(next_build: Path, monkeypatch, value: str):
    monkeypatch.setenv("PAW_SITES_OBSERVABILITY", value)
    meta = await _put_metadata(next_build, ProvisionedResources(), "dispatch")
    assert "observability" not in meta


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["account", "dispatch"])
@pytest.mark.parametrize(
    ("paid", "limits"),
    [
        (False, {"cpu_ms": 50, "subrequests": 50}),
        (True, {"cpu_ms": 300, "subrequests": 10_000}),
    ],
)
async def test_limits_follow_the_site_plan(next_build: Path, target: str, paid: bool, limits):
    meta = await _put_metadata(next_build, ProvisionedResources(), target, paid=paid)
    assert meta["limits"] == limits


@pytest.mark.asyncio
async def test_limits_env_overrides_and_zero_leaves_a_field_out(next_build: Path, monkeypatch):
    monkeypatch.setenv("PAW_SITES_CPU_MS_FREE", "20")
    monkeypatch.setenv("PAW_SITES_SUBREQUESTS_FREE", "0")
    monkeypatch.setenv("PAW_SITES_CPU_MS_PAID", "1000")
    free_meta = await _put_metadata(next_build, ProvisionedResources(), "account")
    assert free_meta["limits"] == {"cpu_ms": 20}
    paid_meta = await _put_metadata(next_build, ProvisionedResources(), "account", paid=True)
    assert paid_meta["limits"] == {"cpu_ms": 1000, "subrequests": 10_000}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["-5", "300001", "fast"])
async def test_an_invalid_cpu_limit_falls_back_to_the_plan_default(
    next_build: Path, monkeypatch, value: str
):
    monkeypatch.setenv("PAW_SITES_CPU_MS_PAID", value)
    meta = await _put_metadata(next_build, ProvisionedResources(), "account", paid=True)
    assert meta["limits"]["cpu_ms"] == 300


@pytest.mark.asyncio
async def test_all_limits_zero_sends_no_limits_block(next_build: Path, monkeypatch):
    monkeypatch.setenv("PAW_SITES_CPU_MS_FREE", "0")
    monkeypatch.setenv("PAW_SITES_SUBREQUESTS_FREE", "0")
    assert "limits" not in await _put_metadata(next_build, ProvisionedResources(), "account")


@pytest.mark.asyncio
async def test_an_assets_only_worker_gets_no_worker_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PAW_SITES_SMART_PLACEMENT", "1")
    meta = await _put_metadata(
        _assets_only_build(tmp_path), ProvisionedResources(d1_database_id="our-d1"), "account"
    )
    assert "main_module" not in meta
    assert not {"placement", "observability", "limits"} & set(meta)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting",
    [
        {"placement": {"mode": "smart"}},
        {"observability": {"enabled": True}},
        {"limits": {"cpu_ms": 50}},
    ],
)
async def test_worker_settings_are_refused_on_the_legacy_put_worker_shapes(setting):
    fake = _FakeCloudflare()
    with pytest.raises(ValidationError):
        await fake.client().put_worker(script_name="s", bundle=b"export default {}", **setting)
    assert fake.requests == []


# ---------------------------------------------------- legacy unchanged


@pytest.mark.asyncio
async def test_legacy_put_worker_paths_are_unchanged():
    fake = _FakeCloudflare()
    client = fake.client()
    await client.put_worker(script_name="s", bundle=b"export default {STATIC}")
    await client.put_worker(
        script_name="s",
        bundle=b"export default {D}",
        bindings=[{"type": "d1", "name": "DB", "id": "x"}],
    )
    static, dynamic = fake.requests
    assert static.content == b"export default {STATIC}"
    assert static.headers["content-type"] == "application/javascript+module"
    meta, module = _parts(dynamic)
    assert json.loads(meta["content"]) == {
        "main_module": "index.mjs",
        "bindings": [{"type": "d1", "name": "DB", "id": "x"}],
        "compatibility_date": "2024-09-23",
    }
    assert module["name"] == "index.mjs" and module["content"] == b"export default {D}"


@pytest.mark.asyncio
async def test_put_worker_refuses_a_bundle_and_modules_together():
    from pocketpaw_ee.sites.cloudflare_client import WorkerModule

    fake = _FakeCloudflare()
    with pytest.raises(ValidationError):
        await fake.client().put_worker(
            script_name="s",
            bundle=b"x",
            modules=[WorkerModule("a.js", b"", "application/javascript+module")],
            main_module="a.js",
            compatibility_date="2026-09-01",
        )
    assert fake.requests == []


# ------------------------------------------------------------- service


class _RecordingCF:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def upload_assets(self, **kw):
        self.calls.append(("upload_assets", kw))
        return "jwt-1"

    async def put_worker(self, **kw):
        self.calls.append(("put_worker", kw))
        return True


@pytest.mark.asyncio
async def test_service_deploy_bundle_uses_site_id_workspace_salt_and_site_d1(next_build: Path):
    from pocketpaw_ee.sites import service as sites_service

    manifest = json.loads((next_build / "paw-build.json").read_text())
    manifest["bindingRequests"].append({"type": "d1", "name": "DB", "id": "author-id"})
    (next_build / "paw-build.json").write_text(json.dumps(manifest))
    site = SimpleNamespace(id="site_9", workspace="ws_9", d1_database_id="d1-ours")
    cf = _RecordingCF()

    result = await sites_service.deploy_bundle(site, next_build, cloudflare=cf)

    (_, upload), (_, put) = cf.calls
    assert upload["script_name"] == "site_9" and upload["salt"] == "ws_9"
    assert put["script_name"] == "site_9"
    assert put["bindings"] == [
        {"type": "assets", "name": "ASSETS"},
        {"type": "d1", "name": "DB", "id": "d1-ours"},
    ]
    assert put["assets"] == {"jwt": "jwt-1", "config": {"_headers": HEADERS}}
    assert result.warnings == []


@pytest.mark.asyncio
async def test_publish_takes_the_bundle_path_only_when_paw_build_exists(
    beanie_test_db, next_build: Path
):
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.generator_client import BuildResult

    class _Gen:
        async def build(self, **kw):
            return BuildResult(project_dir=str(next_build), ripple_version="0.2.0")

    cf = _RecordingCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Bundle",
        _generator=_Gen(),
        _cloudflare=cf,
        _bundle_reader=lambda d: pytest.fail("the legacy bundle reader must not run"),
    )
    assert site.deployed is True
    assert [name for name, _ in cf.calls] == ["upload_assets", "put_worker"]
    assert cf.calls[0][1]["salt"] == "ws1"
    assert cf.calls[1][1]["script_name"] == str(site.id)
