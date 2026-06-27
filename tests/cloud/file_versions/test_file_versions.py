# test_file_versions.py — contract tests for the ported file_versions spine (ART-1).
# Created: 2026-06-26 (ART-1). Locks: (a) write->PUT version bump + archive,
#   (b) two-workspace isolation on version list + content reads, (c) a 4-route
#   HTTP smoke (status codes + aliased wire shape).
# Updated: 2026-06-26 (ART-1 quality fix loop) — added the LOGIC tests for the
#   defects the unique-index-blind mongomock harness hid:
#   C1 cross-tenant — two workspaces share a path -> distinct stored rows, no
#      error, isolated content; soft-delete then recreate revives with a FRESH
#      history (the dead file's version rows are purged on revive).
#   I1 — a no-op update returns the stored version (no phantom +1 / phantom 409).
#   I2 — new-file mime comes from the filename / explicit mime, not hardcoded.
#   I3 — a blob-read failure aborts and archives nothing (history preserved).
#   I4 — the archived row is labelled with the version the content actually was.
#   M2 — list/get reject an empty workspace.
"""Tests for the file_versions write + history storage spine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
from pocketpaw_ee.cloud.file_versions import service
from pocketpaw_ee.cloud.file_versions.dto import (
    UpdateFileContentRequest,
    WriteFileRequest,
)
from pocketpaw_ee.cloud.models.file_version import FileVersionDoc
from pocketpaw_ee.cloud.uploads.models import FileUpload

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


class _ReadFailAdapter(_FakeAdapter):
    """Adapter whose blob read always fails — exercises the I3 abort path."""

    async def open(self, key: str) -> AsyncIterator[bytes]:
        # The `yield` (never-taken branch) makes this an async generator; the
        # read always raises so the service must abort the update.
        if self._blobs.get(key) == b"__never__":
            yield b""
        raise OSError("simulated blob read failure")


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
    """write_file creates v1; a PUT with new content bumps the live counter to
    2 and archives the prior blob as one FileVersionDoc labelled v1."""
    ctx = _ctx("w1")

    res1 = await service.write_file(ctx, WriteFileRequest(path="doc1", content='{"v":1}'))
    assert res1.file_id == "doc1"  # client sees the bare path
    assert res1.version == 1

    res2 = await service.update_file_content(
        ctx, "doc1", UpdateFileContentRequest(content='{"v":2}')
    )
    assert res2.new_version == 2

    # The live FileUpload counter advanced to 2 (stored id is workspace-namespaced).
    doc = await FileUpload.find_one(
        {"file_id": service._storage_id("w1", "doc1"), "workspace": "w1"}
    )
    assert doc is not None
    assert doc.content_version == 2

    # One archived version, labelled with the version it actually was (1),
    # holding the prior (v1) content.
    versions = await service.list_versions(ctx, "doc1")
    assert len(versions) == 1
    assert versions[0].version_number == 1

    full = await service.get_version(ctx, "doc1", versions[0].id)
    assert full.content == '{"v":1}'


@pytest.mark.asyncio
async def test_cross_tenant_same_path_no_collision(mongo_db, fake_storage):
    """C1 — two workspaces writing the SAME path produce distinct stored rows,
    no error, and fully isolated content."""
    ctx_a = _ctx("wA", "ua")
    ctx_b = _ctx("wB", "ub")

    ra = await service.write_file(ctx_a, WriteFileRequest(path="report", content='{"a":1}'))
    # B writing the same path must NOT raise (no global unique-key collision).
    rb = await service.write_file(ctx_b, WriteFileRequest(path="report", content='{"b":1}'))

    # Client-facing id is the bare path for both.
    assert ra.file_id == "report"
    assert rb.file_id == "report"

    # But the STORED FileUpload rows carry distinct workspace-namespaced ids.
    doc_a = await FileUpload.find_one(
        {"file_id": service._storage_id("wA", "report"), "workspace": "wA"}
    )
    doc_b = await FileUpload.find_one(
        {"file_id": service._storage_id("wB", "report"), "workspace": "wB"}
    )
    assert doc_a is not None and doc_b is not None
    assert doc_a.file_id != doc_b.file_id

    # Each workspace edits + reads only its own content.
    await service.update_file_content(ctx_a, "report", UpdateFileContentRequest(content='{"a":2}'))
    await service.update_file_content(ctx_b, "report", UpdateFileContentRequest(content='{"b":2}'))

    a_versions = await service.list_versions(ctx_a, "report")
    b_versions = await service.list_versions(ctx_b, "report")
    assert len(a_versions) == 1
    assert len(b_versions) == 1
    assert (await service.get_version(ctx_a, "report", a_versions[0].id)).content == '{"a":1}'
    assert (await service.get_version(ctx_b, "report", b_versions[0].id)).content == '{"b":1}'

    # A cannot read B's version row (tenant-filtered).
    with pytest.raises(NotFound):
        await service.get_version(ctx_a, "report", b_versions[0].id)


@pytest.mark.asyncio
async def test_soft_delete_then_recreate_purges_history(mongo_db, fake_storage):
    """C1 follow-up — soft-delete then recreate is a NEW file at that path with
    FRESH history: the revive succeeds AND purges the dead file's version rows
    so they don't bleed stale content / duplicate version_number=1 labels."""
    ctx = _ctx("w1")
    stored_id = service._storage_id("w1", "report")

    # Build a 2-version history on the original file.
    await service.write_file(ctx, WriteFileRequest(path="report", content="v1"))
    await service.update_file_content(ctx, "report", UpdateFileContentRequest(content="v2"))
    await service.update_file_content(ctx, "report", UpdateFileContentRequest(content="v3"))
    versions = await service.list_versions(ctx, "report")
    assert [v.version_number for v in versions] == [1, 2]

    # Simulate the uploads soft-delete (file_versions has no delete of its own).
    doc = await FileUpload.find_one({"file_id": stored_id, "workspace": "w1"})
    doc.deleted_at = datetime.now(UTC)
    await doc.save()

    # Recreate the same path — succeeds, and starts a CLEAN history.
    res = await service.write_file(ctx, WriteFileRequest(path="report", content="fresh"))
    assert res.version == 1
    assert await service.list_versions(ctx, "report") == []  # stale v1/v2 purged

    # Exactly ONE live row for the stored id (revived in place).
    rows = await FileUpload.find({"file_id": stored_id, "workspace": "w1"}).to_list()
    assert len(rows) == 1
    assert rows[0].deleted_at is None

    # The next edit archives a clean version_number=1 (no duplicate label,
    # no stale content) — proving the purge worked.
    await service.update_file_content(ctx, "report", UpdateFileContentRequest(content="fresh2"))
    versions2 = await service.list_versions(ctx, "report")
    assert [v.version_number for v in versions2] == [1]
    assert (await service.get_version(ctx, "report", versions2[0].id)).content == "fresh"


