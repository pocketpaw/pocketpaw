# tests/ee/sites/test_import.py — SI-4 (feat/sites-import-endpoint): the Paw Sites
# IMPORT control-plane path.
#
# Created 2026-07-22. Covers:
#   * POST /sites/import happy path through the real router → import_service →
#     pockets (real agent_create) → create_draft_site → publish (FAKED — a real
#     publish spawns bun): the html pocket + Site doc are minted, the generator is
#     handed engine="html" with the text ``source`` map and the base64 ``assets``
#     sideband, and the derived ``import_report`` (pages/titles, asset counts,
#     forms with original actions) is persisted on the Site doc AND surfaced on
#     the response + the GET /sites read.
#   * Safety guards, each failing closed as a 4xx: zip-slip (../ traversal and
#     absolute entry paths), the entry-count cap, the uncompressed-size cap
#     (decompression bomb), the 25MB upload cap at the router (413), and a zip
#     with no root index.html.
#   * Tenant scoping: the imported site is invisible cross-tenant (empty GET
#     /sites; the site-scoped domains read 404s).
#   * POST /sites/import/from-url: a valid URL → 202 {site_id, status:"queued"}
#     with a crawler-pending import_report on a NOT-deployed draft Site doc (the
#     crawler is the next stacked slice); a malformed URL / non-http scheme → 422.
#   * GeneratorClient unit: the html STAGE-2 payload carries ``input.assets`` when
#     (and ONLY when) an assets sideband is passed — the cross-repo seam contract.
from __future__ import annotations

import base64
import io
import zipfile
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.sites import import_service
from pocketpaw_ee.sites import service as sites_service

_INDEX_HTML = (
    "<!doctype html><html><head><title>Bright Import</title></head>"
    "<body><h1>Hi</h1>"
    "<form action='https://old-backend.example/submit' method='post'>"
    "<input name='email'></form>"
    "<script src='app.js'></script>"
    "</body></html>"
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _good_zip() -> bytes:
    return _zip_bytes(
        {
            "index.html": _INDEX_HTML.encode(),
            "about.html": (
                b"<!doctype html><html><head><title>About</title></head><body></body></html>"
            ),
            "app.js": b"console.log('hi')",
            "img/logo.png": _PNG_BYTES,
        }
    )


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, workspace_id: str) -> None:
        self.id = "user-test-1"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="member")]


def _build_app(workspace_id: str, monkeypatch) -> FastAPI:
    """Mirror tests/ee/sites/test_router.py's app wiring: override auth/context
    deps, stub the workspace plan to one that unlocks Sites, mount the router."""
    from datetime import UTC, datetime

    import pocketpaw_ee.cloud.workspace.service as ws_svc
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="go"))

    fake_user = _FakeUser(workspace_id)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=str(fake_user.id),
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: fake_user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


@pytest_asyncio.fixture
async def _fake_publish(beanie_test_db, monkeypatch) -> dict[str, Any]:
    """Fake sites_service.publish (a real one shells out to bun/the generator):
    records the forwarded kwargs and flips the REAL draft Site doc (minted by the
    real create_draft_site the import ran) to deployed, mirroring what the html
    deploy path does. Returns the capture dict for assertions."""
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    captured: dict[str, Any] = {}

    async def _publish(**kw):
        captured.update(kw)
        oid = sites_service._live_object_id(kw["workspace_id"], kw["pocket_id"])
        doc = await _SiteDoc.find_one({"_id": oid, "workspace": kw["workspace_id"]})
        assert doc is not None, "import must mint the draft Site doc before publishing"
        doc.script_name = str(oid)
        doc.deployed = True
        doc.url = f"http://127.0.0.1:9999/{oid}/"
        await doc.save()
        return doc

    monkeypatch.setattr(sites_service, "publish", _publish)
    return captured


async def _post_zip(app: FastAPI, data: bytes, name: str = "") -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(
            "/api/v1/sites/import",
            files={"file": ("site.zip", data, "application/zip")},
            data={"name": name} if name else {},
        )


