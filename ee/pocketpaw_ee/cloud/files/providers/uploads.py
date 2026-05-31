"""UploadsProvider — wraps ee.cloud.uploads.MongoFileStore for the "My Files" mount.

Scope is personal to the current user within the current workspace. Ownership
drives RBAC: the owner has full CRUD; workspace admins get manage; everyone
else is read-only.

2026-04-21: folder support. The ``uploads`` provider is the only one that
surfaces folders; other providers stay flat. Folder entries render with
``mime = "application/x-directory"`` and no ``download`` capability.
"""

from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud.files.dto import (
    FileEntry,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)
from pocketpaw_ee.cloud.files.providers.base import BaseFolderProvider

_MOUNT = "/My Files"
_FOLDER_MIME = "application/x-directory"


def _mount_suffix(mount_path: str | None) -> str:
    """Strip the ``/My Files`` prefix. Returns absolute normalized path."""
    if not mount_path:
        return "/"
    mp = mount_path.rstrip("/") or "/"
    if mp == _MOUNT or mp == "":
        return "/"
    if mp.startswith(_MOUNT + "/"):
        return mp[len(_MOUNT) :] or "/"
    # Not under our mount — treat as root.
    return "/"


class UploadsProvider(BaseFolderProvider):
    provider_id = "uploads"

    def __init__(self, store: Any, folder_store: Any | None = None) -> None:
        self._store = store
        # Lazy default — import here to keep the provider construct side-
        # effect-free in tests that patch the store only.
        if folder_store is None:
            from pocketpaw_ee.cloud.uploads.folder_store import FolderStore

            folder_store = FolderStore()
        self._folders = folder_store

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

        suffix = _mount_suffix(mount_path)

        # Immediate child folders first.
        try:
            folders = await self._folders.list_children_folders(ctx.workspace_id, suffix)
        except Exception:
            folders = []
        for f in folders:
            # Folders have no owner gate beyond workspace scope — workspace
            # members see the tree. baseline_rbac enforces write.
            items.append(self._folder_to_entry(f))

        # Files directly in this folder.
        async for doc in self._store.iter_by_workspace(
            ctx.workspace_id, include_deleted=False, limit=limit
        ):
            if doc.get("owner_id") and doc["owner_id"] != ctx.user_id:
                continue
            fp = doc.get("folder_path") or "/"
            if fp != suffix:
                continue
            items.append(self._to_entry(doc))
        return Page(items=items)

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        # Folder id shape: "uploads:folder:<native_id>"
        parts = entry_id.split(":", 2)
        if len(parts) >= 3 and parts[1] == "folder":
            native = parts[2]
            if not ctx.workspace_id:
                raise LookupError(entry_id)
            folder = await self._folders.get_by_id(ctx.workspace_id, native)
            if folder is None:
                raise LookupError(entry_id)
            return self._folder_to_entry(folder)
        _, _, native = entry_id.partition(":")
        doc = await self._store.get_by_id(native, workspace_id=ctx.workspace_id)
        return self._to_entry(doc)

    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission:
        is_owner = entry.owner_id == ctx.user_id
        role = (ctx.attributes or {}).get("role")
        is_admin = role in {"admin", "owner"}
        if is_owner:
            return Permission(read=True, write=True, manage=True)
        if is_admin:
            return Permission(read=True, write=True, manage=True)
        return Permission(read=True, write=False, manage=False)

    def _to_entry(self, doc: dict) -> FileEntry:
        fp = doc.get("folder_path") or "/"
        filename = doc.get("filename", "")
        if fp == "/":
            mpath = f"{_MOUNT}/{filename}"
        else:
            mpath = f"{_MOUNT}{fp}/{filename}"
        return FileEntry(
            id=f"uploads:{doc['file_id']}",
            provider_id="uploads",
            mount_path=mpath,
            name=filename,
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

    def _folder_to_entry(self, folder: Any) -> FileEntry:
        path = folder.path
        if path == "/":
            mpath = _MOUNT
        else:
            mpath = f"{_MOUNT}{path}"
        return FileEntry(
            id=f"uploads:folder:{folder.folder_id}",
            provider_id="uploads",
            mount_path=mpath,
            name=folder.name,
            mime=_FOLDER_MIME,
            size=0,
            owner_id=folder.owner,
            workspace_id=folder.workspace,
            scope="personal",
            tags=[],
            created_at=folder.created_at,
            updated_at=folder.updated_at,
            source_ref={"kind": "folder"},
            capabilities=["read", "rename", "delete"],
        )
