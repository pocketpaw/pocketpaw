"""UnifiedFilesService — legacy flat /api/v1/files surface (Cluster E #998 contract).

v2 makes this a thin caller of the FolderProvider registry; for now it stays
direct. Response shape: {workspace_id, source, files[], warnings[]}.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ee.cloud.files.mongo_store import MongoFileStore


class UnifiedFilesService:
    def __init__(self, store: MongoFileStore) -> None:
        self._store = store

    async def list(self, workspace_id: str, source: str = "all") -> dict[str, Any]:
        warnings: list[dict[str, str]] = []
        files: list[dict[str, Any]] = []
        if source in ("all", "chat"):
            rows = await self._store.list_by_workspace(workspace_id, limit=500)
            files.extend(asdict(r) for r in rows)
        if source in ("all", "drive"):
            warnings.append({"source": "drive", "code": "drive.not_connected"})
        if source in ("all", "local"):
            warnings.append({"source": "local", "code": "local.client_only"})
        return {
            "workspace_id": workspace_id,
            "source": source,
            "files": files,
            "warnings": warnings,
        }
