"""Turn an upload URL into a local disk path for the agent loop.

The chat bridge receives media entries like ``/api/v1/uploads/{id}`` from the
frontend. Agents consume local paths (Read tool, image blocks). This module
bridges the two.

OSS-only — EE has its own workspace-scoped resolver alongside its Mongo store.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Protocol

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore

logger = logging.getLogger(__name__)

_UPLOAD_URL_RE = re.compile(r"^/api/v1/uploads/(?P<id>[A-Za-z0-9_-]+)$")


def parse_upload_url(url: str) -> str | None:
    """Extract the file_id from an upload URL.

    Returns ``None`` for anything that isn't a canonical upload URL:
    disk paths, other API routes, blob URLs, empty strings, etc.
    """
    if not url:
        return None
    m = _UPLOAD_URL_RE.match(url)
    return m.group("id") if m else None


class _MetaReader(Protocol):
    def get(self, file_id: str) -> FileRecord | None: ...


class UploadResolver:
    """Look up upload URLs in a metadata store and map to local disk paths.

    Returns ``None`` for unresolvable URLs: unknown file_id, soft-deleted
    records, blobs that have vanished from disk, or adapters that don't
    support ``local_path`` (e.g. future S3 adapter).
    """

    def __init__(self, adapter: StorageAdapter, meta: _MetaReader) -> None:
        self._adapter = adapter
        self._meta = meta

    def resolve(self, url: str) -> Path | None:
        file_id = parse_upload_url(url)
        if file_id is None:
            return None
        rec = self._meta.get(file_id)
        if rec is None:
            return None
        # Contain unexpected adapter failures (permission errors on the
        # storage root, remount races, future remote adapters) so chat
        # never crashes over a bad attachment — just drops the entry.
        try:
            return self._adapter.local_path(rec.storage_key)
        except Exception:
            logger.exception(
                "upload adapter.local_path failed for file_id=%s storage_key=%s",
                file_id,
                rec.storage_key,
            )
            return None


def resolve_media_paths(
    media: list[str],
    *,
    resolver: UploadResolver,
) -> list[str]:
    """Map each media entry to a local path string.

    - Upload URLs that resolve → absolute disk path as a string.
    - Upload URLs that don't resolve → dropped silently (orphan/deleted).
    - Non-upload strings (already local paths, opaque tokens) → passthrough.

    Order is preserved; dropped unresolvable URLs do not leave gaps.
    """
    out: list[str] = []
    for entry in media:
        fid = parse_upload_url(entry)
        if fid is None:
            out.append(entry)
            continue
        path = resolver.resolve(entry)
        if path is None:
            # Upload-URL-shaped but unresolvable: record missing, record
            # soft-deleted, blob gone, or adapter failure. Log so the next
            # time a user says "the agent ignored my file" the trail is
            # visible in server logs.
            logger.warning("dropping unresolvable upload entry: %s", entry)
            continue
        out.append(str(path))
    return out


def default_resolver() -> UploadResolver:
    """Return the resolver wired to the OSS /uploads singletons.

    Imported lazily so test code can stub the module-level singletons before
    this is called.
    """
    from pocketpaw.api.v1.uploads import _ADAPTER, _META

    return UploadResolver(adapter=_ADAPTER, meta=_META)


# Keep JSONLFileStore importable for type-friendly call sites.
__all__ = [
    "UploadResolver",
    "default_resolver",
    "parse_upload_url",
    "resolve_media_paths",
    "JSONLFileStore",
]
