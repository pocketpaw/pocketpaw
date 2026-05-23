"""Unified files service — merges chat S3 uploads, local workspace dir,
Drive-synced files (stubbed), KB articles, and agent-produced files into
one list the FE Files panel can render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ee.cloud.uploads.mongo_store import LIST_WORKSPACE_ONLY, MongoFileStore

logger = logging.getLogger(__name__)


FileSource = Literal["chat", "local", "drive", "kb", "agent"]


@dataclass
class UnifiedFile:
    """Row in the unified Files listing. Shape is shared across sources."""

    id: str
    source: FileSource
    filename: str
    mime: str | None
    size: int | None
    url: str | None  # None for local fs / kb / agent sources
    created: datetime | None
    chat_id: str | None = None
    agent_id: str | None = None  # Owning agent for kb/agent-source rows

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


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO-8601 string to datetime; return None on failure."""
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


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

    async def list_kb_articles(
        self,
        workspace_id: str,
        *,
        limit: int,
    ) -> list[UnifiedFile]:
        """KB articles sourced from the kb-go binary via the workspace aggregator.
        """
        from ee.cloud.agents import service as agents_service
        from ee.cloud.kb.workspace_aggregator import aggregate_workspace_articles

        # Resolve agent ids in this workspace for scope fan-out.
        agents = await agents_service.list_agents(workspace_id)
        agent_ids = [a.id for a in agents]

        def _kb_list(scope: str) -> list:
            from ee.cloud.agents.knowledge import _kb

            try:
                result = _kb("list", "--scope", scope)
            except Exception:
                return []
            return result if isinstance(result, list) else []

        articles = await aggregate_workspace_articles(
            workspace_id=workspace_id,
            agent_ids=agent_ids,
            kb_list=_kb_list,
        )

        return [
            UnifiedFile(
                id=a.id,
                source="kb",
                filename=a.title or a.source or a.id,
                mime="text/markdown",
                size=None,
                url=None,
                created=_parse_iso(a.updated_at) if a.updated_at else None,
                agent_id=a.agent_id,
            )
            for a in articles
        ][:limit]

    async def list_agent_files(
        self,
        workspace_id: str,
        *,
        limit: int,
    ) -> list[UnifiedFile]:
        """Agent-produced files.

        Agent tool calls (WriteFileTool, Claude SDK Write) write directly to
        the local filesystem and bypass the upload pipeline. Until we add a
        file-registration hook on tool completion, this returns empty.

        The stub emits an info-level log so operators can confirm the branch
        is reachable; the FE handles an empty list gracefully.
        """
        
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
                await self.list_chat_uploads(
                    workspace_id, limit=per_source, pocket_id=pocket_id
                )
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

        if source in (None, "kb"):
            kb_hits = await self.list_kb_articles(
                workspace_id, limit=per_source
            )
            merged.extend(kb_hits)

        if source in (None, "agent"):
            agent_hits = await self.list_agent_files(
                workspace_id, limit=per_source
            )
            merged.extend(agent_hits)
            if not agent_hits:
                warnings.append(
                    "agent.not_wired: Agent-produced files are not yet "
                    "tracked (Smart Files Milestone 2+)."
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