# --------------------------------------------------------------------------- #
# zip import — happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_import_zip_happy_path(_fake_publish, monkeypatch):
    """A good zip imports end to end: Site doc minted + flipped live, the generator
    input carries engine=html / text source / base64 assets, and the import_report
    (pages, asset counts, forms) is persisted and returned."""
    app = _build_app("ws_owner", monkeypatch)
    resp = await _post_zip(app, _good_zip(), name="My imported site")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The publish rode the html engine with the text source map + assets sideband.
    assert _fake_publish["engine"] == "html"
    assert set(_fake_publish["source"]) == {"index.html", "about.html", "app.js"}
    assert _fake_publish["source"]["index.html"] == _INDEX_HTML
    assert _fake_publish["assets"] == {"img/logo.png": base64.b64encode(_PNG_BYTES).decode("ascii")}
    assert _fake_publish["pattern"] == "imported"

    # The response is the live SiteResponse carrying the report.
    assert body["deployed"] is True
    report = body["import_report"]
    assert {p["path"] for p in report["pages"]} == {"index.html", "about.html"}
    titles = {p["path"]: p["title"] for p in report["pages"]}
    assert titles["index.html"] == "Bright Import"
    assert report["asset_count"] == 1
    assert report["asset_bytes"] == len(_PNG_BYTES)
    assert report["forms"] == [
        {
            "page": "index.html",
            "original_action": "https://old-backend.example/submit",
            "rewired": False,
        }
    ]
    assert "app.js" in report["scripts"]

    # The report persisted on the Site doc and surfaces on the gallery read too.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        listed = await c.get("/api/v1/sites")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["import_report"]["asset_count"] == 1


@pytest.mark.asyncio
async def test_import_zip_single_root_dir_flattens(_fake_publish, monkeypatch):
    """The `zip -r site site/` shape (one top dir holding index.html) imports as if
    that dir were the root."""
    app = _build_app("ws_owner", monkeypatch)
    data = _zip_bytes({"mysite/index.html": _INDEX_HTML.encode(), "mysite/style.css": b":root{}"})
    resp = await _post_zip(app, data)
    assert resp.status_code == 200, resp.text
    assert set(_fake_publish["source"]) == {"index.html", "style.css"}


# --------------------------------------------------------------------------- #
# zip import — guards (each fails closed as a 4xx, nothing minted)
# --------------------------------------------------------------------------- #


async def _assert_nothing_minted() -> None:
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    assert await _SiteDoc.find_one({"workspace": "ws_owner"}) is None


@pytest.mark.asyncio
async def test_import_zip_slip_traversal_rejected(beanie_test_db, monkeypatch):
    """A ``..`` traversal entry fails the WHOLE import (422) — hostile archives are
    not cherry-picked — and no pocket / Site doc is minted."""
    app = _build_app("ws_owner", monkeypatch)
    data = _zip_bytes({"index.html": _INDEX_HTML.encode(), "../evil.html": b"<p>evil</p>"})
    resp = await _post_zip(app, data)
    assert resp.status_code == 422, resp.text
    await _assert_nothing_minted()


@pytest.mark.asyncio
async def test_import_zip_absolute_path_rejected(beanie_test_db, monkeypatch):
    app = _build_app("ws_owner", monkeypatch)
    data = _zip_bytes({"index.html": _INDEX_HTML.encode(), "/etc/cron.d/evil": b"x"})
    resp = await _post_zip(app, data)
    assert resp.status_code == 422, resp.text
    await _assert_nothing_minted()


@pytest.mark.asyncio
async def test_import_zip_entry_cap_rejected(beanie_test_db, monkeypatch):
    app = _build_app("ws_owner", monkeypatch)
    monkeypatch.setattr(import_service, "MAX_IMPORT_ENTRIES", 3)
    data = _zip_bytes({f"f{i}.txt": b"x" for i in range(5)} | {"index.html": b"<html></html>"})
    resp = await _post_zip(app, data)
    assert resp.status_code == 422, resp.text
    await _assert_nothing_minted()


@pytest.mark.asyncio
async def test_import_zip_uncompressed_cap_rejected(beanie_test_db, monkeypatch):
    """A tiny-on-the-wire zip that INFLATES past the cap is rejected (bomb guard)."""
    app = _build_app("ws_owner", monkeypatch)
    monkeypatch.setattr(import_service, "MAX_IMPORT_UNCOMPRESSED_BYTES", 1024)
    data = _zip_bytes({"index.html": b"<html>" + b"A" * 4096 + b"</html>"})
    resp = await _post_zip(app, data)
    assert resp.status_code == 422, resp.text
    await _assert_nothing_minted()


