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
#     insert (which would raise DuplicateKeyError on the tombstoned unique id),
#     and PURGES the dead file's FileVersionDoc rows on revive so the recreated
#     file starts a fresh history (recreate = a new file at that path).
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
# Updated: 2026-07-03 (FL-2, port of #1193) — completes the deferred Slice-D
#   history helpers dropped by ART-1: ``revert_to_version`` (restore a prior
#   version's content as a new live version) and ``diff_versions`` (unified
#   diff between two archived versions). Both are tenant-filtered via
#   ``get_version``. A stale ``If-Match`` now raises ``PreconditionFailed``
#   (412) instead of ``ConflictError`` (409), matching HTTP conditional-request
#   semantics for the file-version write path.
# Updated: 2026-07-03 (FL-16, editor↔Library bridge) — the editor version path
#   (``update_file_content`` and, transitively, ``revert_to_version``) now
#   resolves BOTH id schemes via the new ``_resolve_upload`` helper: it tries the
#   editor's workspace-namespaced id (``${workspace}:${path}``) first, then falls
#   back to a Library ``FileUpload`` row keyed by its OWN bare ``file_id`` (a
#   uuid). Before this, the frontend editor (FL-7/8/9) opened a Library file with
#   its bare ``file_id`` and ``PUT /files/{file_id}`` resolved nothing (the
#   path-based lookup namespaced the uuid and missed the row), so editing a real
#   Library file end-to-end was broken. Both lookups pin ``workspace``, so the
#   bridge keeps tenant isolation (a cross-workspace bare id fails closed).
#   ``FileVersionDoc`` is still keyed on the client-facing ``file_id`` inside this
#   module (the import-linter "FileVersions" contract), so ``list_versions`` /
#   ``get_version`` / ``revert_to_version`` find the archived rows for either
#   scheme. The path-based flow FL-2's tests cover is preserved (namespaced id
#   tried first).
# Updated: 2026-07-03 (FL-3, agent library verbs) — added ``annotate_upload``:
#   the content-mutation entrypoint for the ``annotate_file`` agent verb. A
#   Library file (a ``FileUpload`` row keyed by its OWN bare ``file_id``, NOT the
#   ``${workspace}:${path}`` namespaced id the editor path uses) gets a short
#   note prepended to its content as a NEW archived version, so the annotation is
#   revertable through the same ``list_versions`` / ``get_version`` readers. It
#   reuses the archive-then-rewrite mechanism of ``update_file_content`` but keys
#   on the Library row's own id, keeping every ``FileVersionDoc`` write inside
#   this module (the import-linter "FileVersions" contract) — the agent tool
#   never touches the Beanie doc directly. Tenant-filtered: the ``FileUpload``
#   lookup and every ``FileVersionDoc`` read/write pins ``workspace_id``.
# Updated: 2026-07-03 (FL-5, structural edit tools — port of dewani12's #1193)
#   — added ``read_current_content``: the read half the FL-5 edit tools
#   (edit_document / edit_slides / edit_spreadsheet) need to load a file's
#   CURRENT text so they can apply structural block/deck/workbook operations and
#   write the mutated result back through ``update_file_content`` (editor_kind
#   ="agent"), so every structural edit lands as a NEW revertable version. It
#   resolves BOTH id schemes via ``_resolve_upload`` (editor-namespaced id, then
#   Library bare id) and reads the live blob through the shared adapter, keeping
#   the FL-5 tools off the Beanie doc + storage adapter directly. Tenant-safe:
#   the ``_resolve_upload`` lookup pins ``workspace_id``, so a cross-workspace
#   file_id fails closed (NotFound).
"""FileVersions service — file-version write + history.

Module-level ``async def`` functions. Sole owner of writes to the
``FileVersionDoc`` Beanie document and the ``FileUpload.content_version``
counter. Text-only — non-editable mime types are rejected early.

Public API:
- ``write_file(ctx, body)`` — POST /files/write (create or overwrite)
- ``update_file_content(ctx, file_id, body)`` — PUT /files/{id}
- ``list_versions(ctx, file_id)`` — version list (no content)
- ``get_version(ctx, file_id, version_id)`` — full version with content
- ``revert_to_version(ctx, file_id, version_id)`` — restore a prior version
- ``diff_versions(ctx, file_id, from_id, to_id)`` — unified diff between two

The client-facing file id is the bare ``path`` throughout. The FileUpload
row stores a workspace-namespaced id (``${workspace}:${path}``) because that
collection's ``file_id`` index is globally unique; FileVersionDoc keeps the
bare path (its reads are always workspace-filtered, so no global collision).
"""

from __future__ import annotations

