"""EE workspace-scoped upload URL resolver.

Wraps :class:`pocketpaw.uploads.resolver.UploadResolver` semantics but looks
up metadata in Mongo with workspace isolation. Cross-tenant lookups return
``None`` (treated as "not found" — no leak of existence across workspaces).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ee.cloud.uploads.mongo_store import MongoFileStore
from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.resolver import parse_upload_url

logger = logging.getLogger(__name__)


class EEUploadResolver:
    """Resolve upload URLs inside a single workspace."""

    def __init__(self, adapter: StorageAdapter, meta: MongoFileStore) -> None:
        self._adapter = adapter
        self._meta = meta

    async def resolve(self, url: str, workspace: str) -> Path | None:
        file_id = parse_upload_url(url)
        if file_id is None:
            return None
        rec = await self._meta.get_scoped(file_id, workspace=workspace)
        if rec is None:
            return None
        # Contain unexpected adapter failures (permission errors on the
        # storage root, remount races, future remote adapters) so chat
        # never crashes over a bad attachment — just drops the entry.
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


async def resolve_media_paths_scoped(
    media: list[str],
    *,
    resolver: EEUploadResolver,
    workspace: str,
) -> list[str]:
    """Async counterpart to :func:`pocketpaw.uploads.resolver.resolve_media_paths`.

    Same semantics: upload URLs → local paths, unresolvable URLs dropped,
    non-upload strings passed through.
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
    from ee.cloud.uploads.router import _ADAPTER, _META

    return EEUploadResolver(adapter=_ADAPTER, meta=_META)


__all__ = [
    "EEUploadResolver",
    "default_resolver",
    "resolve_media_paths_scoped",
]
