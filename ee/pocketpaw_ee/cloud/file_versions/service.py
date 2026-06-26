# service.py — FileVersions service: versioned cloud file-write storage spine.
# Created: 2026-06-26 (ART-1) — ported from dewani12's origin/feature/files,
#   imports migrated ee.cloud.* -> pocketpaw_ee.cloud.*. Storage core only
#   (write_file, update_file_content, list_versions, get_version); Slice-D
#   helpers (revert/diff) dropped. Sole Beanie importer for FileVersionDoc;
#   every FileUpload/FileVersionDoc read is workspace-filtered.
# Updated: 2026-06-26 (ART-1 quality fix loop):
#   C1 cross-tenant — FileUpload.file_id is GLOBALLY unique, so a client path
#     collided across workspaces. The STORED FileUpload id is now namespaced
#     `${workspace}:${path}` (two workspaces can share a path); the bare path
#     stays the client-facing id on FileVersionDoc, the DTOs, and the routes.
#     write_file revives a soft-deleted tombstone in place instead of a blind
#     insert (which would raise DuplicateKeyError on the tombstoned unique id).
#   I1 — a no-op (unchanged-content) update now returns the ACTUAL stored
#     version, not a phantom +1.
#   I2 — new files take a real mime (WriteFileRequest.mime, else guessed from
#     the filename extension), no longer hardcoded application/json.
#   I3 — a blob-read failure during archival aborts (CloudError) instead of
#     silently archiving an empty "prior" version and destroying history.
#   I4 — the archived FileVersionDoc is labelled with the version the content
#     actually was (the pre-bump version), so version->content stays correct.
#   M1 — read paths map FileVersionDoc -> FileVersion domain -> DTO.
#   M2 — list/get reject an empty workspace (400), matching write/update.
"""FileVersions service — file-version write + history.

Module-level ``async def`` functions. Sole owner of writes to the
``FileVersionDoc`` Beanie document and the ``FileUpload.content_version``
counter. Text-only — non-editable mime types are rejected early.

Public API:
- ``write_file(ctx, body)`` — POST /files/write (create or overwrite)
- ``update_file_content(ctx, file_id, body)`` — PUT /files/{id}
- ``list_versions(ctx, file_id)`` — version list (no content)
- ``get_version(ctx, file_id, version_id)`` — full version with content

The client-facing file id is the bare ``path`` throughout. The FileUpload
row stores a workspace-namespaced id (``${workspace}:${path}``) because that
collection's ``file_id`` index is globally unique; FileVersionDoc keeps the
bare path (its reads are always workspace-filtered, so no global collision).
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
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
from pocketpaw_ee.cloud.file_versions.domain import FileVersion
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

# Fallback mime when a filename has no usable extension. Editable (text/*),
# so a no-extension write still round-trips through the inline editor.
FALLBACK_EDIT_MIME = "text/plain"

# Explicit extension->mime overrides for the editable types the stdlib
# ``mimetypes`` registry doesn't reliably know across platforms (e.g. .md,
# .yaml). Anything not here falls back to ``mimetypes.guess_type``.
_EXT_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".ndjson": "application/x-ndjson",
    ".csv": "text/csv",
    ".txt": "text/plain",
}

# Canonical local-storage root — the same path the uploads router (_ROOT) and
# ``EEUploadService.write_text_file`` build their adapter against, so the
# version archive reads/writes the SAME blobs the uploads pipeline stored.
_UPLOAD_ROOT = Path.home() / ".pocketpaw" / "uploads"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _storage_id(workspace_id: str, path: str) -> str:
    """Namespace a client path by workspace for the globally-unique
    ``FileUpload.file_id`` index.

    Two workspaces writing the same ``path`` map to distinct stored ids, so
    neither collides on the unique key nor squats the other's path. The bare
    ``path`` stays the client-facing id everywhere else (FileVersionDoc,
    DTOs, routes); this value is only ever the FileUpload lookup/insert key.
    """
    return f"{workspace_id}:{path}"


def _guess_mime(filename: str) -> str:
    """Best-effort mime from a filename's extension.

    Defaults to the inline-editable fallback (``text/plain``) when there's no
    usable extension, so a path like ``"report"`` still produces an editable
    file instead of an opaque blob.
    """
    ext = Path(filename).suffix.lower()
    if ext in _EXT_MIME_OVERRIDES:
        return _EXT_MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or FALLBACK_EDIT_MIME


def _is_editable(mime: str | None) -> bool:
    if not mime:
        return False
    if mime.startswith("text/"):
        return True
    return mime in EDITABLE_MIMES


def _to_domain(doc: FileVersionDoc) -> FileVersion:
    """Map a FileVersionDoc (persistence) to the FileVersion value object the
    read paths build their DTOs from (doc -> domain -> DTO)."""
    return FileVersion(
        id=str(doc.id),
        file_id=doc.file_id,
        workspace_id=doc.workspace_id,
        version_number=doc.version_number,
        content=doc.content,
        content_hash=doc.content_hash,
        size_bytes=doc.size_bytes,
        editor_kind=doc.editor_kind,  # stored as str; FileVersion narrows to EditorKind
        editor_id=doc.editor_id,
        created_at=doc.created_at,
    )


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

    If a live FileUpload record already exists for the (workspace, path),
    content is updated in place (versioned). A soft-deleted tombstone for the
    same path is revived in place — a blind insert would collide on the
    globally-unique stored id. Otherwise a new record + storage blob.
    """
    workspace_id = ctx.workspace_id
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

    path = body.path  # bare, client-facing id
    stored_id = _storage_id(workspace_id, path)
    content = body.content
    mime = body.mime or _guess_mime(body.filename or path)

    adapter = _get_storage()

    # Find ANY existing row for (workspace, path), including a soft-deleted
    # tombstone — it still holds the unique stored id, so a blind insert would
    # raise DuplicateKeyError (500). Revive it instead.
    doc = await FileUpload.find_one({"file_id": stored_id, "workspace": workspace_id})

    if doc is not None and doc.deleted_at is None:
        # Live file — versioned update in place (pass the bare path).
        update_body = UpdateFileContentRequest(content=content)
        result = await update_file_content(ctx, path, update_body)
        return WriteFileResponse(
            file_id=path,
            version=result.new_version,
            size_bytes=result.size_bytes,
        )

    storage_key = (
        doc.storage_key if doc is not None else f"editor/{workspace_id}/{uuid.uuid4().hex}"
    )
    stored = await adapter.put(storage_key, _bytes_iter(content), mime)

    if doc is not None:
        # Revive a soft-deleted tombstone in place — no second unique-key row.
        doc.deleted_at = None
        doc.filename = body.filename or path
        doc.mime = mime
        doc.size = stored.size
        doc.storage_key = storage_key
        doc.owner = ctx.user_id or doc.owner
        doc.content_version = 1
        await doc.save()
    else:
        doc = FileUpload(
            file_id=stored_id,
            filename=body.filename or path,
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
                "file_id": path,
                "workspace_id": workspace_id,
                "size": stored.size,
            },
        )
    )

    logger.info("file %s created via write (version 1)", path)

    return WriteFileResponse(
        file_id=path,
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

    ``file_id`` is the bare client path. Steps:
    1. Fetch the ``FileUpload`` doc by the workspace-namespaced stored id.
    2. Check the ``If-Match`` precondition against ``content_version``.
    3. Read + archive the current blob as a ``FileVersionDoc`` (labelled with
       the version it actually was). A read failure aborts — never archive an
       empty "prior" version.
    4. Upload the new content and bump the version counter.
    """
    workspace_id = ctx.workspace_id
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

    stored_id = _storage_id(workspace_id, file_id)
    doc = await FileUpload.find_one(
        {"file_id": stored_id, "workspace": workspace_id, "deleted_at": None}
    )
    if not doc:
        raise NotFound("file", file_id)

    if not _is_editable(doc.mime):
        raise CloudError(
            422,
            "files.not_editable",
            f"File type '{doc.mime}' cannot be edited inline.",
        )

    current_version = doc.content_version
    expected = body.expected_version
    if expected is not None and expected != current_version:
        raise ConflictError(
            "files.version_conflict",
            f"Expected version {expected} but current is {current_version}. "
            "Someone else edited this file. Reload and try again.",
        )

    new_content = body.content
    new_hash = _sha256(new_content)
    new_size = len(new_content.encode("utf-8"))
    editor_id = ctx.user_id or "unknown"

    adapter = _get_storage()

    # Read the current blob so it can be archived. A read failure must NOT be
    # swallowed into an empty archive — that labels a fake-empty blob as the
    # prior version and destroys real history. Abort the update instead.
    try:
        old_content = await _read_text(adapter, doc.storage_key)
    except Exception as exc:
        logger.error(
            "file %s: cannot read current blob %r to archive prior version: %s",
            file_id,
            doc.storage_key,
            exc,
        )
        raise CloudError(
            500,
            "files.archive_read_failed",
            "Could not read the file's current content to archive it; "
            "aborting to protect version history.",
        ) from exc

    old_hash = _sha256(old_content)

    if old_hash == new_hash:
        # No content change — nothing archived or bumped. Report the ACTUAL
        # stored version so the caller's next If-Match doesn't 409 against it.
        return UpdateFileContentResponse(
            file_id=file_id,
            new_version=current_version,
            size_bytes=new_size,
            content_hash=new_hash,
        )

    new_version = current_version + 1

    # Archive the OLD content under the version it actually was
    # (``current_version``), so a future revert/diff maps version -> content
    # correctly. FileVersionDoc keeps the bare path (reads are
    # workspace-filtered, so the bare id is collision-safe here).
    version_doc = FileVersionDoc(
        file_id=file_id,
        workspace_id=workspace_id,
        version_number=current_version,
        content=old_content,
        content_hash=old_hash,
        size_bytes=len(old_content.encode("utf-8")),
        editor_kind=editor_kind,
        editor_id=editor_id,
        created_at=datetime.now(UTC),
    )
    await version_doc.insert()

    stored = await adapter.put(doc.storage_key, _bytes_iter(new_content), doc.mime)

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
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

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
            id=fv.id,
            file_id=fv.file_id,
            version_number=fv.version_number,
            size_bytes=fv.size_bytes,
            editor_kind=fv.editor_kind,
            editor_id=fv.editor_id,
            created_at=fv.created_at,
        )
        for fv in map(_to_domain, docs)
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
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

    try:
        oid = PydanticObjectId(version_id)
    except Exception:
        raise NotFound("version", version_id) from None

    doc = await FileVersionDoc.find_one(
        {"_id": oid, "file_id": file_id, "workspace_id": workspace_id}
    )
    if not doc:
        raise NotFound("version", version_id)

    fv = _to_domain(doc)
    return FileVersionResponse(
        id=fv.id,
        file_id=fv.file_id,
        version_number=fv.version_number,
        content=fv.content,
        content_hash=fv.content_hash,
        size_bytes=fv.size_bytes,
        editor_kind=fv.editor_kind,
        editor_id=fv.editor_id,
        created_at=fv.created_at,
    )
