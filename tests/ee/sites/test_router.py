# tests/ee/sites/test_router.py — HTTP-layer tests for the Sites control-plane
# router. Created 2026-05-30 (feat/paw-sites-backend, RFC 12 follow-up item 4):
# covers GET /sites/{site_id}/domains end-to-end through a FastAPI app — the new
# tenant-scoped domains read backing the Domains tab's reload rehydration.
#
# Updated 2026-06-17 (feat/sites-svelte-component-edit, SE-2b): covers POST
# /sites/by-pocket/{pocket_id}/editable — the route republishes the pocket as
# editable. The handler delegates to the service, so the tests patch
# publish_pocket (the real generator would spawn bun) and assert the route
# resolves the builder origin in precedence (explicit body > Origin header >
# configured env fallback) and returns the editable SiteResponse.
#
# Auth wiring mirrors tests/cloud/test_ee_fabric_list_endpoints.py: the sites
# router gates on require_plan_feature("fabric") (router level) +
# require_action_any_workspace("fabric.read"), both of which resolve the caller
# via current_active_user / current_workspace_id, while the handler body reads
# ctx.workspace_id from request_context. So the app overrides all three plus
# require_license, and stubs get_workspace_plan -> "business" so the plan gate
# passes (fabric is a business+ feature). add_error_handler maps the service's
# NotFound (cross-tenant) to a 404.
#
# Updated 2026-06-17 (feat/sites-local-reserve): adds coverage for POST
# /sites/reserve — the manual "re-serve local sites" endpoint that (re)starts
# the local static server and returns the workspace's reconciled site list with
# fresh, valid urls. Gated like the other authed sites writes (fabric.write).

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.domain import CustomHostname, HostnameStatus


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    async def put_worker(self, *, script_name, bundle):
        return True

    async def create_custom_hostname(self, hostname):
        return CustomHostname(
            id="ch_1",
            hostname=hostname,
            status=HostnameStatus.PENDING,
            cname_target="zone_1.cdn.cloudflare.net",
        )

    async def get_hostname_status(self, hostname_id):
        return HostnameStatus.LIVE


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Member of the test workspace — fabric.read is MEMBER-tier."""

    def __init__(self, workspace_id: str) -> None:
        self.id = "user-test-1"
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="member")]


def _build_app(workspace_id: str, monkeypatch) -> FastAPI:
    from datetime import UTC, datetime

    import pocketpaw_ee.cloud.workspace.service as ws_svc
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="business"))

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
async def _seeded_site(beanie_test_db) -> Any:
    """Publish a site under ws_owner and attach one custom domain."""
    cf = _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws_owner",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Owner Site",
        _generator=_FakeGenerator(),
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    await sites_service.add_domain(
        workspace_id="ws_owner",
        site_id=site.id,
        hostname="www.owner-site.com",
        _cloudflare=cf,
    )
    return site


@pytest.mark.asyncio
async def test_get_domains_returns_owning_workspace_domains(_seeded_site, monkeypatch):
    site = _seeded_site
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/api/v1/sites/{site.script_name}/domains")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["hostname"] == "www.owner-site.com"
    assert body[0]["status"] == "pending"
    assert body[0]["cname_target"] == "zone_1.cdn.cloudflare.net"


@pytest.mark.asyncio
async def test_get_domains_cross_tenant_is_404(_seeded_site, monkeypatch):
    """A different workspace cannot read the owner's domains — tenant scoping in
    the service surfaces as a 404, never another tenant's data."""
    site = _seeded_site
    app = _build_app("ws_intruder", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/api/v1/sites/{site.script_name}/domains")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# by-pocket reads (pocketpaw#1345 backend half): the builder Preview tab calls
# GET /sites/by-pocket/{pocket_id}/preview (draft content to render) and
# GET /sites/by-pocket/{pocket_id}/status (draft/published + is_live). Both are
# authed fabric.read reads under the router-level fabric plan gate. The frontend
# (#432) already ships these calls; this exercises the missing backend half.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_by_pocket_returns_ripple_content(beanie_test_db, monkeypatch):
    """GET /sites/by-pocket/{id}/preview returns {pocket_id, engine, content} with
    the pocket's rippleSpec for a ripple pocket."""
    from unittest.mock import AsyncMock

    spec = {"type": "container", "ui": {"children": []}}
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value={"name": "x", "engine": "ripple", "rippleSpec": spec}),
    )
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk1/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"pocket_id": "pk1", "engine": "ripple", "content": spec}


