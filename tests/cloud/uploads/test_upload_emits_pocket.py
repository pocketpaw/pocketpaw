# test_upload_emits_pocket.py — pocket_id propagation tests for EEUploadService.
# Created: 2026-05-03 — Stage 3.E "Files as Knowledge". Verifies the
# upload service threads ``pocket_id`` through to ``MongoFileStore.save_scoped``
# and the FileReady event payload. The legacy emit shape is regression-
# tested in ``test_upload_emits.py``; this file covers the new
# pocket-aware shape.
"""Pocket-id propagation through ``EEUploadService.upload_many``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud.realtime.events import FileReady
from pocketpaw_ee.cloud.uploads.service import EEUploadService

from pocketpaw.uploads.service import BulkUploadResult


def _capture_emits():
    recorded: list = []

    async def fake_emit(ev):
        recorded.append(ev)

    return recorded, fake_emit


def _rec(file_id: str = "f1", chat_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=file_id,
        chat_id=chat_id,
        filename="deck.pdf",
        mime="application/pdf",
        size=42,
        owner_id="u1",
        storage_key=f"chat/{file_id}.pdf",
    )


@pytest.mark.asyncio
async def test_upload_many_threads_pocket_id_to_save_and_event(monkeypatch):
    inner_result = BulkUploadResult(uploaded=[_rec("f1")], failed=[])
    recorded, fake_emit = _capture_emits()

    save_mock = AsyncMock()
    svc = EEUploadService.__new__(EEUploadService)
    svc._oss = SimpleNamespace(upload_many=AsyncMock(return_value=inner_result))
    svc._meta = SimpleNamespace(save_scoped=save_mock)
    svc._adapter = SimpleNamespace()

    monkeypatch.setattr("pocketpaw_ee.cloud.uploads.service.emit", fake_emit)

    await svc.upload_many(
        [],
        owner_id="u1",
        chat_id=None,
        workspace="w1",
        folder_path="/",
        pocket_id="PA",
    )

    # save_scoped was called with pocket_id propagated through.
    save_mock.assert_awaited_once()
    save_kwargs = save_mock.await_args.kwargs
    assert save_kwargs["workspace"] == "w1"
    assert save_kwargs["folder_path"] == "/"
    assert save_kwargs["pocket_id"] == "PA"

    # FileReady carries pocket_id so the listener can route into pocket:{id}.
    file_events = [e for e in recorded if isinstance(e, FileReady)]
    assert len(file_events) == 1
    assert file_events[0].data["pocket_id"] == "PA"
    assert file_events[0].data["workspace_id"] == "w1"


@pytest.mark.asyncio
async def test_upload_many_omits_pocket_id_when_unset(monkeypatch):
    """No ``pocket_id`` → event payload doesn't carry the key (regression)."""
    inner_result = BulkUploadResult(uploaded=[_rec("f1")], failed=[])
    recorded, fake_emit = _capture_emits()

    svc = EEUploadService.__new__(EEUploadService)
    svc._oss = SimpleNamespace(upload_many=AsyncMock(return_value=inner_result))
    svc._meta = SimpleNamespace(save_scoped=AsyncMock())
    svc._adapter = SimpleNamespace()

    monkeypatch.setattr("pocketpaw_ee.cloud.uploads.service.emit", fake_emit)

    await svc.upload_many([], owner_id="u1", chat_id=None, workspace="w1")

    file_events = [e for e in recorded if isinstance(e, FileReady)]
    assert len(file_events) == 1
    assert "pocket_id" not in file_events[0].data
    assert file_events[0].data["workspace_id"] == "w1"


@pytest.mark.asyncio
async def test_upload_many_chat_and_pocket_both_present(monkeypatch):
    """A chat-pinned upload inside a pocket carries both keys."""
    inner_result = BulkUploadResult(uploaded=[_rec("f1", chat_id="g1")], failed=[])
    recorded, fake_emit = _capture_emits()

    svc = EEUploadService.__new__(EEUploadService)
    svc._oss = SimpleNamespace(upload_many=AsyncMock(return_value=inner_result))
    svc._meta = SimpleNamespace(save_scoped=AsyncMock())
    svc._adapter = SimpleNamespace()

    monkeypatch.setattr("pocketpaw_ee.cloud.uploads.service.emit", fake_emit)

    await svc.upload_many([], owner_id="u1", chat_id="g1", workspace="w1", pocket_id="PA")

    file_events = [e for e in recorded if isinstance(e, FileReady)]
    assert len(file_events) == 1
    data = file_events[0].data
    assert data["pocket_id"] == "PA"
    assert data["group_id"] == "g1"