@pytest.mark.asyncio
async def test_import_zip_oversized_upload_is_413(beanie_test_db, monkeypatch):
    """The router's streaming upload cap rejects an oversized body with 413 before
    the service ever unpacks it."""
    app = _build_app("ws_owner", monkeypatch)
    monkeypatch.setattr(import_service, "MAX_IMPORT_ZIP_BYTES", 64)
    resp = await _post_zip(app, _good_zip())
    assert resp.status_code == 413, resp.text
    await _assert_nothing_minted()


@pytest.mark.asyncio
async def test_import_zip_without_index_rejected(beanie_test_db, monkeypatch):
    app = _build_app("ws_owner", monkeypatch)
    data = _zip_bytes({"about.html": b"<html><body>no index</body></html>"})
    resp = await _post_zip(app, data)
    assert resp.status_code == 422, resp.text
    await _assert_nothing_minted()


@pytest.mark.asyncio
async def test_import_zip_not_a_zip_rejected(beanie_test_db, monkeypatch):
    app = _build_app("ws_owner", monkeypatch)
    resp = await _post_zip(app, b"this is not a zip archive")
    assert resp.status_code == 422, resp.text
    await _assert_nothing_minted()


# --------------------------------------------------------------------------- #
# tenant scoping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_import_cross_tenant_site_is_invisible(_fake_publish, monkeypatch):
    """An imported site never leaks cross-tenant: the intruder's gallery list is
    empty and the site-scoped detail read (domains) is a 404."""
    owner_app = _build_app("ws_owner", monkeypatch)
    resp = await _post_zip(owner_app, _good_zip())
    assert resp.status_code == 200, resp.text
    script_name = resp.json()["script_name"]

    intruder_app = _build_app("ws_intruder", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=intruder_app), base_url="http://t") as c:
        listed = await c.get("/api/v1/sites")
        detail = await c.get(f"/api/v1/sites/{script_name}/domains")
    assert listed.status_code == 200
    assert listed.json() == []
    assert detail.status_code == 404