@pytest.mark.asyncio
async def test_preview_by_pocket_missing_pocket_is_404(beanie_test_db, monkeypatch):
    """A missing / access-denied pocket surfaces as a 404 (the pockets service's
    NotFound flows through the standard error handler)."""
    from unittest.mock import AsyncMock

    from pocketpaw_ee.cloud._core.errors import NotFound

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(side_effect=NotFound("pocket", "pk_missing")),
    )
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_missing/preview")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_by_pocket_unpublished_is_draft(beanie_test_db, monkeypatch):
    """An unpublished pocket (no Site doc) reads {status: draft, is_live: false}."""
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_unpublished/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pocket_id"] == "pk_unpublished"
    assert body["status"] == "draft"
    assert body["is_live"] is False


@pytest.mark.asyncio
async def test_status_by_pocket_published_is_live(_seeded_site, monkeypatch):
    """A published pocket (the seeded site is under pk1 / ws_owner) reads
    {status: published, is_live: true}."""
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk1/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pocket_id"] == "pk1"
    assert body["status"] == "published"
    assert body["is_live"] is True


@pytest.mark.asyncio
async def test_post_reserve_returns_reconciled_list(beanie_test_db, monkeypatch):
    """POST /sites/reserve (re)starts the local server and returns the caller's
    workspace site list with fresh urls. The handler delegates to
    reserve_local_sites(ctx.workspace_id) then list_for_workspace, so the response
    carries every site's refreshed url."""
    from pocketpaw_ee.sites import local_server

    monkeypatch.setenv("PAW_SITES_LOCAL", "1")
    monkeypatch.setenv("PAW_SITES_LOCAL_DIR", str(__import__("tempfile").mkdtemp()))
    monkeypatch.delenv("PAW_SITES_LOCAL_PORT", raising=False)
    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)

    # Reset the singleton so this test gets a fresh ephemeral port.
    if local_server._server is not None:
        local_server._server.shutdown()
        local_server._server.server_close()
        local_server._server = None

    def fake_local_deploy(site_id: str, project_dir: str) -> str:
        dest = local_server.sites_home() / site_id
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        return local_server.local_url_for(site_id)

    site = await sites_service.publish(
        workspace_id="ws_owner",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Reserve Me",
        _generator=_FakeGenerator(),
        _bundle_reader=lambda d: b"x",
        _local_deploy=fake_local_deploy,
    )

    # Simulate a restart: the server publish started is gone.
    if local_server._server is not None:
        local_server._server.shutdown()
        local_server._server.server_close()
        local_server._server = None

    app = _build_app("ws_owner", monkeypatch)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.post("/api/v1/sites/reserve")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == str(site.id)
        # The url was re-served against the now-live local server.
        live_base = local_server.ensure_server()
        assert body[0]["url"] == f"{live_base}/{site.id}/"
    finally:
        if local_server._server is not None:
            local_server._server.shutdown()
            local_server._server.server_close()
            local_server._server = None


