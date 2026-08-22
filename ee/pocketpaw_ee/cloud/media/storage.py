# ee/pocketpaw_ee/cloud/media/storage.py — where generated + uploaded media lives.
#
# The /studio gallery (direct generations + the canvas "save edited image"
# upload + the legacy agent-side file list) is backed by the SAME storage swap
# the Files / Knowledge Base uploads use: ``pocketpaw.uploads.build_adapter``.
#
#   * POCKETPAW_UPLOAD_ADAPTER=local (default) — files land under
#     ~/.pocketpaw/generated/<name> (key prefix "generated/", local root
#     ~/.pocketpaw). This is exactly the layout the studio shipped with, so the
#     agent-side media MCP files (which write that dir directly) still surface.
#   * POCKETPAW_UPLOAD_ADAPTER=s3 — keys "generated/<name>" in S3_PRIVATE_BUCKET.
#     Durable + shared across instances; the media MCP's local files do NOT
#     surface in this mode (that agent path still writes local disk).
#
# Generated filenames carry a unix-ms timestamp prefix ("<ms>-<uuid>.png") so a
# remote adapter listing (which has no mtime) can still sort newest-first and
# report a meaningful ``modified``.
#
# Created 2026-08-17 (studio-media-s3): new storage module.

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.factory import build_adapter

# Local root for the LOCAL adapter. S3 ignores it entirely (keys are the same in
# both modes — "generated/<name>" — so the local path becomes ~/.pocketpaw/<key>).
MEDIA_ROOT = Path.home() / ".pocketpaw"
MEDIA_KEY_PREFIX = "generated"


def media_key(name: str) -> str:
    """The storage key for a gallery file (both adapters)."""
    return f"{MEDIA_KEY_PREFIX}/{name}"


def name_from_key(key: str) -> str:
    """Strip the media prefix back to the filename ('' for a non-media key)."""
    prefix = f"{MEDIA_KEY_PREFIX}/"
    return key[len(prefix) :] if key.startswith(prefix) else ""


def modified_from_name(name: str) -> int:
    """Best-effort ``modified`` (unix ms) from a timestamp-prefixed filename —
    "<ms>-<uuid>.png". Returns 0 for names without the prefix (uploads, old
    files) so remote listing still returns a number the frontend expects."""
    first = name.split("-", 1)[0]
    return int(first) if first.isdigit() else 0


async def bytes_stream(data: bytes) -> AsyncIterator[bytes]:
    """Adapt an in-memory byte payload into the async byte stream
    ``StorageAdapter.put`` consumes (generated PNGs / uploads are small — no
    chunked file read needed)."""
    yield data


async def save_generated(data: bytes, *, mime: str, ext: str = "png") -> str:
    """Persist a freshly generated blob (image / audio / …) through the media
    adapter and return its backend-relative ``/api/v1/media/<name>`` URL.

    The name carries a unix-ms prefix so a remote listing can sort newest-first
    and report ``modified``. Used by BOTH the direct /studio path and the
    agent-side media MCP so every generated asset lands on the configured
    storage (local disk in dev, S3 in a POCKETPAW_UPLOAD_ADAPTER=s3 deploy)."""
    name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}.{ext.lstrip('.')}"
    await get_adapter().put(media_key(name), bytes_stream(data), mime)
    return f"/api/v1/media/{name}"


# Module-level singleton, same pattern as the uploads router — one adapter per
# process. Tests swap ``_ADAPTER`` for a tmp-backed local adapter.
_ADAPTER: StorageAdapter = build_adapter(MEDIA_ROOT)


def get_adapter() -> StorageAdapter:
    """Return the process's media storage adapter."""
    return _ADAPTER


def local_generated_dir() -> Path | None:
    """The on-disk generated directory for the LOCAL adapter, or None when the
    configured adapter is remote (S3). Lets the list/serve routes branch on the
    live backend instead of sniffing env."""
    return get_adapter().local_path(f"{MEDIA_KEY_PREFIX}/")


__all__ = [
    "MEDIA_ROOT",
    "MEDIA_KEY_PREFIX",
    "media_key",
    "name_from_key",
    "modified_from_name",
    "bytes_stream",
    "save_generated",
    "get_adapter",
    "local_generated_dir",
]