# --------------------------------------------------------------------------- #
# from-url — queue-only (the crawler is the next stacked slice)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_from_url_returns_202_queued(beanie_test_db, monkeypatch):
    """A valid URL queues: 202 {site_id, pocket_id, status:"queued"}, the draft Site
    doc exists NOT deployed, and its import_report carries the crawler-pending
    warning + queued status."""
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/import/from-url", json={"url": "https://example.com/landing"}
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["site_id"]
    assert body["pocket_id"]

    doc = await _SiteDoc.find_one({"workspace": "ws_owner", "pocket_id": body["pocket_id"]})
    assert doc is not None
    assert doc.deployed is False
    assert doc.import_report["status"] == "queued"
    assert doc.import_report["source_url"] == "https://example.com/landing"
    assert any("crawler" in w for w in doc.import_report["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["notaurl", "ftp://example.com", "https://", "  "])
async def test_from_url_invalid_shape_is_422(beanie_test_db, monkeypatch, bad):
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/import/from-url", json={"url": bad})
    assert resp.status_code == 422, resp.text
    await _assert_nothing_minted()


@pytest.mark.asyncio
async def test_crawler_seam_is_explicitly_unimplemented():
    """The crawl seam raises a clear NotImplementedError — the next stacked slice
    (SI-5) owns it; nothing in SI-4 may silently fetch."""
    with pytest.raises(NotImplementedError, match="next stacked slice"):
        await import_service.crawl_site_from_url(
            workspace_id="ws", user_id="u", site_id="s", url="https://example.com"
        )


# --------------------------------------------------------------------------- #
# generator payload — the cross-repo ``assets`` seam
# --------------------------------------------------------------------------- #


class _CapturingRunner:
    """Fake runner capturing generate()'s input_json; projectDir points at a real
    static tree so the html smoke passes (mirrors test_html_publish's spies)."""

    def __init__(self, project_dir: str) -> None:
        self._project_dir = project_dir
        self.input_json: dict | None = None

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.input_json = input_json
        return {"projectDir": self._project_dir, "engine": input_json.get("engine")}


def _gen_kwargs(source: dict[str, str], **extra) -> dict:
    return dict(
        engine="html",
        source=source,
        ripple_spec=None,
        theme={},
        site_id="site_import",
        title="Imported",
        capture_api_base="https://api.paw.example/api/v1",
        capture_signed_key="pp_tok_x",
        **extra,
    )


@pytest.mark.asyncio
async def test_generator_html_payload_carries_assets_sideband(tmp_path):
    """SI-4 seam: when an assets map is passed, the html STAGE-2 payload carries it
    on ``input.assets`` verbatim ({path: base64}), alongside the text source."""
    from pocketpaw_ee.sites.generator_client import GeneratorClient

    (tmp_path / "index.html").write_text("<html><body><p>ok</p></body></html>")
    runner = _CapturingRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    b64 = base64.b64encode(_PNG_BYTES).decode("ascii")
    await client.build(
        **_gen_kwargs({"index.html": "<html><body><p>ok</p></body></html>"}),
        assets={"img/logo.png": b64},
    )
    sent = runner.input_json
    assert sent is not None
    assert sent["engine"] == "html"
    assert sent["assets"] == {"img/logo.png": b64}


@pytest.mark.asyncio
async def test_generator_html_payload_omits_assets_when_none(tmp_path):
    """No assets → the key is ABSENT (a plain html publish's payload stays
    byte-identical to pre-SI-4)."""
    from pocketpaw_ee.sites.generator_client import GeneratorClient

    (tmp_path / "index.html").write_text("<html><body><p>ok</p></body></html>")
    runner = _CapturingRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    await client.build(**_gen_kwargs({"index.html": "<html><body><p>ok</p></body></html>"}))
    assert runner.input_json is not None
    assert "assets" not in runner.input_json


# ---- Review fixes: crafted/abnormal members 422 (contract), never 500 ----


async def test_import_zip_password_protected_member_is_422(beanie_test_db, monkeypatch):
    """An encrypted member raises RuntimeError inside the per-entry read; the
    contract maps it to sites.import_zip_invalid (422), not a 500."""
    app = _build_app("ws_owner", monkeypatch)
    data = bytearray(_zip_bytes({"index.html": _INDEX_HTML.encode()}))
    data[6] |= 0x01  # encryption bit, local file header
    cd = bytes(data).rfind(b"PK\x01\x02")
    data[cd + 8] |= 0x01  # encryption bit, central directory (zipfile reads this one)
    resp = await _post_zip(app, bytes(data))
    assert resp.status_code == 422, resp.text
    assert "import_zip_invalid" in resp.text
    await _assert_nothing_minted()


async def test_import_zip_unsupported_compression_is_422(beanie_test_db, monkeypatch):
    """An exotic compression method (AES marker 99) raises NotImplementedError on
    read; the contract maps it to 422, not a 500."""
    app = _build_app("ws_owner", monkeypatch)
    data = bytearray(_zip_bytes({"index.html": _INDEX_HTML.encode()}))
    data[8:10] = (99).to_bytes(2, "little")  # local header method field
    cd = bytes(data).rfind(b"PK\x01\x02")
    data[cd + 10 : cd + 12] = (99).to_bytes(2, "little")  # central directory too
    resp = await _post_zip(app, bytes(data))
    assert resp.status_code == 422, resp.text
    assert "import_zip_invalid" in resp.text
    await _assert_nothing_minted()


async def test_import_zip_control_char_entry_name_is_422(beanie_test_db, monkeypatch):
    """Control characters in an entry name (NUL, newline) are generator-side path
    input — rejected as unsafe, whole import fails closed."""
    app = _build_app("ws_owner", monkeypatch)
    data = _zip_bytes({"index.html": _INDEX_HTML.encode(), "a\nb.html": b"<p>x</p>"})
    resp = await _post_zip(app, data)
    assert resp.status_code == 422, resp.text
    assert "import_zip_entry_unsafe" in resp.text
    await _assert_nothing_minted()
