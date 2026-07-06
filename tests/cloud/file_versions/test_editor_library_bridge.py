# test_editor_library_bridge.py — FL-16 end-to-end bridge tests (real objects, no mocks at the seam).
# Created: 2026-07-03 (FL-16). Proves the cross-slice bug and its fix: the frontend
#   editor opens a Library file with the file's OWN bare ``file_id`` (a uuid,
#   FL-1) and saves via the FL-2 version path (``update_file_content`` /
#   ``PUT /files/{id}``). Before FL-16 that path namespaced the id to
#   ``${workspace}:${path}`` and resolved NOTHING, so editing a real Library file
#   end-to-end was broken. These tests seed a REAL ``FileUpload`` Library row via
#   the store (like FL-1/FL-3, NOT the guarded POST route which 401s), then drive
#   ``update_file_content`` / ``list_versions`` / ``revert_to_version`` against the
#   bare ``file_id`` and assert versions archive, live content updates, prior
#   content restores, and a DIFFERENT workspace can never touch it (tenant denial).
# The FL-2 path-based flow (editor's own ``write_file`` files, namespaced id) is
#   still covered by test_file_versions.py and preserved by the resolver order.
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.file_versions import service
from pocketpaw_ee.cloud.file_versions.dto import UpdateFileContentRequest
from pocketpaw_ee.cloud.models.file_version import FileVersionDoc
from pocketpaw_ee.cloud.uploads.models import FileUpload

from pocketpaw.uploads.adapter import StoredObject


