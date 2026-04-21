"""EE /uploads router — workspace-scoped upload endpoints.

2026-04-19 (Cluster E sub-PR 3): added ``GET /uploads/{id}/download-url``
as an explicitly-named alias for the existing ``/grant`` endpoint. The
alias returns the same signed-URL-or-cookie-URL payload plus a
short-lived ``expires_at`` and a ``filename`` that the FE can use as the
default save-as name. The underlying service enforces workspace scope +
per-file adapter auth; nothing extra leaks through the alias.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
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
from ee.cloud.shared.deps import current_user_id, current_workspace_id
from ee.cloud.shared.time import iso_utc
from ee.cloud.uploads.mongo_store import MongoFileStore
from ee.cloud.uploads.service import EEUploadService
from pocketpaw.uploads.config import INLINE_MIMES, UploadSettings
from pocketpaw.uploads.errors import NotFound
from pocketpaw.uploads.factory import build_adapter

# Module-level singletons — one adapter + store per process
_ROOT = Path.home() / ".pocketpaw" / "uploads"
_CFG = UploadSettings(local_root=_ROOT)
_ADAPTER = build_adapter(_ROOT)
_META = MongoFileStore()
_SVC = EEUploadService(adapter=_ADAPTER, meta=_META, cfg=_CFG)

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


@router.post("")
async def upload(
    files: Annotated[list[UploadFile], File(...)],
    chat_id: Annotated[str | None, Form()] = None,
    workspace: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    try:
        result = await _SVC.upload_many(files, user_id, chat_id, workspace)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "uploaded": [_record_to_dict(r) for r in result.uploaded],
        "failed": [asdict(f) for f in result.failed],
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
        rec, presigned = await _SVC.presigned_get(
            file_id, user_id, workspace, DEFAULT_TTL_SECONDS
        )
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
        _rec, presigned = await _SVC.presigned_get(
            file_id, user_id, workspace, DEFAULT_TTL_SECONDS
        )
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
