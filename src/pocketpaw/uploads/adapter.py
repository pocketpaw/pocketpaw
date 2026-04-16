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
