# tests/ee/sites/test_draft_preview_origin.py — the draft preview moves to a real,
# cookieless preview origin (captain decision 2026-10-07; workspace design draft
# docs/design/drafts/2026-10-07-sites-open-deps-real-preview.md).
#
# Reproduction tests for the pocketpaw <-> paw-enterprise contract:
#
#   * ``GET /api/v1/sites/by-pocket/{id}/native-artifact`` answers, for EVERY engine
#     including html, a ``preview_url``: the absolute URL of the draft's site root on
#     a separate preview host, carrying a capability token. ``None`` while the build is
#     pending.
#   * That URL serves the FULL draft: index.html with its <head> intact (module script),
#     the JS chunks, CSS, images and public/ files. html drafts serve their source-map
#     files with the declared-package import map injected. Every response has
#     ``Access-Control-Allow-Origin: *``, a correct content-type (application/javascript
#     for .js/.mjs) and no Set-Cookie, and needs no auth beyond the token. An unknown
#     token is a 404.
#
# Seams these tests assume (the smallest set the contract needs; adjust in one place
# if the fix lands them under other names):
#
#   * ``PAW_SITES_PREVIEW_BASE_URL`` — the preview host base URL setting;
#   * ``pocketpaw_ee.sites.preview_origin.preview_app`` — the ASGI app the preview host
#     runs. It is requested with the ABSOLUTE ``preview_url`` and no auth overrides. The
#     token lives in the SUBDOMAIN (``https://<token>.<base host>/``), so
#     root-absolute refs resolve exactly as they do once published;
#   * the worker's ``build_job._store_preview_artifact`` + the default artifact store
#     (pointed at tmp by the conftest) — the draft is seeded exactly as the preview
#     lane seeds it when a sandbox build lands.

from __future__ import annotations

import io
import json
import re
import tarfile
from typing import Any
from urllib.parse import urljoin, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import build_job
from pocketpaw_ee.sites import dependency_manifest as dm
from pocketpaw_ee.sites import service as sites_service

PREVIEW_BASE = "https://preview.paw-sites.test"
ORIGIN = "https://dash.paw.example"

_REACT_SOURCE = {
    "src/App.tsx": "export default function App() { return <main><h1>Hi</h1></main>; }\n",
}
_SVELTE_SOURCE = {
    "src/routes/+page.svelte": "<h1>Hi</h1>",
    "src/routes/+page.ts": "export const prerender = true",
}
_REACT_CHUNK = "import{a as b}from'./vendor-1a2b.js';document.body.dataset.hydrated='1';\n"

# What Vite + the paw prerender actually emit: ROOT-ABSOLUTE asset refs in <head>.
_REACT_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <script type="module" crossorigin src="/assets/index-9f8e7d1c.js"></script>
  <link rel="modulepreload" crossorigin href="/assets/vendor-1a2b.js">
  <link rel="stylesheet" crossorigin href="/assets/index-4c5d.css">
  <link rel="icon" href="/favicon.svg">
</head>
<body>
  <div id="root"><main><h1 data-uid="App:h1:0">Hi</h1></main></div>
