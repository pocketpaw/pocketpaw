# tests/cloud/test_paw_bar_caching.py — HTTP caching of the Paw Bar public surface.
#
# The iframe reloads on every host-page navigation, so Paw Bar load follows the
# customers' site traffic. These pin the caching policy of each public response:
#   * GET /paw-bar/widget.js: bytes held in memory, strong ETag, If-None-Match -> 304,
#     public max-age=300;
#   * the glass app assets (PawBarAssets): immutable for a year ONLY for pawbar.js /
#     pawbar.css requested with the current content-hash ``v``, short otherwise;
#   * GET /paw-bar/frame: ``private, max-age=60`` (never public), dead shell stays
#     no-store, and the key -> Site lookup is memoised for its TTL and no longer;
#   * GET /paw-bar/spec/{id}: public max-age=60 with ``Vary: Origin`` always.

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from pocketpaw.paw_bar.models import PawBarBlock, PawBarSpec

_KEY = "site_key_" + "c" * 24


@pytest_asyncio.fixture
async def client():
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.paw_bar.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


# --------------------------------------------------------------------------- #
# widget.js
# --------------------------------------------------------------------------- #


@pytest.fixture
def loader(tmp_path, monkeypatch):
    path = tmp_path / "loader.js"
    path.write_bytes(b"/* loader v1 */\n")
    monkeypatch.setenv("PAW_BAR_WIDGET_JS", str(path))
    return path


@pytest.mark.asyncio
async def test_widget_js_sends_strong_etag_and_short_public_max_age(client, loader):
    res = await client.get("/paw-bar/widget.js")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "public, max-age=300"
    etag = res.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"') and not etag.startswith("W/")


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["{etag}", "W/{etag}", '"nope", {etag}', "*"])
async def test_widget_js_if_none_match_is_a_bodiless_304(client, loader, form):
    etag = (await client.get("/paw-bar/widget.js")).headers["etag"]
    res = await client.get("/paw-bar/widget.js", headers={"If-None-Match": form.format(etag=etag)})
    assert res.status_code == 304
    assert res.content == b""
    assert res.headers["etag"] == etag
    assert res.headers["cache-control"] == "public, max-age=300"


@pytest.mark.asyncio
async def test_widget_js_stale_etag_gets_the_body(client, loader):
    res = await client.get("/paw-bar/widget.js", headers={"If-None-Match": '"stale"'})
    assert res.status_code == 200
    assert res.content == b"/* loader v1 */\n"


@pytest.mark.asyncio
async def test_widget_js_is_read_once_then_served_from_memory(client, loader, monkeypatch):
    from pathlib import Path

    first = await client.get("/paw-bar/widget.js")
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or real(self))
    again = await client.get("/paw-bar/widget.js")
    assert again.content == first.content
    assert reads == []


@pytest.mark.asyncio
async def test_widget_js_replacing_the_file_changes_body_and_etag(client, loader):
    first = await client.get("/paw-bar/widget.js")
    loader.write_bytes(b"/* loader v2, longer */\n")
    st = loader.stat()
    os.utime(loader, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    second = await client.get(
        "/paw-bar/widget.js", headers={"If-None-Match": first.headers["etag"]}
    )
    assert second.status_code == 200
    assert second.content == b"/* loader v2, longer */\n"
    assert second.headers["etag"] != first.headers["etag"]


# --------------------------------------------------------------------------- #
# Glass app assets (PawBarAssets)
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def assets(tmp_path, monkeypatch):
    from pocketpaw_ee.paw_bar.router import PAWBAR_APP_MOUNT, PawBarAssets

    (tmp_path / "pawbar.js").write_bytes(b"// bundle v1")
    (tmp_path / "pawbar.css").write_bytes(b"/* css v1 */")
    (tmp_path / "pawbar.js.map").write_bytes(b"{}")
    monkeypatch.setenv("PAWBAR_APP_DIR", str(tmp_path))
    app = FastAPI()
    app.mount(PAWBAR_APP_MOUNT, PawBarAssets(directory=str(tmp_path)), name="pawbar-app")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, tmp_path


_IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pawbar.js", "pawbar.css"])
async def test_asset_with_current_version_is_immutable(assets, name):
    from pocketpaw_ee.paw_bar.router import _asset_version

    c, _ = assets
    res = await c.get(f"/pawbar-app/{name}", params={"v": _asset_version()})
    assert res.status_code == 200
    assert res.headers["cache-control"] == _IMMUTABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"v": "stale"}, {"v": "0"}])
