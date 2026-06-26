# service.py — FileVersions service: versioned cloud file-write storage spine.
# Created: 2026-06-26 (ART-1) — ported from dewani12's origin/feature/files
#   (ee/cloud/file_versions/service.py). Migrated imports ee.cloud.* ->
#   pocketpaw_ee.cloud.*. KEEPS the storage core: write_file,
#   update_file_content, list_versions, get_version (+ helpers). DROPPED the
#   Slice-D editor helpers (revert_to_version, diff_versions) — deferred.
#   HARDENED _get_storage(): builds the adapter via the canonical
#   uploads.factory.build_adapter() against the shared upload root, instead
#   of reaching into the uploads router's private _SVC._adapter singleton.
#   Sole Beanie importer for FileVersionDoc; every FileUpload / FileVersionDoc
#   read is tenant-filtered (workspace).
"""FileVersions service — file-version write + history.

Module-level ``async def`` functions. Sole owner of writes to the
``FileVersionDoc`` Beanie document and the ``FileUpload.content_version``
counter. Text-only — non-editable mime types are rejected early.

Public API:
- ``write_file(ctx, body)`` — POST /files/write (create or overwrite)
- ``update_file_content(ctx, file_id, body)`` — PUT /files/{id}
- ``list_versions(ctx, file_id)`` — version list (no content)
- ``get_version(ctx, file_id, version_id)`` — full version with content
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from beanie import PydanticObjectId

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.factory import build_adapter
from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import Event
from pocketpaw_ee.cloud.file_versions.dto import (
    FileVersionListItem,
    FileVersionResponse,
    UpdateFileContentRequest,
    UpdateFileContentResponse,
    WriteFileRequest,
    WriteFileResponse,
)
from pocketpaw_ee.cloud.models.file_version import FileVersionDoc
from pocketpaw_ee.cloud.uploads.models import FileUpload

logger = logging.getLogger(__name__)

# Only these mime types are editable inline. Everything else gets a 422.
EDITABLE_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/css",
        "text/javascript",
        "text/xml",
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-yaml",
        "application/x-ndjson",
    }
)

# Fallback mime for inline-edited content the dashboard renders as code.
FALLBACK_EDIT_MIME = "text/plain"

# Canonical local-storage root — the same path the uploads router (_ROOT) and
# ``EEUploadService.write_text_file`` build their adapter against, so the
# version archive reads/writes the SAME blobs the uploads pipeline stored.
_UPLOAD_ROOT = Path.home() / ".pocketpaw" / "uploads"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_editable(mime: str | None) -> bool:
    if not mime:
        return False
    if mime.startswith("text/"):
        return True
    return mime in EDITABLE_MIMES


async def _read_text(adapter: StorageAdapter, key: str) -> str:
    """Read all chunks from StorageAdapter and decode as UTF-8."""
    chunks: list[bytes] = []
    async for chunk in adapter.open(key):
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


async def _bytes_iter(text: str) -> AsyncIterator[bytes]:
    yield text.encode("utf-8")


# Storage adapter singleton — injectable seam for tests / explicit wiring.
_adapter: StorageAdapter | None = None


def set_adapter(adapter: StorageAdapter) -> None:
    """Inject a StorageAdapter (tests, or an explicit wiring seam)."""
    global _adapter
    _adapter = adapter


def _get_storage() -> StorageAdapter:
    """Return the process StorageAdapter.

    Hardened replacement for dewani12's reach into
    ``uploads.router._SVC._adapter`` (a private singleton inside another
    entity's router module). Builds the adapter via the canonical
    ``uploads.factory.build_adapter`` against the shared upload root — the
    same construction the uploads router and ``write_text_file`` use — so
    the env-driven local/S3 selection stays consistent across both paths.
    """
    global _adapter
    if _adapter is None:
        _UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        _adapter = build_adapter(_UPLOAD_ROOT)
    return _adapter


async def write_file(
    ctx: RequestContext,
    body: WriteFileRequest,
) -> WriteFileResponse:
    """Create or overwrite a file (POST /files/write).

    If a FileUpload record already exists for the given path, content is
    updated in place (versioned). Otherwise a new FileUpload record and
    storage blob are created.
    """
    workspace_id = ctx.workspace_id
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

    file_id = body.path
    content = body.content
    mime = "application/json"  # Editor.js content is JSON

    adapter = _get_storage()

    doc = await FileUpload.find_one(
        {"file_id": file_id, "workspace": workspace_id, "deleted_at": None}
    )

    if doc is not None:
        update_body = UpdateFileContentRequest(content=content)
        result = await update_file_content(ctx, file_id, update_body)
        return WriteFileResponse(
            file_id=result.file_id,
            version=result.new_version,
            size_bytes=result.size_bytes,
        )

    # New file — create FileUpload record and storage blob.
    storage_key = f"editor/{workspace_id}/{uuid.uuid4().hex}/{file_id}"
    stored = await adapter.put(storage_key, _bytes_iter(content), mime)

    doc = FileUpload(
        file_id=file_id,
        filename=body.filename or file_id,
        workspace=workspace_id,
        owner=ctx.user_id or "unknown",
        mime=mime,
        size=stored.size,
        storage_key=storage_key,
        content_version=1,
    )
    await doc.insert()

    await emit(
        Event(
            type="file.created",
            data={
                "file_id": file_id,
                "workspace_id": workspace_id,
                "size": stored.size,
            },
        )
    )

    logger.info("file %s created via write (version 1)", file_id)

    return WriteFileResponse(
        file_id=file_id,
        version=1,
        size_bytes=stored.size,
    )


async def update_file_content(
    ctx: RequestContext,
    file_id: str,
    body: UpdateFileContentRequest,
    *,
    editor_kind: str = "human",
) -> UpdateFileContentResponse:
    """Replace a text file's content with optimistic concurrency.

    1. Fetch the ``FileUpload`` doc (tenant-filtered on workspace).
    2. Check the ``If-Match`` precondition against ``content_version``.
    3. Archive the current blob as a ``FileVersionDoc``.
    4. Upload the new content to StorageAdapter.
    5. Update ``FileUpload`` metadata and bump the version counter.
    """
    workspace_id = ctx.workspace_id
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

    doc = await FileUpload.find_one(
        {"file_id": file_id, "workspace": workspace_id, "deleted_at": None}
    )
    if not doc:
        raise NotFound("file", file_id)

    if not _is_editable(doc.mime):
        raise CloudError(
            422,
            "files.not_editable",
            f"File type '{doc.mime}' cannot be edited inline.",
        )

    expected = body.expected_version
    if expected is not None and expected != doc.content_version:
        raise ConflictError(
            "files.version_conflict",
            f"Expected version {expected} but current is {doc.content_version}. "
            "Someone else edited this file. Reload and try again.",
        )

    new_version = doc.content_version + 1
    new_content = body.content
    new_hash = _sha256(new_content)
    new_size = len(new_content.encode("utf-8"))
    editor_id = ctx.user_id or "unknown"

    adapter = _get_storage()

    # --- archive current blob ---
    try:
        old_content = await _read_text(adapter, doc.storage_key)
    except Exception:
        old_content = ""

    old_hash = _sha256(old_content)

    # Only archive if content actually changed.
    if old_hash != new_hash:
        version_doc = FileVersionDoc(
            file_id=file_id,
            workspace_id=workspace_id,
            version_number=new_version,
            content=old_content,
            content_hash=old_hash,
            size_bytes=len(old_content.encode("utf-8")),
            editor_kind=editor_kind,
            editor_id=editor_id,
            created_at=datetime.now(UTC),
        )
        await version_doc.insert()

        # --- upload new blob ---
        stored = await adapter.put(
            doc.storage_key,
            _bytes_iter(new_content),
            doc.mime,
        )

        # --- update metadata ---
        doc.size = stored.size
        doc.content_version = new_version
        await doc.save()

        await emit(
            Event(
                type="file.updated",
                data={
                    "file_id": file_id,
                    "workspace_id": workspace_id,
                    "version": new_version,
                    "editor_kind": editor_kind,
                    "editor_id": editor_id,
                },
            )
        )

        logger.info(
            "file %s updated to version %d by %s:%s",
            file_id,
            new_version,
            editor_kind,
            editor_id,
        )

    return UpdateFileContentResponse(
        file_id=file_id,
        new_version=new_version,
        size_bytes=new_size,
        content_hash=new_hash,
    )


async def list_versions(
    ctx: RequestContext,
    file_id: str,
) -> list[FileVersionListItem]:
    """List all archived versions for a file (oldest first), content omitted.

    Tenant-filtered: only versions in the caller's workspace are returned.
    """
    workspace_id = ctx.workspace_id
    docs = (
        await FileVersionDoc.find({"file_id": file_id, "workspace_id": workspace_id})
        .sort("+version_number")
        .to_list()
    )

    logger.info(
        "list_versions: file_id=%s workspace=%s found=%d",
        file_id,
        workspace_id,
        len(docs),
    )

    return [
        FileVersionListItem(
            id=str(doc.id),
            file_id=doc.file_id,
            version_number=doc.version_number,
            size_bytes=doc.size_bytes,
            editor_kind=doc.editor_kind,
            editor_id=doc.editor_id,
            created_at=doc.created_at,
        )
        for doc in docs
    ]


async def get_version(
    ctx: RequestContext,
    file_id: str,
    version_id: str,
) -> FileVersionResponse:
    """Fetch a single version with full content.

    Tenant-filtered: the find includes ``workspace_id`` so a caller can
    never read another workspace's version blob.
    """
    workspace_id = ctx.workspace_id
    try:
        oid = PydanticObjectId(version_id)
    except Exception:
        raise NotFound("version", version_id) from None

    doc = await FileVersionDoc.find_one(
        {"_id": oid, "file_id": file_id, "workspace_id": workspace_id}
    )
    if not doc:
        raise NotFound("version", version_id)

    return FileVersionResponse(
        id=str(doc.id),
        file_id=doc.file_id,
        version_number=doc.version_number,
        content=doc.content,
        content_hash=doc.content_hash,
        size_bytes=doc.size_bytes,
        editor_kind=doc.editor_kind,
        editor_id=doc.editor_id,
        created_at=doc.created_at,
    )
