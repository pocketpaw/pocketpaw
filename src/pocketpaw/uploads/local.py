"""Local-disk StorageAdapter backed by aiofiles.

2026-09-14 (feat/uploads-multipart-endpoints): added ``list_parts``, the relay
counterpart of S3's ListParts. Each part now also writes a ``<n>.etag`` sidecar
when its bytes land, so the answer costs a few bytes per part instead of
re-hashing the payload; a missing sidecar is hashed on the spot, so parts
written by an older build still list correctly.

2026-09-14 (feat/uploads-multipart-adapter): added the multipart relay. Local
disk cannot presign, so parts are PUT to the API and land as ``<key>.part/<n>``;
``complete_multipart`` concatenates them in part order and fsyncs, ``abort``
removes the directory. Part numbers are validated before they become filenames.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import aiofiles
import aiofiles.os

from pocketpaw.uploads.adapter import StorageAdapter, StorageItem, StoredObject
from pocketpaw.uploads.config import validate_part_number
from pocketpaw.uploads.errors import AccessDenied, NotFound, StorageFailure

_CHUNK_SIZE = 64 * 1024

#: Suffix of the per-upload part directory that sits beside the final key.
_PART_DIR_SUFFIX = ".part"

#: Session metadata inside the part dir. Named with a leading dot so it can
#: never collide with a part file, which is always a bare decimal number.
_PART_META_NAME = ".meta.json"


class LocalStorageAdapter(StorageAdapter):
    """Store blobs under ``root``. Atomic writes via .tmp + rename.

    Rejects keys that would escape ``root`` after normalization.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        target = (self._root / key).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise AccessDenied(f"key escapes storage root: {key!r}") from exc
        return target

    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str) -> StoredObject:
        final = self._resolve(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(final.name + ".tmp")
        size = 0
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                async for chunk in stream:
                    await fh.write(chunk)
                    size += len(chunk)
            await aiofiles.os.replace(str(tmp), str(final))
        except Exception as exc:
            # Best-effort cleanup of the partial .tmp
            try:
                await aiofiles.os.remove(str(tmp))
            except FileNotFoundError:
                pass
            raise StorageFailure(str(exc)) from exc
        return StoredObject(key=key, size=size, mime=mime)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        target = self._resolve(key)
        if not target.exists():
            raise NotFound(f"missing: {key}")
        async with aiofiles.open(target, "rb") as fh:
            while True:
                chunk = await fh.read(_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    async def delete(self, key: str) -> None:
        target = self._resolve(key)
        try:
            await aiofiles.os.remove(str(target))
        except FileNotFoundError:
            pass

    async def exists(self, key: str) -> bool:
        target = self._resolve(key)
        return target.exists()

    def local_path(self, key: str) -> Path | None:
        try:
            target = self._resolve(key)
        except AccessDenied:
            return None
        return target if target.exists() else None

    async def presigned_get(
        self,
        key: str,
        ttl_seconds: int,
        response_content_disposition: str | None = None,
    ) -> str | None:
        # Local disk can't mint a public URL. Callers fall back to the
        # HMAC-signed ``/uploads/{id}?t=...`` proxy, which sets its own
        # Content-Disposition — so the disposition hint is unused here.
        return None

    async def list_prefix(self, prefix: str) -> list[str]:
        """Return child names (files and directories) under ``prefix``.

        Non-recursive — only the immediate children. Returns an empty list
        when the prefix path does not exist or is not a directory.
        """
        try:
            parent = self._resolve(prefix)
        except Exception:
            return []
        if not parent.is_dir():
            return []
        names: list[str] = []
        for entry in parent.iterdir():
            if entry.name.startswith(".") or _is_part_dir(entry):
                continue
            names.append(entry.name)
        return sorted(names)

    async def browse(self, prefix: str) -> list[StorageItem]:
        """List one directory level, returning files + sub-folders with metadata."""
        try:
            parent = self._resolve(prefix)
        except Exception:
            return []
        if not parent.is_dir():
            return []
        items: list[StorageItem] = []
        for entry in parent.iterdir():
            if entry.name.startswith(".") or _is_part_dir(entry):
                continue
            try:
                st = entry.stat()
                items.append(
                    StorageItem(
                        name=entry.name,
                        is_dir=entry.is_dir(),
                        size=st.st_size if entry.is_file() else 0,
                    )
                )
            except OSError:
                items.append(StorageItem(name=entry.name, is_dir=entry.is_dir()))
        items.sort(key=lambda x: (not x.is_dir, x.name.lower()))
        return items

    async def rename_key(self, old_key: str, new_key: str) -> None:
        """Rename a key on disk (atomic rename within the same filesystem)."""
        old_path = self._resolve(old_key)
        new_path = self._resolve(new_key)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        await aiofiles.os.rename(str(old_path), str(new_path))

    # --- Multipart relay ---------------------------------------------------

    def supports_presigned_parts(self) -> bool:
        """Local disk has no URL to sign. Callers relay via :meth:`put_part`."""
        return False

    def _part_dir(self, key: str) -> Path:
        """Part directory for ``key``, resolved through the same root guard."""
        return self._resolve(key + _PART_DIR_SUFFIX)

    def _read_meta(self, part_dir: Path, upload_id: str) -> dict:
        """Load the session metadata, verifying ``upload_id`` matches.

        The id is checked because the part dir is derived from the key alone:
        without it, a stale client PUTting into a re-created session would
        corrupt the new upload instead of being refused.
        """
        meta_path = part_dir / _PART_META_NAME
        if not meta_path.is_file():
            raise NotFound(f"unknown multipart upload: {upload_id}")
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError) as exc:
            raise StorageFailure(f"unreadable multipart metadata: {exc}") from exc
        if meta.get("upload_id") != upload_id:
            raise NotFound(f"unknown multipart upload: {upload_id}")
        return meta

    async def create_multipart(self, key: str, mime: str) -> str:
        """Create the part directory and return a fresh upload id."""
        part_dir = self._part_dir(key)
        upload_id = uuid.uuid4().hex
        try:
            part_dir.mkdir(parents=True, exist_ok=True)
            (part_dir / _PART_META_NAME).write_text(
                json.dumps({"upload_id": upload_id, "key": key, "mime": mime})
            )
        except OSError as exc:
            raise StorageFailure(str(exc)) from exc
        return upload_id

    async def sign_part(self, key: str, upload_id: str, part_number: int, ttl: int) -> str | None:
        """Always ``None`` — see :meth:`supports_presigned_parts`."""
        return None

    async def put_part(self, key: str, upload_id: str, part_number: int, body: bytes) -> str:
        """Write one part. Returns the md5 hex of its bytes as the etag.

        The etag only has to round-trip, so a content hash is enough — and it
        makes a re-PUT of the same part verifiably identical.
        """
        number = validate_part_number(part_number)
        part_dir = self._part_dir(key)
        self._read_meta(part_dir, upload_id)

        target = part_dir / str(number)
        tmp = target.with_name(f"{number}.tmp")
        etag = hashlib.md5(body, usedforsecurity=False).hexdigest()
        try:
            async with aiofiles.open(tmp, "wb") as fh:
                await fh.write(body)
            await aiofiles.os.replace(str(tmp), str(target))
            # Etag sidecar, written AFTER the part lands so it can never
            # advertise bytes that are not there. One file per part rather than
            # a field in the shared meta.json: parts arrive concurrently, and
            # concurrent writers to one metadata file lose each other's entries.
            # ``list_parts`` falls back to hashing the part when it is missing,
            # so a crash between the two writes costs a re-read, not a part.
            async with aiofiles.open(_etag_path(target), "w") as meta:
                await meta.write(etag)
        except OSError as exc:
            try:
                await aiofiles.os.remove(str(tmp))
            except FileNotFoundError:
                pass
            raise StorageFailure(str(exc)) from exc
        return etag

    async def list_parts(self, key: str, upload_id: str) -> list[tuple[int, str]]:
        """Every part on disk for this upload, ``(number, etag)``, sorted.

        The relay counterpart of S3's ListParts. We computed each etag when the
        bytes arrived, so this reads them back from the sidecars rather than
        re-hashing megabytes; a part whose sidecar is missing (written by an
        older build, or a crash between the two writes) is hashed on the spot
        so the answer is still complete.
        """
        part_dir = self._part_dir(key)
        self._read_meta(part_dir, upload_id)

        parts: list[tuple[int, str]] = []
        for entry in part_dir.iterdir():
            if not entry.is_file() or not entry.name.isdigit():
                continue
            etag_file = _etag_path(entry)
            try:
                if etag_file.is_file():
                    etag = etag_file.read_text().strip()
                else:
                    etag = await asyncio.to_thread(_hash_file, entry)
            except OSError as exc:
                raise StorageFailure(f"unreadable part {entry.name}: {exc}") from exc
            if etag:
                parts.append((int(entry.name), etag))

        parts.sort(key=lambda p: p[0])
        return parts

    async def complete_multipart(
        self, key: str, upload_id: str, parts: list[tuple[int, str]]
    ) -> StoredObject:
        """Concatenate the listed parts in part order, fsync, and rename into place.

        Etags are not re-checked: the bytes on disk are the only copy, so there
        is nothing to compare them against. Ordering comes from the part
        numbers, which is why an out-of-order ``parts`` list is fine.
        """
        part_dir = self._part_dir(key)
        meta = self._read_meta(part_dir, upload_id)

        ordered = sorted({validate_part_number(n) for n, _etag in parts})
        if not ordered:
            raise NotFound(f"no parts listed for upload: {upload_id}")

        final = self._resolve(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(final.name + ".tmp")
        # Off the event loop: this copies the whole file, which for the sizes
        # multipart exists to serve is seconds of blocking I/O.
        size = await asyncio.to_thread(_concat_parts, part_dir, ordered, tmp, final, upload_id)

        shutil.rmtree(part_dir, ignore_errors=True)
        return StoredObject(key=key, size=size, mime=meta.get("mime", ""))

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        """Remove the part directory. Never raises.

        A missing directory or a mismatched id is a no-op rather than an error:
        abort runs on cancel and cleanup paths, and it must only ever delete
        the session it was given.
        """
        try:
            part_dir = self._part_dir(key)
        except AccessDenied:
            return
        try:
            self._read_meta(part_dir, upload_id)
        except (NotFound, StorageFailure):
            return
        shutil.rmtree(part_dir, ignore_errors=True)


def _etag_path(part: Path) -> Path:
    """Sidecar holding a part's etag. Never collides with a part file, which is
    always a bare decimal number."""
    return part.with_name(f"{part.name}.etag")


def _hash_file(path: Path) -> str:
    """The etag a part WOULD have had, recomputed from its bytes.

    Only reached when the sidecar is missing. Streams rather than reading the
    whole part, because this runs on the relay path where a part can be tens of
    megabytes.
    """
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _is_part_dir(entry: Path) -> bool:
    """True for an in-progress multipart scratch directory.

    Hidden from listings: it is transient, and a user browsing mid-upload
    should not see a folder called ``raw.mov.part`` beside their files.
    """
    return entry.name.endswith(_PART_DIR_SUFFIX) and entry.is_dir()


def _concat_parts(
    part_dir: Path, ordered: list[int], tmp: Path, final: Path, upload_id: str
) -> int:
    """Concatenate parts into ``tmp``, fsync, rename onto ``final``. Sync — run
    in a thread. Returns the assembled size."""
    size = 0
    try:
        with open(tmp, "wb") as out:
            for number in ordered:
                source = part_dir / str(number)
                if not source.is_file():
                    raise NotFound(f"missing part {number} for upload: {upload_id}")
                with open(source, "rb") as src:
                    while True:
                        chunk = src.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
                        size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, final)
    except NotFound:
        _unlink_quietly(tmp)
        raise
    except OSError as exc:
        _unlink_quietly(tmp)
        raise StorageFailure(str(exc)) from exc
    return size


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
