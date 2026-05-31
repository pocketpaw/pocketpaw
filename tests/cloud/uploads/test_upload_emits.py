# test_upload_emits.py — emit-time behaviour for EEUploadService.
# Updated: 2026-04-30 — Stage 1.B "Files as Knowledge". FileReady now
#   emits for every successful upload (chat-scoped or workspace-only)
#   and always carries workspace_id + storage_key. Tests updated to
#   match.
"""Tests that EEUploadService emits file.ready / file.deleted."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.realtime.events import FileDeleted, FileReady
from pocketpaw_ee.cloud.uploads.service import EEUploadService

from pocketpaw.uploads.service import BulkUploadResult


def _capture_emits():
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    return recorded, fake_emit


def _rec(file_id: str = "f1", chat_id: str | None = "g1") -> SimpleNamespace:
    """Minimal FileRecord stub matching the fields EEUploadService touches."""
    return SimpleNamespace(
        id=file_id,
        chat_id=chat_id,
        filename="hello.txt",
        mime="text/plain",
        size=11,
        owner_id="u1",
        storage_key=f"chat/202604/{file_id}.txt",
    )


@pytest.mark.asyncio
async def test_upload_many_emits_file_ready_when_chat_scoped(monkeypatch):
    uploaded_recs = [_rec("f1"), _rec("f2")]
    inner_result = BulkUploadResult(uploaded=uploaded_recs, failed=[])

    recorded, fake_emit = _capture_emits()

    svc = EEUploadService.__new__(EEUploadService)
    svc._oss = SimpleNamespace(upload_many=AsyncMock(return_value=inner_result))
    svc._meta = SimpleNamespace(save_scoped=AsyncMock())
    svc._adapter = SimpleNamespace()

    monkeypatch.setattr("pocketpaw_ee.cloud.uploads.service.emit", fake_emit)

    result = await svc.upload_many([], owner_id="u1", chat_id="g1", workspace="w1")

    assert len(result.uploaded) == 2
    file_events = [e for e in recorded if isinstance(e, FileReady)]
    assert len(file_events) == 2
    file_ids = {e.data["file_id"] for e in file_events}
    assert file_ids == {"f1", "f2"}
    for e in file_events:
        assert e.data["group_id"] == "g1"
        assert e.data["workspace_id"] == "w1"
        assert e.data["filename"] == "hello.txt"
        assert e.data["mime"] == "text/plain"
        assert e.data["size"] == 11
        assert "url" in e.data
        assert "storage_key" in e.data


@pytest.mark.asyncio
async def test_upload_many_emits_file_ready_for_workspace_only(monkeypatch):
    """Workspace-only uploads (no chat_id) still emit FileReady so the KB
    indexer fires; the broadcast just skips the chat audience."""
    inner_result = BulkUploadResult(uploaded=[_rec("f1", chat_id=None)], failed=[])

    recorded, fake_emit = _capture_emits()

    svc = EEUploadService.__new__(EEUploadService)
    svc._oss = SimpleNamespace(upload_many=AsyncMock(return_value=inner_result))
    svc._meta = SimpleNamespace(save_scoped=AsyncMock())
    svc._adapter = SimpleNamespace()

    monkeypatch.setattr("pocketpaw_ee.cloud.uploads.service.emit", fake_emit)

    await svc.upload_many([], owner_id="u1", chat_id=None, workspace="w1")

    file_events = [e for e in recorded if isinstance(e, FileReady)]
    assert len(file_events) == 1
    ev = file_events[0]
    assert ev.data["workspace_id"] == "w1"
    assert ev.data["file_id"] == "f1"
    assert "group_id" not in ev.data


@pytest.mark.asyncio
async def test_delete_emits_file_deleted_when_chat_scoped(monkeypatch):
    recorded, fake_emit = _capture_emits()

    rec = _rec("f1", chat_id="g1")
    svc = EEUploadService.__new__(EEUploadService)
    svc._meta = SimpleNamespace(
        get_scoped=AsyncMock(return_value=rec),
        soft_delete_scoped=AsyncMock(),
    )
    svc._adapter = SimpleNamespace(delete=AsyncMock())

    monkeypatch.setattr("pocketpaw_ee.cloud.uploads.service.emit", fake_emit)

    await svc.delete("f1", requester_id="u1", workspace="w1")

    file_events = [e for e in recorded if isinstance(e, FileDeleted)]
    assert len(file_events) == 1
    assert file_events[0].data == {
        "group_id": "g1",
        "file_id": "f1",
        "workspace_id": "w1",
    }


@pytest.mark.asyncio
async def test_delete_emits_file_deleted_for_workspace_only(monkeypatch):
    """Workspace-only delete still emits so KB-side cleanup can react."""
    recorded, fake_emit = _capture_emits()

    rec = _rec("f1", chat_id=None)
    svc = EEUploadService.__new__(EEUploadService)
    svc._meta = SimpleNamespace(
        get_scoped=AsyncMock(return_value=rec),
        soft_delete_scoped=AsyncMock(),
    )
    svc._adapter = SimpleNamespace(delete=AsyncMock())

    monkeypatch.setattr("pocketpaw_ee.cloud.uploads.service.emit", fake_emit)

    await svc.delete("f1", requester_id="u1", workspace="w1")

    file_events = [e for e in recorded if isinstance(e, FileDeleted)]
    assert len(file_events) == 1
    ev = file_events[0]
    assert ev.data == {"file_id": "f1", "workspace_id": "w1"}