import difflib
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
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound, PreconditionFailed
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import Event
from pocketpaw_ee.cloud.file_versions.domain import FileVersion
from pocketpaw_ee.cloud.file_versions.dto import (
    DiffResponse,
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


async def _resolve_upload(workspace_id: str, file_id: str) -> FileUpload | None:
    """Resolve the live ``FileUpload`` row a client ``file_id`` refers to (FL-16).

    Two id schemes reach the version path and BOTH must resolve:

    * The editor's own ``write_file``-created files store the FileUpload under
      the workspace-namespaced id ``${workspace}:${path}`` (see ``_storage_id``);
      the client passes the bare ``path``.
    * A Library file (uploaded via the /files pipeline, FL-1) keys the FileUpload
      by its OWN globally-unique bare ``file_id`` (a uuid); the frontend editor
      (FL-7/8/9) opens it with exactly that bare id.

    Try the namespaced editor id first (the historical behavior the FL-2 tests
    cover), then fall back to the bare Library id. Both lookups pin
    ``workspace`` so a caller can never resolve another tenant's row — the bare
    ``file_id`` is globally unique but the workspace filter still fail-closes
    cross-tenant access. Returns the live (non-tombstoned) row or ``None``.
    """
    stored_id = _storage_id(workspace_id, file_id)
    doc = await FileUpload.find_one(
        {"file_id": stored_id, "workspace": workspace_id, "deleted_at": None}
    )
    if doc is not None:
        return doc
    # Fall back to a Library row keyed by its own bare file_id.
    return await FileUpload.find_one(
        {"file_id": file_id, "workspace": workspace_id, "deleted_at": None}
    )


def _storage_id(workspace_id: str, path: str) -> str:
    """Namespace a client path by workspace for the globally-unique
    ``FileUpload.file_id`` index.

    Two workspaces writing the same ``path`` map to distinct stored ids, so
    neither collides on the unique key nor squats the other's path. The bare
    ``path`` stays the client-facing id everywhere else (FileVersionDoc,
    DTOs, routes); this value is only ever the FileUpload lookup/insert key.

    Invariant: ``workspace_id`` is a colon-free 24-hex Mongo ObjectId, so the
    first ``':'`` unambiguously splits ws from path — ``(ws, path) -> ws:path``
    stays injective even when ``path`` itself contains a colon.
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
        # Revive a soft-deleted tombstone in place — a blind insert would
        # collide on the still-unique stored id. Recreate semantics = a NEW
        # file at this path = FRESH history, so purge the dead file's archived
        # versions first (they outlive the FileUpload row — file_versions never
        # deletes version rows otherwise — and would bleed stale content +
        # duplicate version_number=1 labels into the revived file).
        await FileVersionDoc.find({"file_id": path, "workspace_id": workspace_id}).delete()
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

    ``file_id`` is the client-facing id. It resolves to a ``FileUpload`` row via
    ``_resolve_upload`` — either the editor's workspace-namespaced id
    (``${workspace}:${path}``) for a ``write_file``-created file, OR a Library
    row's own bare ``file_id`` (FL-16 bridge), so the frontend editor can save a
    real Library file end-to-end. Steps:
    1. Resolve the ``FileUpload`` doc (editor-namespaced id, then Library id).
    2. Check the ``If-Match`` precondition against ``content_version``.
    3. Read + archive the current blob as a ``FileVersionDoc`` (labelled with
       the version it actually was). A read failure aborts — never archive an
       empty "prior" version.
    4. Upload the new content and bump the version counter.

    ``FileVersionDoc`` is always keyed on the client-facing ``file_id`` passed
    here, so ``list_versions`` / ``get_version`` / ``revert_to_version`` — which
    query by that same id — find the archived rows regardless of which id scheme
    resolved the FileUpload.
    """
    workspace_id = ctx.workspace_id
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

    doc = await _resolve_upload(workspace_id, file_id)
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
        # Stale ``If-Match`` — a conditional-request precondition failure (412),
        # not a generic 409 conflict. Standard optimistic-concurrency semantics.
        raise PreconditionFailed(
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


async def read_current_content(
    ctx: RequestContext,
    file_id: str,
) -> str:
    """Return a file's CURRENT live text content (FL-5 read half).

    The structural edit tools (edit_document / edit_slides / edit_spreadsheet)
    load the current content, apply their block/deck/workbook operations, then
    write the result back via ``update_file_content`` so the edit archives as a
    new revertable version. This is the read they need.

    Resolves BOTH id schemes via ``_resolve_upload`` (the editor's
    ``${workspace}:${path}`` id first, then a Library row's own bare ``file_id``)
    and reads the live blob through the shared adapter — the FL-5 tools never
    touch the Beanie doc or the adapter directly (import-linter "FileVersions"
    contract). Tenant-filtered: ``_resolve_upload`` pins ``workspace_id``, so a
    cross-workspace ``file_id`` raises ``NotFound``. A non-editable mime raises
    422 (the same guard the write path uses).
    """
    workspace_id = ctx.workspace_id
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

    doc = await _resolve_upload(workspace_id, file_id)
    if not doc:
        raise NotFound("file", file_id)

    if not _is_editable(doc.mime):
        raise CloudError(
            422,
            "files.not_editable",
            f"File type '{doc.mime}' cannot be edited inline.",
        )

    adapter = _get_storage()
    try:
        return await _read_text(adapter, doc.storage_key)
    except Exception as exc:
        logger.error(
            "read_current_content %s: cannot read blob %r: %s",
            file_id,
            doc.storage_key,
            exc,
        )
        raise CloudError(
            500,
            "files.read_failed",
            "Could not read the file's current content.",
        ) from exc


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


async def revert_to_version(
    ctx: RequestContext,
    file_id: str,
    version_id: str,
) -> UpdateFileContentResponse:
    """Restore the live file to a historical version's content.

    Fetches the target version (tenant-filtered via ``get_version`` — a
    cross-workspace id is a NotFound), then routes its content back through
    ``update_file_content`` so the restore is itself a normal versioned write:
    the current (pre-revert) content is archived as a new version and the live
    counter bumps. A no-op revert (target content == current content) returns
    the stored version without archiving, matching the update path.

    ``NotFound`` propagates when the version is missing / cross-tenant.
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
    """Return a unified diff between two archived versions.

    Both versions are fetched via ``get_version`` (tenant-filtered), so a diff
    can never span a workspace boundary. Produces a standard ``difflib``
    unified diff of ``from`` -> ``to`` content.
    """
    v_from = await get_version(ctx, file_id, from_version_id)
    v_to = await get_version(ctx, file_id, to_version_id)

    diff = "".join(
        difflib.unified_diff(
            v_from.content.splitlines(keepends=True),
            v_to.content.splitlines(keepends=True),
            fromfile=f"v{v_from.version_number}",
            tofile=f"v{v_to.version_number}",
        )
    )
    return DiffResponse(
        from_version=v_from.version_number,
        to_version=v_to.version_number,
        diff=diff,
    )


async def annotate_upload(
    ctx: RequestContext,
    file_id: str,
    note: str,
) -> UpdateFileContentResponse:
    """Prepend a short note to a Library file's content as a new version (FL-3).

    Unlike ``update_file_content`` (which keys on the ``${workspace}:${path}``
    namespaced editor id), this targets a Library ``FileUpload`` row by its OWN
    bare ``file_id`` — the id the uploads pipeline stamps and the /files listing
    surfaces. It archives the current blob as a ``FileVersionDoc`` and rewrites
    the live blob with ``note`` prepended, bumping ``content_version`` so the
    annotation is revertable via ``list_versions`` / ``get_version``.

    Tenant-safe: the ``FileUpload`` lookup and the ``FileVersionDoc`` write both
    pin ``workspace_id``, so a caller can never annotate another workspace's
    file. Editable-mime only (a binary Library file returns 422); a blank note
    is rejected (400). ``editor_kind`` is ``"agent"`` — this is the agent verb.
    """
    workspace_id = ctx.workspace_id
    if not workspace_id:
        raise CloudError(400, "files.missing_workspace", "Workspace is required.")

    clean_note = (note or "").strip()
    if not clean_note:
        raise CloudError(400, "files.empty_note", "Annotation note must be non-empty.")

    # Library rows key on the bare file_id (NOT the namespaced editor id).
    doc = await FileUpload.find_one(
        {"file_id": file_id, "workspace": workspace_id, "deleted_at": None}
    )
    if not doc:
        raise NotFound("file", file_id)

    if not _is_editable(doc.mime):
        raise CloudError(
            422,
            "files.not_editable",
            f"File type '{doc.mime}' cannot be annotated inline.",
        )

    editor_id = ctx.user_id or "unknown"
    adapter = _get_storage()

    # Read the current blob so it can be archived. A read failure must NOT be
    # swallowed into an empty archive (same guard as ``update_file_content``).
    try:
        old_content = await _read_text(adapter, doc.storage_key)
    except Exception as exc:
        logger.error(
            "annotate %s: cannot read current blob %r to archive prior version: %s",
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
    current_version = doc.content_version or 0

    # Prepend the note as a comment-style banner. Kept plain so it round-trips
    # through any text/* mime without corrupting structured formats badly.
    new_content = f"[note] {clean_note}\n{old_content}"
    new_hash = _sha256(new_content)
    new_size = len(new_content.encode("utf-8"))

    # Archive the OLD content under the version it actually was. A legacy
    # Library row starts at content_version 0; label the archived blob with that
    # so the history is monotonic and the revived counter bumps cleanly.
    version_doc = FileVersionDoc(
        file_id=file_id,
        workspace_id=workspace_id,
        version_number=current_version,
        content=old_content,
        content_hash=old_hash,
        size_bytes=len(old_content.encode("utf-8")),
        editor_kind="agent",
        editor_id=editor_id,
        created_at=datetime.now(UTC),
    )
    await version_doc.insert()

    stored = await adapter.put(doc.storage_key, _bytes_iter(new_content), doc.mime)

    new_version = current_version + 1
    doc.size = stored.size
    doc.content_version = new_version
    await doc.save()

    await emit(
        Event(
            type="file.annotated",
            data={
                "file_id": file_id,
                "workspace_id": workspace_id,
                "version": new_version,
                "editor_kind": "agent",
                "editor_id": editor_id,
            },
        )
    )

    logger.info("file %s annotated to version %d by agent:%s", file_id, new_version, editor_id)

    return UpdateFileContentResponse(
        file_id=file_id,
        new_version=new_version,
        size_bytes=new_size,
        content_hash=new_hash,
    )
