# tests/ee/sites/test_site_data_files_kv.py — the Sites Data dashboard's file (R2) and
# key-value (KV) routes (``sites.data_router`` -> ``sites.data_service`` ->
# ``site_data.files_kv``), and the connectors' ``dashboard_url``.
#
# R2 and KV are an in-memory ``FakeStores`` client (buckets and namespaces as dicts,
# listing with prefix / delimiter / cursor like Cloudflare), so the service logic runs
# for real while the HTTP shapes are pinned separately in
# ``test_cloudflare_client_r2_kv.py``. Audit events are captured, not stored.
#
# Covers TDD §11 "PR 7" and its security pass: folder listing and pagination, the
# file-key validation table, risky content types stored and served as octet-stream
# with ``attachment`` + ``nosniff``, the 50 MB cap (route and body ceiling), move
# without overwrite -> 409, a binding not on the Site -> 404 with no Cloudflare call,
# member write -> 403, read-only pocket -> 403, masked KV values, binary values, TTL
# and metadata limits, audit without values, local mode, and dashboard_url refusals.
from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.errors import CloudError  # noqa: E402
from pocketpaw_ee.sites import data_service  # noqa: E402
from pocketpaw_ee.sites import service as sites_service  # noqa: E402
from pocketpaw_ee.sites.cloudflare_client import KvValue  # noqa: E402
from pocketpaw_ee.sites.site_data import budget, files_kv  # noqa: E402

_PATCH_GET = "pocketpaw_ee.cloud.pockets.service.get"
_PATCH_EDIT = "pocketpaw_ee.cloud.pockets.service.has_edit_access"
_PATCH_CONNECTORS = "pocketpaw_ee.integrations.bindings.list_for_pocket"
_DB = "11111111-2222-3333-4444-555555555555"
_WIRE = {
    "name": "Shop",
    "workspace": "ws1",
    "engine": "project",
    "siteMeta": {"project": {"template": "astro", "recipes": ["d1-drizzle"]}},
    "source": {"package.json": "{}"},
}


# --------------------------------------------------------------------------- fakes


class _Obj:
    def __init__(self, data: bytes, content_type: str) -> None:
        self.content_type = content_type
        self.size = len(data)
        self.etag = "e"
        self._data = data
        self.closed = False

    async def iter_bytes(self):
        for i in range(0, len(self._data), 7):
            yield self._data[i : i + 7]
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


