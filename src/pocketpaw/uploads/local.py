"""Local-disk StorageAdapter backed by aiofiles."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiofiles
import aiofiles.os

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.errors import AccessDenied, NotFound, StorageFailure

_CHUNK_SIZE = 64 * 1024


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
