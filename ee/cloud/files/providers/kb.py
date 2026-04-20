"""KbProvider — workspace Knowledge Base documents.

Reuses ee.cloud.kb listing helpers; does NOT duplicate their access checks.
Workspace membership gives read; admin/owner gives write/manage. Non-members
see nothing (list returns empty because the underlying service only yields
for entitled workspaces).

Note: `ee.cloud.kb` currently exposes KB operations only as FastAPI routes
that shell out to the `kb` Go binary via `_kb(...)`. There is no async service
object with the shape this provider expects. A thin adapter must be wired in
at registry construction time (see Task 14/bootstrap) that satisfies the
`_KbService` Protocol below by calling `_kb("list", ...)` / `_kb("show", ...)`
and mapping the returned dicts to the expected keys (id, title, mime, size,
owner_id, workspace_id, created_at, updated_at, tags).
"""
from __future__ import annotations

from typing import Any, Protocol

from ee.cloud.files.providers.base import BaseFolderProvider
from ee.cloud.files.schemas import (
    FileEntry,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)


class _KbService(Protocol):
    async def list_documents(self, workspace_id: str, *, limit: int = 500) -> list[dict]: ...
    async def get_document(self, doc_id: str, *, workspace_id: str) -> dict: ...


_ADMIN_ROLES = {"admin", "owner"}
_MEMBER_ROLES = {"admin", "owner", "member", "editor"}


class KbProvider(BaseFolderProvider):
    provider_id = "kb"

    def __init__(self, service: _KbService) -> None:
        self._service = service

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        if not ctx.workspace_id:
            return []
        return [
            ResolvedMount(
                provider_id=self.provider_id,
                path=f"/Workspaces/{ctx.workspace_id}/Knowledge Base",
                writable=True,
                order=20,
                variables={"workspace_id": ctx.workspace_id},
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
        if not ctx.workspace_id:
            return Page(items=[])
        docs = await self._service.list_documents(ctx.workspace_id, limit=limit)
        return Page(items=[self._to_entry(ctx.workspace_id, d) for d in docs])

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        _, _, native = entry_id.partition(":")
        doc = await self._service.get_document(native, workspace_id=ctx.workspace_id or "")
        return self._to_entry(ctx.workspace_id or "", doc)

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        role = str(ctx.attributes.get("role", "")).lower()
        if role in _ADMIN_ROLES:
            return Permission(read=True, write=True, manage=True)
        if role in _MEMBER_ROLES:
            return Permission(read=True, write=False, manage=False)
        if ctx.workspace_id and entry.workspace_id == ctx.workspace_id:
            return Permission(read=True, write=False, manage=False)
        return Permission()

    def _to_entry(self, workspace_id: str, doc: dict[str, Any]) -> FileEntry:
        title = doc.get("title", doc.get("name", ""))
        return FileEntry(
            id=f"kb:{doc['id']}",
            provider_id="kb",
            mount_path=f"/Workspaces/{workspace_id}/Knowledge Base/{title}",
            name=title,
            mime=doc.get("mime", "application/octet-stream"),
            size=int(doc.get("size", 0)),
            owner_id=doc.get("owner_id"),
            workspace_id=doc.get("workspace_id"),
            scope="workspace",
            tags=list(doc.get("tags", [])),
            created_at=doc["created_at"],
            updated_at=doc.get("updated_at", doc["created_at"]),
            source_ref={"kb_doc_id": doc["id"]},
            capabilities=["read", "download", "rename", "delete"],
        )