@pytest.mark.asyncio
async def test_noop_update_returns_stored_version(mongo_db, fake_storage):
    """I1 — an unchanged-content update returns the actual stored version, not a
    phantom +1, and doesn't poison the next If-Match."""
    ctx = _ctx("w1")
    await service.write_file(ctx, WriteFileRequest(path="doc", content="same"))

    res = await service.update_file_content(ctx, "doc", UpdateFileContentRequest(content="same"))
    assert res.new_version == 1  # stored version, not 2
    assert await service.list_versions(ctx, "doc") == []  # nothing archived

    # A subsequent If-Match=1 update applies cleanly (no phantom 409 against 2).
    res2 = await service.update_file_content(
        ctx, "doc", UpdateFileContentRequest(content="changed", expected_version=1)
    )
    assert res2.new_version == 2


@pytest.mark.asyncio
async def test_new_file_mime_from_filename(mongo_db, fake_storage):
    """I2 — new-file mime comes from the filename extension or an explicit mime,
    not a hardcoded application/json."""
    ctx = _ctx("w1")

    await service.write_file(ctx, WriteFileRequest(path="data", filename="data.csv", content="a,b"))
    doc = await FileUpload.find_one(
        {"file_id": service._storage_id("w1", "data"), "workspace": "w1"}
    )
    assert doc.mime == "text/csv"

    # Explicit mime wins over the (here misleading) extension.
    await service.write_file(
        ctx, WriteFileRequest(path="page", filename="page.bin", content="x", mime="text/html")
    )
    doc2 = await FileUpload.find_one(
        {"file_id": service._storage_id("w1", "page"), "workspace": "w1"}
    )
    assert doc2.mime == "text/html"

    # Extension-less path falls back to an editable text/plain.
    await service.write_file(ctx, WriteFileRequest(path="notes", content="hi"))
    doc3 = await FileUpload.find_one(
        {"file_id": service._storage_id("w1", "notes"), "workspace": "w1"}
    )
    assert doc3.mime == "text/plain"


@pytest.mark.asyncio
async def test_archive_read_failure_aborts_and_preserves_history(mongo_db, fake_storage):
    """I3 — a blob-read failure aborts the update with a CloudError and archives
    nothing, rather than writing an empty 'prior' version."""
    ctx = _ctx("w1")
    await service.write_file(ctx, WriteFileRequest(path="doc", content="orig"))

    # Swap in an adapter whose blob read fails.
    service.set_adapter(_ReadFailAdapter())

    with pytest.raises(CloudError) as ei:
        await service.update_file_content(ctx, "doc", UpdateFileContentRequest(content="new"))
    assert ei.value.code == "files.archive_read_failed"

    # No empty prior version was archived — history is intact.
    archived = await FileVersionDoc.find({"file_id": "doc", "workspace_id": "w1"}).to_list()
    assert archived == []


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


@pytest.mark.asyncio
async def test_empty_workspace_rejected_on_reads(mongo_db, fake_storage):
    """M2 — list/get reject an empty workspace (400), matching write/update."""
    ctx_empty = _ctx("")
    with pytest.raises(CloudError):
        await service.list_versions(ctx_empty, "doc")
    with pytest.raises(CloudError):
        await service.get_version(ctx_empty, "doc", "000000000000000000000000")


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

    # GET /files/{id}/versions -> the archived row, labelled v1 (aliased)
    r = await client.get("/api/v1/files/d1/versions")
    assert r.status_code == 200
    versions = r.json()
    assert len(versions) == 1
    assert versions[0]["versionNumber"] == 1

    # GET /files/{id}/versions/{vid} -> the prior content blob
    vid = versions[0]["id"]
    r = await client.get(f"/api/v1/files/d1/versions/{vid}")
    assert r.status_code == 200
    assert r.json()["content"] == '{"v":1}'
