# tests/ee/sites/test_router.py — HTTP-layer tests for the Sites control-plane
# router. Created 2026-05-30 (feat/paw-sites-backend, RFC 12 follow-up item 4):
# covers GET /sites/{site_id}/domains end-to-end through a FastAPI app — the new
# tenant-scoped domains read backing the Domains tab's reload rehydration.
#
# Auth wiring mirrors tests/cloud/test_ee_fabric_list_endpoints.py: the sites
# router gates on require_plan_feature("fabric") (router level) +
# require_action_any_workspace("fabric.read"), both of which resolve the caller
# via current_active_user / current_workspace_id, while the handler body reads
# ctx.workspace_id from request_context. So the app overrides all three plus
# require_license, and stubs get_workspace_plan -> "business" so the plan gate
# passes (fabric is a business+ feature). add_error_handler maps the service's
# NotFound (cross-tenant) to a 404.

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
