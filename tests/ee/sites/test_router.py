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
# router gates on require_plan_feature("sites") (router level) +
# require_action_any_workspace("fabric.read"), both of which resolve the caller
# via current_active_user / current_workspace_id, while the handler body reads
# ctx.workspace_id from request_context. So the app overrides all three plus
# require_license, and stubs get_workspace_plan -> "go" so the plan gate
# passes (fabric is a business+ feature). add_error_handler maps the service's
# NotFound (cross-tenant) to a 404.
#
# Updated 2026-06-17 (feat/sites-local-reserve): adds coverage for POST
# /sites/reserve — the manual "re-serve local sites" endpoint that (re)starts
# the local static server and returns the workspace's reconciled site list with
# fresh, valid urls. Gated like the other authed sites writes (fabric.write).
#
# Updated 2026-06-18 (feat/branch-primitive-audit, BP-7): adds coverage for POST
# /sites/by-pocket/{pocket_id}/audit — the deterministic site audit. The handler
# delegates to sites_service.audit_pocket, which reads the pocket via the pockets
# service (mocked here, as the preview/status tests do) and runs the pure audit
# engine. Asserts a site with known issues surfaces findings (each with a
# fix_prompt), a clean site returns an empty list, and a missing pocket is a 404.
#
# Updated 2026-06-19 (P2b-backend): adds coverage for POST
# /sites/by-pocket/{pocket_id}/versions/{version_no}/revert — revert to a prior
# version by ordinal. The handler delegates to sites_service.revert_pocket_version
# (over the real versions spine) and returns the new draft as a SiteVersionResponse;
# an unknown version_no is a 404. Also asserts the seeded-site status response now
# carries a ``deployed_at`` ISO string (None before any deploy).
#
# Updated 2026-07-01 (NE-4b — native-editing leaf-edit persist): adds coverage for
# POST /sites/by-pocket/{pocket_id}/leaf-edits. The happy path creates a REAL svelte
# pocket, fakes ONLY the apply-leaf-edit CLI (generator_client.apply_leaf_edits), and
# asserts the endpoint returns per-uid verdicts AND persists the edit through the real
# service→pockets path. A non-svelte pocket is a 422; a missing pocket is a 404.
#
# Updated 2026-07-01 (NE-5b — native-artifact endpoint): adds coverage for GET
# /sites/by-pocket/{pocket_id}/native-artifact. The happy path patches
# sites_service.get_native_artifact (a real build would spawn bun) and asserts the
# route returns {pocket_id, body_html, css} AND threads the request Origin header as
# the builder_origin the armed build needs. A non-svelte pocket (real service, mocked
# ripple pocket read) is a 422.
#
# Updated 2026-07-17 (fix/sites-prewarm-origin): POST /sites/publish and POST
# /sites/by-pocket/{pocket_id}/leaf-edits now thread the request Origin header into the
# service as ``prewarm_origin`` so the background native-artifact pre-warm builds with
# the same origin a browser view resolves. New tests patch publish_pocket /
# apply_leaf_edits and assert the route forwards the request Origin as prewarm_origin
# (and, for publish, that builder_origin is NOT set from the header — the public deploy
# stays plain), plus the no-Origin-header case forwards None.

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
    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True

    async def create_custom_hostname(self, hostname, *, features=None):
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


def _build_app(workspace_id: str) -> FastAPI:
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.router import router as sites_router

    # NO plan patch here. The tree's conftest already patches `get_workspace_plan` for
    # every test in `tests/ee/sites/` (`_default_sites_plan`), so this module's own
    # `monkeypatch.setattr` on the SAME target was redundant — and actively harmful.
    #
    # Two patches on one attribute unwind in the wrong order. The conftest's `patch()`
    # starts first and saves the REAL function; `monkeypatch.setattr` then runs and
    # saves the conftest's MOCK as its "original". At teardown the conftest's patch
    # exits first and correctly restores the real function — and then monkeypatch undoes
    # its own, putting the mock back. The mock outlives every fixture that installed it
    # and stays for the rest of the process.
    #
    # Measured 2026-08-12: this leaked ACROSS TEST TREES. With `tests/ee/sites/` ahead of
    # `tests/cloud/sites/test_site_billing.py` in the run, a `free` workspace read back
    # as `go`, so the entitlement test's `pytest.raises(Forbidden)` saw a publish that
    # was cheerfully allowed. The failure named the billing test and pointed at the plan
    # gate — neither of which had anything to do with it.
    #
    # `monkeypatch` is deliberately NOT a parameter any more: this function patches
    # nothing, and an unused fixture arg is an invitation to patch that target again.

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
    app = _build_app("ws_owner")
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
    app = _build_app("ws_intruder")
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

    spec = {"type": "container", "ui": {"children": []}}
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value={"name": "x", "engine": "ripple", "rippleSpec": spec}),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk1/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"pocket_id": "pk1", "engine": "ripple", "content": spec}


