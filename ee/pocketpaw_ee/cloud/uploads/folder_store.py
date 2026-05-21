"""Folder CRUD for the "My Files" mount.

Thin Mongo wrapper around :class:`FileFolder`. All queries are
workspace-scoped; folders live ONLY on the uploads provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud.uploads.models import FileFolder
from pocketpaw_ee.cloud.uploads.paths import basename, normalize_path, parent_of


class FolderStore:
    """Workspace-scoped folder store for uploads."""

    async def get_by_path(self, workspace: str, path: str) -> FileFolder | None:
        p = normalize_path(path)
        if p == "/":
            # Root is implicit — no row represents it.
            return None
        return await FileFolder.find_one(
            FileFolder.workspace == workspace,
            FileFolder.path == p,
            FileFolder.deleted_at == None,  # noqa: E711
        )

    async def get_by_id(self, workspace: str, folder_id: str) -> FileFolder | None:
        return await FileFolder.find_one(
            FileFolder.workspace == workspace,
            FileFolder.folder_id == folder_id,
            FileFolder.deleted_at == None,  # noqa: E711
        )

    async def path_exists(self, workspace: str, path: str) -> bool:
        p = normalize_path(path)
        if p == "/":
            return True
        return (await self.get_by_path(workspace, p)) is not None

    async def create(
        self,
        workspace: str,
        owner: str,
        path: str,
    ) -> FileFolder:
        """Create a single folder record at ``path`` (no auto-parents)."""
        p = normalize_path(path)
        if p == "/":
            raise ValueError("cannot create the root folder")
        now = datetime.now(UTC)
        doc = FileFolder(
            workspace=workspace,
            owner=owner,
            path=p,
            name=basename(p),
            created_at=now,
            updated_at=now,
        )
        await doc.insert()
        return doc

    async def ensure_chain(
        self,
        workspace: str,
        owner: str,
        path: str,
    ) -> list[FileFolder]:
        """Create every missing folder from root down to ``path``.

        Returns the list of records (created + already-existing) in
        root-to-leaf order. No-op for root.
        """
        p = normalize_path(path)
        if p == "/":
            return []
        segments = p.strip("/").split("/")
        created: list[FileFolder] = []
        cur = ""
        for seg in segments:
            cur = cur + "/" + seg
            existing = await self.get_by_path(workspace, cur)
            if existing is None:
                created.append(await self.create(workspace, owner, cur))
            else:
                created.append(existing)
        return created

    async def list_children_folders(
        self,
        workspace: str,
        parent_path: str,
    ) -> list[FileFolder]:
        """Return live folders whose parent is ``parent_path``."""
        p = normalize_path(parent_path)
        docs = await FileFolder.find(
            FileFolder.workspace == workspace,
            FileFolder.deleted_at == None,  # noqa: E711
        ).to_list()
        return [d for d in docs if parent_of(d.path) == p]

    async def rewrite_path_prefix(
        self,
        workspace: str,
        old_prefix: str,
        new_prefix: str,
    ) -> int:
        """Rewrite ``path`` on every descendant folder under ``old_prefix``.

        Strict descendants only — the row AT ``old_prefix`` is handled
        separately by ``rename_folder``. Returns count updated.
        """
        old = normalize_path(old_prefix)
        new = normalize_path(new_prefix)
        if old == "/" or old == new:
            return 0
        desc_prefix = old + "/"
        count = 0
        cursor = FileFolder.find(
            FileFolder.workspace == workspace,
            FileFolder.deleted_at == None,  # noqa: E711
        )
        async for d in cursor:
            if d.path.startswith(desc_prefix):
                new_path = new + d.path[len(old) :]
                d.path = new_path
                d.name = basename(new_path)
                d.updated_at = datetime.now(UTC)
                await d.save()
                count += 1
        return count

    async def count_subfolders(self, workspace: str, parent_path: str) -> int:
        base = normalize_path(parent_path)
        if base == "/":
            # Any live folder counts as a descendant of root.
            return await FileFolder.find(
                FileFolder.workspace == workspace,
                FileFolder.deleted_at == None,  # noqa: E711
            ).count()
        desc_prefix = base + "/"
        count = 0
        cursor = FileFolder.find(
            FileFolder.workspace == workspace,
            FileFolder.deleted_at == None,  # noqa: E711
        )
        async for d in cursor:
            if d.path.startswith(desc_prefix):
                count += 1
        return count

    async def soft_delete_under_prefix(self, workspace: str, prefix: str) -> int:
        """Soft-delete the folder AT ``prefix`` and all descendants."""
        base = normalize_path(prefix)
        if base == "/":
            return 0
        count = 0
        now = datetime.now(UTC)
        cursor = FileFolder.find(
            FileFolder.workspace == workspace,
            FileFolder.deleted_at == None,  # noqa: E711
        )
        async for d in cursor:
            if d.path == base or d.path.startswith(base + "/"):
                d.deleted_at = now
                await d.save()
                count += 1
        return count

    async def to_dict(self, doc: FileFolder) -> dict[str, Any]:
        return {
            "id": doc.folder_id,
            "path": doc.path,
            "name": doc.name,
            "owner_id": doc.owner,
            "workspace_id": doc.workspace,
            "created_at": doc.created_at,
            "updated_at": doc.updated_at,
        }
