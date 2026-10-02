# tests/cloud/test_paw_bar_actions_js.py — GET /paw-bar/actions.js, the public
# page-actions host script a site owner adds beside the loader so the concierge
# can scroll to and highlight things on the host page.
#
# It is served exactly like the loader (test_paw_bar_widget_js.py and the
# widget.js half of test_paw_bar_caching.py), so these pin the same contract:
#   * JavaScript, public max-age=300, strong ETag, If-None-Match -> bodiless 304,
#     no auth and nothing tenant-specific in the body;
#   * PAW_BAR_ACTIONS_JS overrides the vendored copy, a missing file is a clean
#     404 naming that variable;
#   * the default is the copy vendored in the package, served byte for byte,
#     wrapped in one IIFE, generated (not hand-edited), and still speaking the
#     pawbar:act / pawbar:act-result protocol the glass app posts, and writing
#     no page globals beyond its load flag and window.pawbarTools.

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def client():
    """A bare app: the route reads no DB and needs no auth, which is the contract."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def _vendored_source() -> str:
    from pocketpaw_ee.paw_bar.router import paw_bar_actions_file

    return paw_bar_actions_file().read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(
        ln for ln in _vendored_source().splitlines() if not ln.lstrip().startswith("//")
    )


@pytest.mark.asyncio
async def test_serves_the_vendored_script_as_javascript(client, monkeypatch):
    from pocketpaw_ee.paw_bar.router import paw_bar_actions_file

    monkeypatch.delenv("PAW_BAR_ACTIONS_JS", raising=False)
    res = await client.get("/paw-bar/actions.js")

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/javascript; charset=utf-8"
    assert res.headers["cache-control"] == "public, max-age=300"
    etag = res.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"') and not etag.startswith("W/")
    assert res.content == paw_bar_actions_file().read_bytes()


@pytest.mark.asyncio
async def test_matching_etag_is_a_bodiless_304(client, monkeypatch):
    monkeypatch.delenv("PAW_BAR_ACTIONS_JS", raising=False)
    etag = (await client.get("/paw-bar/actions.js")).headers["etag"]

    res = await client.get("/paw-bar/actions.js", headers={"If-None-Match": f"W/{etag}"})

    assert res.status_code == 304
    assert res.content == b""
    assert res.headers["etag"] == etag
    assert res.headers["cache-control"] == "public, max-age=300"


@pytest.mark.asyncio
async def test_actions_and_loader_are_cached_separately(client, tmp_path, monkeypatch):
    """The two routes share one serving path; their in-memory copies must not."""
    loader = tmp_path / "loader.js"
    loader.write_bytes(b"/* loader */\n")
    actions = tmp_path / "actions.js"
    actions.write_bytes(b"/* actions */\n")
    monkeypatch.setenv("PAW_BAR_WIDGET_JS", str(loader))
    monkeypatch.setenv("PAW_BAR_ACTIONS_JS", str(actions))

    a = await client.get("/paw-bar/actions.js")
    w = await client.get("/paw-bar/widget.js")

    assert a.content == b"/* actions */\n"
    assert w.content == b"/* loader */\n"
    assert a.headers["etag"] != w.headers["etag"]


@pytest.mark.asyncio
async def test_env_override_wins(client, tmp_path, monkeypatch):
    custom = tmp_path / "built-actions.js"
    # Bytes, not text: text mode writes CRLF on Windows and the route copies bytes.
    custom.write_bytes(b"/* a newer build */\n")
    monkeypatch.setenv("PAW_BAR_ACTIONS_JS", str(custom))

    res = await client.get("/paw-bar/actions.js")

    assert res.status_code == 200
    assert res.text == "/* a newer build */\n"


@pytest.mark.asyncio
async def test_missing_script_is_a_clean_404(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PAW_BAR_ACTIONS_JS", str(tmp_path / "nope.js"))

    res = await client.get("/paw-bar/actions.js")

    assert res.status_code == 404
    assert "PAW_BAR_ACTIONS_JS" in res.json()["detail"]


def test_default_path_is_the_vendored_copy(monkeypatch):
    from pocketpaw_ee.paw_bar.router import paw_bar_actions_file

    monkeypatch.delenv("PAW_BAR_ACTIONS_JS", raising=False)
    path = paw_bar_actions_file()

    assert path.is_file()
    assert path.name == "paw-bar-actions.js"
    assert path.parent.name == "static"
    assert path.parent.parent.name == "paw_bar"


def test_vendored_script_keeps_its_globals_to_itself():
    """Served raw as a classic script onto a page we do not own, so nothing may be
    declared at column 0 outside the wrapper."""
    body = [ln for ln in _vendored_source().splitlines() if ln and not ln.startswith("//")]
    opener = [ln for ln in body if ln != '"use strict";'][0]
    assert opener.startswith("(() =>") or opener.startswith("(function")
    stray = [
        ln for ln in body[1:] if ln.startswith(("const ", "let ", "var ", "function ", "class "))
    ]
    assert stray == []


def test_vendored_script_writes_only_its_two_page_globals():
    """Inside the wrapper it may write exactly two page properties: its own
    double-load flag and window.pawbarTools, the array a site declares tools on
    (created if absent, its push replaced so later declarations register)."""
    code = _code()
    writes = set(re.findall(r"\bwin(\.[A-Za-z_$][\w$]*|\[[^\]]+\])\s*=(?!=)", code))
    assert writes == {"[LOADED_FLAG]", ".pawbarTools"}
    assert 'LOADED_FLAG = "__pawBarActionsLoaded"' in code
    assert "window." not in code and "globalThis" not in code
    assert re.findall(r"(\w+)\.push\s*=(?!=)", code) == ["queue"]
    assert "const queue = Array.isArray(win.pawbarTools)" in code


def test_vendored_script_carries_nothing_tenant_specific():
    code = _code()
    assert "site_key_" not in code
    assert "workspace" not in code.lower()


def test_vendored_script_speaks_the_frame_protocol():
    """The glass app posts pawbar:act and waits for pawbar:act-result; the script
    must only trust a /paw-bar/frame iframe and find itself by its own path."""
    code = _code()
    assert '"pawbar:act"' in code
    assert '"pawbar:act-result"' in code
    assert r"/\/paw-bar\/frame$/" in code
    assert r"/\/paw-bar\/actions\.js$/" in code


def test_vendored_script_is_generated_not_hand_edited():
    header = _vendored_source().split('"use strict";')[0]
    assert "GENERATED, DO NOT EDIT BY HAND" in header
    assert "actions/src/actions.ts" in header
