# tests/ee/sites/test_draft_preview_proxy.py: the preview origin reverse-proxies a
# full-mode project draft to its account-level draft Worker (design:
# docs/design/drafts/2026-10-08-sites-draft-worker.md, workspace root).
#
# The upstream is an httpx MockTransport; the token store and the draft registry are
# in memory. What must hold: only the token's own deployed draft is proxied, every
# method reaches it, cookies pass through host-only, the workers.dev host never leaks,
# HTML gets the runtime reporter (and the edit bridge under ``?paw_edit=1``), and
# static drafts keep their cookieless GET/HEAD-only contract.
from __future__ import annotations

import gzip
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.sites import draft_worker, preview_origin, preview_proxy
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.test_draft_worker import POCKET, Store

PREVIEW_BASE = "https://preview.paw.test"
TOKEN = "a" * 32
OLD_TOKEN = "b" * 32
STATIC_TOKEN = "c" * 32
HOST = f"{TOKEN}.preview.paw.test"
UPSTREAM = "paw-draft-x-0123456789abcdef.acct.workers.dev"
BUILDER = "https://dash.paw.example"


class _Stream(httpx.AsyncByteStream):
    """A real streamed body: a MockTransport response built from bytes is already
    read, unlike anything a network upstream returns."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):
        for i in range(0, len(self.data), 7):
            yield self.data[i : i + 7]


def _resp(status: int, body: bytes = b"", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, stream=_Stream(body))


class Upstream:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.respond = self.default

    def default(self, request: httpx.Request) -> httpx.Response:
        return _resp(200, b"ok", {"content-type": "text/plain"})

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(await request.aread())
        self.requests.append(request)
        return self.respond(request)


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", PREVIEW_BASE)
    monkeypatch.setenv("PAW_SITES_DRAFT_WORKERS", "1")
    preview_origin._clear_caches()
    draft_worker._reset_caches()
    store = Store()
    store.tokens[(POCKET, "h1")] = TOKEN
    store.tokens[(POCKET, "h0")] = OLD_TOKEN
    store.dist[(POCKET, "h0")] = preview_origin.pack_files({"index.html": b"<h1>old</h1>"})
    store.tokens[("static-pocket", "s1")] = STATIC_TOKEN
    store.dist[("static-pocket", "s1")] = preview_origin.pack_files(
        {"index.html": b"<html><head></head><body>static</body></html>"}
    )
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: store)

    registry = draft_worker.MemoryRegistry()
    registry.rows[POCKET] = draft_worker.DraftRecord(
        pocket_id=POCKET, workspace="ws1", script="paw-draft-x", host=UPSTREAM, deployed_hash="h1"
    )
    monkeypatch.setattr(draft_worker, "default_registry", lambda: registry)
    monkeypatch.setattr(preview_proxy, "builder_origin_for", lambda pocket_id: BUILDER)

    up = Upstream()
    preview_proxy.set_transport(httpx.MockTransport(up.handler))
    up.registry = registry  # type: ignore[attr-defined]
    yield up
    preview_proxy.set_transport(None)
    preview_origin._clear_caches()
    draft_worker._reset_caches()


async def _call(method: str, url: str, **kw: Any) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=preview_origin.preview_app)) as client:
        return await client.request(method, url, **kw)


async def test_post_reaches_the_draft_worker_with_its_body_and_cookies(upstream):
    upstream.respond = lambda r: _resp(
        201,
        b'{"ok":true}',
        {
            "content-type": "application/json",
            "set-cookie": "s=1; Domain=preview.paw.test; Path=/; HttpOnly; SameSite=Lax",
        },
    )
    resp = await _call(
        "POST",
        f"https://{HOST}/api/auth/sign-up/email?x=1",
        content=b'{"email":"a@b.c"}',
        headers={
            "cookie": "s=0",
            "content-type": "application/json",
            "cf-connecting-ip": "1.2.3.4",
        },
    )
    assert resp.status_code == 201
    (req,) = upstream.requests
    assert req.url.host == UPSTREAM and req.url.path == "/api/auth/sign-up/email"
    assert req.url.query == b"x=1"
    assert req.method == "POST" and upstream.bodies == [b'{"email":"a@b.c"}']
    assert req.headers["cookie"] == "s=0"
    assert req.headers["x-forwarded-host"] == HOST
    assert req.headers["x-forwarded-proto"] == "https"
    assert "cf-connecting-ip" not in req.headers
    cookie = resp.headers["set-cookie"]
    assert "domain" not in cookie.lower()
    assert "Secure" in cookie and "SameSite=None" in cookie and "Partitioned" in cookie
    assert "HttpOnly" in cookie
    assert resp.headers.get("access-control-allow-origin") is None
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"


async def test_options_is_forwarded_for_a_proxied_draft(upstream):
    upstream.respond = lambda r: _resp(204, b"", {"access-control-allow-methods": "POST"})
    resp = await _call("OPTIONS", f"https://{HOST}/api/x")
    assert resp.status_code == 204 and upstream.requests[0].method == "OPTIONS"
    assert resp.headers["access-control-allow-methods"] == "POST"


async def test_redirects_to_the_workers_dev_host_are_rewritten(upstream):
    upstream.respond = lambda r: _resp(302, b"", {"location": f"https://{UPSTREAM}/login?next=/"})
    resp = await _call("GET", f"https://{HOST}/account")
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://{HOST}/login?next=/"
    assert UPSTREAM not in resp.text


async def test_html_gets_the_runtime_reporter_in_head(upstream):
    page = "<!doctype html><html><head><title>x</title></head><body><h1>SSR</h1></body></html>"
    upstream.respond = lambda r: _resp(
        200,
        gzip.compress(page.encode()),
        {"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"},
    )
    resp = await _call("GET", f"https://{HOST}/")
    assert resp.status_code == 200
    html = resp.text
    assert html.index('id="paw-runtime-reporter"') < html.index("<title>")
    assert 'id="paw-edit-bridge"' not in html
    assert "<h1>SSR</h1>" in html
    assert "content-encoding" not in resp.headers


async def test_paw_edit_adds_the_bridge_and_is_not_forwarded(upstream):
    page = "<html><head></head><body><h1>SSR</h1></body></html>"
    upstream.respond = lambda r: _resp(200, page.encode(), {"content-type": "text/html"})
    resp = await _call("GET", f"https://{HOST}/about?paw_edit=1&tab=2")
    assert 'id="paw-edit-bridge"' in resp.text
    assert BUILDER in resp.text
    assert upstream.requests[0].url.query == b"tab=2"


async def test_non_html_streams_through_with_its_encoding(upstream):
    body = gzip.compress(b"console.log(1)")
    upstream.respond = lambda r: _resp(
        200, body, {"content-type": "application/javascript", "content-encoding": "gzip"}
    )
    async with AsyncClient(transport=ASGITransport(app=preview_origin.preview_app)) as client:
        resp = await client.get(f"https://{HOST}/assets/app.js")
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.content == b"console.log(1)"  # httpx decodes it client-side


async def test_a_superseded_token_is_a_404_without_touching_upstream(upstream):
    resp = await _call("GET", f"https://{OLD_TOKEN}.preview.paw.test/")
    assert resp.status_code == 404
    assert upstream.requests == []


async def test_a_draft_being_deleted_is_never_proxied(upstream):
    upstream.registry.rows[POCKET].state = "deleting"
    resp = await _call("GET", f"https://{HOST}/")
    assert resp.status_code == 404 and upstream.requests == []


async def test_static_drafts_stay_cookieless_and_get_only(upstream):
    url = f"https://{STATIC_TOKEN}.preview.paw.test/"
    resp = await _call("GET", url, headers={"cookie": "s=1"})
    assert resp.status_code == 200 and "static" in resp.text
    assert "set-cookie" not in resp.headers
    assert resp.headers["access-control-allow-origin"] == "*"
    assert (await _call("POST", url)).status_code == 405
    assert upstream.requests == []


async def test_flag_off_never_proxies(upstream, monkeypatch):
    monkeypatch.delenv("PAW_SITES_DRAFT_WORKERS")
    resp = await _call("POST", f"https://{HOST}/api/x")
    assert resp.status_code == 405 and upstream.requests == []


async def test_the_request_cannot_choose_the_upstream(upstream):
    await _call("GET", f"https://{HOST}/x", headers={"x-forwarded-host": "evil.test"})
    (req,) = upstream.requests
    assert req.url.host == UPSTREAM
    assert req.headers["x-forwarded-host"] == HOST


async def test_an_oversized_body_is_refused(upstream, monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_MAX_BODY", "10")
    resp = await _call("POST", f"https://{HOST}/api/x", content=b"x" * 64)
    assert resp.status_code == 413


async def test_an_unreachable_worker_is_a_502(upstream):
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    upstream.respond = boom
    resp = await _call("GET", f"https://{HOST}/")
    assert resp.status_code == 502
    assert UPSTREAM not in resp.text
