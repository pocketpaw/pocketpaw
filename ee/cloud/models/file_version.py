"""FileVersion Beanie document — full-copy text blobs archived per edit.

Stored in the ``file_versions`` collection. The live (current) file
content lives in the StorageAdapter; this collection is the history trail.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field


class FileVersionDoc(Document):
    file_id: Indexed(str)  # type: ignore[valid-type]
    workspace_id: Indexed(str)  # type: ignore[valid-type]
    version_number: int
    content: str
    content_hash: str
    size_bytes: int
    editor_kind: str  # "human" | "agent"
    editor_id: str  # user_id or agent_id
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "file_versions"
        indexes = [
            # List versions for a file, oldest-first so the picker shows v1→vN.
            [("file_id", 1), ("version_number", 1)],
            # Workspace-scoped queries if needed.
            [("workspace_id", 1), ("file_id", 1)],
        ]