</body>
</html>
"""
_REACT_DIST = {
    "index.html": _REACT_INDEX,
    "assets/index-9f8e7d1c.js": _REACT_CHUNK,
    "assets/vendor-1a2b.js": "export const a=1;\n",
    "assets/index-4c5d.css": "h1{color:red}\n",
    "assets/hero-77aa.png": b"\x89PNG\r\n\x1a\n" + b"\0" * 32,
    "favicon.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
}
_SVELTE_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <link rel="modulepreload" href="/_app/immutable/entry/start.Ab12.js">
</head>
<body>
  <div style="display: contents"><h1 data-uid="Page:h1:0">Hi</h1>
  <script>import('/_app/immutable/entry/start.Ab12.js').then((m) => m.start());</script>
  </div>
</body>
</html>
"""
_SVELTE_BUILD = {
    "index.html": _SVELTE_INDEX,
    "_app/immutable/entry/start.Ab12.js": "export function start(){}\n",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _preview_base(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", PREVIEW_BASE)


def _targz(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _make_pocket(engine: str, source: dict[str, Any]) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine=engine,
        source=source,
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


async def _content_hash(pocket_id: str, engine: str) -> str:
    from pocketpaw_ee.sites import generator_client

    wire = await pockets_service.get(pocket_id, "u1")
    return sites_service._artifact_content_hash(
        source=wire["source"],
        theme={},
        builder_origin=ORIGIN,
        gen_version=generator_client.generator_version(),
        engine=engine,
        keeps_client_bundle=sites_service._resolve_keeps_client_bundle(wire),
    )


async def _land_build(pocket_id: str, engine: str, files: dict, output_rel: str) -> None:
    """What the preview worker does when the sandbox build lands."""
    build_job._store_preview_artifact(
        _targz(files),
        engine=engine,
        pocket_id=pocket_id,
        content_hash=await _content_hash(pocket_id, engine),
        output_rel=output_rel,
        store=sites_service._default_artifact_store(),
    )


class _NoPool:
    async def enqueue_job(self, *a, **kw):  # pragma: no cover - a warm view must not queue
        raise AssertionError("a landed draft must not queue another build")


async def _artifact(
    pocket_id: str, pool: Any | None = None, arm: Any | None = None
) -> dict[str, Any]:
    return await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin=ORIGIN,
        _pool=pool or _NoPool(),
        _arm=arm,
    )


async def _no_arm(**_kw: Any) -> dict[str, Any]:
    """An API host with no paw-sites CLI: arm-html is unavailable."""
    raise RuntimeError("arm-html failed: bun not found")


def _preview_app():
    try:
        from pocketpaw_ee.sites.preview_origin import preview_app
    except ImportError as exc:  # not a skip: a skipped contract reads green
        pytest.fail(f"no preview-origin app to serve drafts from: {exc}")
    return preview_app


async def _fetch(url: str):
    """GET ``url`` against the preview host with NO auth, NO cookies, NO overrides."""
    async with AsyncClient(transport=ASGITransport(app=_preview_app())) as client:
        return await client.get(url)


def _assert_public_asset(resp, content_type: str) -> None:
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert resp.headers.get("content-type", "").split(";")[0].strip() == content_type
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}


async def _react_draft() -> tuple[str, str]:
    pocket_id = await _make_pocket("react", dict(_REACT_SOURCE))
    await _land_build(pocket_id, "react", _REACT_DIST, "dist")
    result = await _artifact(pocket_id)
    url = result.get("preview_url")
    assert isinstance(url, str) and url, f"no preview_url in {sorted(result)}"
    return pocket_id, url


# ---------------------------------------------------------------------------
# Service: preview_url on every engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_draft_has_an_absolute_preview_url_on_the_preview_host(beanie_test_db):
    pocket_id = await _make_pocket("react", dict(_REACT_SOURCE))
    await _land_build(pocket_id, "react", _REACT_DIST, "dist")

    result = await _artifact(pocket_id)

    url = result.get("preview_url")
    assert isinstance(url, str), f"preview_url missing: {sorted(result)}"
    parts, base = urlsplit(url), urlsplit(PREVIEW_BASE)
    assert parts.scheme == base.scheme
    assert parts.hostname and parts.hostname.endswith(base.hostname)
    assert parts.path == "/"


def test_preview_url_targets_the_site_root():
    """The handed-out URL is the site root, never ``/index.html``: client routers
    (TanStack, React Router) treat ``/index.html`` as an unmatched route."""
    from pocketpaw_ee.sites.preview_origin import preview_url_for

    url = preview_url_for("0123456789abcdef0123456789abcdef")
    assert url is not None
    assert urlsplit(url).path == "/"


@pytest.mark.asyncio
async def test_the_site_root_serves_index_html_and_its_edit_variant(beanie_test_db):
    page = "<!DOCTYPE html><html><head></head><body><h1>hi</h1></body></html>"
    pocket_id = await _make_pocket("html", {"index.html": page})
    url = (await _artifact(pocket_id, arm=_no_arm)).get("preview_url")
    root = urljoin(url, "/")

    plain = await _fetch(root)
    armed = await _fetch(root + "?paw_edit=1")

    _assert_public_asset(plain, "text/html")
    _assert_public_asset(armed, "text/html")
    assert "<h1>hi</h1>" in plain.text
    assert 'id="paw-edit-bridge"' not in plain.text
    assert 'id="paw-edit-bridge"' in armed.text


@pytest.mark.asyncio
async def test_svelte_draft_has_a_preview_url(beanie_test_db):
    pocket_id = await _make_pocket("svelte", dict(_SVELTE_SOURCE))
    await _land_build(pocket_id, "svelte", _SVELTE_BUILD, "build")

    result = await _artifact(pocket_id)

    url = result.get("preview_url")
    assert isinstance(url, str) and urlsplit(url).hostname.endswith("preview.paw-sites.test")


@pytest.mark.asyncio
async def test_html_draft_has_a_preview_url_without_any_build(beanie_test_db):
    """html is served from its source map; today the endpoint 422s it."""
    pocket_id = await _make_pocket("html", {"index.html": "<h1>hi</h1>"})

    result = await _artifact(pocket_id)

    assert result.get("build_status") == "none"
    url = result.get("preview_url")
    assert isinstance(url, str) and urlsplit(url).path == "/"


@pytest.mark.asyncio
async def test_a_pending_build_reports_a_null_preview_url(beanie_test_db):
    class _Pool:
        async def enqueue_job(self, *a, **kw):
            return object()

    pocket_id = await _make_pocket("react", dict(_REACT_SOURCE))

    result = await _artifact(pocket_id, pool=_Pool())

    assert result["build_status"] == "queued"
    assert "preview_url" in result and result["preview_url"] is None


# ---------------------------------------------------------------------------
# Preview host: the full draft, public, cookieless
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_index_keeps_its_head_and_module_script(beanie_test_db):
    _pid, url = await _react_draft()

    resp = await _fetch(url)

    _assert_public_asset(resp, "text/html")
    assert "<head>" in resp.text
    assert '<script type="module" crossorigin src="/assets/index-9f8e7d1c.js">' in resp.text
    assert 'data-uid="App:h1:0"' in resp.text


@pytest.mark.asyncio
async def test_preview_serves_the_js_chunk_with_acao_and_js_content_type(beanie_test_db):
    _pid, url = await _react_draft()

    resp = await _fetch(urljoin(url, "assets/index-9f8e7d1c.js"))

    _assert_public_asset(resp, "application/javascript")
    assert resp.text == _REACT_CHUNK


@pytest.mark.asyncio
async def test_root_absolute_asset_refs_resolve_on_the_preview_host(beanie_test_db):
    """index.html says src="/assets/...": the browser resolves it against the preview
    URL's ORIGIN root, so draft == published only if that lands on this draft."""
    _pid, url = await _react_draft()

    resp = await _fetch(urljoin(url, "/assets/index-9f8e7d1c.js"))

    _assert_public_asset(resp, "application/javascript")
    assert resp.text == _REACT_CHUNK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rel", "content_type"),
    [
        ("assets/index-4c5d.css", "text/css"),
        ("assets/hero-77aa.png", "image/png"),
        ("favicon.svg", "image/svg+xml"),
    ],
)
async def test_preview_serves_css_images_and_public_files(beanie_test_db, rel, content_type):
    _pid, url = await _react_draft()

    resp = await _fetch(urljoin(url, rel))

    _assert_public_asset(resp, content_type)


