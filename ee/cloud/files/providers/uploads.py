"""UploadsProvider — wraps ee.cloud.uploads.MongoFileStore for the "My Files" mount.

Scope is personal to the current user within the current workspace. Ownership
drives RBAC: the owner has full CRUD; everyone else is read-only (the My Files
mount is effectively private in v2 Phase 1).
"""
from __future__ import annotations

from typing import Any

from ee.cloud.files.providers.base import BaseFolderProvider
from ee.cloud.files.schemas import (
    FileEntry,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)

_MOUNT = "/My Files"


class UploadsProvider(BaseFolderProvider):
    provider_id = "uploads"

    def __init__(self, store: Any) -> None:
        self._store = store

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        if not ctx.workspace_id:
            return []
        return [
            ResolvedMount(
                provider_id=self.provider_id,
                path=_MOUNT,
                writable=True,
                order=10,
                variables={},
            )
        ]

    async def list_entries(
        self,
        ctx: RequestContext,
        mount_path: str,
        cursor: str | None,
        limit: int,
        filters: dict,
    ) -> Page[FileEntry]:
        items: list[FileEntry] = []
        if not ctx.workspace_id:
            return Page(items=items)
        async for doc in self._store.iter_by_workspace(
            ctx.workspace_id, include_deleted=False, limit=limit
        ):
            if doc.get("owner_id") and doc["owner_id"] != ctx.user_id:
                continue
            items.append(self._to_entry(doc))
        return Page(items=items)

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        _, _, native = entry_id.partition(":")
        doc = await self._store.get_by_id(native, workspace_id=ctx.workspace_id)
        return self._to_entry(doc)

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        is_owner = entry.owner_id == ctx.user_id
        return Permission(read=True, write=is_owner, manage=is_owner)

    def _to_entry(self, doc: dict) -> FileEntry:
        return FileEntry(
            id=f"uploads:{doc['file_id']}",
            provider_id="uploads",
            mount_path=f"{_MOUNT}/{doc.get('filename', '')}",
            name=doc.get("filename", ""),
            mime=doc.get("mime", "application/octet-stream"),
            size=int(doc.get("size", 0)),
            owner_id=doc.get("owner_id"),
            workspace_id=doc.get("workspace_id"),
            scope="personal",
            tags=list(doc.get("tags", [])),
            created_at=doc["created_at"],
            updated_at=doc.get("updated_at", doc["created_at"]),
            source_ref={},
            capabilities=["read", "download", "rename", "delete"],
        )
