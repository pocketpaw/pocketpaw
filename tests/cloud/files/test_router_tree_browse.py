import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.router import build_router
from ee.cloud.files.schemas import MountConfig, RequestContext
from ee.cloud.files.tree import invalidate_tree_cache
from tests.cloud.files.conftest import FakeProvider


@pytest.fixture(autouse=True)
def _clear_tree_cache():
    invalidate_tree_cache()
    yield
    invalidate_tree_cache()


def _mount(provider_id, path, writable=False, order=100):
    from ee.cloud.files.schemas import ResolvedMount
    return ResolvedMount(
        provider_id=provider_id, path=path, writable=writable, order=order, variables={}
    )


def _ctx_factory(request):
    return RequestContext(user_id="u1", workspace_id="ws_1", attributes={"role": "member"})


@pytest.mark.asyncio
async def test_get_tree_returns_folder_nodes():
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
        ]
    )
    reg.register(FakeProvider("uploads", mounts=[_mount("uploads", "/My Files", True, 10)]))

    app = FastAPI()
    app.include_router(
        build_router(registry=reg, rules=AbacRuleSet(), ctx_factory=_ctx_factory),
        prefix="/api/v1",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/tree")
    assert r.status_code == 200
    body = r.json()
    assert body["children"][0]["name"] == "My Files"
    assert body["warnings"] == []


@pytest.mark.asyncio
async def test_get_browse_returns_entries(make_entry):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
        ]
    )
    entry = make_entry("uploads", "a", "/My Files/a")
    reg.register(FakeProvider("uploads", entries=[entry]))

    app = FastAPI()
    app.include_router(
        build_router(registry=reg, rules=AbacRuleSet(), ctx_factory=_ctx_factory),
        prefix="/api/v1",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/browse", params={"mount": "/My Files"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == "uploads:a"


@pytest.mark.asyncio
async def test_get_browse_unknown_mount_is_404():
    reg = ProviderRegistry(configs=[])
    app = FastAPI()
    app.include_router(
        build_router(registry=reg, rules=AbacRuleSet(), ctx_factory=_ctx_factory),
        prefix="/api/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/browse", params={"mount": "/nope"})
    assert r.status_code == 404
    assert r.json()["detail"] == "files.mount_not_found"


@pytest.mark.asyncio
async def test_get_browse_workspace_mismatch_is_403(make_entry):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
        ]
    )
    reg.register(FakeProvider("uploads", entries=[]))

    app = FastAPI()
    app.include_router(
        build_router(registry=reg, rules=AbacRuleSet(), ctx_factory=_ctx_factory),
        prefix="/api/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get(
            "/api/v1/files/browse",
            params={"mount": "/My Files", "workspace_id": "ws_other"},
        )
    assert r.status_code == 403
    assert r.json()["detail"] == "files.workspace_mismatch"


@pytest.mark.asyncio
async def test_get_tree_workspace_mismatch_is_403():
    reg = ProviderRegistry(configs=[])
    app = FastAPI()
    app.include_router(
        build_router(registry=reg, rules=AbacRuleSet(), ctx_factory=_ctx_factory),
        prefix="/api/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/tree", params={"workspace_id": "ws_other"})
    assert r.status_code == 403
    assert r.json()["detail"] == "files.workspace_mismatch"
