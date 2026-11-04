# tests/ee/sites/site_ai_harness.py: shared fixtures for the site AI suites.
#
# Builds a FastAPI app over the site AI routers with a stubbed session (role per
# test), inserts pockets and sites, and exposes ``WS``/``OTHER_WS`` workspaces.
# No network: the LLM seam is faked per suite.
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

WS = "ws_ai"
OTHER_WS = "ws_ai_other"
OWNER = "user-owner"
SITE_KEY = "site_key_" + "a" * 32
SITE_HOST = "app.example.com"
ORIGIN = f"https://{SITE_HOST}"
GATEWAY_TOKEN = "gw-token-" + "z" * 40


class _Membership:
    def __init__(self, workspace: str, role: str) -> None:
        self.workspace = workspace
        self.role = role


def build_app(workspace_id: str = WS, user_id: str = OWNER, role: str = "admin") -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.site_ai import router as site_ai_router

    user = SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[_Membership(workspace_id, role)],
    )
    app = FastAPI()
    add_error_handler(app)
    app.include_router(site_ai_router.router, prefix="/api/v1")
    public = getattr(site_ai_router, "public_router", None)
    if public is not None:
        app.include_router(public, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="t",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def client(workspace_id: str = WS, user_id: str = OWNER, role: str = "admin") -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=build_app(workspace_id, user_id, role)),
        base_url="https://api.test",
    )


async def make_pocket(workspace: str = WS, owner: str = OWNER) -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace=workspace,
        name="App",
        type="site",
        owner=owner,
        visibility="workspace",
        engine="html",
        widgets=[],
    )
    await doc.insert()
    return str(doc.id)


async def make_site(pocket_id: str, workspace: str = WS, plan_tier: str | None = "staff"):
    from pocketpaw_ee.cloud.models.site import Site

    site = Site(
        workspace=workspace,
        pocket_id=pocket_id,
        owner=OWNER,
        script_name=f"app-{pocket_id[-6:]}",
        signed_key=SITE_KEY,
        allowed_origins=[SITE_HOST],
        plan_tier=plan_tier,
    )
    await site.insert()
    return site