class _FakeAdapter:
    """In-memory StorageAdapter stand-in (put + open over a dict of blobs).

    This is the STORAGE adapter — a legitimate seam (real blob I/O would need a
    disk/S3). The FileUpload row, FileVersionDoc rows, and the service functions
    under test are all REAL objects hitting the mongomock db; nothing at the
    bridge seam (id resolution, version archival, tenant filter) is mocked.
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


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _seed_library_file(
    workspace: str,
    file_id: str,
    *,
    filename: str = "note.txt",
    mime: str = "text/plain",
    content: str = "original body",
    adapter: _FakeAdapter,
) -> FileUpload:
    """Seed a REAL Library FileUpload row keyed by its OWN bare ``file_id`` + its
    blob — the FL-1 shape the /files pipeline produces, bypassing the guarded
    (401-ing) POST route. This is the row the frontend editor opens by bare id."""
    storage_key = f"editor/{workspace}/{file_id}"
    await adapter.put(storage_key, _one_chunk(content.encode("utf-8")), mime)
    doc = FileUpload(
        file_id=file_id,  # bare uuid — NOT the ${workspace}:${path} editor id
        storage_key=storage_key,
        filename=filename,
        mime=mime,
        size=len(content.encode("utf-8")),
        workspace=workspace,
        owner="u1",
        content_version=1,
    )
    await doc.insert()
    return doc


@pytest.mark.asyncio
async def test_editor_saves_library_file_by_bare_id(mongo_db, fake_storage):
    """THE bug fixed by FL-16: the editor opens a Library file by its bare
    ``file_id`` and saves via ``update_file_content``. Before the fix the id was
    namespaced and resolved nothing -> NotFound. After the fix the Library row's
    content updates and a FileVersionDoc archives the prior content."""
    ctx = _ctx("w1")
    file_id = "11111111-1111-1111-1111-111111111111"  # bare Library uuid
    await _seed_library_file("w1", file_id, content="original body", adapter=fake_storage)

    # Editor save path — keyed on the BARE Library file_id.
    res = await service.update_file_content(
        ctx, file_id, UpdateFileContentRequest(content="edited body")
    )
    assert res.new_version == 2

    # The live Library blob + counter updated.
    doc = await FileUpload.find_one({"file_id": file_id, "workspace": "w1"})
    assert doc is not None
    assert doc.content_version == 2
    assert fake_storage._blobs[doc.storage_key].decode() == "edited body"

    # A FileVersionDoc archived the prior content, keyed on the bare file_id.
    raw = await FileVersionDoc.find({"file_id": file_id, "workspace_id": "w1"}).to_list()
    assert len(raw) == 1
    assert raw[0].content == "original body"

    # And it's readable through the version readers on the same bare id.
    versions = await service.list_versions(ctx, file_id)
    assert len(versions) == 1
    assert versions[0].version_number == 1
    archived = await service.get_version(ctx, file_id, versions[0].id)
    assert archived.content == "original body"


@pytest.mark.asyncio
async def test_editor_revert_restores_library_file(mongo_db, fake_storage):
    """list_versions + revert_to_version work on a Library bare id: reverting
    restores the prior content as a new live version."""
    ctx = _ctx("w1")
    file_id = "22222222-2222-2222-2222-222222222222"
    await _seed_library_file("w1", file_id, content="one", adapter=fake_storage)

    await service.update_file_content(ctx, file_id, UpdateFileContentRequest(content="two"))
    await service.update_file_content(ctx, file_id, UpdateFileContentRequest(content="three"))

    versions = await service.list_versions(ctx, file_id)
    assert [v.version_number for v in versions] == [1, 2]
    v1 = next(v for v in versions if v.version_number == 1)  # content "one"

    # Revert to v1 -> a new live version whose content is "one" again.
    res = await service.revert_to_version(ctx, file_id, v1.id)
    assert res.new_version == 4

    # Confirm the live blob was restored to "one".
    doc = await FileUpload.find_one({"file_id": file_id, "workspace": "w1"})
    assert fake_storage._blobs[doc.storage_key].decode() == "one"

    # A follow-up edit archives the reverted "one" as the next version.
    await service.update_file_content(ctx, file_id, UpdateFileContentRequest(content="four"))
    versions2 = await service.list_versions(ctx, file_id)
    v4 = next(v for v in versions2 if v.version_number == 4)
    assert (await service.get_version(ctx, file_id, v4.id)).content == "one"


@pytest.mark.asyncio
async def test_cross_workspace_cannot_edit_library_file(mongo_db, fake_storage):
    """Tenant isolation: a caller in workspace B cannot edit / list / revert a
    Library file that belongs to workspace A, even with its exact bare file_id.
    The bare id is globally unique but the workspace filter fails closed."""
    ctx_b = _ctx("wB", "ub")
    file_id = "33333333-3333-3333-3333-333333333333"
    await _seed_library_file("wA", file_id, content="secret", adapter=fake_storage)

    # B tries to save A's file by its exact bare id -> NotFound (fail-closed).
    with pytest.raises(NotFound):
        await service.update_file_content(
            ctx_b, file_id, UpdateFileContentRequest(content="hijacked")
        )

    # B sees no versions for A's file, and A's live blob is untouched.
    assert await service.list_versions(ctx_b, file_id) == []
    doc = await FileUpload.find_one({"file_id": file_id, "workspace": "wA"})
    assert doc.content_version == 1
    assert fake_storage._blobs[doc.storage_key].decode() == "secret"
    # No version rows were written for A's file at all.
    assert await FileVersionDoc.find({"file_id": file_id}).to_list() == []


@pytest.mark.asyncio
async def test_editor_namespaced_path_still_resolves(mongo_db, fake_storage):
    """Regression guard: the editor's OWN write_file-created files (keyed by the
    ${workspace}:${path} namespaced id, bare path client-side) still resolve —
    the FL-16 resolver tries the namespaced id first, so FL-2's flow is intact."""
    ctx = _ctx("w1")
    from pocketpaw_ee.cloud.file_versions.dto import WriteFileRequest

    await service.write_file(ctx, WriteFileRequest(path="doc1", content="v1"))
    res = await service.update_file_content(ctx, "doc1", UpdateFileContentRequest(content="v2"))
    assert res.new_version == 2

    # Stored under the namespaced id, not the bare path.
    doc = await FileUpload.find_one(
        {"file_id": service._storage_id("w1", "doc1"), "workspace": "w1"}
    )
    assert doc is not None and doc.content_version == 2
