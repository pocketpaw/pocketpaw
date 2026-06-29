"""StorageAdapter protocol — the swap point for local, S3, etc."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class StoredObject:
    """Return value of ``StorageAdapter.put``."""

    key: str
    size: int
    mime: str


@dataclass
class StorageItem:
    """One entry in a directory listing returned by ``browse``."""

    name: str
    is_dir: bool
    size: int = 0


class StorageAdapter(Protocol):
    """Abstract byte storage. Knows nothing about metadata, auth, or mime logic.

    Implementations must be safe to call from asyncio contexts.
    """

    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str) -> StoredObject:
        """Persist ``stream`` at ``key``. Returns the canonical ``StoredObject``."""

    def open(self, key: str) -> AsyncIterator[bytes]:  # pragma: no cover
        """Yield the stored bytes in chunks. Raises ``NotFound`` if missing.

        Note: not ``async def`` — implementations are async generator
        functions (``async def`` + ``yield``), which Python types as
        ``AsyncIterator[bytes]`` when called (no ``await`` on the call).
        """

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present. Idempotent."""

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` is currently stored."""

    def local_path(self, key: str) -> Path | None:
        """Return an absolute local path to the blob, or ``None`` if unsupported.

        Lets the agent loop pass local files to built-in tools (e.g. Read)
        without streaming through HTTP. Remote adapters (S3, GCS) return
        ``None`` — the caller should fall back to streaming via ``open``.
        """

    async def presigned_get(
        self,
        key: str,
        ttl_seconds: int,
        response_content_disposition: str | None = None,
    ) -> str | None:
        """Return a time-limited public URL for reading ``key``.

        Adapters that natively support presigning (S3, GCS) return an
        absolute URL the browser can fetch without an Authorization header.
        Adapters that don't (local disk) return ``None``; the caller should
        fall back to its own signing scheme.

        ``response_content_disposition`` (when set) is forwarded so the served
        response carries that ``Content-Disposition`` — callers pass
        ``attachment; filename="…"`` to force a download for content that must
        not render inline on the storage origin (e.g. delivered HTML/SVG). The
        local adapter ignores it (it never presigns); ``None`` preserves the
        adapter/object default disposition.
        """

    async def list_prefix(self, prefix: str) -> list[str]:
        """List every key that starts with ``prefix`` (non-recursive, one level).

        Returns the unique "sub-directory" names (the next path segment after
        ``prefix``). S3 adapters return the CommonPrefixes from a
        delimiter'd ``list_objects_v2``. Local adapters return the child
        file/directory names.

        Adapters that don't support prefix listing return ``[]``.
        """

    async def browse(self, prefix: str) -> list[StorageItem]:
        """List one directory level. Returns both files and sub-folders.

        Each item carries its name, whether it is a directory, and the file
        size in bytes (zero for directories). The default (no-op)
        implementation returns empty — adapters override to provide actual
        listing.
        """
        return []

    async def rename_key(self, old_key: str, new_key: str) -> None:
        """Rename (move) a key from ``old_key`` to ``new_key``.

        The default raises ``NotImplementedError``. Adapters that support
        rename must implement this so the cloud project file endpoints can
        rename files and directories.
        """
        raise NotImplementedError("rename_key not supported by this adapter")
