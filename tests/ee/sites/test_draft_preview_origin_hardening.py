# tests/ee/sites/test_draft_preview_origin_hardening.py — the draft preview origin
# under the conditions a security review raised: where it sits in the REAL app
# factories' middleware stack, what happens when the artifact store fails or refuses
# a write, how it holds up against cache-busting and encoded-traversal requests, a
# misconfigured preview base URL, and which responses are cacheable.
#
# The happy-path contract lives in test_draft_preview_origin.py; this file reuses its
# pocket/build helpers so both seed drafts exactly as the preview lane does.

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import urljoin, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.sites import preview_origin as po
from pocketpaw_ee.sites import service as sites_service

from tests.ee.sites.test_draft_preview_origin import (
    _REACT_DIST,
    _REACT_SOURCE,
    PREVIEW_BASE,
    _artifact,
    _land_build,
    _make_pocket,
)

PREVIEW_HOST = urlsplit(PREVIEW_BASE).hostname


@pytest.fixture(autouse=True)
def _preview_env(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", PREVIEW_BASE)
    for env in po._APP_URL_ENVS:
        monkeypatch.delenv(env, raising=False)
    po._clear_caches()
    sites_service._preview_retry_at.clear()
    yield
    po._clear_caches()
    sites_service._preview_retry_at.clear()


def _store():
    return sites_service._default_artifact_store()


def _seed(files: dict[str, bytes], pocket: str = "p1", content_hash: str = "h1") -> str:
    url = po.publish_draft(_store(), pocket, content_hash, po.pack_files(files))
    assert url is not None
    return url


async def _get(url: str, **kw: Any):
    async with AsyncClient(transport=ASGITransport(app=po.preview_app)) as client:
        return await client.get(url, **kw)


class _CountingPool:
    def __init__(self) -> None:
        self.calls = 0

    async def enqueue_job(self, *a: Any, **kw: Any) -> object:
        self.calls += 1
        return object()


# ---------------------------------------------------------------------------
# 1. The dispatch is the OUTERMOST layer of the real app factories
# ---------------------------------------------------------------------------


def _api_app():
    from pocketpaw.api.serve import create_api_app

    return create_api_app()


def test_preview_dispatch_is_outermost_in_the_serve_app():
    order = [m.cls for m in _api_app().user_middleware]
    assert order[0] is po.PreviewHostDispatch, order


def test_preview_dispatch_is_outermost_in_the_dashboard_app():
    from pocketpaw.dashboard import app

    order = [m.cls for m in app.user_middleware]
    assert order[0] is po.PreviewHostDispatch, order


def test_a_remote_unauthenticated_preview_request_reaches_the_draft():
    """No cookie, no token, a non-loopback client (TestClient's peer is
    ``testclient``): auth, rate limits and CORS must not see this request."""
    from fastapi.testclient import TestClient

    url = _seed({"index.html": b"<h1>draft</h1>"})
    parts = urlsplit(url)
    client = TestClient(_api_app(), base_url=f"{parts.scheme}://{parts.netloc}")

    resp = client.get("/index.html", headers={"Origin": "https://dash.paw.example"})

    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    assert resp.text == "<h1>draft</h1>"
    assert resp.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in resp.headers
    assert "set-cookie" not in resp.headers


# ---------------------------------------------------------------------------
# 2. No URL for a draft the store did not keep; no rebuild loop either
# ---------------------------------------------------------------------------


def test_no_url_when_the_files_write_fails(monkeypatch):
    store = _store()
    monkeypatch.setattr(type(store), "write_dist", lambda self, *a: False)

    assert po.publish_draft(store, "p1", "h1", po.pack_files({"index.html": b"x"})) is None
    assert store.read_preview_token("p1", "h1") is None  # no token minted for nothing


def test_no_url_when_the_token_write_fails(monkeypatch):
    store = _store()
    monkeypatch.setattr(type(store), "write_preview_token", lambda self, *a: False)

    assert po.publish_draft(store, "p1", "h1", po.pack_files({"index.html": b"x"})) is None


def test_a_failed_filesystem_write_reports_false(monkeypatch):
    store = _store()

    def _boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(type(store), "_atomic_write", staticmethod(_boom))

    assert store.write_dist("p1", "h1", b"x") is False
    assert store.write_preview_token("p1", "h1", "a" * 32) is False


@pytest.mark.asyncio
async def test_a_failed_token_write_is_repaired_without_a_rebuild(beanie_test_db, monkeypatch):
    store = _store()
    pocket_id = await _make_pocket("react", dict(_REACT_SOURCE))
    real = type(store).write_preview_token
    monkeypatch.setattr(type(store), "write_preview_token", lambda self, *a: False)
    await _land_build(pocket_id, "react", _REACT_DIST, "dist")
    monkeypatch.setattr(type(store), "write_preview_token", real)

    pool = _CountingPool()
    result = await _artifact(pocket_id, pool=pool)

    assert pool.calls == 0, "the files were stored; only the token was missing"
    assert result["build_status"] == "none"
    assert result["body_html"]
    assert (await _get(result["preview_url"])).status_code == 200


@pytest.mark.asyncio
async def test_a_refused_draft_serves_its_render_instead_of_rebuilding_forever(
    beanie_test_db, monkeypatch
):
    store = _store()
    monkeypatch.setattr(type(store), "write_dist", lambda self, *a: False)
    pocket_id = await _make_pocket("react", dict(_REACT_SOURCE))
    await _land_build(pocket_id, "react", _REACT_DIST, "dist")
    pool = _CountingPool()

    first = await _artifact(pocket_id, pool=pool)
    await _land_build(pocket_id, "react", _REACT_DIST, "dist")  # the one rebuild lands
    views = [await _artifact(pocket_id, pool=pool) for _ in range(3)]

    assert first["build_status"] == "queued" and pool.calls == 1
    for view in views:
        assert view["build_status"] == "none"
        assert view["body_html"]
        assert view["preview_url"] is None
    assert pool.calls == 1, "every view queued a sandbox for files the store refuses"


@pytest.mark.asyncio
async def test_an_html_draft_the_store_refuses_is_not_rematerialized_per_view(
    beanie_test_db, monkeypatch
):
    store = _store()
    monkeypatch.setattr(type(store), "write_dist", lambda self, *a: False)
    calls = 0
    real = po.materialize_html_draft

    async def _counting(*a: Any, **kw: Any):
        nonlocal calls
        calls += 1
        return await real(*a, **kw)

    monkeypatch.setattr(po, "materialize_html_draft", _counting)
    pocket_id = await _make_pocket("html", {"index.html": "<h1>hi</h1>"})

    results = [await _artifact(pocket_id) for _ in range(3)]

    assert [r["preview_url"] for r in results] == [None, None, None]
    assert calls == 1


# ---------------------------------------------------------------------------
# 3. Per-request store traffic: token cache, negative cache, single flight
# ---------------------------------------------------------------------------


class _CountingStore:
    """Wraps the real store and counts the reads a preview request costs."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.resolves = 0
        self.dist_reads = 0
        self.gate: threading.Event | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def resolve_preview_token(self, token: str):
        self.resolves += 1
        return self._inner.resolve_preview_token(token)

    def read_dist(self, pocket_id: str, content_hash: str):
        self.dist_reads += 1
        if self.gate is not None:
            self.gate.wait(5)
        return self._inner.read_dist(pocket_id, content_hash)


@pytest.fixture
def counting_store(monkeypatch):
    store = _CountingStore(_store())
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_asset_requests_resolve_the_token_once(counting_store):
    url = _seed({"index.html": b"<h1>x</h1>", "assets/a.js": b"1", "assets/b.css": b"2"})

    for rel in ("/index.html", "/assets/a.js", "/assets/b.css", "/assets/a.js"):
        assert (await _get(urljoin(url, rel))).status_code == 200

    assert counting_store.resolves == 1
    assert counting_store.dist_reads == 1


@pytest.mark.asyncio
async def test_an_unknown_token_is_remembered_as_a_miss(counting_store):
    url = _seed({"index.html": b"x"})
    token = urlsplit(url).hostname.split(".", 1)[0]
    bogus = url.replace(token, "0" * 32)

    for _ in range(5):
        assert (await _get(bogus)).status_code == 404

    assert counting_store.resolves == 1


def test_concurrent_loads_of_one_draft_read_the_store_once(counting_store):
    url = _seed({"index.html": b"x"})
    token = urlsplit(url).hostname.split(".", 1)[0]
    counting_store.gate = threading.Event()
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(po._load_draft(token))) for _ in range(6)
    ]
    for t in threads:
        t.start()
    counting_store.gate.set()
    for t in threads:
        t.join(5)

    assert len(results) == 6 and all(r and r["index.html"] == b"x" for r in results)
    assert counting_store.dist_reads == 1
    assert po._inflight == {}


def test_every_servable_draft_fits_the_cache():
    assert po.MAX_TOTAL_BYTES <= po._CACHE_BYTES


# ---------------------------------------------------------------------------
# 5. A refused preview base turns previews off instead of hijacking the API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base",
    ["preview.paw-sites.test", "ftp://preview.paw-sites.test", "https://localhost", "https://"],
)
def test_a_malformed_base_is_refused(monkeypatch, base):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", base)

    assert po.preview_base_problem() is not None
    assert po.preview_url_for("a" * 32) is None
    assert po.publish_draft(_store(), "p1", "h1", po.pack_files({"index.html": b"x"})) is None


@pytest.mark.parametrize(
    ("base", "env", "app_url", "refused"),
    [
        ("https://paw-sites.test", "POCKETPAW_PUBLIC_BASE_URL", "https://paw-sites.test", True),
        ("https://paw-sites.test", "POCKETPAW_PUBLIC_BASE_URL", "https://api.paw-sites.test", True),
        (PREVIEW_BASE, "POCKETPAW_FRONTEND_BASE_URL", PREVIEW_BASE, True),
        (PREVIEW_BASE, "PAW_SITES_BUILDER_ORIGIN", "https://dash.preview.paw-sites.test", True),
        (PREVIEW_BASE, "PAW_SITES_BUILDER_ORIGIN", "https://dash.paw-sites.test", False),
    ],
)
def test_a_base_that_equals_or_contains_an_app_host_is_refused(
    monkeypatch, base, env, app_url, refused
):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", base)
    monkeypatch.setenv(env, app_url)

    assert (po.preview_base_problem() is not None) is refused
    assert not po.is_preview_host(urlsplit(app_url).hostname)


@pytest.mark.asyncio
async def test_a_refused_base_routes_nothing_to_the_preview_app(monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://paw.example")
    monkeypatch.setenv("POCKETPAW_PUBLIC_BASE_URL", "https://api.paw.example")
    api = FastAPI()

    @api.get("/v1/me")
    async def _me():
        return {"served_by": "api"}

    api.add_middleware(po.PreviewHostDispatch)
    async with AsyncClient(transport=ASGITransport(app=api)) as client:
        resp = await client.get("https://api.paw.example/v1/me")

    assert resp.json() == {"served_by": "api"}


def test_the_startup_check_logs_a_refused_base(monkeypatch, caplog):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "preview.paw-sites.test")

    assert po.check_preview_base() is False
    assert any("DISABLED" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. Only content-addressed files are immutable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "immutable"),
    [
        ("assets/index-BvK3x9_a.js", True),
        ("assets/chunk.3f9a1c2e.css", True),
        ("_app/immutable/entry/start.Ab12.js", True),
        ("assets/logo.png", False),
        ("assets/hero-banner.png", False),
        ("assets/team-pictures.jpg", False),
        ("index.html", False),
    ],
)
def test_cache_control(rel, immutable):
    value = po._cache_control(rel, 200)
    if immutable:
        assert value == "public, max-age=31536000, immutable"
    else:
        assert value == "private, no-cache"


def test_errors_are_never_stored():
    assert po._cache_control("404.html", 404) == "no-store"


# ---------------------------------------------------------------------------
# Encoded traversal never leaves the draft or reaches the edit variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/%2e%2e/etc/passwd",
        "/assets/%2e%2e/%2e%2e/secret",
        "/%252e%252e/secret",
        "/assets%5c..%5csecret",
        "/%5c%5cevil",
        "/%2epaw-edit/index.html",
        "/.paw-edit/index.html",
        "/%2Epaw-edit%2Findex.html",
        "/assets/%00.js",
    ],
)
async def test_encoded_traversal_is_a_plain_404(path):
    url = _seed(
        {
            "index.html": b"<h1>public</h1>",
            f"{po.EDIT_VARIANT_DIR}/index.html": b"<h1>EDIT VARIANT</h1>",
        }
    )
    parts = urlsplit(url)

    resp = await _get(f"{parts.scheme}://{parts.netloc}{path}")

    assert resp.status_code == 404, (path, resp.status_code, resp.text[:80])
    assert b"EDIT VARIANT" not in resp.content