@pytest.mark.asyncio
async def test_make_editable_route_republishes_with_builder_origin(beanie_test_db, monkeypatch):
    """POST /sites/by-pocket/{pocket_id}/editable republishes the pocket as
    editable and returns the editable SiteResponse (builder_origin set). The real
    generator would spawn bun, so publish_pocket is patched to record the
    forwarded builder_origin."""
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    captured: dict[str, Any] = {}

    async def _fake_publish_pocket(*, builder_origin=None, **kw):
        captured["builder_origin"] = builder_origin
        return _SiteDoc(
            id=ObjectId(),
            workspace="ws_owner",
            pocket_id="pk1",
            owner="user-test-1",
            name="Owner Site",
            script_name="site1",
            deployed=True,
            signed_key="k",
            builder_origin=builder_origin or "",
        )

    monkeypatch.setattr(sites_service, "publish_pocket", _fake_publish_pocket)
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk1/editable",
            json={"builder_origin": "https://app.paw.example"},
        )
    assert resp.status_code == 200, resp.text
    assert captured["builder_origin"] == "https://app.paw.example"
    body = resp.json()
    assert body["pocket_id"] == "pk1"
    assert body["builder_origin"] == "https://app.paw.example"


@pytest.mark.asyncio
async def test_make_editable_route_empty_body_falls_back_to_config(beanie_test_db, monkeypatch):
    """An empty body works — the service falls back to the configured dashboard
    origin (PAW_SITES_BUILDER_ORIGIN)."""
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://configured.paw.example")

    async def _fake_publish_pocket(*, builder_origin=None, **kw):
        return _SiteDoc(
            id=ObjectId(),
            workspace="ws_owner",
            pocket_id="pk1",
            owner="user-test-1",
            name="Owner Site",
            script_name="site1",
            deployed=True,
            signed_key="k",
            builder_origin=builder_origin or "",
        )

    # make_site_editable computes the origin (the PAW_SITES_BUILDER_ORIGIN
    # fallback) BEFORE it calls publish_pocket, so patching publish_pocket still
    # exercises the genuine fallback and the resolved origin reaches the response.
    monkeypatch.setattr(sites_service, "publish_pocket", _fake_publish_pocket)
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # No Origin header and no body field -> the configured env fallback fires.
        resp = await c.post("/api/v1/sites/by-pocket/pk1/editable", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["builder_origin"] == "https://configured.paw.example"


@pytest.mark.asyncio
async def test_make_editable_route_uses_origin_header(beanie_test_db, monkeypatch):
    """When the body carries no builder_origin, the route derives it from the
    request's Origin header — the dashboard origin the SE-3 editor calls from."""
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    captured: dict[str, Any] = {}

    async def _fake_publish_pocket(*, builder_origin=None, **kw):
        captured["builder_origin"] = builder_origin
        return _SiteDoc(
            id=ObjectId(),
            workspace="ws_owner",
            pocket_id="pk1",
            owner="user-test-1",
            name="Owner Site",
            script_name="site1",
            deployed=True,
            signed_key="k",
            builder_origin=builder_origin or "",
        )

    monkeypatch.setattr(sites_service, "publish_pocket", _fake_publish_pocket)
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk1/editable",
            json={},
            headers={"Origin": "https://dash.paw.example"},
        )
    assert resp.status_code == 200, resp.text
    assert captured["builder_origin"] == "https://dash.paw.example"
    assert resp.json()["builder_origin"] == "https://dash.paw.example"


@pytest.mark.asyncio
async def test_make_editable_route_body_beats_origin_header(beanie_test_db, monkeypatch):
    """An explicit builder_origin in the body wins over the Origin header — it's
    an override the editor can pass to publish against a different dashboard."""
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    captured: dict[str, Any] = {}

    async def _fake_publish_pocket(*, builder_origin=None, **kw):
        captured["builder_origin"] = builder_origin
        return _SiteDoc(
            id=ObjectId(),
            workspace="ws_owner",
            pocket_id="pk1",
            owner="user-test-1",
            name="Owner Site",
            script_name="site1",
            deployed=True,
            signed_key="k",
            builder_origin=builder_origin or "",
        )

    monkeypatch.setattr(sites_service, "publish_pocket", _fake_publish_pocket)
    app = _build_app("ws_owner", monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk1/editable",
            json={"builder_origin": "https://explicit.paw.example"},
            headers={"Origin": "https://dash.paw.example"},
        )
    assert resp.status_code == 200, resp.text
    assert captured["builder_origin"] == "https://explicit.paw.example"
