# tests/cloud/storage/test_upload_gate.py — the UPLOAD seam of the storage cap
# (feat/billing-storage-caps).
#
# ``EEUploadService.upload_many`` (the HTTP upload path) and the programmatic
# ``write_text_file`` both enforce the workspace's plan ``max_storage_bytes``:
# when the new blobs would push the workspace's live ``FileUpload`` total over
# its cap, they raise ``StorageLimitError`` (402, ``billing.storage_limit``) and
# roll back the just-written blobs — an over-cap upload is a NO-OP (no Mongo
# row, no FileReady event, no orphan object in storage). Gated on
# ``billing_enforced``: with billing off (OSS / self-host) the upload proceeds
# unchanged.
#
# DB-backed (mongo_db): real Workspace + FileUpload docs drive the usage sum;
# a real MongoFileStore persists rows; a fake in-memory adapter simulates S3.
#
# Created 2026-08-08 (feat/billing-storage-caps).

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import NotFound

PNG = b"\x89PNG\r\n\x1a\n" + b"rest"


class _MemAdapter(StorageAdapter):
    """In-memory S3 stand-in — tracks written keys so the rollback is testable."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        buf = b""
        async for c in stream:
            buf += c
        self.blobs[key] = buf
        return StoredObject(key=key, size=len(buf), mime=mime)

    async def open(self, key):
        if key not in self.blobs:
            raise NotFound()
        yield self.blobs[key]

    async def delete(self, key):
        self.blobs.pop(key, None)

    async def exists(self, key):
        return key in self.blobs


def _upload(content: bytes, filename: str, mime: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers={"content-type": mime},  # type: ignore[arg-type]
    )


def _billing(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    import pocketpaw.config as ppconfig

    monkeypatch.setattr(
        ppconfig,
        "get_settings",
        lambda: SimpleNamespace(billing_enforced=on, dodo_plan_products=None),
    )


async def _make_workspace(plan: str) -> str:
    import uuid

    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = Workspace(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}", owner="u-owner", plan=plan)
    await ws.insert()
    return str(ws.id)


async def _seed_file(workspace_id: str, size: int) -> None:
    import uuid

    from pocketpaw_ee.cloud.uploads.models import FileUpload

    await FileUpload(
        file_id=uuid.uuid4().hex,
        storage_key=f"u/{uuid.uuid4().hex}",
        filename="old.pdf",
        mime="application/pdf",
        size=size,
        workspace=workspace_id,
        owner="u-owner",
    ).insert()


def _svc(adapter: _MemAdapter, store):
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

    return EEUploadService(
        adapter=adapter,
        meta=store,
        cfg=UploadSettings(local_root=Path("/tmp/storage-gate")),
    )


async def test_upload_over_cap_raises_and_rolls_back(
    mongo_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Free (5 GB) already full → a 1-byte upload raises and leaves nothing behind."""
    from pocketpaw_ee.cloud._core.errors import StorageLimitError
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    _billing(monkeypatch, on=True)
    ws = await _make_workspace("free")
    await _seed_file(ws, 5_000_000_000)

    adapter = _MemAdapter()
    store = MongoFileStore()
    svc = _svc(adapter, store)

    with pytest.raises(StorageLimitError) as exc:
        await svc.upload(
            _upload(PNG, "cat.png", "image/png"), owner_id="u1", chat_id="c1", workspace=ws
        )
    assert exc.value.code == "billing.storage_limit"

    # Rollback: no blob survives in the adapter, no Mongo row, no event.
    assert adapter.blobs == {}
    from pocketpaw_ee.cloud.uploads.models import FileUpload

    rows = await FileUpload.find(FileUpload.workspace == ws).to_list()
    assert len(rows) == 1  # only the seeded old file


async def test_upload_within_budget_succeeds(mongo_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Free with plenty of headroom → the upload lands normally."""
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    _billing(monkeypatch, on=True)
    ws = await _make_workspace("free")
    await _seed_file(ws, 1_000)

    adapter = _MemAdapter()
    store = MongoFileStore()
    svc = _svc(adapter, store)

    rec = await svc.upload(
        _upload(PNG, "cat.png", "image/png"), owner_id="u1", chat_id="c1", workspace=ws
    )
    assert rec.size == len(PNG)
    assert adapter.blobs  # the blob was written
    from pocketpaw_ee.cloud.uploads.models import FileUpload

    rows = await FileUpload.find(FileUpload.workspace == ws).to_list()
    assert len(rows) == 2  # seeded old file + the new one


async def test_upload_gate_is_noop_when_billing_off(
    mongo_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """billing off (OSS / self-host) → an over-cap upload is NOT blocked."""
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    _billing(monkeypatch, on=False)
    ws = await _make_workspace("free")
    await _seed_file(ws, 5_000_000_000)  # Free cap fully consumed

    adapter = _MemAdapter()
    store = MongoFileStore()
    svc = _svc(adapter, store)

    rec = await svc.upload(
        _upload(PNG, "cat.png", "image/png"), owner_id="u1", chat_id="c1", workspace=ws
    )
    assert rec.size == len(PNG)
    assert len(adapter.blobs) == 1  # the over-cap upload still landed


async def test_write_text_file_over_cap_raises_and_rolls_back(
    mongo_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_text_file honours the cap too — raises and leaves no orphan blob."""
    from pocketpaw_ee.cloud._core.errors import StorageLimitError
    from pocketpaw_ee.cloud.uploads.service import write_text_file

    _billing(monkeypatch, on=True)
    ws = await _make_workspace("free")
    await _seed_file(ws, 5_000_000_000)

    with pytest.raises(StorageLimitError):
        await write_text_file(
            workspace_id=ws,
            owner_id="u1",
            folder_path="/",
            filename="prd.md",
            content="x" * 10,
        )

    from pocketpaw_ee.cloud.uploads.models import FileUpload

    rows = await FileUpload.find(FileUpload.workspace == ws).to_list()
    assert len(rows) == 1  # no new row, no orphan