@pytest.mark.asyncio
async def test_svelte_preview_serves_its_immutable_entry_chunk(beanie_test_db):
    pocket_id = await _make_pocket("svelte", dict(_SVELTE_SOURCE))
    await _land_build(pocket_id, "svelte", _SVELTE_BUILD, "build")
    url = (await _artifact(pocket_id)).get("preview_url")
    assert isinstance(url, str), "no preview_url"

    resp = await _fetch(urljoin(url, "/_app/immutable/entry/start.Ab12.js"))

    _assert_public_asset(resp, "application/javascript")


@pytest.mark.asyncio
async def test_html_preview_serves_source_files_with_the_import_map(beanie_test_db):
    esm = dm.jsdelivr_esm_url("gsap", "3.13.0")
    source = {
        "index.html": (
            "<!DOCTYPE html><html><head><link rel='stylesheet' href='styles.css'></head>"
            "<body><h1>Hi</h1><script type='module'>import { gsap } from 'gsap';"
            "</script></body></html>"
        ),
        "styles.css": "h1{color:blue}",
        "js/app.mjs": "export const x = 1;",
        dm.DEPENDENCY_MANIFEST_PATH: dm.render_manifest(
            {"gsap": {"version": "3.13.0", "esm": esm}}
        ),
    }
    pocket_id = await _make_pocket("html", source)
    url = (await _artifact(pocket_id)).get("preview_url")
    assert isinstance(url, str), "no preview_url"

    index = await _fetch(url)
    _assert_public_asset(index, "text/html")
    assert '<script type="importmap">' in index.text
    assert '"gsap"' in index.text and esm in index.text

    _assert_public_asset(await _fetch(urljoin(url, "styles.css")), "text/css")
    _assert_public_asset(await _fetch(urljoin(url, "js/app.mjs")), "application/javascript")


def _corrupt_token(url: str) -> str:
    """Overwrite the longest opaque run in the URL (the capability token) with x's."""
    runs = re.findall(r"[A-Za-z0-9_-]{16,}", url)
    assert runs, f"no capability token visible in {url}"
    token = max(runs, key=len)
    return url.replace(token, "x" * len(token))