class FakeStores:
    """R2 buckets and KV namespaces in memory, with Cloudflare's listing rules."""

    def __init__(self) -> None:
        self.r2: dict[str, dict[str, tuple[bytes, str]]] = {"bkt-1": {}, "bkt-other": {}}
        self.kv: dict[str, dict[str, tuple[bytes, Any, int | None]]] = {"ns-1": {}, "ns-x": {}}
        self.calls: list[tuple[str, str]] = []

    async def r2_list(self, bucket, *, prefix="", delimiter="/", cursor=None, per_page=200):
        self.calls.append(("r2_list", bucket))
        keys = sorted(k for k in self.r2[bucket] if k.startswith(prefix))
        folders: list[str] = []
        objects: list[str] = []
        for k in keys:
            rest = k[len(prefix) :]
            if delimiter and delimiter in rest:
                folder = prefix + rest.split(delimiter, 1)[0] + delimiter
                if folder not in folders:
                    folders.append(folder)
            else:
                objects.append(k)
        start = int(cursor or 0)
        page = objects[start : start + per_page]
        more = start + per_page < len(objects)
        return {
            "objects": [
                {
                    "key": k,
                    "size": len(self.r2[bucket][k][0]),
                    "etag": "e",
                    "last_modified": "2026-10-28T10:00:00Z",
                    "content_type": self.r2[bucket][k][1],
                }
                for k in page
            ],
            "folders": folders if start == 0 else [],
            "cursor": str(start + per_page) if more else None,
            "truncated": more,
        }

    async def r2_get(self, bucket, key):
        self.calls.append(("r2_get", bucket))
        found = self.r2[bucket].get(key)
        return None if found is None else _Obj(*found)

    async def r2_put(self, bucket, key, body, *, content_type, size=None):
        self.calls.append(("r2_put", bucket))
        data = b"".join([c async for c in body])
        assert size is None or size == len(data)
        self.r2[bucket][key] = (data, content_type)
        return {"key": key, "size": len(data), "etag": "e", "uploaded": "2026-10-28T11:00:00Z"}

    async def r2_delete(self, bucket, key):
        self.calls.append(("r2_delete", bucket))
        self.r2[bucket].pop(key, None)

    async def kv_list_keys(self, ns, *, prefix="", cursor=None, limit=200):
        self.calls.append(("kv_list_keys", ns))
        names = sorted(k for k in self.kv[ns] if k.startswith(prefix))
        start = int(cursor or 0)
        page = names[start : start + limit]
        return {
            "keys": [
                {"name": n, "expiration": self.kv[ns][n][2], "metadata": self.kv[ns][n][1]}
                for n in page
            ],
            "cursor": str(start + limit) if start + limit < len(names) else None,
        }

    async def kv_get(self, ns, key, *, max_bytes=None):
        self.calls.append(("kv_get", ns))
        found = self.kv[ns].get(key)
        if found is None:
            return None
        data = found[0]
        kept = data if max_bytes is None else data[:max_bytes]
        truncated = len(kept) < len(data)
        try:
            text = None if truncated else kept.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        return KvValue(data=kept, size=len(data), truncated=truncated, text=text)

    async def kv_metadata(self, ns, key):
        self.calls.append(("kv_metadata", ns))
        found = self.kv[ns].get(key)
        return None if found is None else found[1]

    async def kv_put(self, ns, key, value, *, metadata=None, expiration_ttl=None):
        self.calls.append(("kv_put", ns))
        self.kv[ns][key] = (value, metadata, 2_000_000_000 if expiration_ttl else None)

    async def kv_delete(self, ns, key):
        self.calls.append(("kv_delete", ns))
        self.kv[ns].pop(key, None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    budget.reset()
    monkeypatch.setenv("PAW_CF_ACCOUNT_ID", "acct_1")
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    yield
    budget.reset()


@pytest.fixture
def site_doc(monkeypatch):
    doc = SimpleNamespace(
        d1_database_id=_DB,
        kv_namespaces={"CACHE": "ns-1"},
        r2_buckets={"SITE_FILES": "bkt-1"},
        name="Shop",
    )

    async def _doc(workspace_id, pocket_id):
        return doc

    monkeypatch.setattr(sites_service, "_canonical_site_doc", _doc)
    return doc


@pytest.fixture
def audit(monkeypatch):
    events: list[dict[str, Any]] = []

    async def _record(workspace_id, actor_id, action, **kw):
        events.append({"workspace_id": workspace_id, "actor_id": actor_id, "action": action, **kw})

    from pocketpaw_ee.cloud.audit import service as audit_service

    monkeypatch.setattr(audit_service, "record", _record)
    return events


@pytest.fixture
def cf(site_doc, audit, monkeypatch) -> FakeStores:
    fake = FakeStores()
    monkeypatch.setattr(sites_service, "_data_cf_client", lambda: fake)
    return fake


class _pocket:  # noqa: N801 — reads like the patch it is
    def __init__(self, *, can_edit: bool = True) -> None:
        self._patches = [
            patch(_PATCH_GET, new=AsyncMock(return_value=_WIRE)),
            patch(_PATCH_EDIT, new=AsyncMock(return_value=can_edit)),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


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


async def _call(
    method: str,
    path: str,
    *,
    role: str = "admin",
    can_edit: bool = True,
    json: Any = None,
    content: bytes | None = None,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
):
    base = "/api/v1/sites/by-pocket/pk1/data"
    with _pocket(can_edit=can_edit):
        transport = ASGITransport(app=_app(role))
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.request(
                method,
                base + path,
                json=json,
                content=content,
                params=params,
                headers=headers,
            )


def _code(resp) -> str:
    return resp.json()["error"]["code"]


# --------------------------------------------------------------------------- files: list


@pytest.mark.asyncio
async def test_list_folders_and_objects(cf):
    cf.r2["bkt-1"].update(
        {
            "a.txt": (b"hi", "text/plain"),
            "img/b.png": (b"\x89PNG", "image/png"),
            "img/sub/c.png": (b"x", "image/png"),
            "docs/d.pdf": (b"%PDF", "application/pdf"),
        }
    )
    root = await _call("GET", "/files/SITE_FILES", role="member")
    assert root.status_code == 200, root.text
    assert root.json() == {
        "pocket_id": "pk1",
        "binding": "SITE_FILES",
        "available": True,
        "reason": "",
        "prefix": "",
        "folders": ["docs/", "img/"],
        "objects": [
            {
                "key": "a.txt",
                "name": "a.txt",
                "size": 2,
                "content_type": "text/plain",
                "modified": "2026-10-28T10:00:00Z",
            }
        ],
        "cursor": None,
    }
    img = await _call("GET", "/files/SITE_FILES", role="member", params={"prefix": "img/"})
    assert img.json()["folders"] == ["img/sub/"]
    assert [(o["key"], o["name"]) for o in img.json()["objects"]] == [("img/b.png", "b.png")]


@pytest.mark.asyncio
async def test_list_paginates_200_at_a_time(cf):
    cf.r2["bkt-1"].update({f"f{i:03}.txt": (b"x", "text/plain") for i in range(250)})
    first = await _call("GET", "/files/SITE_FILES")
    assert len(first.json()["objects"]) == 200 and first.json()["cursor"] == "200"
    second = await _call("GET", "/files/SITE_FILES", params={"cursor": "200"})
    assert len(second.json()["objects"]) == 50 and second.json()["cursor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "kw"),
    [
        ("GET", "/files/OTHER", {}),
        ("GET", "/files/OTHER/object", {"params": {"key": "a"}}),
        ("PUT", "/files/OTHER/object", {"params": {"key": "a"}, "content": b"x"}),
        ("DELETE", "/files/OTHER/object", {"params": {"key": "a"}}),
        ("POST", "/files/OTHER/object:move", {"json": {"from": "a", "to": "b"}}),
        ("GET", "/kv/OTHER/keys", {}),
        ("GET", "/kv/OTHER/keys/a", {}),
        ("PUT", "/kv/OTHER/keys/a", {"json": {"value": "x"}}),
        ("DELETE", "/kv/OTHER/keys/a", {}),
        # Another site's bucket / namespace id named directly is not a binding either.
        ("GET", "/files/bkt-other", {}),
        ("GET", "/kv/ns-x/keys", {}),
    ],
)
async def test_a_binding_not_on_the_site_is_404_before_any_call(cf, method, path, kw):
    resp = await _call(method, path, **kw)
    assert resp.status_code == 404, resp.text
    assert cf.calls == []


@pytest.mark.asyncio
async def test_local_mode_lists_unavailable_and_refuses_the_rest(cf, monkeypatch):
    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    files = await _call("GET", "/files/SITE_FILES")
    assert files.json()["available"] is False
    assert files.json()["reason"] == "live_on_cloudflare_only"
    keys = await _call("GET", "/kv/CACHE/keys")
    assert keys.json()["available"] is False and keys.json()["keys"] == []
    down = await _call("GET", "/files/SITE_FILES/object", params={"key": "a"})
    assert down.status_code == 409 and _code(down) == "sites.data_unavailable"
    put = await _call("PUT", "/kv/CACHE/keys/a", json={"value": "x"})
    assert put.status_code == 409
    assert cf.calls == []


# --------------------------------------------------------------------------- files: keys


BAD_KEYS = [
    "/lead",
    "a//b",
    "a/",
    "./a",
    "a/./b",
    "../a",
    "a/../b",
    "a/..",
    "a\\b",
    "a\x00b",
    "a\nb",
    "a\x7fb",
    "x" * 1025,
    "é" * 513,  # 1,026 bytes
]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", BAD_KEYS)
async def test_bad_file_keys_are_422_before_any_call(cf, key):
    for method, kw in [
        ("PUT", {"content": b"x"}),
        ("GET", {}),
        ("DELETE", {}),
    ]:
        resp = await _call(method, "/files/SITE_FILES/object", params={"key": key}, **kw)
        assert resp.status_code == 422, (method, key, resp.text)
        assert _code(resp) == "sites.data_bad_file_key"
    move = await _call("POST", "/files/SITE_FILES/object:move", json={"from": "ok.txt", "to": key})
    assert move.status_code == 422 and _code(move) == "sites.data_bad_file_key"
    assert cf.calls == []


@pytest.mark.parametrize(
    "key", ["a", "a/b/c.txt", "x" * 1024, "日本/ファイル.txt", "a b.txt", "..a"]
)
def test_good_file_keys_pass(key):
    assert files_kv.check_file_key(key) == key


# --------------------------------------------------------------------------- files: types


ALLOWED = [
    ("image/png", "a.png"),
    ("image/jpeg", "a.jpg"),
    ("image/gif", "a.gif"),
    ("image/webp", "a.webp"),
    ("image/avif", "a.avif"),
    ("image/bmp", "a.bmp"),
    ("image/x-icon", "a.ico"),
    ("image/vnd.microsoft.icon", "a.ico"),
    ("application/pdf", "a.pdf"),
    ("text/plain", "a.txt"),
    ("text/csv", "a.csv"),
    ("application/json", "a.json"),
    ("audio/mpeg", "a.mp3"),
    ("audio/ogg", "a.ogg"),
    ("video/mp4", "a.mp4"),
    ("video/webm", "a.webm"),
    ("font/woff", "a.woff"),
    ("font/woff2", "a.woff2"),
    ("font/ttf", "a.ttf"),
    ("font/otf", "a.otf"),
    ("application/zip", "a.zip"),
    ("application/gzip", "a.gz"),
    ("text/css", "a.css"),
]

REFUSED_TYPES = [
    "text/html",
    "TEXT/HTML",
    "text/html; charset=utf-8",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
    "text/xsl",  # an XML type in Blink: XHTML-namespaced script runs
    "text/xsl; charset=utf-8",
    "application/xslt+xml",
    "text/javascript",
    "application/javascript",
    "text/livescript",
    "text/jscript",
    "text/vbscript",
    "message/rfc822",
    "application/x-mimearchive",
    "multipart/related",
    "multipart/x-mixed-replace",
    "text/vnd.wap.wml",
    "text/x-component",
    "application/x-shockwave-flash",
    "application/wasm",
    "application/postscript",
    "audio/x-script",
    "video/vnd.foo+xml",
    "video/x-xmlish",
    "image/tiff",
    "not a type",
    "image/png/x",
]


@pytest.mark.parametrize(("mime", "key"), ALLOWED)
def test_allowed_types_are_kept(mime, key):
    assert files_kv.stored_content_type(mime, key) == mime
    assert files_kv.stored_content_type(mime.upper(), key) == mime
    assert files_kv.stored_content_type(mime + "; charset=utf-8", key) == mime
    assert files_kv.stored_content_type(" " + mime + " ;q=1", key) == mime
    assert files_kv.stored_content_type("", key) in (mime, "image/vnd.microsoft.icon")
    assert files_kv.download_content_type(mime, key) == mime
    assert files_kv.download_content_type(mime.upper() + ";x=y", key) == mime


@pytest.mark.parametrize("mime", REFUSED_TYPES)
@pytest.mark.parametrize("key", ["a.png", "a", "a.bin"])
def test_every_other_type_is_octet_stream(mime, key):
    assert files_kv.stored_content_type(mime, key) == files_kv.OCTET
    assert files_kv.download_content_type(mime, key) == files_kv.OCTET


@pytest.mark.parametrize(
    "key",
    [
        "page.html",
        "x.HTM",
        "logo.svg",
        "a.xml",
        "a.xsl",
        "a.js",
        "a.mjs",
        "a.eml",  # message/rfc822
        "a.mht",
        "a.wasm",
        "a.ps",
    ],
)
def test_a_harmful_extension_forces_octet_stream(key):
    for mime in ("image/png", "text/plain", ""):
        assert files_kv.stored_content_type(mime, key) == files_kv.OCTET
        assert files_kv.download_content_type(mime or "image/png", key) == files_kv.OCTET


@pytest.mark.parametrize(
    ("declared", "key", "stored"),
    [
        ("image/png", "photo.jpg", "image/png"),
        ("", "photo.jpg", "image/jpeg"),
        ("application/octet-stream", "doc.pdf", "application/pdf"),
        ("", "noext", "application/octet-stream"),
        ("", "a.unknownext", "application/octet-stream"),
        ("text/plain", "notes.unknownext", "text/plain"),
    ],
)
def test_stored_content_type(declared, key, stored):
    assert files_kv.stored_content_type(declared, key) == stored


@pytest.mark.asyncio
async def test_upload_stores_risky_files_as_octet_stream(cf, audit):
    resp = await _call(
        "PUT",
        "/files/SITE_FILES/object",
        params={"key": "pages/evil.html"},
        content=b"<script>alert(1)</script>",
        headers={"content-type": "text/html"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "object": {
            "key": "pages/evil.html",
            "name": "evil.html",
            "size": 25,
            "content_type": "application/octet-stream",
            "modified": "2026-10-28T11:00:00Z",
        }
    }
    assert cf.r2["bkt-1"]["pages/evil.html"][1] == "application/octet-stream"
    svg = await _call(
        "PUT",
        "/files/SITE_FILES/object",
        params={"key": "logo.png"},
        content=b"<svg onload=alert(1)>",
        headers={"content-type": "image/svg+xml"},
    )
    assert svg.json()["object"]["content_type"] == "application/octet-stream"
    ok = await _call(
        "PUT",
        "/files/SITE_FILES/object",
        params={"key": "img/a.png"},
        content=b"\x89PNG",
        headers={"content-type": "image/png"},
    )
    assert cf.r2["bkt-1"]["img/a.png"] == (b"\x89PNG", "image/png")
    assert ok.json()["object"]["size"] == 4
    # Audit: key, size and type only.
    put = [e for e in audit if e["action"] == "site_data.file_put"]
    assert len(put) == 3
    assert put[0]["target_type"] == "site_store"
    assert put[0]["target_id"] == "pk1:SITE_FILES:pages/evil.html"
    assert put[0]["metadata"] == {
        "binding": "SITE_FILES",
        "key": "pages/evil.html",
        "size": 25,
        "content_type": "application/octet-stream",
    }
    assert "script" not in repr(audit)


@pytest.mark.asyncio
async def test_download_headers_never_render(cf):
    cf.r2["bkt-1"]["x/page.html"] = (b"<script>1</script>", "text/html")
    cf.r2["bkt-1"]['x/a bé".png'] = (b"\x89PNG", "image/png")
    html = await _call("GET", "/files/SITE_FILES/object", params={"key": "x/page.html"})
    assert html.status_code == 200 and html.content == b"<script>1</script>"
    assert html.headers["content-type"] == "application/octet-stream"
    assert html.headers["x-content-type-options"] == "nosniff"
    assert html.headers["content-disposition"].startswith('attachment; filename="page.html"')
    assert "sandbox" in html.headers["content-security-policy"]
    assert html.headers["cache-control"] == "no-store"
    png = await _call("GET", "/files/SITE_FILES/object", params={"key": 'x/a bé".png'})
    assert png.headers["content-type"] == "image/png"
    disposition = png.headers["content-disposition"]
    assert disposition == "attachment; filename=\"a_b_.png\"; filename*=UTF-8''a%20b%C3%A9%22.png"
    missing = await _call("GET", "/files/SITE_FILES/object", params={"key": "nope"})
    assert missing.status_code == 404


# --------------------------------------------------------------------------- files: size


@pytest.mark.asyncio
async def test_upload_over_the_cap_is_413(cf, monkeypatch):
    monkeypatch.setattr(files_kv, "MAX_FILE_BYTES", 10)
    over = await _call(
        "PUT", "/files/SITE_FILES/object", params={"key": "a.bin"}, content=b"x" * 11
    )
    assert over.status_code == 413 and _code(over) == "sites.data_file_too_large"
    at = await _call("PUT", "/files/SITE_FILES/object", params={"key": "a.bin"}, content=b"x" * 10)
    assert at.status_code == 200
    assert [c for c in cf.calls if c[0] == "r2_put"] == [("r2_put", "bkt-1")]


@pytest.mark.asyncio
async def test_upload_streamed_past_the_cap_is_413(cf, monkeypatch):
    monkeypatch.setattr(files_kv, "MAX_FILE_BYTES", 10)

    async def _stream():
        for _ in range(5):
            yield b"xxx"

    with _pocket(), pytest.raises(CloudError) as exc:
        await data_service.upload_file(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            binding="SITE_FILES",
            key="a.bin",
            content_type=None,
            body=_stream(),
        )
    assert exc.value.code == "sites.data_file_too_large" and exc.value.status_code == 413
    assert "a.bin" not in cf.r2["bkt-1"]


def test_the_file_cap_and_the_body_ceiling_agree():
    from pocketpaw.security import body_limit

    assert files_kv.MAX_FILE_BYTES == 50 * 1024 * 1024
    assert files_kv.MAX_FILE_BYTES < body_limit._SITE_DATA_FILE_BYTES <= 52 * 1024 * 1024
    assert body_limit._SITE_DATA_FILE_PATH_RE.match(
        "/api/v1/sites/by-pocket/pk1/data/files/SITE_FILES/object"
    )


# --------------------------------------------------------------------------- files: move, delete


@pytest.mark.asyncio
async def test_move_refuses_to_overwrite(cf, audit):
    cf.r2["bkt-1"].update({"a.txt": (b"A", "text/plain"), "b.txt": (b"B", "text/plain")})
    resp = await _call(
        "POST", "/files/SITE_FILES/object:move", json={"from": "a.txt", "to": "b.txt"}
    )
    assert resp.status_code == 409 and _code(resp) == "sites.data_file_exists"
    assert cf.r2["bkt-1"]["b.txt"][0] == b"B" and "a.txt" in cf.r2["bkt-1"]
    forced = await _call(
        "POST",
        "/files/SITE_FILES/object:move",
        json={"from": "a.txt", "to": "b.txt", "overwrite": True},
    )
    assert forced.status_code == 200, forced.text
    assert cf.r2["bkt-1"] == {"b.txt": (b"A", "text/plain")}


@pytest.mark.asyncio
async def test_move_renames_and_retypes_by_the_new_name(cf, audit):
    cf.r2["bkt-1"]["notes.txt"] = (b"<b>hi</b>", "text/plain")
    resp = await _call(
        "POST", "/files/SITE_FILES/object:move", json={"from": "notes.txt", "to": "dir/notes.html"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["object"]["key"] == "dir/notes.html"
    assert resp.json()["object"]["content_type"] == "application/octet-stream"
    assert cf.r2["bkt-1"] == {"dir/notes.html": (b"<b>hi</b>", "application/octet-stream")}
    ev = [e for e in audit if e["action"] == "site_data.file_move"][0]
    assert ev["metadata"]["from"] == "notes.txt" and ev["metadata"]["to"] == "dir/notes.html"
    missing = await _call("POST", "/files/SITE_FILES/object:move", json={"from": "x", "to": "y"})
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_move_over_the_cap_is_413_and_leaves_the_file(cf, monkeypatch):
    monkeypatch.setattr(files_kv, "MAX_FILE_BYTES", 3)
    cf.r2["bkt-1"]["big.bin"] = (b"xxxx", "application/octet-stream")
    resp = await _call("POST", "/files/SITE_FILES/object:move", json={"from": "big.bin", "to": "b"})
    assert resp.status_code == 413
    assert "big.bin" in cf.r2["bkt-1"] and "b" not in cf.r2["bkt-1"]


@pytest.mark.asyncio
async def test_delete_file(cf, audit):
    cf.r2["bkt-1"]["a.txt"] = (b"A", "text/plain")
    resp = await _call("DELETE", "/files/SITE_FILES/object", params={"key": "a.txt"})
    assert resp.status_code == 204 and cf.r2["bkt-1"] == {}
    assert audit[-1]["action"] == "site_data.file_delete"
    assert audit[-1]["metadata"] == {"binding": "SITE_FILES", "key": "a.txt"}


# --------------------------------------------------------------------------- permissions


_WRITES = [
    ("PUT", "/files/SITE_FILES/object", {"params": {"key": "a"}, "content": b"x"}),
    ("POST", "/files/SITE_FILES/object:move", {"json": {"from": "a", "to": "b"}}),
    ("DELETE", "/files/SITE_FILES/object", {"params": {"key": "a"}}),
    ("PUT", "/kv/CACHE/keys/a", {"json": {"value": "x"}}),
    ("DELETE", "/kv/CACHE/keys/a", {}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kw"), _WRITES)
async def test_member_cannot_write(cf, method, path, kw):
    resp = await _call(method, path, role="member", **kw)
    assert resp.status_code == 403 and _code(resp) == "sites.data_write_forbidden"
    assert cf.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kw"), _WRITES)
async def test_read_only_pocket_cannot_write(cf, method, path, kw):
    resp = await _call(method, path, can_edit=False, **kw)
    assert resp.status_code == 403 and _code(resp) == "pocket.access_denied"
    assert cf.calls == []


# --------------------------------------------------------------------------- KV


@pytest.mark.asyncio
async def test_kv_list_and_masking(cf):
    cf.kv["ns-1"].update(
        {
            "greeting": (b"hello", {"lang": "en"}, None),
            "stripe_api_key": (b"sk_live_123", {"hint": "sk_live"}, None),
            "session:abc": (b"{}", None, 1767225600),
        }
    )
    resp = await _call("GET", "/kv/CACHE/keys")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "pocket_id": "pk1",
        "binding": "CACHE",
        "available": True,
        "reason": "",
        "prefix": "",
        "keys": [
            {"name": "greeting", "expiration": None, "metadata": {"lang": "en"}, "masked": False},
            {"name": "session:abc", "expiration": 1767225600, "metadata": None, "masked": True},
            {"name": "stripe_api_key", "expiration": None, "metadata": None, "masked": True},
        ],
        "cursor": None,
    }
    secret = await _call("GET", "/kv/CACHE/keys/stripe_api_key")
    body = secret.json()
    assert body["value"] == files_kv.MASK and body["value_base64"] is None
    assert body["masked"] is True and body["editable"] is False and body["metadata"] is None
    assert body["size"] is None  # a size hints at the secret's shape
    assert "sk_live" not in secret.text
    refused = await _call("PUT", "/kv/CACHE/keys/stripe_api_key", json={"value": "x"})
    assert refused.status_code == 403 and _code(refused) == "sites.data_masked_key"
    assert cf.kv["ns-1"]["stripe_api_key"][0] == b"sk_live_123"


@pytest.mark.asyncio
async def test_kv_text_json_binary_and_large_values(cf, monkeypatch):
    cf.kv["ns-1"].update(
        {
            "a/b": (b'{"plan": "pro"}', {"v": 1}, 1767225600),
            "blob": (b"\xff\xd8\xff\x00", None, None),
            "big": (b"x" * 20, None, None),
        }
    )
    text = await _call("GET", "/kv/CACHE/keys/a%2Fb")
    assert text.status_code == 200, text.text
    assert text.json() == {
        "binding": "CACHE",
        "name": "a/b",
        "value": '{"plan": "pro"}',
        "value_base64": None,
        "size": 15,
        "is_json": True,
        "metadata": {"v": 1},
        "expiration": 1767225600,
        "masked": False,
        "too_large": False,
        "editable": True,
    }
    blob = await _call("GET", "/kv/CACHE/keys/blob")
    assert blob.json()["value"] is None
    assert base64.b64decode(blob.json()["value_base64"]) == b"\xff\xd8\xff\x00"
    assert blob.json()["editable"] is False
    monkeypatch.setattr(files_kv, "MAX_KV_EDIT_BYTES", 10)
    big = await _call("GET", "/kv/CACHE/keys/big")
    assert big.json()["too_large"] is True and big.json()["size"] == 20
    assert big.json()["value"] is None and big.json()["value_base64"] is None
    missing = await _call("GET", "/kv/CACHE/keys/nope")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_kv_put_and_delete(cf, audit):
    resp = await _call(
        "PUT",
        "/kv/CACHE/keys/cfg%2Fsite",
        json={"value": "on", "metadata": {"by": "owner"}, "expiration_ttl": 3600},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["propagation_note"] is True and out["size"] == 2
    assert out["key"]["name"] == "cfg/site" and out["key"]["metadata"] == {"by": "owner"}
    assert isinstance(out["key"]["expiration"], int)
    assert cf.kv["ns-1"]["cfg/site"][:2] == (b"on", {"by": "owner"})
    ev = audit[-1]
    assert ev["action"] == "site_data.kv_put" and ev["target_id"] == "pk1:CACHE:cfg/site"
    assert ev["metadata"] == {
        "binding": "CACHE",
        "key": "cfg/site",
        "size": 2,
        "expiration_ttl": 3600,
        "has_metadata": True,
    }
    assert "owner" not in repr(audit) and "'on'" not in repr(audit)
    gone = await _call("DELETE", "/kv/CACHE/keys/cfg%2Fsite")
    assert gone.status_code == 204 and "cfg/site" not in cf.kv["ns-1"]
    assert audit[-1]["action"] == "site_data.kv_delete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"value": "x", "expiration_ttl": 59}, "sites.data_bad_kv_ttl"),
        ({"value": "x", "expiration_ttl": 0}, "sites.data_bad_kv_ttl"),
        ({"value": "x", "metadata": {"k": "v" * 1100}}, "sites.data_bad_kv_metadata"),
        ({"value": "x", "extra": 1}, None),
    ],
)
async def test_kv_put_limits(cf, body, code):
    resp = await _call("PUT", "/kv/CACHE/keys/k", json=body)
    assert resp.status_code == 422, resp.text
    if code:
        assert _code(resp) == code
    assert ("kv_put", "ns-1") not in cf.calls


@pytest.mark.asyncio
async def test_kv_put_value_over_1_mb_is_413(cf):
    resp = await _call("PUT", "/kv/CACHE/keys/k", json={"value": "x" * (1024 * 1024 + 1)})
    assert resp.status_code == 413 and _code(resp) == "sites.data_kv_value_too_large"
    assert cf.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [".", "..", "k" * 513, "a/..", "../x", "a/./b"])
async def test_kv_bad_keys(cf, key):
    resp = await _call("PUT", "/kv/CACHE/keys/" + key.replace(".", "%2E"), json={"value": "x"})
    assert resp.status_code in (404, 422), resp.text
    with _pocket(), pytest.raises(CloudError) as exc:
        await data_service.get_kv_key(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", binding="CACHE", key=key
        )
    assert exc.value.code == "sites.data_bad_kv_key"
    assert cf.calls == []


def test_every_files_kv_route_is_guarded():
    from pocketpaw_ee.sites.data_router import router

    seen = 0
    for route in router.routes:
        if "/files/" not in route.path and "/kv/" not in route.path:
            continue
        seen += 1
        guards = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        assert "require_action_fabric_read" in guards, route.path
        writes = bool(getattr(route, "methods", set()) & {"POST", "PUT", "DELETE"})
        # Reading KV (key names hold session tokens) or a file's contents is a write-level
        # read; only the file LISTING stays on fabric.read.
        admin_read = route.path.endswith(("/kv/{binding}/keys", "/keys/{key:path}", "/object"))
        assert ("require_action_sites_data_write" in guards) == (writes or admin_read), route.path
        if not (writes or admin_read):
            assert route.path.endswith("/files/{binding}"), route.path
    assert seen == 9


_ADMIN_READS = [
    ("GET", "/kv/CACHE/keys", {}),
    ("GET", "/kv/CACHE/keys/greeting", {}),
    ("GET", "/files/SITE_FILES/object", {"params": {"key": "a.txt"}}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "kw"), _ADMIN_READS)
async def test_member_cannot_read_kv_or_download(cf, method, path, kw):
    cf.kv["ns-1"]["greeting"] = (b"hi", None, None)
    cf.r2["bkt-1"]["a.txt"] = (b"A", "text/plain")
    resp = await _call(method, path, role="member", **kw)
    assert resp.status_code == 403 and _code(resp) == "sites.data_write_forbidden"
    denied = await _call(method, path, can_edit=False, **kw)
    assert denied.status_code == 403 and _code(denied) == "pocket.access_denied"
    assert cf.calls == []
    ok = await _call(method, path, **kw)
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_member_can_still_list_files(cf):
    cf.r2["bkt-1"]["a.txt"] = (b"A", "text/plain")
    resp = await _call("GET", "/files/SITE_FILES", role="member", can_edit=False)
    assert resp.status_code == 200 and resp.json()["objects"][0]["key"] == "a.txt"


@pytest.mark.parametrize(
    "name",
    [
        "stripe_api_key",
        "password",
        "credentials",
        "credential:x",
        "aws_access_key",
        "accessKey",
        "session:9f8e7d",
        "sessions/abc",
        "better-auth:session:tok",
        "jwt_cache",
        "cookie:abc",
        "passphrase",
        "auth:abc",
        "oauth_state",
        "refresh_token",
    ],
)
def test_kv_names_that_look_like_credentials_are_masked(name):
    assert files_kv.masked_kv_key(name) is True


@pytest.mark.parametrize("name", ["greeting", "cfg/site", "page_views", "cache:home", "a/b"])
def test_ordinary_kv_names_are_not_masked(name):
    assert files_kv.masked_kv_key(name) is False


@pytest.mark.asyncio
async def test_kv_audit_hashes_token_like_or_long_names(cf, audit):
    cf.kv["ns-1"]["session:tok123"] = (b"{}", None, None)
    gone = await _call("DELETE", "/kv/CACHE/keys/session%3Atok123")
    assert gone.status_code == 204
    long_name = "k" * 65
    put = await _call("PUT", "/kv/CACHE/keys/" + long_name, json={"value": "x"})
    assert put.status_code == 200, put.text
    ok = await _call("PUT", "/kv/CACHE/keys/cfg", json={"value": "x"})
    assert ok.status_code == 200
    deleted, long_put, plain = audit[-3:]
    for event, name in ((deleted, "session:tok123"), (long_put, long_name)):
        hidden = event["metadata"]["key"]
        assert hidden.startswith("sha256:") and len(hidden) == len("sha256:") + 16
        assert event["target_id"] == f"pk1:CACHE:{hidden}"
        assert name not in repr(event)
    assert plain["metadata"]["key"] == "cfg" and plain["target_id"] == "pk1:CACHE:cfg"


# --------------------------------------------------------------------------- budget


@pytest.mark.asyncio
async def test_r2_calls_spend_the_r2_bucket(cf, monkeypatch):
    monkeypatch.setattr(budget, "R2_LIMIT", (1, 300))
    first = await _call("GET", "/files/SITE_FILES")
    assert first.status_code == 200
    second = await _call("GET", "/files/SITE_FILES")
    assert second.status_code == 429 and _code(second) == "sites.data_busy"
    kv = await _call("GET", "/kv/CACHE/keys")
    assert kv.status_code == 200  # KV doesn't spend the R2 bucket


# --------------------------------------------------------------------------- inventory


@pytest.mark.asyncio
async def test_inventory_store_hints_and_dashboard_urls(cf, site_doc):
    site_doc.d1_database_id = ""  # no database: the inventory skips introspection
    views = [
        SimpleNamespace(
            provider="supabase",
            status="connected",
            settings={"project_ref": "abcdefghijklmnopqrst"},
        ),
        SimpleNamespace(
            provider="airtable", status="connected", settings={"base_id": "appABCDEFGHIJKLMN"}
        ),
        SimpleNamespace(
            provider="google_sheets",
            status="connected",
            settings={
                "spreadsheet_url": "https://docs.google.com/spreadsheets/d/" + "A" * 44 + "/edit"
            },
        ),
        SimpleNamespace(
            provider="notion",
            status="connected",
            settings={"database_id": "1F2E3D4C-5B6A-7980-1A2B-3C4D5E6F7A8B"},
        ),
        SimpleNamespace(provider="stripe", status="connected", settings={}),
    ]
    with _pocket(), patch(_PATCH_CONNECTORS, new=AsyncMock(return_value=views)):
        inv = await data_service.get_inventory(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", can_write=True
        )
    assert [(s.binding, s.writable, s.readable) for s in inv.files] == [("SITE_FILES", True, True)]
    assert [(s.binding, s.writable, s.readable) for s in inv.kv] == [("CACHE", True, True)]
    assert {c.provider: c.dashboard_url for c in inv.connectors} == {
        "supabase": "https://supabase.com/dashboard/project/abcdefghijklmnopqrst",
        "airtable": "https://airtable.com/appABCDEFGHIJKLMN",
        "google_sheets": "https://docs.google.com/spreadsheets/d/" + "A" * 44,
        "notion": "https://www.notion.so/1f2e3d4c5b6a79801a2b3c4d5e6f7a8b",
        "stripe": None,
    }
    with _pocket(), patch(_PATCH_CONNECTORS, new=AsyncMock(return_value=[])):
        reader = await data_service.get_inventory(
            workspace_id="ws1", user_id="u1", pocket_id="pk1", can_write=False
        )
    assert [s.writable for s in reader.files + reader.kv] == [False, False]
    # A reader sees the stores but can't open KV ("Ask an admin"); file listing still works.
    assert [s.readable for s in reader.files] == [True]
    assert [s.readable for s in reader.kv] == [False]


@pytest.mark.parametrize(
    ("provider", "settings"),
    [
        ("supabase", {"project_ref": "evil.com/x"}),
        ("supabase", {"project_ref": "abc"}),
        ("supabase", {"project_ref": "ABCDEFGHIJKLMNOPQRST"}),
        ("supabase", {"project_ref": "abcdefghijklmnopqrst@evil.com"}),
        ("airtable", {"base_id": "appABC"}),
        ("airtable", {"base_id": "https://evil.com"}),
        ("airtable", {"base_id": "appABCDEFGHIJKLMN/../x"}),
        (
            "google_sheets",
            {"spreadsheet_url": "https://evil.com/spreadsheets/d/" + "A" * 44 + "/edit"},
        ),
        (
            "google_sheets",
            {
                "spreadsheet_url": "https://docs.google.com.evil.com/spreadsheets/d/"
                + "A" * 44
                + "/edit"
            },
        ),
        (
            "google_sheets",
            {
                "spreadsheet_url": "https://evil.com/?u=https://docs.google.com/spreadsheets/d/"
                + "A" * 44
                + "/edit"
            },
        ),
        ("google_sheets", {"spreadsheet_id": "short"}),
        ("google_sheets", {"spreadsheet_id": "A" * 30 + "?token=x"}),
        ("notion", {"database_id": "not-an-id"}),
        ("notion", {"database_id": "1f2e3d4c5b6a79801a2b3c4d5e6f7a8b?x=1"}),
        ("notion", {}),
    ],
)
def test_dashboard_url_refuses_bad_ids(provider, settings):
    assert data_service._dashboard_url(provider, settings) is None


def test_dashboard_url_unknown_provider_is_none():
    assert data_service._dashboard_url("nope", {"base_id": "appABCDEFGHIJKLMN"}) is None
