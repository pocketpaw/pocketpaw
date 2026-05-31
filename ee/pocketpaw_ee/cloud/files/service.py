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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

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
        return [
            UnifiedFile(
                id=rec.id,
                source="chat",
                filename=rec.filename,
                mime=rec.mime,
                size=rec.size,
                url=f"/api/v1/uploads/{rec.id}",
                created=rec.created,
                chat_id=rec.chat_id,
            )
            for rec in records
        ]

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

    async def list_unified(
        self,
        workspace_id: str,
        *,
        source: FileSource | None,
        limit: int,
        pocket_id: str | None = None,
    ) -> tuple[list[UnifiedFile], list[str]]:
        """Return (files, warnings).

        ``source`` is optional — omit for "everything we can reach". When
        a specific source is requested, only that source is queried.

        Stage 3.E: when ``pocket_id`` is set, the chat slice is filtered
        to that pocket. When ``None`` (the default), the chat slice
        returns workspace-only rows — pocket files don't bleed into the
        workspace Files panel. The Drive branch is workspace-level for
        now (a Drive-per-pocket connector is Phase 4 territory).
        """
        per_source = max(1, min(limit, 500))
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

        return merged[:limit], warnings