@pytest.mark.asyncio
async def test_an_unknown_token_is_a_404(beanie_test_db):
    _pid, url = await _react_draft()

    resp = await _fetch(_corrupt_token(url))

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_the_preview_host_never_sets_a_cookie_even_on_a_404(beanie_test_db):
    _pid, url = await _react_draft()

    resp = await _fetch(_corrupt_token(url))

    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}


# ---------------------------------------------------------------------------
# Router: the field reaches the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_artifact_route_carries_preview_url(beanie_test_db, monkeypatch):
    from tests.ee.sites.test_router import _build_app

    url = "https://0123456789abcdef0123456789abcdef.preview.paw-sites.test/index.html"

    async def _served(**kw):
        return {
            "pocket_id": "pk1",
            "body_html": "",
            "css": "",
            "build_status": "none",
            "build_reason": None,
            "build_job_id": None,
            "preview_url": url,
        }

    monkeypatch.setattr(sites_service, "get_native_artifact", _served)
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk1/native-artifact")

    assert resp.status_code == 200, resp.text
    assert resp.json().get("preview_url") == url


@pytest.mark.asyncio
async def test_native_artifact_route_serves_an_html_pocket(beanie_test_db):
    """End to end through the route: html used to be a 422 pocket.no_native_edit_lane."""
    from tests.ee.sites.test_router import _build_app

    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws_owner",
        owner_id="u1",
        name="Plain",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="html",
        source={"index.html": "<h1>hi</h1>"},
        trusted=True,
    )
    assert err is None, err
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(
            f"/api/v1/sites/by-pocket/{pocket_id}/native-artifact",
            headers={"Origin": ORIGIN},
        )

    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json().get("preview_url"), str)


# ---------------------------------------------------------------------------
# Subdomain token, edit bridge, host dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_token_is_a_subdomain_label_not_a_path_segment(beanie_test_db):
    _pid, url = await _react_draft()

    parts = urlsplit(url)
    label = parts.hostname.split(".")[0]
    assert re.fullmatch(r"[a-f0-9]{32}", label), url
    assert parts.hostname == f"{label}.preview.paw-sites.test"
    assert parts.path == "/"


@pytest.mark.asyncio
async def test_an_edit_gets_a_new_preview_url(beanie_test_db):
    pocket_id = await _make_pocket("html", {"index.html": "<h1>one</h1>"})
    first = (await _artifact(pocket_id)).get("preview_url")
    again = (await _artifact(pocket_id)).get("preview_url")
    await pockets_service.set_html_source_file(
        pocket_id, "u1", file_path="index.html", new_source="<h1>two</h1>"
    )
    second = (await _artifact(pocket_id)).get("preview_url")

    assert first == again, "an unchanged draft must keep its URL"
    assert second and second != first
    # The old URL keeps serving its own immutable build.
    assert "one" in (await _fetch(first)).text
    assert "two" in (await _fetch(second)).text


@pytest.mark.asyncio
async def test_html_preview_arms_the_edit_bridge_only_under_paw_edit(beanie_test_db):
    page = "<!DOCTYPE html><html><head></head><body><h1>Hi</h1></body></html>"
    pocket_id = await _make_pocket("html", {"index.html": page})
    url = (await _artifact(pocket_id, arm=_no_arm)).get("preview_url")
    assert isinstance(url, str)

    plain = await _fetch(url)
    armed = await _fetch(url + "?paw_edit=1")

    assert 'id="paw-edit-bridge"' not in plain.text
    _assert_public_asset(armed, "text/html")
    assert 'id="paw-edit-bridge"' in armed.text
    # The bridge posts only to the builder origin, never "*".
    assert f"var ORIGIN = {json.dumps(ORIGIN)};" in armed.text
    assert armed.text.index("paw-edit-bridge") < armed.text.lower().rindex("</body>")
    # ... and takes builder commands (highlight / inline edit) over the same trust.
    assert "__pawEditCmd" in armed.text


_DESIGN_PAGE = (
    "<!DOCTYPE html><html><head><title>Bright</title></head><body>"
    '<section class="hero"><span class="eyebrow">Hi</span><h1>Smile</h1>'
    '<p>Body <b>copy</b></p><a class="btn" href="/book">Book</a></section>'
    "</body></html>"
)


