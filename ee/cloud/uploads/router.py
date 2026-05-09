"""EE /uploads router — workspace-scoped upload endpoints.

2026-04-19 (Cluster E sub-PR 3): added ``GET /uploads/{id}/download-url``
as an explicitly-named alias for the existing ``/grant`` endpoint. The
alias returns the same signed-URL-or-cookie-URL payload plus a
short-lived ``expires_at`` and a ``filename`` that the FE can use as the
default save-as name. The underlying service enforces workspace scope +
per-file adapter auth; nothing extra leaks through the alias.

2026-04-21: folder support on the uploads provider (folders live ONLY
here). ``POST /uploads/folders``, ``PATCH /uploads/folders/{id}``,
``DELETE /uploads/folders/{id}``, ``PATCH /uploads/{file_id}``, plus a
new ``path`` multipart field on ``POST /uploads`` that auto-creates any
missing folder chain.

2026-05-03 (Stage 3.E "Files as Knowledge"): ``POST /uploads`` accepts
an optional ``pocket_id`` form field. When set, the upload is gated
by the pocket's ABAC (must have edit access via
``pockets.service.has_edit_access``) and the resulting ``FileUpload``
row carries ``pocket_id`` so it's only visible through pocket-scoped
listings + indexed into ``pocket:{id}`` KB.

2026-05-06 (fix/rbac-connector-upload-guards): ``POST /uploads`` and
``POST /uploads/folders`` now require ``uploads.write`` (MEMBER) via
``require_action_any_workspace``. Previously ``current_workspace_id``
authenticated the caller but did not verify workspace membership, so a
user with a foreign workspace set as active_workspace could write files
into it.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse

from ee.cloud.license import require_license
from ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from ee.cloud.shared.time import iso_utc
from ee.cloud.uploads.folder_store import FolderStore
from ee.cloud.uploads.mongo_store import MongoFileStore
from ee.cloud.uploads.paths import normalize_path, parent_of
from ee.cloud.uploads.service import EEUploadService
from pocketpaw.uploads.config import INLINE_MIMES, UploadSettings
from pocketpaw.uploads.errors import NotFound
from pocketpaw.uploads.factory import build_adapter

# Module-level singletons — one adapter + store per process
_ROOT = Path.home() / ".pocketpaw" / "uploads"
_CFG = UploadSettings(local_root=_ROOT)
_ADAPTER = build_adapter(_ROOT)
_META = MongoFileStore()
_FOLDERS = FolderStore()


async def _is_chat_member(chat_id: str, user_id: str, _workspace: str) -> bool:
    """Return True if ``user_id`` is a member of the chat group.

    Reuses ``group_service.list_member_ids`` which handles missing/invalid
    ids gracefully (returns ``[]``). The workspace arg is accepted for
    interface symmetry but not used — membership is the authoritative signal
    and the upstream ``get_scoped(workspace=workspace)`` already binds the
    file to the workspace.
    """
    from ee.cloud.chat import group_service

    members = await group_service.list_member_ids(chat_id)
    return user_id in members


async def _is_workspace_admin(user_id: str, workspace: str) -> bool:
    """Return True if ``user_id`` is an owner/admin of ``workspace``."""
    from ee.cloud.workspace import service as workspace_service

    admins = await workspace_service.list_admin_ids(workspace)
    return user_id in admins


_SVC = EEUploadService(
    adapter=_ADAPTER,
    meta=_META,
    cfg=_CFG,
    is_chat_member=_is_chat_member,
    is_workspace_admin=_is_workspace_admin,
)

router = APIRouter(
    prefix="/uploads",
    tags=["Uploads"],
    dependencies=[Depends(require_license)],
)


def _record_to_dict(rec) -> dict:
    return {
        "id": rec.id,
        "filename": rec.filename,
        "mime": rec.mime,
        "size": rec.size,
        "url": f"/api/v1/uploads/{rec.id}",
        "created": iso_utc(rec.created),
    }


# ---------------------------------------------------------------------------
# Folder routes
# ---------------------------------------------------------------------------


@router.post(
    "/folders",
    dependencies=[Depends(require_action_any_workspace("uploads.write"))],
)
async def create_folder(
    body: dict = Body(...),
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    raw_path = body.get("path")
    if not isinstance(raw_path, str):
        raise HTTPException(status_code=400, detail="path is required")
    try:
        path = normalize_path(raw_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if path == "/":
        raise HTTPException(status_code=400, detail="cannot create root folder")
    parent = parent_of(path)
    if parent != "/" and not await _FOLDERS.path_exists(workspace, parent):
        raise HTTPException(status_code=400, detail="parent folder does not exist")
    if await _FOLDERS.path_exists(workspace, path):
        raise HTTPException(status_code=409, detail="folder.exists")
    doc = await _FOLDERS.create(workspace=workspace, owner=user_id, path=path)
    return {
        "id": doc.folder_id,
        "path": doc.path,
        "name": doc.name,
        "created_at": iso_utc(doc.created_at),
    }


@router.patch("/folders/{folder_id}")
async def rename_folder(
    folder_id: str,
    body: dict = Body(...),
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    raw_new = body.get("new_path")
    if not isinstance(raw_new, str):
        raise HTTPException(status_code=400, detail="new_path is required")
    try:
        new_path = normalize_path(raw_new)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if new_path == "/":
        raise HTTPException(status_code=400, detail="cannot move to root")

    doc = await _FOLDERS.get_by_id(workspace, folder_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="not found")

    # Write-side ACL — owner OR workspace admin.
    if doc.owner != user_id:
        try:
            is_admin = await _is_workspace_admin(user_id, workspace)
        except Exception:
            is_admin = False
        if not is_admin:
            raise HTTPException(status_code=403, detail="files.forbidden")

    old_path = doc.path
    if old_path == new_path:
        return {
            "id": doc.folder_id,
            "path": doc.path,
            "name": doc.name,
        }

    # Moving under self is not allowed.
    if new_path.startswith(old_path + "/"):
        raise HTTPException(status_code=400, detail="cannot move folder into itself")

    new_parent = parent_of(new_path)
    if new_parent != "/" and not await _FOLDERS.path_exists(workspace, new_parent):
        raise HTTPException(status_code=400, detail="parent folder does not exist")

    # Retry-safe: if the target already exists AND is the same row (unlikely
    # since we checked by id), fall through; otherwise it's a real conflict.
    existing = await _FOLDERS.get_by_path(workspace, new_path)
    if existing is not None and existing.folder_id != folder_id:
        raise HTTPException(status_code=409, detail="folder.exists")

    # Rewrite descendants first, then the row itself — descendants read the
    # current path prefix. If the caller retries after partial progress we
    # re-enter here and the already-moved rows are just no-ops.
    await _FOLDERS.rewrite_path_prefix(workspace, old_path, new_path)
    await _META.rewrite_folder_prefix(workspace, old_path, new_path)

    from datetime import UTC
    from datetime import datetime as _dt

    from ee.cloud.uploads.paths import basename as _basename

    doc.path = new_path
    doc.name = _basename(new_path)
    doc.updated_at = _dt.now(UTC)
    await doc.save()

    return {
        "id": doc.folder_id,
        "path": doc.path,
        "name": doc.name,
    }


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: str,
    cascade: bool = False,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> Response:
    doc = await _FOLDERS.get_by_id(workspace, folder_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="not found")

    if doc.owner != user_id:
        try:
            is_admin = await _is_workspace_admin(user_id, workspace)
        except Exception:
            is_admin = False
        if not is_admin:
            raise HTTPException(status_code=403, detail="files.forbidden")

    # Emptiness check — files live in _META, subfolders in _FOLDERS.
    if not cascade:
        subs = await _FOLDERS.count_subfolders(workspace, doc.path)
        files_n = await _META.count_under_prefix(workspace, doc.path)
        if subs > 0 or files_n > 0:
            raise HTTPException(status_code=409, detail="folder.not_empty")

    await _FOLDERS.soft_delete_under_prefix(workspace, doc.path)
    await _META.soft_delete_under_prefix(workspace, doc.path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# File routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    dependencies=[Depends(require_action_any_workspace("uploads.write"))],
)
async def upload(
    files: Annotated[list[UploadFile], File(...)],
    chat_id: Annotated[str | None, Form()] = None,
    path: Annotated[str | None, Form()] = None,
    pocket_id: Annotated[str | None, Form()] = None,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    try:
        folder_path = normalize_path(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Stage 3.E ABAC gate: pocket-scoped uploads require edit access on the
    # target pocket. Owner / shared-with / workspace-visible all grant
    # write per ``pockets.service.has_edit_access``. Non-members get a 403
    # with the same code paw-enterprise's pocket UI already handles.
    if pocket_id:
        from ee.cloud.pockets import service as pockets_service

        try:
            allowed = await pockets_service.has_edit_access(pocket_id=pocket_id, user_id=user_id)
        except Exception:
            allowed = False
        if not allowed:
            raise HTTPException(status_code=403, detail="files.pocket_forbidden")
    # Auto-create missing folder chain (only when not root).
    if folder_path != "/":
        try:
            await _FOLDERS.ensure_chain(workspace=workspace, owner=user_id, path=folder_path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        result = await _SVC.upload_many(
            files,
            user_id,
            chat_id,
            workspace,
            folder_path=folder_path,
            pocket_id=pocket_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "uploaded": [_record_to_dict(r) for r in result.uploaded],
        "failed": [asdict(f) for f in result.failed],
    }


@router.patch("/{file_id}")
async def patch_upload(
    file_id: str,
    body: dict = Body(...),
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    doc = await _META.get_doc_scoped(file_id, workspace=workspace)
    if doc is None:
        raise HTTPException(status_code=404, detail="not found")

    # Write-side ACL — owner OR workspace admin. Chat-member does NOT grant write.
    if doc.owner != user_id:
        try:
            is_admin = await _is_workspace_admin(user_id, workspace)
        except Exception:
            is_admin = False
        if not is_admin:
            raise HTTPException(status_code=403, detail="files.forbidden")

    new_filename = body.get("filename")
    new_folder = body.get("folder_path")

    if new_filename is not None:
        if not isinstance(new_filename, str) or not new_filename.strip():
            raise HTTPException(status_code=400, detail="filename must be a non-empty string")
        if "/" in new_filename or "\\" in new_filename:
            raise HTTPException(status_code=400, detail="filename must not contain slashes")
        doc.filename = new_filename

    if new_folder is not None:
        try:
            norm = normalize_path(new_folder)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if norm != "/" and not await _FOLDERS.path_exists(workspace, norm):
            raise HTTPException(status_code=400, detail="destination folder does not exist")
        doc.folder_path = norm

    await doc.save()
    return {
        "id": doc.file_id,
        "filename": doc.filename,
        "folder_path": doc.folder_path or "/",
        "mime": doc.mime,
        "size": doc.size,
    }


@router.get("/{file_id}/download-url")
async def download_url(
    file_id: str,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Return a short-TTL download URL for ``file_id``.

    Cluster E sub-PR 3 alias of ``/grant``. The payload shape matches —
    ``{url, expires_at}`` — with an extra ``filename`` so the FE's
    "Save As" dialog opens with a sensible default. Workspace scope is
    enforced by ``EEUploadService.presigned_get``; the alias does not
    relax any check.
    """
    import time

    from pocketpaw.uploads.signing import DEFAULT_TTL_SECONDS

    try:
        rec, presigned = await _SVC.presigned_get(file_id, user_id, workspace, DEFAULT_TTL_SECONDS)
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e

    url = presigned or f"/api/v1/uploads/{file_id}"
    return {
        "url": url,
        "expires_at": int(time.time()) + DEFAULT_TTL_SECONDS,
        "filename": rec.filename,
    }


