from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": "chat/202604/aaa.png",
        "filename": "cat.png",
        "mime": "image/png",
        "size": 1,
        "owner_id": "u1",
        "chat_id": "c1",
        "created": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


class TestMongoFileStore:
    async def test_save_then_get(self, store):
        await store.save_scoped(_record(), workspace="w1")
        got = await store.get_scoped("f1", workspace="w1")
        assert got is not None
        assert got.filename == "cat.png"

    async def test_cross_workspace_get_returns_none(self, store):
        await store.save_scoped(_record(), workspace="w1")
        assert await store.get_scoped("f1", workspace="w2") is None

    async def test_soft_delete_hides(self, store):
        await store.save_scoped(_record(), workspace="w1")
        await store.soft_delete_scoped("f1", workspace="w1")
        assert await store.get_scoped("f1", workspace="w1") is None

    async def test_get_missing_returns_none(self, store):
        assert await store.get_scoped("nope", workspace="w1") is None