@pytest.mark.asyncio
async def test_html_edit_variant_is_stamped_without_the_paw_sites_cli(beanie_test_db):
    """No CLI on the API host: the Python port stamps the paw-sites uids."""
    pocket_id = await _make_pocket("html", {"index.html": _DESIGN_PAGE})
    url = (await _artifact(pocket_id, arm=_no_arm)).get("preview_url")

    armed = (await _fetch(url + "?paw_edit=1")).text
    plain = (await _fetch(url)).text

    assert re.findall(r'data-uid="([^"]+)"', armed) == [
        "index:title:0",
        "index:eyebrow:0",
        "index:headline:0",
        "index:b:0",
        "index:cta:0",
    ]
    # Browse serves the author's bytes: no stamps.
    assert "data-uid" not in plain


@pytest.mark.asyncio
async def test_html_edit_variant_prefers_arm_html_when_it_is_available(beanie_test_db):
    calls: list[dict[str, str]] = []

    async def _arm(*, source: dict[str, str]) -> dict[str, Any]:
        calls.append(source)
        stamped = '<h1 data-uid="from:cli:0">'
        return {"source": {k: v.replace("<h1>", stamped) for k, v in source.items()}}

    pocket_id = await _make_pocket("html", {"index.html": _DESIGN_PAGE})
    url = (await _artifact(pocket_id, arm=_arm)).get("preview_url")

    armed = (await _fetch(url + "?paw_edit=1")).text
    assert calls and 'data-uid="from:cli:0"' in armed
    assert "index:headline:0" not in armed


@pytest.mark.asyncio
async def test_every_html_preview_page_reports_runtime_errors(beanie_test_db):
    """Browse and Design both carry the reporter, first in <head>, so the builder
    sees script errors from the page's own scripts in either mode."""
    pages = {
        "index.html": "<!DOCTYPE html><html><head><script>boom()</script></head><body></body>",
        "about.html": "<!doctype html><p>No head tag</p>",
    }
    pocket_id = await _make_pocket("html", pages)
    url = (await _artifact(pocket_id, arm=_no_arm)).get("preview_url")

    for path in ("/index.html", "/index.html?paw_edit=1", "/about.html", "/about.html?paw_edit=1"):
        text = (await _fetch(urljoin(url, path))).text
        assert text.count('id="paw-runtime-reporter"') == 1, path
        assert "__pawRuntime" in text and f"var TARGET = {json.dumps(ORIGIN)};" in text, path
    index = (await _fetch(url)).text
    assert index.index("paw-runtime-reporter") < index.index("boom()")
    about = (await _fetch(urljoin(url, "/about.html"))).text
    # Never ahead of the doctype: that would put the page in quirks mode.
    assert about.lower().startswith('<!doctype html><script id="paw-runtime-reporter">')


@pytest.mark.asyncio
async def test_the_edit_variant_is_not_addressable_by_path(beanie_test_db):
    pocket_id = await _make_pocket("html", {"index.html": "<h1>hi</h1>"})
    url = (await _artifact(pocket_id)).get("preview_url")

    resp = await _fetch(urljoin(url, "/.paw-edit/index.html"))

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_hashed_assets_are_immutable_and_unknown_files_404(beanie_test_db):
    _pid, url = await _react_draft()

    asset = await _fetch(urljoin(url, "/assets/index-9f8e7d1c.js"))
    missing = await _fetch(urljoin(url, "/assets/nope.js"))

    assert "immutable" in asset.headers.get("cache-control", "")
    assert asset.headers.get("x-content-type-options") == "nosniff"
    assert "x-frame-options" not in {k.lower() for k in asset.headers.keys()}
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_the_preview_host_ignores_cookies_and_auth(beanie_test_db):
    _pid, url = await _react_draft()

    async with AsyncClient(transport=ASGITransport(app=_preview_app())) as client:
        resp = await client.get(
            url, headers={"Cookie": "session=abc", "Authorization": "Bearer nope"}
        )

    _assert_public_asset(resp, "text/html")


@pytest.mark.asyncio
async def test_host_dispatch_sends_only_preview_hosts_to_the_preview_app(beanie_test_db):
    from fastapi import FastAPI
    from pocketpaw_ee.sites.preview_origin import PreviewHostDispatch

    _pid, url = await _react_draft()
    api = FastAPI()

    @api.get("/index.html")
    async def _api_route():
        return {"served_by": "api"}

    api.add_middleware(PreviewHostDispatch)
    async with AsyncClient(transport=ASGITransport(app=api)) as client:
        preview = await client.get(url)
        app_host = await client.get("https://dash.paw.example/index.html")

    _assert_public_asset(preview, "text/html")
    assert 'data-uid="App:h1:0"' in preview.text
    assert app_host.json() == {"served_by": "api"}
