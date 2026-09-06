# test_file_ready_emit.py — the editor write path emits FileReady (B2).
# Created: 2026-09-05 (feat/files-links). Proves write_file and
#   update_file_content emit a FileReady whose file_id RESOLVES through
#   MongoFileStore (the stored id, not the bare client path), that the payload
#   carries the keys the uploads pipeline sends, that a no-op update emits
#   nothing, and that the FL-16 bare-id Library path emits with the bare id.
"""FileReady emission from the file_versions write path."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.realtime.events import FileReady
from pocketpaw_ee.cloud.file_versions import service
from pocketpaw_ee.cloud.file_versions.dto import UpdateFileContentRequest, WriteFileRequest
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

from pocketpaw.uploads.file_store import FileRecord
from tests.cloud.file_versions.test_file_versions import _ctx
from tests.cloud.file_versions.test_file_versions import fake_storage as _fake_storage

fake_storage = _fake_storage

EXPECTED_KEYS = {"workspace_id", "file_id", "filename", "mime", "size", "storage_key", "url"}


def _ready(bus) -> list[FileReady]:
    return [e for e in bus.events if isinstance(e, FileReady)]


@pytest.mark.asyncio
async def test_write_emits_file_ready_with_resolvable_id(mongo_db, fake_storage, recording_bus):
    await service.write_file(_ctx("w1"), WriteFileRequest(path="notes/plan.md", content="# hi"))

    events = _ready(recording_bus)
    assert len(events) == 1
    data = events[0].data
    assert set(data) == EXPECTED_KEYS
    assert data["workspace_id"] == "w1"
    assert data["mime"] == "text/markdown"
    assert data["url"] == f"/api/v1/uploads/{data['file_id']}"
    doc = await MongoFileStore().get_doc_scoped(data["file_id"], "w1")
    assert doc is not None, "listener resolves the row by this id; a bare path finds nothing"
    assert doc.storage_key == data["storage_key"]


@pytest.mark.asyncio
async def test_update_emits_file_ready_and_noop_does_not(mongo_db, fake_storage, recording_bus):
    ctx = _ctx("w1")
    await service.write_file(ctx, WriteFileRequest(path="plan.md", content="v1"))
    await service.update_file_content(ctx, "plan.md", UpdateFileContentRequest(content="v2"))
    assert len(_ready(recording_bus)) == 2
    assert await MongoFileStore().get_doc_scoped(_ready(recording_bus)[1].data["file_id"], "w1")

    await service.update_file_content(ctx, "plan.md", UpdateFileContentRequest(content="v2"))
    assert len(_ready(recording_bus)) == 2, "unchanged content must not re-index"


@pytest.mark.asyncio
async def test_library_bare_id_update_emits_bare_id(mongo_db, fake_storage, recording_bus):
    rec = FileRecord(
        id="lib-uuid",
        storage_key="chat/x",
        filename="lib.md",
        mime="text/markdown",
        size=1,
        owner_id="u1",
        chat_id=None,
        created=datetime.now(UTC),
    )
    await MongoFileStore().save_scoped(rec, workspace="w1", pocket_id="p1")
    await fake_storage.put("chat/x", _chunks(b"old"), "text/markdown")

    await service.update_file_content(
        _ctx("w1"), "lib-uuid", UpdateFileContentRequest(content="new")
    )

    (event,) = _ready(recording_bus)
    assert event.data["file_id"] == "lib-uuid"
    assert event.data["pocket_id"] == "p1"


async def _chunks(data: bytes):
    yield data
