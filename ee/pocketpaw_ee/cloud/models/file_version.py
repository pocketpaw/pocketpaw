# file_version.py — FileVersionDoc Beanie document for the file-version history trail.
# Created: 2026-06-26 (ART-1) — ported from dewani12's origin/feature/files
#   (was ee/cloud/models/file_version.py) onto the post-cloud-restructure
#   layout. Path migrated to ee/pocketpaw_ee/cloud/models/; registered in
#   models.get_all_documents(). Only ``file_versions.service`` may import this
#   class (import-linter "FileVersions" contract).
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
            # List versions for a file, oldest-first so the picker shows v1->vN.
            [("file_id", 1), ("version_number", 1)],
            # Workspace-scoped queries (tenant-filtered reads).
            [("workspace_id", 1), ("file_id", 1)],
        ]
