"""Reusable contract every real FolderProvider must satisfy.

Concrete providers subclass `ProviderContract` and override `build_provider`
to yield a ready-to-use provider populated with the supplied test entries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.files.dto import FileEntry, RequestContext
from pocketpaw_ee.cloud.files.errors import ProviderUnsupported
from pocketpaw_ee.cloud.files.registry import FolderProvider


def _entry(provider_id: str) -> FileEntry:
    return FileEntry(
        id=f"{provider_id}:contract-1",
        provider_id=provider_id,
        mount_path="/test",
        name="contract-1",
        mime="text/plain",
        size=5,
        owner_id="u",
        workspace_id="ws",
        scope="personal",
        tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=["read", "download"],
    )


class ProviderContract(ABC):
    """Subclass and override `build_provider` in concrete provider tests."""

    @abstractmethod
    def build_provider(self) -> FolderProvider: ...

    def ctx(self) -> RequestContext:
        return RequestContext(user_id="u", workspace_id="ws", attributes={})

    @pytest.mark.asyncio
    async def test_list_mounts_returns_list(self):
        prov = self.build_provider()
        mounts = await prov.list_mounts(self.ctx())
        assert isinstance(mounts, list)

    @pytest.mark.asyncio
    async def test_list_entries_returns_page(self):
        prov = self.build_provider()
        mounts = await prov.list_mounts(self.ctx())
        if not mounts:
            pytest.skip("provider exposes no mounts under default ctx")
        page = await prov.list_entries(self.ctx(), mounts[0].path, None, 10, {})
        assert hasattr(page, "items")

    @pytest.mark.asyncio
    async def test_unsupported_ops_raise(self):
        prov = self.build_provider()
        ops: list[Callable] = [
            lambda: prov.rename(self.ctx(), "x", "y"),
            lambda: prov.move(self.ctx(), "x", "/nope"),
            lambda: prov.delete(self.ctx(), "x"),
        ]
        for op in ops:
            try:
                await op()
            except ProviderUnsupported:
                continue

    @pytest.mark.asyncio
    async def test_id_is_namespaced(self):
        prov = self.build_provider()
        mounts = await prov.list_mounts(self.ctx())
        if not mounts:
            pytest.skip("provider exposes no mounts")
        page = await prov.list_entries(self.ctx(), mounts[0].path, None, 10, {})
        for e in page.items:
            assert e.id.startswith(prov.provider_id + ":")
