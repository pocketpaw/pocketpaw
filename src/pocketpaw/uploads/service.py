"""UploadService — validates, stores, and persists metadata."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import UploadFile

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.config import UploadSettings, extension_for
from pocketpaw.uploads.errors import (
    EmptyFile,
    NotFound,
    StorageFailure,
    TooLarge,
    UnsupportedMime,
    UploadError,
)
from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore
from pocketpaw.uploads.keys import new_storage_key

_SNIFF_BYTES = 512


def _sniff_mime(head: bytes, fallback: str) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        # ZIP container — docx/xlsx both use this. Keep fallback if it matches.
        if fallback in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ):
            return fallback
    return fallback


FailCode = Literal["too_large", "unsupported_mime", "empty", "storage_error"]


@dataclass
class FailedUpload:
    filename: str
    reason: str
    code: FailCode


@dataclass
class BulkUploadResult:
    uploaded: list[FileRecord]
    failed: list[FailedUpload]


class UploadService:
    def __init__(
        self,
        adapter: StorageAdapter,
        meta: JSONLFileStore,
        cfg: UploadSettings,
    ) -> None:
        self._adapter = adapter
        self._meta = meta
        self._cfg = cfg

    async def upload(self, file: UploadFile, owner_id: str, chat_id: str | None) -> FileRecord:
        result = await self.upload_many([file], owner_id, chat_id)
        if result.failed:
            f = result.failed[0]
            _raise(f.code, f.reason)
        return result.uploaded[0]

    async def upload_many(
        self,
        files: list[UploadFile],
        owner_id: str,
        chat_id: str | None,
    ) -> BulkUploadResult:
        if not files:
            raise ValueError("empty upload batch")
        if len(files) > self._cfg.max_files_per_batch:
            raise ValueError(f"too many files: {len(files)} > {self._cfg.max_files_per_batch}")

        uploaded: list[FileRecord] = []
        failed: list[FailedUpload] = []

        for file in files:
            try:
                rec = await self._upload_one(file, owner_id, chat_id)
                uploaded.append(rec)
            except TooLarge as e:
                failed.append(
                    FailedUpload(filename=_basename(file.filename), reason=str(e), code="too_large")
                )
            except UnsupportedMime as e:
                failed.append(
                    FailedUpload(
                        filename=_basename(file.filename), reason=str(e), code="unsupported_mime"
                    )
                )
            except EmptyFile as e:
                failed.append(
                    FailedUpload(filename=_basename(file.filename), reason=str(e), code="empty")
                )
            except StorageFailure as e:
                failed.append(
                    FailedUpload(
                        filename=_basename(file.filename), reason=str(e), code="storage_error"
                    )
                )

        return BulkUploadResult(uploaded=uploaded, failed=failed)

    async def _upload_one(
        self,
        file: UploadFile,
        owner_id: str,
        chat_id: str | None,
    ) -> FileRecord:
        head = await file.read(_SNIFF_BYTES)
        if not head:
            raise EmptyFile()

        # Size-check the head first so TooLarge beats UnsupportedMime when both apply.
        cap = self._cfg.max_file_bytes
        if len(head) > cap:
            raise TooLarge(f"file exceeds {cap} bytes")

        mime = _sniff_mime(head, file.content_type or "application/octet-stream")
        if mime not in self._cfg.allowed_mimes:
            raise UnsupportedMime(f"mime not allowed: {mime}")

        ext = extension_for(mime)
        key = new_storage_key("chat", ext)

        first = head

        async def _body() -> AsyncIterator[bytes]:
            size = len(first)
            if size > cap:
                raise TooLarge(f"file exceeds {cap} bytes")
            yield first
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    raise TooLarge(f"file exceeds {cap} bytes")
                yield chunk

        try:
            obj = await self._adapter.put(key, _body(), mime)
        except StorageFailure as e:
            # Check if the root cause was our TooLarge (wrapped by LocalStorageAdapter)
            if isinstance(e.__cause__, TooLarge):
                raise e.__cause__
            raise

        file_id = uuid.uuid4().hex
        filename = _basename(file.filename) or "upload"
        record = FileRecord(
            id=file_id,
            storage_key=obj.key,
            filename=filename,
            mime=obj.mime,
            size=obj.size,
            owner_id=owner_id,
            chat_id=chat_id,
            created=datetime.now(UTC),
        )
        self._meta.save(record)
        return record

    async def stream(
        self, file_id: str, requester_id: str
    ) -> tuple[FileRecord, AsyncIterator[bytes]]:
        rec = self._meta.get(file_id)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            raise NotFound()
        return rec, self._adapter.open(rec.storage_key)

    async def presigned_get(
        self, file_id: str, requester_id: str, ttl_seconds: int
    ) -> tuple[FileRecord, str | None]:
        """Return (record, presigned_url_or_None) for ``file_id``.

        Delegates to the adapter's ``presigned_get``. Callers that get ``None``
        should fall back to their own signing scheme (e.g. HMAC proxy URL).
        """
        rec = self._meta.get(file_id)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            raise NotFound()
        url = await self._adapter.presigned_get(rec.storage_key, ttl_seconds)
        return rec, url

    async def delete(self, file_id: str, requester_id: str) -> None:
        rec = self._meta.get(file_id)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            raise NotFound()
        # Tombstone metadata before unlinking the blob so a mid-op crash
        # leaves an orphan blob (cleanable) rather than a dangling record.
        self._meta.soft_delete(file_id)
        await self._adapter.delete(rec.storage_key)


def _basename(name: str | None) -> str:
    if not name:
        return ""
    return os.path.basename(name.replace("\\", "/"))


def _raise(code: FailCode, reason: str) -> None:
    mapping: dict[FailCode, type[UploadError]] = {
        "too_large": TooLarge,
        "unsupported_mime": UnsupportedMime,
        "empty": EmptyFile,
        "storage_error": StorageFailure,
    }
    raise mapping[code](reason)
