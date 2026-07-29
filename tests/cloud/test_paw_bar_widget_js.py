# tests/cloud/test_paw_bar_widget_js.py — GET /paw-bar/widget.js, the public
# glass-bar loader route.
# Created 2026-07-30 (feat/paw-bar-autoembed). Before this route there was no URL
# that served the bar at all: the only one ever advertised pointed at an
# unprovisioned CDN placeholder, so even a hand-pasted embed snippet 404'd. The
# publish path now bakes this URL into customers' deployed HTML, which makes three
# things worth pinning down:
#   * it serves the bundle as JavaScript, cacheably, with NOTHING tenant-specific
#     in it (it is one file shared by every visitor of every site — the credential
#     is presented later, by the iframe it mounts, at /paw-bar/frame);
#   * the path resolves from PAW_BAR_WIDGET_JS when set and from the copy vendored
#     in the package otherwise, so a machine with no sibling checkout still serves
#     a working loader;
#   * a missing bundle is a clean 404 naming the fix, not a FileNotFoundError
#     surfacing as an opaque 500 to whoever is debugging a barless site.
# The vendored default is also syntax-relevant: it is served raw to a foreign page,
# so the last test asserts it stays wrapped in one IIFE and keeps its globals to
# window.PawBar.

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def widget_client():
    """A bare app client. The route reads no DB and needs no store — that IS the
    contract under test, so mounting it alone is the honest harness."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


@pytest.mark.asyncio
async def test_serves_the_loader_as_javascript(widget_client):
    res = await widget_client.get("/paw-bar/widget.js")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/javascript")
    assert "max-age" in res.headers["cache-control"]
    # It is the loader, not some other asset: it mounts the concierge iframe.
    assert "/paw-bar/frame" in res.text


@pytest.mark.asyncio
async def test_the_loader_carries_nothing_tenant_specific(widget_client):
    """This one file is served byte-identically to every visitor of every site. A
    key, a widget id or a workspace baked into it would leak across tenants; the
    per-site config rides on the embedding <script> tag's data attributes instead."""
    res = await widget_client.get("/paw-bar/widget.js")

    body = res.text
    assert "site_key_" not in body
    assert "workspace" not in body.lower()
    # It READS the config attributes; it must not contain a resolved value.
    assert "data-site-key" in body


@pytest.mark.asyncio
async def test_env_override_wins(widget_client, tmp_path, monkeypatch):
    """PAW_BAR_WIDGET_JS is the seam for serving a freshly built bundle without a
    redeploy."""
    custom = tmp_path / "built-widget.js"
    custom.write_text("/* a newer build */\n", encoding="utf-8")
    monkeypatch.setenv("PAW_BAR_WIDGET_JS", str(custom))

    res = await widget_client.get("/paw-bar/widget.js")

    assert res.status_code == 200
    assert res.text == "/* a newer build */\n"


@pytest.mark.asyncio
async def test_missing_bundle_is_a_clean_404(widget_client, tmp_path, monkeypatch):
    """The operator hitting this is debugging why a live site shows no bar, so the
    message has to name the fix rather than dump a traceback."""
    monkeypatch.setenv("PAW_BAR_WIDGET_JS", str(tmp_path / "nope.js"))

    res = await widget_client.get("/paw-bar/widget.js")

    assert res.status_code == 404
    assert "PAW_BAR_WIDGET_JS" in res.json()["detail"]


def test_default_path_is_the_vendored_copy(monkeypatch):
    """The default must live IN the package: the publish path bakes this URL into
    customers' HTML, so it has to resolve on every machine running the backend, not
    only on a developer's with a sibling checkout."""
    from pocketpaw_ee.paw_bar.router import paw_bar_widget_file

    monkeypatch.delenv("PAW_BAR_WIDGET_JS", raising=False)
    path = paw_bar_widget_file()

    assert path.is_file()
    assert path.parent.name == "static"
    assert path.parent.parent.name == "paw_bar"


def test_vendored_loader_keeps_its_globals_to_itself():
    """It is served raw as a CLASSIC script onto a page we do not own. A bare
    top-level ``const clamp`` would collide with the host site's own globals (and a
    duplicate ``const`` is a hard SyntaxError that kills the whole file), so the
    bundle must stay wrapped and expose only window.PawBar."""
    from pocketpaw_ee.paw_bar.router import paw_bar_widget_file

    source = paw_bar_widget_file().read_text(encoding="utf-8")
    body = [ln for ln in source.splitlines() if ln and not ln.startswith("//")]

    assert body[0].startswith("(function")
    # Nothing declared at column 0 except the wrapper itself and its close.
    stray = [
        ln for ln in body[1:] if (ln.startswith(("const ", "let ", "var ", "function ", "class ")))
    ]
    assert stray == []
    assert "win.PawBar" in source
