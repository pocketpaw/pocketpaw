"""Shared provider helpers."""
from __future__ import annotations

from collections.abc import AsyncIterator

from ee.cloud.files.errors import ProviderUnsupported
from ee.cloud.files.schemas import (
    FileEntry,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
    SearchQuery,
)


class BaseFolderProvider:
    """Default implementations that raise ProviderUnsupported.

    Providers override only the operations they support.
    """

    provider_id: str = ""

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        return []

    async def list_entries(
        self, ctx: RequestContext, mount_path: str, cursor: str | None, limit: int, filters: dict
    ) -> Page[FileEntry]:
        return Page(items=[], next_cursor=None)

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        raise ProviderUnsupported()

    async def open_stream(self, ctx: RequestContext, entry_id: str) -> AsyncIterator[bytes]:
        raise ProviderUnsupported()

    async def upload(self, ctx: RequestContext, mount_path: str, upload: object) -> FileEntry:
        raise ProviderUnsupported()

    async def rename(self, ctx: RequestContext, entry_id: str, new_name: str) -> FileEntry:
        raise ProviderUnsupported()

    async def move(self, ctx: RequestContext, entry_id: str, dest_mount_path: str) -> FileEntry:
        raise ProviderUnsupported()

    async def delete(self, ctx: RequestContext, entry_id: str) -> None:
        raise ProviderUnsupported()

    async def search(self, ctx: RequestContext, query: SearchQuery) -> Page[FileEntry]:
        return Page(items=[], next_cursor=None)

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        return Permission(read=True, write=False, manage=False)
