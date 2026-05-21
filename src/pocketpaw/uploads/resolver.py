"""Turn an upload URL into a local disk path for the agent loop.

The chat bridge receives media entries like ``/api/v1/uploads/{id}`` from the
frontend. Agents consume local paths (Read tool, image blocks). This module
bridges the two.

OSS-only — EE has its own workspace-scoped resolver alongside its Mongo store.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import aiofiles
import aiofiles.os

from pocketpaw._registry import first
from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.config import extension_for
from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore


@dataclass(frozen=True)
class ResolvedMedia:
    """One entry of the resolved media list.

    ``path`` is the string the agent loop will inject into the prompt
    (absolute disk path for resolved upload URLs, original string for
    passthroughs). ``record`` carries the metadata when the entry was
    resolved from an upload store — used to build the richer prompt
    block (filename, mime, size). ``None`` for passthroughs / non-upload
    entries where we don't know the metadata.
    """

    path: str
    record: FileRecord | None


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


async def _resolve_via_ee_mongo(
    file_id: str,
) -> tuple[Path | None, FileRecord | None]:
    """Fallback: look up ``file_id`` in the EE Mongo store with no workspace
    filter. Returns ``(None, None)`` if EE isn't installed or Mongo can't
    reach the id.

    Intended for single-user self-hosted deployments where the EE router is
    mounted (uploads land in Mongo) but chat still goes through the OSS
    endpoint. Multi-tenant cloud chat should route through EE with explicit
    workspace context instead of calling this.
    """
    provider = first("pocketpaw.storage_backends")
    if provider is None:
        return None, None
    EE_ADAPTER = provider.adapter()
    EE_META = provider.meta()

    try:
        rec = await EE_META.get_unscoped(file_id)
    except Exception:
        logger.exception("EE mongo lookup failed for file_id=%s", file_id)
        return None, None
    if rec is None:
        return None, None
    try:
        return EE_ADAPTER.local_path(rec.storage_key), rec
    except Exception:
        logger.exception(
            "EE adapter.local_path failed for file_id=%s storage_key=%s",
            file_id,
            rec.storage_key,
        )
        return None, rec


async def resolve_media_paths_any(media: list[str]) -> list[str]:
    """Async counterpart to :func:`resolve_media_paths` that falls back to
    the EE Mongo store when the OSS JSONL lookup misses. Returns only the
    path strings; use :func:`resolve_media_with_records` when callers need
    per-entry metadata (filename / mime / size) for richer prompts.
    """
    resolved = await resolve_media_with_records(media)
    return [r.path for r in resolved]


async def resolve_media_with_records(media: list[str]) -> list[ResolvedMedia]:
    """Return path + FileRecord pairs for each resolvable media entry.

    - Upload URL that resolves via OSS JSONL → ``ResolvedMedia(path, rec)``
    - Upload URL that resolves via EE Mongo fallback → ``ResolvedMedia(path, rec)``
    - Non-upload string (already a path / opaque token) → ``ResolvedMedia(str, None)``
    - Upload URL that resolves nowhere → dropped with a warning log

    When the storage adapter doesn't expose a local path (S3 et al.), the
    blob is streamed to a local cache file so the agent loop gets a real
    path to read from. Repeat lookups for the same file hit the cache.

    Callers that only want the paths can use :func:`resolve_media_paths_any`.
    """
    resolver = default_resolver()
    out: list[ResolvedMedia] = []
    for entry in media:
        fid = parse_upload_url(entry)
        if fid is None:
            out.append(ResolvedMedia(path=entry, record=None))
            continue

        # Locate the metadata record — OSS JSONL first, then EE Mongo.
        rec = resolver._meta.get(fid)
        adapter: StorageAdapter = resolver._adapter
        if rec is None:
            ee_path, ee_rec = await _resolve_via_ee_mongo(fid)
            if ee_rec is not None:
                rec = ee_rec
                # _resolve_via_ee_mongo only returns a record when a storage
                # backend provider is registered, so first() is non-None here.
                _sb = first("pocketpaw.storage_backends")
                if _sb is not None:
                    adapter = _sb.adapter()
                # EE's LocalStorageAdapter may have given us a local path already.
                if ee_path is not None:
                    out.append(ResolvedMedia(path=str(ee_path), record=rec))
                    continue

        if rec is None:
            logger.warning("dropping unresolvable upload entry: %s", entry)
            continue

        # Prefer a native local path (LocalStorageAdapter). Otherwise stream
        # the remote blob to the local cache so the agent loop can read it.
        try:
            local = adapter.local_path(rec.storage_key)
        except Exception:
            logger.exception("adapter.local_path failed for file_id=%s", fid)
            local = None

        if local is None:
            local = await _materialize_remote(adapter, rec)

        if local is None:
            logger.warning("dropping unresolvable upload entry: %s", entry)
            continue
        out.append(ResolvedMedia(path=str(local), record=rec))
    return out


_REMOTE_CACHE_DIR = Path.home() / ".pocketpaw" / "uploads" / "_remote_cache"


async def _materialize_remote(adapter: StorageAdapter, rec: FileRecord) -> Path | None:
    """Stream a remote blob into the local cache and return its path.

    The cache is keyed by ``file_id`` so repeat calls reuse the download.
    Agents can pass the returned path to Read / image tools without caring
    that the original bytes live in S3.
    """
    try:
        await aiofiles.os.makedirs(str(_REMOTE_CACHE_DIR), exist_ok=True)
    except Exception:
        logger.exception("failed to create remote upload cache dir")
        return None

    ext = extension_for(rec.mime)
    target = _REMOTE_CACHE_DIR / f"{rec.id}{ext}"
    if target.exists():
        return target

    # Unique .part suffix so two concurrent calls for the same file_id don't
    # race each other into the same tmp path (POSIX silently interleaves,
    # Windows blocks the second open). The first one to os.replace wins and
    # the loser is cleaned up in the except branch.
    tmp = target.with_suffix(f"{target.suffix}.{uuid.uuid4().hex}.part")
    try:
        async with aiofiles.open(tmp, "wb") as fh:
            async for chunk in adapter.open(rec.storage_key):
                await fh.write(chunk)
        await aiofiles.os.replace(str(tmp), str(target))
    except Exception:
        logger.exception(
            "failed to materialize remote blob file_id=%s storage_key=%s",
            rec.id,
            rec.storage_key,
        )
        try:
            await aiofiles.os.remove(str(tmp))
        except FileNotFoundError:
            pass
        return None
    return target


# Keep JSONLFileStore importable for type-friendly call sites.
__all__ = [
    "JSONLFileStore",
    "ResolvedMedia",
    "UploadResolver",
    "default_resolver",
    "parse_upload_url",
    "resolve_media_paths",
    "resolve_media_paths_any",
    "resolve_media_with_records",
]
