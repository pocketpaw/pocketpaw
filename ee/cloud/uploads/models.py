"""EE FileUpload document — Mongo metadata for blobs stored via StorageAdapter."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from beanie import Document, Indexed
from pydantic import Field

from ee.cloud.models.base import TimestampedDocument


class FileUpload(TimestampedDocument):
    """Metadata for one uploaded file. Blob bytes live in the StorageAdapter.

    Distinct from ``ee.cloud.models.file.FileObj`` (pre-signed URL storage):
    ``FileUpload`` is the adapter-backed path for chat attachments, with
    workspace scoping and soft-delete.
    """

    file_id: Indexed(str, unique=True)  # type: ignore[valid-type]
    storage_key: str
    filename: str
    mime: str
    size: int
    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str
    chat_id: Indexed(str) | None = None  # type: ignore[valid-type]
    # Absolute folder path for the "My Files" mount. Root is ``"/"``.
    # Missing/None on legacy rows → treat as root.
    folder_path: str | None = "/"
    deleted_at: datetime | None = None

    class Settings:
        name = "file_uploads"
        indexes = [
            [("workspace", 1), ("chat_id", 1), ("createdAt", -1)],
            [("workspace", 1), ("owner", 1), ("createdAt", -1)],
            [("workspace", 1), ("folder_path", 1), ("deleted_at", 1)],
        ]


class FileFolder(Document):
    """Folder node for the "My Files" mount (uploads provider only).

    Folders are workspace-scoped, owner-stamped, soft-deleted. They exist
    only for the uploads provider — other providers (kb, chat, drive,
    local) stay flat in this release.
    """

    folder_id: str = Field(default_factory=lambda: uuid4().hex)
    workspace: str
    owner: str
    path: str  # absolute normalized, e.g. "/reports/2026"
    name: str  # final segment of ``path``
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None

    class Settings:
        name = "file_folders"
        indexes = [
            [("workspace", 1), ("path", 1)],
            [("workspace", 1), ("owner", 1), ("deleted_at", 1)],
        ]
