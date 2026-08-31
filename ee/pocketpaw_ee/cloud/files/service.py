"""Unified files service — merges chat S3 uploads, local workspace dir,
and (stubbed for now) Drive-synced files into one list the FE Files
panel can render.

Cluster E sub-PR 4. The Drive branch returns an empty list today;
Cluster C owns the connector-status endpoint that will tell us which
pockets have a connected Drive account. Once that lands we can fan a
Drive listing in here without changing the response shape the FE already
consumes. See ``docs/plans/cluster-E-reality.md`` for the handshake.

2026-05-03 (Stage 3.E "Files as Knowledge"): ``list_unified`` accepts an
optional ``pocket_id``. When set, the chat-uploads slice is filtered to
that pocket only. When ``None`` (the default), the listing returns
workspace-only rows — the workspace Files panel never sees pocket files,
which is the privacy contract for pocket-scoped uploads.

2026-08-29 (T3 "Files content search"): the record→row projection that was
inlined in ``list_chat_uploads`` is now the module-level
``unified_from_record``. ``files/content_search.py`` projects the rows a kb
hit resolves to through the SAME function — a second hand-written copy is how
summary/collections/tags/agent_id got dropped three separate times in this
pipeline, and the fix was always "one projection, one place".

2026-08-13 (Files pagination): ``list_unified`` now returns a
``UnifiedPage`` (files + warnings + total + has_more) and accepts an
``offset`` for offset-based paging. ``total`` comes from a cheap count
query on the dominant chat-uploads source so ``has_more`` is accurate
even when the fetch is capped at the mongo 500-row limit. Each source is
fetched with ``offset + limit`` rows so slicing the merged, deduped set
at ``[offset:offset+limit]`` stays correct within that cap. Callers that
only need the flat list keep working via ``page.files``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pocketpaw.uploads.file_store import FileRecord
from pocketpaw_ee.cloud.uploads.mongo_store import LIST_WORKSPACE_ONLY, MongoFileStore

logger = logging.getLogger(__name__)


FileSource = Literal["chat", "local", "drive"]


@dataclass
class UnifiedFile:
    """Row in the unified Files listing. Shape is shared across sources."""

    id: str
    source: FileSource
    filename: str
    mime: str | None
    size: int | None
    url: str | None  # None for local fs (FE uses Tauri for those)
    created: datetime | None
    chat_id: str | None = None
    # Library metadata. THIS is the shape the flat ``GET /files`` listing
    # returns — the one the Files panel renders. FL-1/FC-1/BA-1 each added
    # their field to ``files/dto.py::FileEntry`` (the v2 /files/browse tree)
    # and to the uploads provider, but not here, so the values were written,
    # stored and then dropped one layer before the client: a summary that
    # exists in Mongo and renders as an empty panel. Defaults keep every
    # non-upload source (drive, local, kb) unchanged.
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    summary: str | None = None
    agent_id: str | None = None
    # Where the row LIVES. Absent until 2026-08-29, so the flat listing never
    # told a client which folder a file was in — and a Move UI that guards on
    # `folder_path ?? "/"` therefore decided every file was already at the
    # root and quietly did nothing. Non-upload sources have no folders and
    # keep the "/" default.
    folder_path: str = "/"

    def to_json(self) -> dict:
        """The wire shape of one flat-listing row.

        The router used to hand-build this dict inline, which re-dropped
        summary/collections/tags/agent_id AFTER the service started carrying
        them — the third hop in the same pipeline to silently narrow the row.
        Serialization lives on the dataclass now so a new field has exactly
        one place to be forgotten, and the carrier test pins this method.
        """
        return {
            "id": self.id,
            "source": self.source,
            "filename": self.filename,
            "mime": self.mime,
            "size": self.size,
            "url": self.url,
            "created": self.created.isoformat() if self.created else None,
            "chat_id": self.chat_id,
            "tags": self.tags,
            "collections": self.collections,
            "summary": self.summary,
            "agent_id": self.agent_id,
            "folder_path": self.folder_path,
        }


@dataclass
class UnifiedPage:
    """One page of the unified Files listing plus pagination metadata.

    ``total`` is the number of rows matching the query (independent of the
    per-source fetch cap), so ``has_more`` stays correct on a full page.
    """

    files: list[UnifiedFile]
    warnings: list[str]
    total: int
    has_more: bool


def unified_from_record(rec: FileRecord) -> UnifiedFile:
    """Project one uploads ``FileRecord`` into a flat-listing row.

    THE record→row projection, extracted 2026-08-29 so there is exactly one.
    It used to be inlined in ``list_chat_uploads``; content search
    (``files/content_search.py``) needs the same projection for the rows a kb
    hit resolves to, and a second hand-written copy is precisely how
    summary/collections/tags/agent_id were dropped three times in this
    pipeline already. A new field added to ``UnifiedFile`` now has one hop to
    be threaded through, not two.
    """
    return UnifiedFile(
        id=rec.id,
        source="chat",
        filename=rec.filename,
        mime=rec.mime,
        size=rec.size,
        url=f"/api/v1/uploads/{rec.id}",
        created=rec.created,
        chat_id=rec.chat_id,
        folder_path=getattr(rec, "folder_path", None) or "/",
        tags=list(rec.tags or []),
        collections=list(rec.collections or []),
        summary=rec.summary,
        agent_id=rec.agent_id,
    )


def _dedupe(files: list[UnifiedFile]) -> list[UnifiedFile]:
    """Drop later duplicates keyed on ``(filename, size, mime)``.

    The same file that lives in both the Drive mirror and a chat upload
    would otherwise show up twice in the panel. We keep the first hit
    (which is the newest because we sort before dedupe).
    """
    seen: set[tuple[str, int | None, str | None]] = set()
    out: list[UnifiedFile] = []
    for f in files:
        key = (f.filename, f.size, f.mime)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


class UnifiedFilesService:
    """Stateless façade that pulls each source and merges the results."""

    def __init__(self, uploads: MongoFileStore | None = None) -> None:
        self._uploads = uploads or MongoFileStore()

    async def list_chat_uploads(
        self,
        workspace_id: str,
        *,
        limit: int,
        pocket_id: str | None = None,
    ) -> list[UnifiedFile]:
        # Pocket scope routing (Stage 3.E):
        # - ``pocket_id`` set → return that pocket's rows only.
        # - ``pocket_id`` None → return workspace-only rows. Pocket-scoped
        #   uploads MUST NOT bleed into the workspace Files panel.
        if pocket_id:
            records = await self._uploads.list_by_workspace(
                workspace_id, limit=limit, pocket_id=pocket_id
            )
        else:
            records = await self._uploads.list_by_workspace(
                workspace_id, limit=limit, pocket_id=LIST_WORKSPACE_ONLY
            )
        return [unified_from_record(rec) for rec in records]

    async def list_drive(self, workspace_id: str, *, limit: int) -> list[UnifiedFile]:
        """Drive source — stubbed until Cluster C lands connector status.

        Returns an empty list and logs at debug level. The FE handles an
        empty Drive branch gracefully (no empty-state surprise).
        """
        logger.debug(
            "Drive source for workspace %s not yet wired (Cluster C dep)",
            workspace_id,
        )
        return []

    async def _count_chat_uploads(self, workspace_id: str, pocket_id: str | None) -> int:
        """Count live chat uploads under the same scope ``list_chat_uploads``
        lists. Mirrors its tri-state ``pocket_id`` handling so ``total`` and
        ``has_more`` describe exactly the rows that can be paged through."""
        if pocket_id:
            return await self._uploads.count_by_workspace(workspace_id, pocket_id=pocket_id)
        return await self._uploads.count_by_workspace(workspace_id, pocket_id=LIST_WORKSPACE_ONLY)

    async def list_unified(
        self,
        workspace_id: str,
        *,
        source: FileSource | None,
        limit: int,
        offset: int = 0,
        pocket_id: str | None = None,
    ) -> UnifiedPage:
        """Return a ``UnifiedPage`` for the given offset/limit window.

        ``source`` is optional — omit for "everything we can reach". When
        a specific source is requested, only that source is queried.

        Pagination: each source is fetched with ``offset + limit`` rows
        (capped at the mongo 500-row fetch limit), then the merged,
        deduped set is sliced at ``[offset : offset + limit]``. ``total``
        is the real matching count from a cheap count query so ``has_more``
        is accurate even when the fetch is capped. Sources that can't be
        paged (local files enumerated client-side, the stubbed Drive
        branch) report ``total=0`` / ``has_more=False``.

        Stage 3.E: when ``pocket_id`` is set, the chat slice is filtered
        to that pocket. When ``None`` (the default), the chat slice
        returns workspace-only rows — pocket files don't bleed into the
        workspace Files panel. The Drive branch is workspace-level for
        now (a Drive-per-pocket connector is Phase 4 territory).
        """
        # Fetch enough rows per source to satisfy the requested window.
        per_source = max(1, min(offset + limit, 500))
        warnings: list[str] = []
        merged: list[UnifiedFile] = []

        if source in (None, "chat"):
            merged.extend(
                await self.list_chat_uploads(workspace_id, limit=per_source, pocket_id=pocket_id)
            )

        if source in (None, "drive"):
            drive_hits = await self.list_drive(workspace_id, limit=per_source)
            merged.extend(drive_hits)
            if not drive_hits:
                # Visible to the FE so it can render a "connect Drive" hint.
                warnings.append(
                    "drive.not_connected: Drive source is not wired yet; "
                    "see Cluster C connector-status endpoint."
                )

        # Local filesystem is addressed by the FE's Tauri bridge (no
        # single authoritative path on the server). The unified endpoint
        # only reports remote-sourced files; the FE merges its local
        # listing in-client. Flag the intent so the panel's filter chips
        # still read meaningfully.
        if source == "local":
            warnings.append(
                "local.client_only: Local files are enumerated by the "
                "Tauri filesystem bridge; the server does not keep a "
                "canonical copy."
            )

        # Dedupe once after all sources are merged. Sort newest first.
        merged.sort(key=lambda f: f.created or datetime.min, reverse=True)
        merged = _dedupe(merged)

        total = 0
        if source in (None, "chat"):
            total = await self._count_chat_uploads(workspace_id, pocket_id)

        page = merged[offset : offset + limit]
        has_more = offset + len(page) < total

        return UnifiedPage(files=page, warnings=warnings, total=total, has_more=has_more)
