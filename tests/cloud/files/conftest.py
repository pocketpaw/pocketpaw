"""Shared fixtures for files tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud.files.dto import (
    FileEntry,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)
from pocketpaw_ee.cloud.files.errors import ProviderUnsupported


class FakeProvider:
    def __init__(
        self,
        provider_id: str,
        mounts: list[ResolvedMount] | None = None,
        entries: list[FileEntry] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._mounts = mounts or []
        self._entries = entries or []

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        return list(self._mounts)

    async def list_entries(
        self, ctx: RequestContext, mount_path: str, cursor: str | None, limit: int, filters: dict
    ) -> Page[FileEntry]:
        return Page(items=[e for e in self._entries if e.mount_path.startswith(mount_path)])

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        for e in self._entries:
            if e.id == entry_id:
                return e
        raise KeyError(entry_id)

    async def open_stream(self, ctx: RequestContext, entry_id: str) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            yield b"data"

        return _gen()

    async def upload(self, ctx, mount_path, upload):  # noqa: ANN001
        raise ProviderUnsupported()

    async def rename(self, ctx, entry_id, new_name):  # noqa: ANN001
        raise ProviderUnsupported()

    async def move(self, ctx, entry_id, dest_mount_path):  # noqa: ANN001
        raise ProviderUnsupported()

    async def delete(self, ctx, entry_id):  # noqa: ANN001
        raise ProviderUnsupported()

    async def search(self, ctx, query):  # noqa: ANN001
        return Page(items=[])

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        return Permission(read=True, write=False, manage=False)


@pytest.fixture
def ctx() -> RequestContext:
    return RequestContext(user_id="u1", workspace_id="ws_1", attributes={"role": "member"})


@pytest.fixture
def make_entry():
    def _make(provider_id: str, native_id: str, mount: str, **overrides: Any) -> FileEntry:
        base = dict(
            id=f"{provider_id}:{native_id}",
            provider_id=provider_id,
            mount_path=mount,
            name=native_id,
            mime="text/plain",
            size=10,
            owner_id="u1",
            workspace_id="ws_1",
            scope="personal",
            tags=[],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            source_ref={},
            capabilities=["read", "download"],
        )
        base.update(overrides)
        return FileEntry(**base)

    return _make


@pytest.fixture
def make_mount():
    def _make(
        provider_id: str, path: str, writable: bool = False, order: int = 100
    ) -> ResolvedMount:
        return ResolvedMount(
            provider_id=provider_id,
            path=path,
            writable=writable,
            order=order,
            variables={},
        )

    return _make
