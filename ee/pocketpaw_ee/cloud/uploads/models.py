"""EE FileUpload document — Mongo metadata for blobs stored via StorageAdapter.

2026-07-03 — FL-11b "hide-from-AI purge". Added ``kb_article_id`` and
``kb_scope`` (both ``str | None``, default ``None``) so a row remembers the
kb-go article it was ingested into. The FileReady listener records them after a
successful ingest; the PATCH route reads them to purge that article when a file
is later hidden from AI (``hide_from_ai`` flips false→true). Legacy rows read
back ``None`` via the defaults — no migration needed (Beanie field-add). Not
indexed: they're read only after resolving a row by ``file_id``.

2026-07-03 — FL-1 "Library metadata". Added ``tags`` (list[str]),
``collections`` (list[str]) and ``hide_from_ai`` (bool) so a file can carry
library organization + an AI-visibility flag that persist and round-trip
through the /files listing. Legacy rows without the fields read back as empty
lists / False via the Pydantic defaults (no migration script needed — Beanie
field-add with a default is backward compatible). ``tags`` and ``collections``
each get a Mongo index so library filtering stays cheap. ``tags`` was already
read defensively via ``getattr`` in ``mongo_store``; this formalizes it as a
declared field alongside the two new ones.

2026-06-26 — ART-1. Added ``content_version`` (default 0) so the
``file_versions`` service can run optimistic-concurrency edits and archive
each prior blob as a ``FileVersionDoc``. Legacy rows read as 0; ``write_file``
stamps new rows at 1. Beanie field-add is backward compatible (no migration).

2026-05-03 — Stage 3.E "Files as Knowledge". Added ``pocket_id`` so the
unified Files panel and the FileReady listener can route a single upload
into a pocket-scoped KB instead of the workspace pool. ``None`` is the
right semantic for "workspace upload" (no migration script needed; Beanie
field-add is backward compatible).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from beanie import Document, Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


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
    # Pocket scoping (Stage 3.E). ``None`` = workspace-scoped (default,
    # the pre-Stage-3.E semantics). When set, the file shows up only in
    # the pocket's Files panel and gets indexed into ``pocket:{id}`` KB.
    # Storage layout is unchanged — partitioning is metadata-only.
    pocket_id: str | None = None
    deleted_at: datetime | None = None
    # Optimistic-concurrency counter for the file_versions edit pipeline
    # (ART-1). Each successful inline edit bumps this and archives the prior
    # blob as a ``FileVersionDoc``. 0 on legacy rows; ``write_file`` stamps 1.
    content_version: int = 0
    # Library metadata (FL-1). Free-form ``tags`` and named ``collections``
    # organize the file in the library UI; ``hide_from_ai`` opts a file out of
    # AI/KB visibility. Legacy rows without these read back as empty lists /
    # False via the defaults — no migration script needed.
    tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    hide_from_ai: bool = False
    # KB tracking (FL-11b). After a successful ingest the FileReady listener
    # records the kb-go article id and the scope it landed in, so the file can
    # be retroactively purged from the KB if it's later hidden from AI. ``None``
    # means "not (currently) indexed" — a hide toggle then skips the purge, and
    # a later re-index re-populates these. Legacy rows read ``None``.
    kb_article_id: str | None = None
    kb_scope: str | None = None

    class Settings:
        name = "file_uploads"
        indexes = [
            [("workspace", 1), ("chat_id", 1), ("createdAt", -1)],
            [("workspace", 1), ("owner", 1), ("createdAt", -1)],
            [("workspace", 1), ("folder_path", 1), ("deleted_at", 1)],
            # Stage 3.E: pocket-scoped queries hit this index. Newest
            # first because the Files panel orders by ``created`` desc.
            [("workspace", 1), ("pocket_id", 1), ("createdAt", -1)],
            # FL-1: library filtering by tag / collection within a workspace.
            [("workspace", 1), ("tags", 1)],
            [("workspace", 1), ("collections", 1)],
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
