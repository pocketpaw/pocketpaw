import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ee.cloud.files.bootstrap import build_files_router
from ee.cloud.files.schemas import RequestContext


class _Store:
    async def iter_by_workspace(self, workspace_id, *, include_deleted=False, limit=500):
        if False:
            yield {}


class _Kb:
    async def list_documents(self, workspace_id, *, limit=500):
        return []

    async def get_document(self, doc_id, *, workspace_id):
        raise KeyError


@pytest.mark.asyncio
async def test_bootstrap_tree_endpoint_works():
    app = FastAPI()
    app.include_router(
        build_files_router(
            uploads_store=_Store(),
            kb_service=_Kb(),
            ctx_factory=lambda req: RequestContext(
                user_id="u", workspace_id="ws_1", attributes={"role": "member"}
            ),
        ),
        prefix="/api/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/v1/files/tree")
    assert r.status_code == 200
    assert "children" in r.json()
