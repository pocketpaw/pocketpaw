"""EE workspace-scoped upload URL resolver.

Wraps :class:`pocketpaw.uploads.resolver.UploadResolver` semantics but looks
up metadata in Mongo with workspace isolation. Cross-tenant lookups return
``None`` (treated as "not found" — no leak of existence across workspaces).
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.file_store import FileRecord
from pocketpaw.uploads.resolver import parse_upload_url
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

logger = logging.getLogger(__name__)


def _extension_for(filename: str, mime: str) -> str:
    """Pick a temp-file suffix so suffix-routed extractors still match."""
    suffix = Path(filename).suffix
    if suffix:
        return suffix
    return mimetypes.guess_extension(mime, strict=False) or ""


@contextlib.asynccontextmanager
async def materialize_to_local_path(
    adapter: StorageAdapter,
    storage_key: str,
    *,
    mime: str = "application/octet-stream",
    filename: str = "upload",
) -> AsyncIterator[Path | None]:
    """Yield a local :class:`Path` for ``storage_key`` regardless of adapter.

    Local-disk adapter: yields ``adapter.local_path(...)`` unchanged
    (zero extra I/O). Remote adapter (S3, GCS): streams the blob into a
    ``NamedTemporaryFile`` whose suffix matches the original filename,
    yields its Path, then deletes the file on exit.

    Yields ``None`` when neither path works — callers should treat that as
    "skip this entry, log, and move on".
    """
    if not storage_key:
        yield None
        return

    try:
        direct = adapter.local_path(storage_key)
    except Exception:
        logger.exception("local_path lookup failed for storage_key=%r", storage_key)
        direct = None
    if direct is not None:
        yield direct
        return

    suffix = _extension_for(filename, mime)
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual lifecycle
        prefix="paw-upload-",
        suffix=suffix,
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        try:
            async for chunk in adapter.open(storage_key):
                tmp.write(chunk)
        except Exception:
            logger.exception("stream-to-temp failed for storage_key=%r", storage_key)
            yield None
            return
        finally:
            tmp.close()

        yield tmp_path
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("temp cleanup failed for %s", tmp_path)


class EEUploadResolver:
    """Resolve upload URLs inside a single workspace."""

    def __init__(self, adapter: StorageAdapter, meta: MongoFileStore) -> None:
        self._adapter = adapter
        self._meta = meta

    async def resolve(self, url: str, workspace: str) -> Path | None:
        """Return a local :class:`Path` for ``url`` if the adapter exposes one.

        Returns ``None`` for remote adapters (S3, GCS) — callers that need
        to read the bytes regardless should use :meth:`open_local_for_url`
        instead, which streams the blob into a temp file for the duration
        of the ``async with`` block.
        """
        file_id = parse_upload_url(url)
        if file_id is None:
            return None
        rec = await self._meta.get_scoped(file_id, workspace=workspace)
        if rec is None:
            return None
        try:
            return self._adapter.local_path(rec.storage_key)
        except Exception:
            logger.exception(
                "upload adapter.local_path failed for file_id=%s storage_key=%s workspace=%s",
                file_id,
                rec.storage_key,
                workspace,
            )
            return None

    @contextlib.asynccontextmanager
    async def open_local_for_url(
        self, url: str, workspace: str
    ) -> AsyncIterator[tuple[FileRecord, Path] | None]:
        """Yield ``(record, local_path)`` for ``url`` regardless of adapter.

        S3-safe counterpart to :meth:`resolve` — when the adapter has no
        on-disk path, the blob is streamed into a temp file for the body
        of the ``async with`` block and cleaned up afterwards.
        """
        file_id = parse_upload_url(url)
        if file_id is None:
            yield None
            return
        rec = await self._meta.get_scoped(file_id, workspace=workspace)
        if rec is None:
            yield None
            return
        async with materialize_to_local_path(
            self._adapter,
            rec.storage_key,
            mime=rec.mime,
            filename=rec.filename,
        ) as path:
            if path is None:
                yield None
            else:
                yield rec, path


async def resolve_media_paths_scoped(
    media: list[str],
    *,
    resolver: EEUploadResolver,
    workspace: str,
) -> list[str]:
    """Async counterpart to :func:`pocketpaw.uploads.resolver.resolve_media_paths`.

    Same semantics: upload URLs → local paths, unresolvable URLs dropped,
    non-upload strings passed through. Note this uses the local-path-only
    :meth:`EEUploadResolver.resolve`; on S3 the path is ``None`` and the
    entry is dropped. Callers that need the bytes should iterate with
    :meth:`EEUploadResolver.open_local_for_url`.
    """
    out: list[str] = []
    for entry in media:
        fid = parse_upload_url(entry)
        if fid is None:
            out.append(entry)
            continue
        path = await resolver.resolve(entry, workspace=workspace)
        if path is None:
            logger.warning(
                "dropping unresolvable upload entry (workspace=%s): %s", workspace, entry
            )
            continue
        out.append(str(path))
    return out


def default_resolver() -> EEUploadResolver:
    """Return the resolver wired to the EE /uploads singletons."""
    from pocketpaw_ee.cloud.uploads.router import _ADAPTER, _META

    return EEUploadResolver(adapter=_ADAPTER, meta=_META)


__all__ = [
    "EEUploadResolver",
    "default_resolver",
    "materialize_to_local_path",
    "resolve_media_paths_scoped",
]