@router.get("/{file_id}/grant")
async def grant(
    file_id: str,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Mint a short-lived download URL for ``file_id``.

    Returns the storage adapter's presigned URL when available (S3 and
    friends). Otherwise returns the authenticated cloud download URL —
    the paw-enterprise browser attaches ``paw_auth`` cookies via
    ``withCredentials`` so ``<img src>`` / ``<a href download>`` work
    directly without a Bearer header.

    HMAC-signed ``?t=`` grants are intentionally NOT used here: the EE
    download route at ``GET /uploads/{id}`` requires ``current_active_user``
    (JWT), and the OSS dashboard auth middleware verifies HMAC with its
    own master token, not EE's ``SECRET``. Embedding these URLs in
    cookie-less contexts (mobile webviews, cross-origin embeds) requires
    S3 presigning — use that adapter for production.
    """
    import time

    from pocketpaw.uploads.signing import DEFAULT_TTL_SECONDS

    try:
        _rec, presigned = await _SVC.presigned_get(file_id, user_id, workspace, DEFAULT_TTL_SECONDS)
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e

    if presigned:
        return {
            "url": presigned,
            "expires_at": int(time.time()) + DEFAULT_TTL_SECONDS,
        }

    return {
        "url": f"/api/v1/uploads/{file_id}",
        "expires_at": int(time.time()) + DEFAULT_TTL_SECONDS,
    }


@router.get("/{file_id}")
async def download(
    file_id: str,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> StreamingResponse:
    try:
        rec, it = await _SVC.stream(file_id, user_id, workspace)
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e
    disposition = "inline" if rec.mime in INLINE_MIMES else "attachment"
    return StreamingResponse(
        it,
        media_type=rec.mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{rec.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{file_id}", status_code=204)
async def delete_upload(
    file_id: str,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> Response:
    try:
        await _SVC.delete(file_id, user_id, workspace)
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
