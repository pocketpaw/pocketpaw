"""EEUploadService — workspace-scoped upload pipeline on top of the OSS service."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import UploadFile

from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import FileDeleted, FileReady
from ee.cloud.uploads.mongo_store import MongoFileStore
from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import NotFound
from pocketpaw.uploads.file_store import FileRecord
from pocketpaw.uploads.service import (
    BulkUploadResult,
    UploadService,
    _raise,
)


class _NullMeta:
    """Stub JSONLFileStore — validates but doesn't persist (EE uses Mongo)."""

    def save(self, record: FileRecord) -> None:
        pass

    def get(self, file_id: str) -> FileRecord | None:
        return None

    def soft_delete(self, file_id: str) -> None:
        pass


class EEUploadService:
    """Workspace-scoped upload pipeline.

    Wraps the OSS ``UploadService`` for validation + magic-byte sniff + adapter
    writes, then persists metadata to Mongo with workspace scoping.
    """

    def __init__(
        self,
        adapter: StorageAdapter,
        meta: MongoFileStore,
        cfg: UploadSettings,
        is_chat_member: Callable[[str, str, str], Awaitable[bool]] | None = None,
        is_workspace_admin: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._adapter = adapter
        self._meta = meta
        self._cfg = cfg
        # Optional collaborator checks. When ``None``, the corresponding
        # branch is skipped and the gate reduces to owner-only — preserving
        # behaviour for tests and callers that don't wire them.
        self._is_chat_member = is_chat_member
        self._is_workspace_admin = is_workspace_admin
        # Use a null meta under OSS service so we control Mongo writes here
        self._oss = UploadService(adapter=adapter, meta=_NullMeta(), cfg=cfg)  # type: ignore[arg-type]

    async def _assert_can_write(
        self,
        rec: FileRecord,
        requester_id: str,
        workspace: str,
    ) -> None:
        """Gate a write on a file record.

        Owner OR workspace admin/owner only. Chat-member does NOT grant
        write. Mirrors :meth:`_assert_can_read` but does not fall through
        silently — callers translate the raised exception to a 403.
        """
        if rec.owner_id == requester_id:
            return
        if self._is_workspace_admin is not None:
            try:
                if await self._is_workspace_admin(requester_id, workspace):
                    return
            except Exception:
                pass
        raise PermissionError("files.forbidden")

    async def _assert_can_read(
        self,
        rec: FileRecord,
        requester_id: str,
        workspace: str,
    ) -> None:
        """Gate a read on a file record.

        Allows owner, chat-members (when the file is pinned to a chat), and
        workspace admins/owners. Every denial raises :class:`NotFound` so
        callers cannot distinguish "missing" from "forbidden".
        """
        if rec.owner_id == requester_id:
            return
        chat_id = getattr(rec, "chat_id", None)
        if chat_id and self._is_chat_member is not None:
            try:
                if await self._is_chat_member(chat_id, requester_id, workspace):
                    return
            except Exception:
                # Defensive: collaborator lookup must never leak info or
                # convert a 404 into a 500.
                pass
        if self._is_workspace_admin is not None:
            try:
                if await self._is_workspace_admin(requester_id, workspace):
                    return
            except Exception:
                pass
        raise NotFound()

    async def upload(
        self,
        file: UploadFile,
        owner_id: str,
        chat_id: str | None,
        workspace: str,
        folder_path: str = "/",
    ) -> FileRecord:
        result = await self.upload_many(
            [file], owner_id, chat_id, workspace, folder_path=folder_path
        )
        if result.failed:
            f = result.failed[0]
            _raise(f.code, f.reason)
        return result.uploaded[0]

    async def upload_many(
        self,
        files: list[UploadFile],
        owner_id: str,
        chat_id: str | None,
        workspace: str,
        folder_path: str = "/",
    ) -> BulkUploadResult:
        # Delegate validation + adapter writes; metadata is discarded inside OSS
        result = await self._oss.upload_many(files, owner_id, chat_id)
        # Persist each successful record in Mongo with workspace scoping
        for rec in result.uploaded:
            await self._meta.save_scoped(
                rec, workspace=workspace, folder_path=folder_path
            )
            # Only chat-scoped uploads are realtime-broadcastable; avatars
            # and knowledge uploads aren't rendered in chat timelines.
            if rec.chat_id:
                await emit(
                    FileReady(
                        data={
                            "group_id": rec.chat_id,
                            "file_id": rec.id,
                            "filename": rec.filename,
                            "mime": rec.mime,
                            "size": rec.size,
                            "url": f"/api/v1/uploads/{rec.id}",
                        }
                    )
                )
        return result

    async def stream(
        self,
        file_id: str,
        requester_id: str,
        workspace: str,
    ) -> tuple[FileRecord, AsyncIterator[bytes]]:
        rec = await self._meta.get_scoped(file_id, workspace=workspace)
        if rec is None:
            raise NotFound()
        await self._assert_can_read(rec, requester_id, workspace)
        return rec, self._adapter.open(rec.storage_key)

    async def presigned_get(
        self,
        file_id: str,
        requester_id: str,
        workspace: str,
        ttl_seconds: int,
    ) -> tuple[FileRecord, str | None]:
        rec = await self._meta.get_scoped(file_id, workspace=workspace)
        if rec is None:
            raise NotFound()
        await self._assert_can_read(rec, requester_id, workspace)
        url = await self._adapter.presigned_get(rec.storage_key, ttl_seconds)
        return rec, url

    async def delete(
        self,
        file_id: str,
        requester_id: str,
        workspace: str,
    ) -> None:
        rec = await self._meta.get_scoped(file_id, workspace=workspace)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            raise NotFound()
        # Mark deleted in Mongo first — if the adapter call fails, the record
        # stays tombstoned and the blob becomes an orphan (picked up by a
        # future cleanup job) rather than silently surviving visibility.
        await self._meta.soft_delete_scoped(file_id, workspace=workspace)
        await self._adapter.delete(rec.storage_key)
        if rec.chat_id:
            await emit(
                FileDeleted(
                    data={
                        "group_id": rec.chat_id,
                        "file_id": rec.id,
                    }
                )
            )
