# test_file_versions.py — contract tests for the ported file_versions spine (ART-1).
# Created: 2026-06-26 (ART-1). Locks two guarantees the storage core must hold:
#   (a) write_file then PUT bumps the version and archives the prior blob;
#   (b) two-workspace isolation — a workspace can never list or read another
#       workspace's versions or content (tenant-filtered reads).
# Plus an HTTP smoke test that exercises the 4 routes end-to-end through the
# router (status codes + aliased wire shape) so the port's wiring is proven,
# not just the service functions.
"""Tests for the file_versions write + history storage spine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.file_versions import service
from pocketpaw_ee.cloud.file_versions.dto import (
    UpdateFileContentRequest,
    WriteFileRequest,
)

from pocketpaw.uploads.adapter import StoredObject


class _FakeAdapter:
    """In-memory StorageAdapter stand-in.

    Implements only the two methods the file_versions service uses
    (``put`` + ``open``) over a dict of blobs, so tests never touch the
    real on-disk / S3 adapter.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str) -> StoredObject:
        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
        data = b"".join(chunks)
        self._blobs[key] = data
        return StoredObject(key=key, size=len(data), mime=mime)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        data = self._blobs.get(key)
        if data is None:
            raise FileNotFoundError(key)
        yield data


def _ctx(workspace_id: str, user_id: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture
def fake_storage():
    """Inject an in-memory StorageAdapter for the duration of a test."""
    adapter = _FakeAdapter()
    prev = service._adapter
    service.set_adapter(adapter)
    yield adapter
    service._adapter = prev


@pytest.mark.asyncio
async def test_write_then_update_bumps_version(mongo_db, fake_storage):
    """write_file creates v1; a PUT with new content bumps to v2 and archives
    the prior blob as exactly one FileVersionDoc."""
    from pocketpaw_ee.cloud.uploads.models import FileUpload

    ctx = _ctx("w1")

    res1 = await service.write_file(ctx, WriteFileRequest(path="doc1", content='{"v":1}'))
    assert res1.file_id == "doc1"
    assert res1.version == 1

    res2 = await service.update_file_content(
        ctx, "doc1", UpdateFileContentRequest(content='{"v":2}')
    )
    assert res2.new_version == 2

    # The live FileUpload counter advanced to 2.
    doc = await FileUpload.find_one({"file_id": "doc1", "workspace": "w1"})
    assert doc is not None
    assert doc.content_version == 2

    # Exactly one archived version exists, holding the PRIOR (v1) content.
    versions = await service.list_versions(ctx, "doc1")
    assert len(versions) == 1
    assert versions[0].version_number == 2

    full = await service.get_version(ctx, "doc1", versions[0].id)
    assert full.content == '{"v":1}'


@pytest.mark.asyncio
async def test_two_workspace_isolation(mongo_db, fake_storage):
    """A workspace can never list or read another workspace's versions/content."""
    ctx_a = _ctx("wA", "ua")
    ctx_b = _ctx("wB", "ub")

    # Each workspace creates its own file and edits it (archiving a version).
    await service.write_file(ctx_a, WriteFileRequest(path="fileA", content='{"a":1}'))
    await service.update_file_content(ctx_a, "fileA", UpdateFileContentRequest(content='{"a":2}'))
    await service.write_file(ctx_b, WriteFileRequest(path="fileB", content='{"b":1}'))
    await service.update_file_content(ctx_b, "fileB", UpdateFileContentRequest(content='{"b":2}'))

    a_versions = await service.list_versions(ctx_a, "fileA")
    b_versions = await service.list_versions(ctx_b, "fileB")
    assert len(a_versions) == 1
    assert len(b_versions) == 1

    # Cross-workspace listing returns ZERO rows — no version leaks either way.
    assert await service.list_versions(ctx_b, "fileA") == []
    assert await service.list_versions(ctx_a, "fileB") == []

    # Cross-workspace content read by a known version id is a tenant-filtered
    # miss (NotFound) — the blob never crosses the workspace boundary.
    with pytest.raises(NotFound):
        await service.get_version(ctx_b, "fileA", a_versions[0].id)
    with pytest.raises(NotFound):
        await service.get_version(ctx_a, "fileB", b_versions[0].id)

    # And editing the other workspace's file_id is a miss — the FileUpload is
    # invisible across the boundary (tenant-filtered find).
    with pytest.raises(NotFound):
        await service.update_file_content(
            ctx_b, "fileA", UpdateFileContentRequest(content='{"x":1}')
        )


@pytest_asyncio.fixture
async def client(mongo_db, fake_storage):
    """A FastAPI app with just the file_versions router mounted, auth/license
    bypassed, and request_context pinned to workspace ``w1``."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from pocketpaw_ee.cloud._core.context import request_context
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.file_versions.router import router
    from pocketpaw_ee.cloud.license import require_license

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[request_context] = lambda: _ctx("w1")
    app.dependency_overrides[require_license] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.mark.asyncio
async def test_router_write_put_list_get_smoke(client):
    """End-to-end through the 4 routes: status codes + aliased wire shape."""
    # POST /files/write -> 201, aliased {fileId, version, sizeBytes}
    r = await client.post("/api/v1/files/write", json={"path": "d1", "content": '{"v":1}'})
    assert r.status_code == 201
    body = r.json()
    assert body["fileId"] == "d1"
    assert body["version"] == 1
    assert "sizeBytes" in body

    # PUT /files/{id} -> 200, bumps version (aliased newVersion)
    r = await client.put("/api/v1/files/d1", json={"content": '{"v":2}'})
    assert r.status_code == 200
    assert r.json()["newVersion"] == 2

    # GET /files/{id}/versions -> the archived row (aliased versionNumber)
    r = await client.get("/api/v1/files/d1/versions")
    assert r.status_code == 200
    versions = r.json()
    assert len(versions) == 1
    assert versions[0]["versionNumber"] == 2

    # GET /files/{id}/versions/{vid} -> the prior content blob
    vid = versions[0]["id"]
    r = await client.get(f"/api/v1/files/d1/versions/{vid}")
    assert r.status_code == 200
    assert r.json()["content"] == '{"v":1}'