async def test_asset_without_current_version_gets_short_max_age(assets, params):
    c, _ = assets
    res = await c.get("/pawbar-app/pawbar.js", params=params)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "public, max-age=300"


@pytest.mark.asyncio
async def test_unversioned_file_never_immutable_even_with_current_v(assets):
    """The source map is not part of ``v``, so ``v`` says nothing about its bytes."""
    from pocketpaw_ee.paw_bar.router import _asset_version

    c, _ = assets
    res = await c.get("/pawbar-app/pawbar.js.map", params={"v": _asset_version()})
    assert res.headers["cache-control"] == "public, max-age=300"


@pytest.mark.asyncio
async def test_new_bytes_with_the_same_mtime_mint_a_new_version(assets):
    """The hole mtime-based ``v`` had: a deploy that preserves mtimes put new bytes
    under the old ``v``, which the immutable header would pin for a year. The old
    ``v`` must now read as stale."""
    from pocketpaw_ee.paw_bar.router import _asset_version

    c, d = assets
    old_v = _asset_version()
    js = d / "pawbar.js"
    st = js.stat()
    js.write_bytes(b"// bundle v2 !")  # different size, so the stat signature changes
    os.utime(js, ns=(st.st_atime_ns, st.st_mtime_ns))
    new_v = _asset_version()
    assert new_v != old_v
    res = await c.get("/pawbar-app/pawbar.js", params={"v": old_v})
    assert res.headers["cache-control"] == "public, max-age=300"


def test_asset_version_is_zero_without_a_bundle(tmp_path, monkeypatch):
    from pocketpaw_ee.paw_bar.router import _asset_version

    monkeypatch.setenv("PAWBAR_APP_DIR", str(tmp_path / "empty"))
    assert _asset_version() == "0"


def test_the_cloud_mount_uses_pawbarassets():
    """The policy only exists if the real mount is this class."""
    import inspect

    import pocketpaw_ee.cloud as cloud

    assert "PawBarAssets(directory=str(pawbar_dir))" in inspect.getsource(cloud)


# --------------------------------------------------------------------------- #
# Frame
# --------------------------------------------------------------------------- #


def _fake_site(**ov):
    d = dict(
        allowed_origins=["brewco.com"],
        concierge_greeting="hi",
        concierge_appearance=None,
        workspace="ws-1",
        pocket_id="pocket-1",
    )
    d.update(ov)
    return SimpleNamespace(**d)


@pytest.fixture
def lookups(monkeypatch):
    """Replace the DB key lookup with a counter; ``state['site']`` is what it returns,
    or an exception to raise (a revoked key)."""
    from pocketpaw_ee.cloud.auth import site_keys

    state = {"calls": 0, "site": _fake_site()}

    async def _lookup(key):
        state["calls"] += 1
        if isinstance(state["site"], Exception):
            raise state["site"]
        return state["site"]

    monkeypatch.setattr(site_keys, "lookup_site_by_key", _lookup)
    monkeypatch.setattr(site_keys, "concierge_available", lambda site: True)
    monkeypatch.delenv("PAWBAR_DASHBOARD_ORIGIN", raising=False)
    monkeypatch.delenv("POCKETPAW_API_CORS_ALLOWED_ORIGINS", raising=False)
    return state


def _expire(key: str = _KEY) -> None:
    from pocketpaw_ee.paw_bar import router as r

    _, site = r._frame_site_memo[key]
    r._frame_site_memo[key] = (0.0, site)


@pytest.mark.asyncio
async def test_frame_is_private_short_cache_never_public(client, lookups):
    res = await client.get("/paw-bar/frame", params={"key": _KEY})
    assert res.status_code == 200
    cc = res.headers["cache-control"]
    assert cc == "private, max-age=60"
    assert "public" not in cc


