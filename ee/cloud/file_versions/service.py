"""FileVersions service — inline-edit version history.

Module-level ``async def`` functions. Sole owner of writes to the
``FileVersionDoc`` Beanie document and the ``FileUpload.content_version``
counter. Text-only — binary files are rejected early.

Public API:
- ``update_file_content(ctx, file_id, body)`` — PUT /files/{id}
- ``list_versions(ctx, file_id)`` — version list (no content)
- ``get_version(ctx, file_id, version_id)`` — full version with content
- ``revert_to_version(ctx, file_id, version_id)`` — POST /files/{id}/versions/{vid}/revert
- ``diff_versions(ctx, file_id, from_v, to_v)`` — unified diff
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from beanie import PydanticObjectId

from ee.cloud._core.context import RequestContext
from ee.cloud._core.errors import CloudError, ConflictError, NotFound
from ee.cloud._core.realtime.emit import emit
from ee.cloud._core.realtime.events import Event
from ee.cloud.file_versions.dto import (
    DiffResponse,
    FileVersionListItem,
    FileVersionResponse,
    UpdateFileContentRequest,
    UpdateFileContentResponse,
    WriteFileRequest,
    WriteFileResponse,
)
from ee.cloud.models.file_version import FileVersionDoc
from ee.cloud.uploads.models import FileUpload

logger = logging.getLogger(__name__)

# Only these mime types are editable inline. Everything else gets a 422.
EDITABLE_MIMES = frozenset({
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
})

# Use a mimetype that isn't text/* so the dashboard renders it as code.
FALLBACK_EDIT_MIME = "text/plain"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_editable(mime: str | None) -> bool:
    if not mime:
        return False
    if mime.startswith("text/"):
        return True
    return mime in EDITABLE_MIMES


async def _read_text(adapter, key: str) -> str:
    """Read all chunks from StorageAdapter and decode as UTF-8."""
    chunks: list[bytes] = []
    async for chunk in adapter.open(key):
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


async def _bytes_iter(text: str) -> AsyncIterator[bytes]:
    yield text.encode("utf-8")


# Storage adapter singleton — set by the router at mount time.
_adapter: object | None = None


def set_adapter(adapter: object) -> None:
    global _adapter
    _adapter = adapter


def _get_storage():
    global _adapter
    if _adapter is None:
        from ee.cloud.uploads.router import _SVC
        _adapter = _SVC._adapter
    return _adapter


async def update_file_content(
    ctx: RequestContext,
    file_id: str,
    body: UpdateFileContentRequest,
    *,
    editor_kind: str = "human",
) -> UpdateFileContentResponse:
    """Replace a text file's content with optimistic concurrency.

    1. Fetch the ``FileUpload`` doc.
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

        await emit(Event(type="file.updated", data={
            "file_id": file_id,
            "workspace_id": workspace_id,
            "version": new_version,
            "editor_kind": editor_kind,
            "editor_id": editor_id,
        }))

        logger.info(
            "file %s updated to version %d by %s:%s",
            file_id, new_version, editor_kind, editor_id,
        )

    return UpdateFileContentResponse(
        file_id=file_id,
        new_version=new_version,
        size_bytes=new_size,
        content_hash=new_hash,
    )


async def write_file(
    ctx: RequestContext,
    body: WriteFileRequest,
) -> WriteFileResponse:
    """Create or overwrite a file. Used by the editor for first-save and by
    the local-fs web-mode fallback (POST /files/write).

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
    import uuid

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

    await emit(Event(type="file.created", data={
        "file_id": file_id,
        "workspace_id": workspace_id,
        "size": stored.size,
    }))

    logger.info("file %s created via write (version 1)", file_id)

    return WriteFileResponse(
        file_id=file_id,
        version=1,
        size_bytes=stored.size,
    )


async def list_versions(
    ctx: RequestContext,
    file_id: str,
) -> list[FileVersionListItem]:
    """List all archived versions for a file (oldest first), content omitted."""
    workspace_id = ctx.workspace_id
    docs = await FileVersionDoc.find(
        {"file_id": file_id, "workspace_id": workspace_id}
    ).sort("+version_number").to_list()

    logger.info(
        "list_versions: file_id=%s workspace=%s found=%d",
        file_id, workspace_id, len(docs),
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
    """Fetch a single version with full content."""
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


async def revert_to_version(
    ctx: RequestContext,
    file_id: str,
    version_id: str,
) -> UpdateFileContentResponse:
    """Revert the live file to a historical version.

    Archives the current content first, then uploads the version's content
    as the new live blob.
    """
    version = await get_version(ctx, file_id, version_id)
    body = UpdateFileContentRequest(content=version.content)
    return await update_file_content(ctx, file_id, body, editor_kind="human")


async def diff_versions(
    ctx: RequestContext,
    file_id: str,
    from_version_id: str,
    to_version_id: str,
) -> DiffResponse:
    """Return a unified diff between two historical versions."""
    v1 = await get_version(ctx, file_id, from_version_id)
    v2 = await get_version(ctx, file_id, to_version_id)

    import difflib

    diff = "".join(
        difflib.unified_diff(
            v1.content.splitlines(keepends=True),
            v2.content.splitlines(keepends=True),
            fromfile=f"v{v1.version_number}",
            tofile=f"v{v2.version_number}",
        )
    )
    return DiffResponse(
        from_version=v1.version_number,
        to_version=v2.version_number,
        diff=diff,
    )