@pytest.mark.asyncio
async def test_preview_by_pocket_missing_pocket_is_404(beanie_test_db, monkeypatch):
    """A missing / access-denied pocket surfaces as a 404 (the pockets service's
    NotFound flows through the standard error handler)."""

    from pocketpaw_ee.cloud._core.errors import NotFound

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(side_effect=NotFound("pocket", "pk_missing")),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_missing/preview")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_status_by_pocket_unpublished_is_draft(beanie_test_db, monkeypatch):
    """An unpublished pocket (no Site doc) reads {status: draft, is_live: false}."""
    app = _build_app("ws_owner")
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
    {status: published, is_live: true} and carries a deployed_at ISO string (P2b)."""
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk1/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pocket_id"] == "pk1"
    assert body["status"] == "published"
    assert body["is_live"] is True
    # P2b: the deployed site carries a last-deploy ISO timestamp.
    assert body["deployed_at"] is not None
    from datetime import datetime

    datetime.fromisoformat(body["deployed_at"])


@pytest.mark.asyncio
async def test_status_by_pocket_unpublished_deployed_at_is_none(beanie_test_db, monkeypatch):
    """P2b: an unpublished pocket (no Site doc) reads deployed_at None — the DTO
    exposes None before the first deploy."""
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_never_deployed/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deployed_at"] is None


@pytest.mark.asyncio
async def test_dev_preview_by_pocket_returns_url(beanie_test_db, monkeypatch):
    """POST /sites/by-pocket/{id}/dev-preview returns {pocket_id, url} — the live
    Vite dev-server URL for the editing preview (P2a). The route delegates to
    sites_service.dev_preview_pocket → the DevServerManager singleton; we inject a
    manager built with fake spawn/port/materialize seams so no real vite is spawned,
    exercising the real service→manager→endpoint path end to end.

    S1: the fake materialize also records the ``builder_origin`` the manager forwards
    so we assert the endpoint resolves the request ``Origin`` header (the dev source
    then carries the gated edit-bridge), mirroring the /editable origin precedence."""
    import pocketpaw_ee.sites.dev_server as dev_server_mod
    from pocketpaw_ee.sites.dev_server import DevServerManager

    seen: dict = {}

    async def _fake_materialize(*, workspace_id, user_id, pocket_id, builder_origin=None):
        seen["builder_origin"] = builder_origin
        return f"/tmp/site-builds/{pocket_id}"

    async def _fake_spawn(cmd, cwd, port):
        class _P:
            returncode = None

            def terminate(self):
                pass

            def kill(self):
                pass

            async def wait(self):
                return 0

        return _P()

    mgr = DevServerManager(
        _spawn=_fake_spawn, _free_port=lambda: 41234, _materialize=_fake_materialize
    )
    monkeypatch.setattr(dev_server_mod, "_MANAGER", mgr)

    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk1/dev-preview",
            headers={"origin": "https://dash.paw.example"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"pocket_id": "pk1", "url": "http://127.0.0.1:41234/"}
    # The endpoint actually started a server in the singleton.
    assert mgr.live_pocket_ids() == ["pk1"]
    # S1: the request Origin header was sourced + threaded to materialize (same
    # precedence /editable uses), so the dev source carries the edit-bridge.
    assert seen["builder_origin"] == "https://dash.paw.example"
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_dev_preview_by_pocket_defaults_origin_from_config(beanie_test_db, monkeypatch):
    """S1: with NO request Origin header, the dev-preview endpoint falls back to the
    configured PAW_SITES_BUILDER_ORIGIN (the same env fallback /editable uses), so the
    dev source is still bridged when the call carries no Origin."""
    import pocketpaw_ee.sites.dev_server as dev_server_mod
    from pocketpaw_ee.sites.dev_server import DevServerManager

    monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://configured.paw.example")
    seen: dict = {}

    async def _fake_materialize(*, workspace_id, user_id, pocket_id, builder_origin=None):
        seen["builder_origin"] = builder_origin
        return f"/tmp/site-builds/{pocket_id}"

    async def _fake_spawn(cmd, cwd, port):
        class _P:
            returncode = None

            def terminate(self):
                pass

            def kill(self):
                pass

            async def wait(self):
                return 0

        return _P()

    mgr = DevServerManager(
        _spawn=_fake_spawn, _free_port=lambda: 41235, _materialize=_fake_materialize
    )
    monkeypatch.setattr(dev_server_mod, "_MANAGER", mgr)

    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/by-pocket/pk1/dev-preview")
    assert resp.status_code == 200, resp.text
    assert seen["builder_origin"] == "https://configured.paw.example"
    await mgr.stop_all()


@pytest.mark.asyncio
async def test_dev_preview_by_pocket_missing_pocket_is_404(beanie_test_db, monkeypatch):
    """A missing / access-denied pocket surfaces as a 404: the DEFAULT materialize
    reads the pocket via the pockets service, whose NotFound flows through the
    standard error handler."""

    import pocketpaw_ee.sites.dev_server as dev_server_mod
    from pocketpaw_ee.cloud._core.errors import NotFound
    from pocketpaw_ee.sites.dev_server import DevServerManager

    # Fresh singleton with the REAL materialize so it hits the pockets service.
    monkeypatch.setattr(dev_server_mod, "_MANAGER", DevServerManager())
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(side_effect=NotFound("pocket", "pk_missing")),
    )

    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/by-pocket/pk_missing/dev-preview")
    assert resp.status_code == 404


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

    app = _build_app("ws_owner")
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
    app = _build_app("ws_owner")
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
    app = _build_app("ws_owner")
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
    app = _build_app("ws_owner")
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
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk1/editable",
            json={"builder_origin": "https://explicit.paw.example"},
            headers={"Origin": "https://dash.paw.example"},
        )
    assert resp.status_code == 200, resp.text
    assert captured["builder_origin"] == "https://explicit.paw.example"


# ---------------------------------------------------------------------------
# audit (BP-7): POST /sites/by-pocket/{pocket_id}/audit runs the deterministic
# site audit and returns findings, each with a fix_prompt the UI feeds to the
# existing edit path. fabric.read; tenant-scoped; a missing pocket is a 404.
# ---------------------------------------------------------------------------

# A svelte source with three known issues: an <img> without alt, an empty href,
# and a head that lacks <title> / meta description / Open Graph.
_AUDIT_DIRTY_SOURCE = {
    "src/app.html": "<!doctype html><html><head></head><body></body></html>",
    "src/lib/components/Hero.svelte": (
        "<section><h1>Hi</h1><img src='/hero.jpg'/><a href=''>Dead link</a></section>"
    ),
}


@pytest.mark.asyncio
async def test_audit_by_pocket_surfaces_known_issues(beanie_test_db, monkeypatch):
    """A site with an img-without-alt, an empty href, and a missing title surfaces
    a finding for each — each carrying a non-empty fix_prompt the UI can feed to
    the existing edit path."""

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value={"name": "x", "engine": "svelte", "source": _AUDIT_DIRTY_SOURCE}),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/by-pocket/pk_dirty/audit")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pocket_id"] == "pk_dirty"
    assert body["engine"] == "svelte"
    checks = {f["check"] for f in body["findings"]}
    assert "a11y.img_alt" in checks
    assert "links.placeholder" in checks
    assert "seo.title" in checks
    # Every finding carries a usable fix_prompt + a location file.
    for f in body["findings"]:
        assert f["fix_prompt"].strip()
        assert f["location"]["file"]


@pytest.mark.asyncio
async def test_audit_by_pocket_clean_site_has_no_findings(beanie_test_db, monkeypatch):
    """A clean site returns an empty findings list."""

    clean = {
        "src/app.html": (
            "<!doctype html><html><head>"
            "<title>Bright Smile Dental</title>"
            "<meta name='description' content='A whiter, healthier smile with modern dentistry.'>"
            "<meta property='og:title' content='Bright Smile'>"
            "<meta property='og:image' content='https://x.example/og.png'>"
            "</head><body></body></html>"
        ),
        "src/lib/components/Hero.svelte": (
            "<section><h1>Brighter Smiles</h1>"
            "<img src='/hero.jpg' alt='A patient smiling'/>"
            "<a href='/book'>Book a consult</a></section>"
        ),
    }
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value={"name": "x", "engine": "svelte", "source": clean}),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/by-pocket/pk_clean/audit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["findings"] == []


@pytest.mark.asyncio
async def test_audit_by_pocket_missing_pocket_is_404(beanie_test_db, monkeypatch):
    """A missing / access-denied pocket surfaces as a 404 (the pockets service's
    NotFound flows through the standard error handler)."""

    from pocketpaw_ee.cloud._core.errors import NotFound

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(side_effect=NotFound("pocket", "pk_missing")),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/by-pocket/pk_missing/audit")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# revert (P2b-backend): POST /sites/by-pocket/{pocket_id}/versions/{version_no}/revert
# reverts a pocket's site to a prior version by ordinal — it writes a NEW
# forward-moving draft snapshot of the target version and returns it as a
# SiteVersionResponse. fabric.write; tenant-scoped; an unknown version_no is a 404.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revert_by_pocket_creates_draft(beanie_test_db, monkeypatch):
    """POST .../versions/{n}/revert returns 200 with a NEW draft whose content is a
    snapshot of version n — the normal review/publish flow then applies."""
    from pocketpaw_ee.versions import service as versions

    v1 = await versions.write_draft(
        scope_type="pocket", scope_id="pk_rev", workspace_id="ws_owner", content={"v": "one"}
    )
    await versions.publish(
        scope_type="pocket", scope_id="pk_rev", workspace_id="ws_owner", version_id=str(v1.id)
    )
    await versions.write_draft(
        scope_type="pocket", scope_id="pk_rev", workspace_id="ws_owner", content={"v": "two"}
    )

    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/api/v1/sites/by-pocket/pk_rev/versions/{v1.version_no}/revert")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # A new draft row, forward of v1, labelled as a revert.
    assert body["status"] == "draft"
    assert body["version_no"] > v1.version_no
    assert body["label"] == f"Revert to v{v1.version_no}"

    # The new draft carries v1's content (verified through the versions spine).
    draft = await versions.get_draft(scope_type="pocket", scope_id="pk_rev")
    assert draft is not None
    assert str(draft.id) == body["id"]
    assert draft.content == {"v": "one"}


@pytest.mark.asyncio
async def test_revert_by_pocket_unknown_version_is_404(beanie_test_db, monkeypatch):
    """An unknown version_no → 404 (the service ValueError maps to a 404)."""
    from pocketpaw_ee.versions import service as versions

    await versions.write_draft(
        scope_type="pocket", scope_id="pk_rev2", workspace_id="ws_owner", content={"v": 1}
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/by-pocket/pk_rev2/versions/999/revert")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revert_by_pocket_cross_tenant_is_404(beanie_test_db, monkeypatch):
    """A version that exists only under ANOTHER workspace is a 404 for this caller —
    the service's tenant filter treats it as no such version."""
    from pocketpaw_ee.versions import service as versions

    foreign = await versions.write_draft(
        scope_type="pocket", scope_id="pk_rev3", workspace_id="ws_other", content={"v": 1}
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/api/v1/sites/by-pocket/pk_rev3/versions/{foreign.version_no}/revert")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# data view (DS-3): GET /sites/by-pocket/{pocket_id}/data lists a dynamic site's
# tables (from the spec's objects), and GET .../data/{table} reads one table's
# rows. fabric.read; tenant-scoped; a non-dynamic pocket is a 422; an unknown
# table is a 404; local mode degrades cleanly (available=False).
# ---------------------------------------------------------------------------

_DATA_DYNAMIC_WIRE = {
    "name": "Guestbook",
    "pattern": "dynamic",
    "rippleSpec": {
        "type": "container",
        "objects": [
            {
                "name": "entry",
                "fields": {"id": "text", "name": "text", "message": "text"},
                "primaryKey": "id",
            }
        ],
    },
}


@pytest.mark.asyncio
async def test_data_tables_by_pocket_lists_schema(beanie_test_db, monkeypatch):
    """GET .../data lists the dynamic site's tables from the spec's objects; in
    local mode (no CF creds) available=False but the schema is still listed."""

    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value=_DATA_DYNAMIC_WIRE),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_dyn/data")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pocket_id"] == "pk_dyn"
    assert body["available"] is False
    assert body["reason"] == "live_on_cloudflare_only"
    assert [t["name"] for t in body["tables"]] == ["entry"]


