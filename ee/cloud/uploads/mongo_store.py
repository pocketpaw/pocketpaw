"""Mongo-backed metadata store, workspace-scoped.

2026-04-19 (Cluster E sub-PR 4): added ``list_by_workspace`` so the
unified files endpoint can pull chat-sourced uploads alongside local
filesystem entries. Soft-deleted rows are skipped. Results are capped
to keep the unified list cheap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from ee.cloud.uploads.models import FileUpload
from pocketpaw.uploads.file_store import FileRecord


class MongoFileStore:
    """Workspace-scoped metadata store for EE uploads."""

    async def save_scoped(self, record: FileRecord, workspace: str) -> None:
        doc = FileUpload(
            file_id=record.id,
            storage_key=record.storage_key,
            filename=record.filename,
            mime=record.mime,
            size=record.size,
            workspace=workspace,
            owner=record.owner_id,
            chat_id=record.chat_id,
        )
        await doc.insert()

    async def get_scoped(self, file_id: str, workspace: str) -> FileRecord | None:
        doc = await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.workspace == workspace,
            FileUpload.deleted_at == None,  # noqa: E711 beanie needs literal None
        )
        return self._to_record(doc)

    async def get_unscoped(self, file_id: str) -> FileRecord | None:
        """Find a live record by file_id without workspace filter.

        Intended for call sites that lack tenant context (e.g. the OSS chat
        bridge in single-user self-hosted deployments). Multi-tenant cloud
        chat flows should use ``get_scoped`` with an authenticated workspace
        and never call this.
        """
        doc = await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.deleted_at == None,  # noqa: E711
        )
        return self._to_record(doc)

    @staticmethod
    def _to_record(doc: FileUpload | None) -> FileRecord | None:
        if doc is None:
            return None
        return FileRecord(
            id=doc.file_id,
            storage_key=doc.storage_key,
            filename=doc.filename,
            mime=doc.mime,
            size=doc.size,
            owner_id=doc.owner,
            chat_id=doc.chat_id,
            created=doc.createdAt or datetime.now(UTC),
        )

    async def iter_by_workspace(
        self,
        workspace: str,
        *,
        include_deleted: bool = False,
        limit: int = 500,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield upload docs for a workspace as plain dicts.

        Used by the unified files module (ee/cloud/files) to surface the
        chat-source slice. Keeps the shape minimal — callers convert to their
        own row types.
        """
        query: list[Any] = [FileUpload.workspace == workspace]
        if not include_deleted:
            query.append(FileUpload.deleted_at == None)  # noqa: E711
        cursor = FileUpload.find(*query).limit(limit)
        async for doc in cursor:
            created = doc.createdAt
            updated = getattr(doc, "updatedAt", None) or created
            yield {
                "file_id": doc.file_id,
                "filename": doc.filename,
                "mime": doc.mime,
                "size": doc.size,
                # Legacy keys (workspace/owner) retained for back-compat.
                "workspace": doc.workspace,
                "owner": doc.owner,
                # Canonical keys used by ee.cloud.files providers.
                "workspace_id": doc.workspace,
                "owner_id": doc.owner,
                "chat_id": doc.chat_id,
                "created_at": created,
                "updated_at": updated,
                "tags": list(getattr(doc, "tags", []) or []),
            }

    async def list_by_workspace(
        self,
        workspace: str,
        *,
        limit: int = 200,
        chat_id: str | None = None,
    ) -> list[FileRecord]:
        """Return live (non-deleted) file records in a workspace.

        Newest first. When ``chat_id`` is supplied, narrow further to the
        uploads that originated in that chat. The workspace filter always
        applies — cross-workspace bleed is not allowed through this API.
        """
        capped = max(1, min(limit, 500))
        query: dict = {
            "workspace": workspace,
            "deleted_at": None,
        }
        if chat_id:
            query["chat_id"] = chat_id
        docs = (
            await FileUpload.find(query)
            .sort([("createdAt", -1)])
            .limit(capped)
            .to_list()
        )
        return [r for r in (self._to_record(d) for d in docs) if r is not None]

    async def soft_delete_scoped(self, file_id: str, workspace: str) -> None:
        doc = await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.workspace == workspace,
        )
        if doc is None:
            return
        doc.deleted_at = datetime.now(UTC)
        await doc.save()
