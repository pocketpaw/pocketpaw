"""UploadService — validates, stores, persists metadata, and generates thumbnails."""

from __future__ import annotations

import io
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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

logger = logging.getLogger(__name__)

# ── Thumbnail constants ────────────────────────────────────────────────
_THUMB_MAX_DIM = 2048
_THUMB_MAX_SOURCE = 50 * 1024 * 1024  # 50 MiB
_THUMB_CACHE_LIMIT_MB = 500
_THUMB_DEFAULT_Q = 80
_THUMB_FORMATS: dict[str, tuple[str, str, str]] = {
    "webp": ("WEBP", "image/webp", ".webp"),
    "jpeg": ("JPEG", "image/jpeg", ".jpg"),
    "jpg": ("JPEG", "image/jpeg", ".jpg"),
    "png": ("PNG", "image/png", ".png"),
}

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

    # ── Thumbnail ────────────────────────────────────────────────────

    async def thumbnail(
        self,
        file_id: str,
        requester_id: str,
        *,
        width: int = 0,
        height: int = 0,
        quality: int = _THUMB_DEFAULT_Q,
        fmt: str = "webp",
    ) -> tuple[FileRecord, str, AsyncIterator[bytes]]:
        """Return ``(record, mime_type, chunk_iterator)`` for a resized thumbnail."""
        rec = self._meta.get(file_id)
        if rec is None:
            raise NotFound()
        if rec.owner_id != requester_id:
            raise NotFound()
        return await generate_thumbnail(
            self._adapter,
            rec,
            self._cfg.local_root,
            width=width,
            height=height,
            quality=quality,
            fmt=fmt,
        )

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


# ── Shared thumbnail generator ────────────────────────────────────────


async def generate_thumbnail(
    adapter: StorageAdapter,
    record: FileRecord,
    cache_root: Path | None = None,
    *,
    width: int = 0,
    height: int = 0,
    quality: int = _THUMB_DEFAULT_Q,
    fmt: str = "webp",
) -> tuple[FileRecord, str, AsyncIterator[bytes]]:
    """Fetch *record*'s original from *adapter*, resize, cache, and stream.

    This is a module-level function so both ``UploadService`` (OSS) and
    ``EEUploadService`` can call it after their own auth + metadata lookup.
    """

    mime = (record.mime or "").lower()
    if not mime.startswith("image/"):
        raise NotFound("file is not an image")

    w = min(max(width, 0), _THUMB_MAX_DIM)
    h = min(max(height, 0), _THUMB_MAX_DIM)
    if w == 0 and h == 0:
        w = 256
    q = min(max(quality, 1), 100)
    fmt_key = fmt.lower() if fmt.lower() in _THUMB_FORMATS else "webp"
    pil_fmt, out_mime, _out_ext = _THUMB_FORMATS[fmt_key]

    cache_dir = (
        (cache_root / "_thumbs")
        if cache_root
        else (Path.home() / ".pocketpaw" / "uploads" / "_thumbs")
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    flat_key = record.storage_key.replace("/", "_").replace("\\", "_")
    cache_file = cache_dir / f"{flat_key}_{w}x{h}_q{q}.{fmt_key}"

    # Cache hit — stream from disk.
    if cache_file.exists():
        try:
            os.utime(cache_file, None)
        except OSError:
            pass
        return record, out_mime, _stream_file(cache_file)

    # Evict if needed.
    await _evict_thumb_cache(cache_dir, _THUMB_CACHE_LIMIT_MB * 1024 * 1024)

    # Fetch, resize.
    thumb_bytes = await _resize_from_adapter(adapter, record.storage_key, w, h, q, pil_fmt, fmt_key)

    # Write-through cache (atomic).
    tmp = cache_file.with_suffix(cache_file.suffix + ".tmp")
    try:
        import aiofiles as aio

        async with aio.open(tmp, "wb") as fh:
            await fh.write(thumb_bytes)
        await aio.os.replace(str(tmp), str(cache_file))
    except Exception:
        try:
            os.remove(str(tmp))
        except FileNotFoundError:
            pass

    return record, out_mime, _bytes_iter(thumb_bytes)


async def _resize_from_adapter(
    adapter: StorageAdapter,
    storage_key: str,
    w: int,
    h: int,
    q: int,
    pil_fmt: str,
    fmt_key: str,
) -> bytes:
    """Fetch original bytes, resize with Pillow, return result."""
    import asyncio

    from PIL import Image as PILImage

    buf = io.BytesIO()
    total = 0
    try:
        async for chunk in adapter.open(storage_key):
            total += len(chunk)
            if total > _THUMB_MAX_SOURCE:
                raise TooLarge(f"source exceeds {_THUMB_MAX_SOURCE} bytes")
            buf.write(chunk)
    except (NotFound, TooLarge):
        raise
    except Exception as exc:
        raise NotFound("cannot read source") from exc
    if total == 0:
        raise NotFound("empty source")
    buf.seek(0)

    def _resize() -> bytes:
        img = PILImage.open(buf)
        if fmt_key in ("jpeg", "jpg", "webp"):
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
        elif img.mode == "P":
            img = img.convert("RGBA")
        size = (w, h) if (w and h) else ((w, w) if w else (h, h))
        img.thumbnail(size, PILImage.LANCZOS)
        out = io.BytesIO()
        kwargs: dict = {"format": pil_fmt}
        if fmt_key != "png":
            kwargs["quality"] = q
        else:
            kwargs["optimize"] = True
        img.save(out, **kwargs)
        return out.getvalue()

    try:
        return await asyncio.to_thread(_resize)
    except Exception as exc:
        raise NotFound("cannot resize") from exc


async def _stream_file(path: Path) -> AsyncIterator[bytes]:
    import aiofiles

    async with aiofiles.open(path, "rb") as fh:
        while True:
            chunk = await fh.read(64 * 1024)
            if not chunk:
                break
            yield chunk


async def _bytes_iter(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _evict_thumb_cache(cache_dir: Path, limit_bytes: int) -> None:
    import asyncio

    def _scan_and_evict() -> None:
        entries: list[tuple[float, Path, int]] = []
        total = 0
        try:
            for entry in cache_dir.iterdir():
                if entry.is_file() and not entry.name.endswith(".tmp"):
                    try:
                        st = entry.stat()
                        entries.append((st.st_atime, entry, st.st_size))
                        total += st.st_size
                    except OSError:
                        pass
        except OSError:
            return
        if total <= limit_bytes:
            return
        entries.sort(key=lambda e: e[0])
        to_free = total - limit_bytes
        freed = 0
        for _, path, size in entries:
            if freed >= to_free:
                break
            try:
                os.remove(str(path))
                freed += size
            except (FileNotFoundError, OSError):
                pass

    await asyncio.to_thread(_scan_and_evict)