@pytest.mark.asyncio
async def test_data_tables_by_pocket_non_dynamic_is_422(beanie_test_db, monkeypatch):
    """A NON-dynamic pocket has no data store → 422 (not_dynamic)."""

    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value={"name": "x", "pattern": "landing", "rippleSpec": {"type": "c"}}),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_landing/data")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_data_rows_by_pocket_local_mode_degrades(beanie_test_db, monkeypatch):
    """GET .../data/{table} in local mode returns the clean unavailable shape with
    the table's declared columns listed and no rows."""

    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("PAW_SITES_LOCAL", raising=False)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value=_DATA_DYNAMIC_WIRE),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_dyn/data/entry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["table"] == "entry"
    assert body["available"] is False
    assert body["columns"] == ["id", "name", "message"]
    assert body["rows"] == []


@pytest.mark.asyncio
async def test_data_rows_by_pocket_unknown_table_is_404(beanie_test_db, monkeypatch):
    """An unknown table is rejected with a 404 (the SQL-safety gate)."""

    monkeypatch.delenv("PAW_CF_ACCOUNT_ID", raising=False)
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value=_DATA_DYNAMIC_WIRE),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_dyn/data/unknown_table")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# native-editing leaf edits (NE-4b): POST /sites/by-pocket/{pocket_id}/leaf-edits
# persists the native editor's forwarded {uid, op} edits as a reviewable Branch
# draft (splice via the apply-leaf-edit CLI → set_svelte_source_file), NO rebuild.
# fabric.write; tenant-scoped; a non-svelte pocket is a 422; a missing pocket a 404.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaf_edits_route_persists_and_returns_verdicts(beanie_test_db, monkeypatch):
    """The endpoint splices + persists a real svelte pocket's edit and returns one
    verdict per uid. Only the external Bun CLI is faked; the service→pockets persist
    runs for real, so a re-read shows the new source."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws_owner",
        owner_id="user-test-1",
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source={
            "src/lib/components/Hero.svelte": "<h1>Bright Smile</h1>",
            "src/app.css": ":root{}",
        },
        trusted=True,
    )
    assert err is None, err

    async def _fake_apply(*, source, edits):
        new = dict(source)
        new["src/lib/components/Hero.svelte"] = "<h1>Brighter</h1>"
        return {"source": new, "results": [{"uid": edits[0]["uid"], "applied": True}]}

    monkeypatch.setattr("pocketpaw_ee.sites.generator_client.apply_leaf_edits", _fake_apply)

    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            f"/api/v1/sites/by-pocket/{pocket_id}/leaf-edits",
            json={
                "edits": [{"uid": "Hero:headline:0", "op": {"kind": "setText", "html": "Brighter"}}]
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pocket_id"] == pocket_id
    assert body["results"] == [{"uid": "Hero:headline:0", "applied": True, "reason": None}]
    # The edit persisted through the real service → pockets path.
    wire = await pockets_service.get(pocket_id, "user-test-1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == "<h1>Brighter</h1>"


@pytest.mark.asyncio
async def test_leaf_edits_route_non_svelte_is_422(beanie_test_db, monkeypatch):
    """A ripple pocket has no svelte source map → 422 (the service ValidationError
    maps to a 422); the CLI bridge is never reached."""

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value={"name": "x", "engine": "ripple", "rippleSpec": {"type": "c"}}),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk_ripple/leaf-edits",
            json={"edits": [{"uid": "x:0", "op": {"kind": "setText", "html": "y"}}]},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_leaf_edits_route_missing_pocket_is_404(beanie_test_db, monkeypatch):
    """A missing / access-denied pocket surfaces as a 404 (the pockets service's
    NotFound flows through the standard error handler)."""

    from pocketpaw_ee.cloud._core.errors import NotFound

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(side_effect=NotFound("pocket", "pk_missing")),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk_missing/leaf-edits",
            json={"edits": [{"uid": "x:0", "op": {"kind": "setText", "html": "y"}}]},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# native artifact (NE-5b): GET /sites/by-pocket/{pocket_id}/native-artifact serves
# the armed svelte build's <body> inner HTML + concatenated CSS as {pocket_id,
# body_html, css} so the native editor shadow-renders the site. fabric.write (it
# ensures/triggers the armed build); builder_origin from the request Origin header;
# a non-svelte pocket is a 422.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_artifact_route_returns_body_and_css(beanie_test_db, monkeypatch):
    """The endpoint returns {pocket_id, body_html, css} and threads the request Origin
    header as the builder_origin (the armed build needs it to stamp data-uid + the
    manifest). The service is patched — a real build would spawn bun."""
    captured: dict[str, Any] = {}

    async def _fake_get_native_artifact(*, builder_origin=None, **kw):
        captured["builder_origin"] = builder_origin
        captured["pocket_id"] = kw.get("pocket_id")
        return {
            "pocket_id": "pk1",
            "body_html": '<h1 data-uid="Hero:headline:0">Hi</h1>',
            "css": ".hero{color:#0A84FF}",
        }

    monkeypatch.setattr(sites_service, "get_native_artifact", _fake_get_native_artifact)
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(
            "/api/v1/sites/by-pocket/pk1/native-artifact",
            headers={"Origin": "https://dash.paw.example"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pocket_id"] == "pk1"
    assert "data-uid" in body["body_html"]
    assert body["css"] == ".hero{color:#0A84FF}"
    # The route derived builder_origin from the request Origin header and forwarded
    # the path's pocket_id.
    assert captured["builder_origin"] == "https://dash.paw.example"
    assert captured["pocket_id"] == "pk1"


@pytest.mark.asyncio
async def test_native_artifact_route_carries_the_build_pending_shape(beanie_test_db, monkeypatch):
    """SP-2: a cold miss answers with a build to POLL, and the route has to carry the
    handle. ``NativeArtifactResponse`` defaults ``build_status`` to "none", so a response
    model that had not been widened would 200 with the pending fields silently dropped —
    the client would see an empty render and no reason, forever."""

    async def _pending(**kw):
        return {
            "pocket_id": "pk1",
            "body_html": "",
            "css": "",
            "build_status": "queued",
            "build_reason": None,
            "build_job_id": "site-preview-pk1-deadbeef",
        }

    monkeypatch.setattr(sites_service, "get_native_artifact", _pending)
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk1/native-artifact")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["build_status"] == "queued"
    assert body["build_job_id"] == "site-preview-pk1-deadbeef"
    assert body["body_html"] == ""


@pytest.mark.asyncio
async def test_native_artifact_route_non_svelte_is_422(beanie_test_db, monkeypatch):
    """A ripple pocket has no svelte build → 422 (the service ValidationError maps to
    422); no build is triggered (the service raises before constructing a generator)."""

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.pockets.service.get",
        AsyncMock(return_value={"name": "x", "engine": "ripple", "rippleSpec": {"type": "c"}}),
    )
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_ripple/native-artifact")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# fix/sites-prewarm-origin: POST /sites/publish + POST .../leaf-edits thread the
# request Origin header into the service as ``prewarm_origin`` so the background
# native-artifact pre-warm warms the origin a browser view will ask for. The publish
# route must NOT arm the public deploy from the header (builder_origin stays unset).
# ---------------------------------------------------------------------------


def _stub_published_site_doc():
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

    return _SiteDoc(
        id=ObjectId(),
        workspace="ws_owner",
        pocket_id="pk1",
        owner="user-test-1",
        name="Owner Site",
        script_name="site1",
        deployed=True,
        signed_key="k",
        builder_origin="",
    )


@pytest.mark.asyncio
async def test_publish_route_threads_origin_as_prewarm_origin(beanie_test_db, monkeypatch):
    """POST /sites/publish forwards the request Origin header as ``prewarm_origin`` (so
    the pre-warm warms the origin a browser view resolves) WITHOUT setting
    ``builder_origin`` from it — the public deploy stays plain. publish_pocket is patched
    (a real publish would spawn bun)."""
    captured: dict[str, Any] = {}

    async def _fake_publish_pocket(*, prewarm_origin=None, builder_origin=None, **kw):
        captured["prewarm_origin"] = prewarm_origin
        captured["builder_origin"] = builder_origin
        return _stub_published_site_doc()

    monkeypatch.setattr(sites_service, "publish_pocket", _fake_publish_pocket)
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/publish",
            json={"pocket_id": "pk1"},
            headers={"Origin": "https://paw.hzd.interacly.com"},
        )
    assert resp.status_code == 200, resp.text
    assert captured["prewarm_origin"] == "https://paw.hzd.interacly.com"
    # The public deploy is unchanged — the header must NOT arm it.
    assert captured["builder_origin"] in (None, "")


@pytest.mark.asyncio
async def test_publish_route_no_origin_header_prewarm_origin_none(beanie_test_db, monkeypatch):
    """POST /sites/publish with NO Origin header forwards ``prewarm_origin=None`` so the
    pre-warm keeps its PAW_SITES_BUILDER_ORIGIN env fallback (a call with no browser
    origin, e.g. server-to-server)."""
    captured: dict[str, Any] = {}

    async def _fake_publish_pocket(*, prewarm_origin=None, **kw):
        captured["prewarm_origin"] = prewarm_origin
        return _stub_published_site_doc()

    monkeypatch.setattr(sites_service, "publish_pocket", _fake_publish_pocket)
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post("/api/v1/sites/publish", json={"pocket_id": "pk1"})
    assert resp.status_code == 200, resp.text
    assert captured["prewarm_origin"] is None


@pytest.mark.asyncio
async def test_leaf_edits_route_threads_origin_as_prewarm_origin(beanie_test_db, monkeypatch):
    """POST /sites/by-pocket/{id}/leaf-edits forwards the request Origin header as
    ``prewarm_origin`` so the background native-artifact pre-warm warms the origin the
    browser's native-artifact view (called from the same dashboard) will ask for.
    apply_leaf_edits is patched to capture the forwarded origin."""
    captured: dict[str, Any] = {}

    async def _fake_apply_leaf_edits(*, prewarm_origin=None, **kw):
        captured["prewarm_origin"] = prewarm_origin
        return [{"uid": "Hero:headline:0", "applied": True, "reason": None}]

    monkeypatch.setattr(sites_service, "apply_leaf_edits", _fake_apply_leaf_edits)
    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/sites/by-pocket/pk1/leaf-edits",
            json={"edits": [{"uid": "Hero:headline:0", "op": {"kind": "setText", "html": "y"}}]},
            headers={"Origin": "https://paw.hzd.interacly.com"},
        )
    assert resp.status_code == 200, resp.text
    assert captured["prewarm_origin"] == "https://paw.hzd.interacly.com"


def test_zz_no_module_double_patches_the_plan_gate():
    """No module in this tree may patch ``get_workspace_plan`` while the conftest is
    also patching it. Two patches on one attribute unwind in the wrong order and the
    mock outlives every fixture that installed it.

    That is not a theory. It escaped this whole tree and broke
    ``tests/cloud/sites/test_site_billing.py``'s entitlement test, where a ``free``
    workspace read back as ``go`` and a publish that should have been Forbidden was
    allowed. The failure named a test and a subsystem that were both innocent, and it
    only appeared when the two trees ran in the same process — so nobody running one
    file ever saw it. FIVE modules here had the same double-patch.

    Checked by SCANNING THE TREE'S SOURCE, not at runtime: the conftest's own legitimate
    patch is active during any test, so a runtime probe cannot tell a correct patch from
    a doubled one. The source can.

    A module that genuinely needs a plan other than the conftest's ``go`` opts OUT of the
    default (``_OWNS_ITS_OWN_PLAN_PATCH`` in the conftest) and then owns the target
    alone — never both.
    """
    import pathlib as _p

    tree = _p.Path(__file__).parent
    conftest = (tree / "conftest.py").read_text(encoding="utf-8")
    opted_out = {
        line.split('"')[1]
        for line in conftest.splitlines()
        if line.strip().startswith('"test_') and "#" in line
    }

    offenders: dict[str, list[str]] = {}
    for path in sorted(tree.glob("test_*.py")):
        if path.stem in opted_out:
            continue
        hits = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if "get_workspace_plan" in line
            and ("setattr(" in line or "patch(" in line)
            and not line.lstrip().startswith("#")
        ]
        if hits:
            offenders[path.name] = hits

    assert not offenders, (
        "these modules patch get_workspace_plan while the conftest also patches it; the "
        "two unwind in the wrong order and leak a permissive plan gate into every later "
        f"test in the run: {offenders}. Either drop the patch and take the conftest "
        "default, or add the module to _OWNS_ITS_OWN_PLAN_PATCH in conftest.py."
    )


# ---------------------------------------------------------------------------
# GET /sites/by-pocket/{pocket_id}/versions — the owner-facing timeline. What it
# puts on the wire is read by a person, so a raw status token or a raw user id
# is a defect here even though both are technically correct.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_versions_timeline_names_the_author(beanie_test_db, monkeypatch):
    """The author goes over the wire as a name, not the ObjectId on the row.

    ``author`` is stored as ``str(user.id)``, so every row in the timeline was
    captioned with 24 characters of hex. One batched lookup covers the whole
    timeline; an id the resolver cannot name keeps its raw value rather than
    going blank.
    """
    from pocketpaw_ee.sites import router as sites_router
    from pocketpaw_ee.versions import service as versions

    await versions.write_draft(
        scope_type="pocket",
        scope_id="pk_names",
        workspace_id="ws_owner",
        content={"v": 1},
        author="6a1f0c2e4b09d3f1a2b3c4d5",
    )
    await versions.write_draft(
        scope_type="pocket",
        scope_id="pk_names",
        workspace_id="ws_owner",
        content={"v": 2},
        author="an-agent",  # not an ObjectId — the resolver skips these
    )

    async def _fake_names(user_ids):
        assert user_ids == {"6a1f0c2e4b09d3f1a2b3c4d5", "an-agent"}, user_ids
        return {"6a1f0c2e4b09d3f1a2b3c4d5": "Jo Blogs"}

    monkeypatch.setattr(sites_router, "resolve_display_names", _fake_names)

    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_names/versions")
    assert resp.status_code == 200, resp.text

    authors = [v["author"] for v in resp.json()["versions"]]
    assert authors == ["Jo Blogs", "an-agent"]


@pytest.mark.asyncio
async def test_versions_timeline_splits_legacy_reverted_rows(beanie_test_db, monkeypatch):
    """Rows written before the lifecycle split do not reach the client saying
    "reverted" — the endpoint resolves them by lineage on the way out.

    v1 has a descendant, so an edit replaced it; v2 is where the lineage
    stopped. Both are stored as the same word.
    """
    from pocketpaw_ee.versions import service as versions
    from pocketpaw_ee.versions.models import ArtifactVersion

    v1 = await versions.write_draft(
        scope_type="pocket", scope_id="pk_legacy", workspace_id="ws_owner", content={"v": 1}
    )
    v2 = await versions.write_draft(
        scope_type="pocket", scope_id="pk_legacy", workspace_id="ws_owner", content={"v": 2}
    )
    for row_id in (v1.id, v2.id):
        row = await ArtifactVersion.get(row_id)
        assert row is not None
        row.status = "reverted"
        await row.save()

    app = _build_app("ws_owner")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/v1/sites/by-pocket/pk_legacy/versions")
    assert resp.status_code == 200, resp.text

    statuses = [v["status"] for v in resp.json()["versions"]]
    assert statuses == ["superseded", "discarded"]
    assert "reverted" not in statuses
