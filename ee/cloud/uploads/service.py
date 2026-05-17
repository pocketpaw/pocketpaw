# service.py — EEUploadService: workspace-scoped upload pipeline on top of OSS.
# Updated: 2026-05-17 — pocketpaw#1118 P1. Added ``write_text_file()`` —
#   a programmatic text-content write path for callers that have bytes
#   in memory rather than an inbound HTTP ``UploadFile``. The cloud
#   planner uses it to land PRD / goal.md / plan.json into the
#   workspace Files panel without spinning a fake multipart request.
#   Same MongoFileStore + StorageAdapter + FileReady emit pipeline as
#   the HTTP path — only the source-of-bytes differs.
# Updated: 2026-04-30 — Stage 1.B "Files as Knowledge". FileReady now fires
#   for every successful upload (chat-scoped or workspace-only) and always
#   carries workspace_id so downstream subscribers (the KB indexer) can route
#   the file into the right scope without a follow-up Mongo lookup.
# Updated: 2026-05-03 — Stage 3.E "Files as Knowledge". ``upload_many`` /
#   ``upload`` now thread ``pocket_id`` from the router through to
#   ``MongoFileStore.save_scoped`` and into the FileReady event payload.
#   The KB listener picks up the new key and routes the article into
#   ``pocket:{id}`` instead of ``workspace:{wid}``. Storage layout is
#   unchanged — partitioning is metadata-only (Captain Option A).
"""EEUploadService — workspace-scoped upload pipeline on top of the OSS service."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import FileDeleted, FileReady
from ee.cloud.uploads.mongo_store import MongoFileStore
from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import NotFound
from pocketpaw.uploads.factory import build_adapter
from pocketpaw.uploads.file_store import FileRecord
from pocketpaw.uploads.keys import new_storage_key
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
        pocket_id: str | None = None,
    ) -> FileRecord:
        result = await self.upload_many(
            [file],
            owner_id,
            chat_id,
            workspace,
            folder_path=folder_path,
            pocket_id=pocket_id,
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
        pocket_id: str | None = None,
    ) -> BulkUploadResult:
        # Delegate validation + adapter writes; metadata is discarded inside OSS
        result = await self._oss.upload_many(files, owner_id, chat_id)
        # Persist each successful record in Mongo with workspace + pocket scoping.
        for rec in result.uploaded:
            await self._meta.save_scoped(
                rec,
                workspace=workspace,
                folder_path=folder_path,
                pocket_id=pocket_id,
            )
            # Emit FileReady for every successful upload. Chat-scoped rows
            # carry ``group_id`` so the timeline broadcast still works;
            # workspace-only uploads (avatars, KB files) skip the broadcast
            # but still fire local subscribers (the KB indexer). Pocket-
            # scoped uploads carry ``pocket_id`` so the KB listener routes
            # the article into ``pocket:{id}`` rather than the workspace
            # pool.
            data: dict = {
                "workspace_id": workspace,
                "file_id": rec.id,
                "filename": rec.filename,
                "mime": rec.mime,
                "size": rec.size,
                "storage_key": rec.storage_key,
                "url": f"/api/v1/uploads/{rec.id}",
            }
            if rec.chat_id:
                data["group_id"] = rec.chat_id
            if pocket_id:
                data["pocket_id"] = pocket_id
            await emit(FileReady(data=data))
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
        # Emit FileDeleted on every delete so subscribers can prune cached
        # state (KB index, search caches). Chat-scoped rows include group_id
        # for the timeline broadcast; workspace-only rows skip it.
        data: dict = {
            "workspace_id": workspace,
            "file_id": rec.id,
        }
        if rec.chat_id:
            data["group_id"] = rec.chat_id
        await emit(FileDeleted(data=data))


# ---------------------------------------------------------------------------
# Programmatic write helper (pocketpaw#1118 P1)
# ---------------------------------------------------------------------------


_PLANNER_EXTENSION_BY_MIME = {
    "text/markdown": "md",
    "text/plain": "txt",
    "application/json": "json",
}


async def write_text_file(
    *,
    workspace_id: str,
    owner_id: str,
    folder_path: str,
    filename: str,
    content: str | bytes,
    mime: str = "text/markdown",
    pocket_id: str | None = None,
) -> FileRecord:
    """Persist a text blob into the workspace Files panel.

    Programmatic counterpart to ``EEUploadService.upload`` for callers
    that have bytes in memory rather than a FastAPI ``UploadFile``. The
    cloud planner uses it to land PRD / goal.md / plan.json against a
    Project without spinning a fake multipart request. Same
    ``MongoFileStore`` + ``StorageAdapter`` + ``FileReady`` emit pipeline
    the HTTP path uses — only the source-of-bytes differs.

    Lives at module scope (not on :class:`EEUploadService`) because most
    callers (services, listeners, jobs) don't have a built service
    instance and shouldn't have to thread one through. Module-level
    state stays minimal — the StorageAdapter is built per-call against
    ``~/.pocketpaw/uploads`` so tests that monkeypatch ``Path.home``
    get isolation for free.

    Returns the persisted :class:`FileRecord` so callers can grab
    ``rec.id`` for downstream linking (PlanSession.prd_file_id, etc.).
    """

    if not workspace_id:
        raise ValueError("write_text_file: workspace_id is required")
    if not filename:
        raise ValueError("write_text_file: filename is required")

    root = Path.home() / ".pocketpaw" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    adapter = build_adapter(root)

    payload = content.encode("utf-8") if isinstance(content, str) else content
    storage_key = new_storage_key("planner", _PLANNER_EXTENSION_BY_MIME.get(mime, "bin"))

    async def _body() -> AsyncIterator[bytes]:
        yield payload

    obj = await adapter.put(storage_key, _body(), mime)

    rec = FileRecord(
        id=uuid4().hex,
        storage_key=obj.key,
        filename=filename,
        mime=obj.mime,
        size=obj.size,
        owner_id=owner_id,
        chat_id=None,
        created=datetime.now(UTC),
    )

    store = MongoFileStore()
    await store.save_scoped(
        rec,
        workspace=workspace_id,
        folder_path=folder_path or "/",
        pocket_id=pocket_id,
    )

    # Mirror the FileReady payload the HTTP path emits so the KB indexer
    # and the unified Files panel don't need a separate code path for
    # planner-written files.
    data: dict = {
        "workspace_id": workspace_id,
        "file_id": rec.id,
        "filename": rec.filename,
        "mime": rec.mime,
        "size": rec.size,
        "storage_key": rec.storage_key,
        "url": f"/api/v1/uploads/{rec.id}",
    }
    if pocket_id:
        data["pocket_id"] = pocket_id
    await emit(FileReady(data=data))

    return rec
