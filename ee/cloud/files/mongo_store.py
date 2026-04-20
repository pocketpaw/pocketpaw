"""Minimal Mongo file-listing store (stand-in until PR #998 lands).

If #998 has merged, its version supersedes this file. The shape below matches
the contract #998 introduces (list_by_workspace with soft-delete skip + cap).
"""
from __future__ import annotations

from dataclasses import dataclass

from ee.cloud.uploads.mongo_store import MongoFileStore as UploadsStore


@dataclass(slots=True)
class LegacyFileRow:
    id: str
    source: str
    filename: str
    mime: str
    size: int
    url: str | None
    created: str
    chat_id: str | None = None


class MongoFileStore:
    """Wraps uploads.mongo_store for the legacy /api/v1/files surface."""

    def __init__(self, inner: UploadsStore) -> None:
        self._inner = inner

    async def list_by_workspace(
        self, workspace_id: str, *, limit: int = 500
    ) -> list[LegacyFileRow]:
        rows: list[LegacyFileRow] = []
        async for doc in self._inner.iter_by_workspace(
            workspace_id, include_deleted=False, limit=limit
        ):
            rows.append(
                LegacyFileRow(
                    id=str(doc["file_id"]),
                    source="chat",
                    filename=doc.get("filename", ""),
                    mime=doc.get("mime", "application/octet-stream"),
                    size=int(doc.get("size", 0)),
                    url=None,
                    created=doc.get("created_at", "").isoformat()
                    if doc.get("created_at")
                    else "",
                    chat_id=doc.get("chat_id"),
                )
            )
        return rows