@pytest.mark.asyncio
async def test_frame_html_has_no_per_request_nonce(client, lookups):
    """The caching decision rests on the document being the same for every visitor
    of one URL. Two renders must be byte-identical."""
    a = await client.get("/paw-bar/frame", params={"key": _KEY, "po": "https://brewco.com"})
    b = await client.get("/paw-bar/frame", params={"key": _KEY, "po": "https://brewco.com"})
    assert a.text == b.text
    assert a.headers["content-security-policy"] == b.headers["content-security-policy"]
    assert "nonce" not in a.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_dead_frame_stays_no_store(client, lookups, monkeypatch):
    from pocketpaw_ee.cloud.auth import site_keys

    monkeypatch.setattr(site_keys, "concierge_available", lambda site: False)
    res = await client.get("/paw-bar/frame", params={"key": _KEY})
    assert res.status_code == 403
    assert res.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_frame_lookup_is_memoised_within_ttl(client, lookups):
    for _ in range(3):
        assert (await client.get("/paw-bar/frame", params={"key": _KEY})).status_code == 200
    assert lookups["calls"] == 1


@pytest.mark.asyncio
async def test_frame_lookup_reruns_after_ttl_and_a_revoked_key_stops(client, lookups):
    assert (await client.get("/paw-bar/frame", params={"key": _KEY})).status_code == 200
    lookups["site"] = HTTPException(status_code=401, detail="invalid_site_key")

    # Inside the TTL the memoised Site still answers.
    assert (await client.get("/paw-bar/frame", params={"key": _KEY})).status_code == 200
    assert lookups["calls"] == 1

    _expire()
    res = await client.get("/paw-bar/frame", params={"key": _KEY})
    assert res.status_code == 401
    assert lookups["calls"] == 2


@pytest.mark.asyncio
async def test_frame_failures_are_never_memoised(client, lookups):
    from pocketpaw_ee.paw_bar import router as r

    lookups["site"] = HTTPException(status_code=401, detail="invalid_site_key")
    for _ in range(2):
        assert (await client.get("/paw-bar/frame", params={"key": _KEY})).status_code == 401
    assert lookups["calls"] == 2
    assert _KEY not in r._frame_site_memo


def test_frame_ttl_constants():
    """The PR states these numbers; a change should be deliberate."""
    from pocketpaw_ee.paw_bar import router as r

    assert r._FRAME_SITE_TTL_S == 30.0
    assert r._FRAME_MAX_AGE_S == 60


# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #


@pytest.fixture
def spec_widget(monkeypatch):
    from pocketpaw_ee.paw_bar import router as r

    widget = SimpleNamespace(
        allowed_domains=["brewco.com"],
        spec=PawBarSpec(
            widget_id="pp_c",
            pocket_id="pocket-1",
            blocks=[PawBarBlock(type="text", content="hi")],
        ),
    )

    class _Store:
        async def get_widget(self, widget_id):
            return widget if widget_id == "pp_c" else None

    monkeypatch.setattr(r, "_store", lambda: _Store())
    r._PUBLIC_IP_LIMITER.cleanup(max_age=0)
    return widget


@pytest.mark.asyncio
async def test_spec_is_public_short_cache_and_varies_on_origin(client, spec_widget):
    res = await client.get("/paw-bar/spec/pp_c", headers={"Origin": "https://brewco.com"})
    assert res.status_code == 200
    assert res.headers["cache-control"] == "public, max-age=60"
    assert res.headers["vary"] == "Origin"
    assert res.headers["access-control-allow-origin"] == "https://brewco.com"


@pytest.mark.asyncio
async def test_spec_without_origin_still_varies_on_origin(client, spec_widget):
    """A shared cache that stored this origin-less reply (no ACAO) must not replay it
    to a cross-origin fetch, so ``Vary: Origin`` is unconditional."""
    spec_widget.allowed_domains = []
    res = await client.get("/paw-bar/spec/pp_c")
    assert res.status_code == 200
    assert res.headers["vary"] == "Origin"
    assert "access-control-allow-origin" not in res.headers


@pytest.mark.asyncio
async def test_spec_refusal_is_not_cacheable(client, spec_widget):
    res = await client.get("/paw-bar/spec/pp_c", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    assert "public" not in res.headers.get("cache-control", "")
