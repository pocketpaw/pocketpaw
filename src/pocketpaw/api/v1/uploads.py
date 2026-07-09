"""OSS /uploads router — POST (single + bulk), GET (stream), DELETE."""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse

from pocketpaw.api.deps import require_scope
from pocketpaw.dashboard_auth import get_access_token
from pocketpaw.uploads.config import INLINE_MIMES, UploadSettings
from pocketpaw.uploads.errors import NotFound
from pocketpaw.uploads.factory import build_adapter
from pocketpaw.uploads.file_store import JSONLFileStore
from pocketpaw.uploads.service import UploadService
from pocketpaw.uploads.signing import DEFAULT_TTL_SECONDS, sign_grant

_OWNER = "local"  # OSS is single-user; all uploads are "owned" by the local user.

_ROOT = Path.home() / ".pocketpaw" / "uploads"
_INDEX = _ROOT / "_idx.jsonl"
_CFG = UploadSettings(local_root=_ROOT)
_ADAPTER = build_adapter(_ROOT)
_META = JSONLFileStore(path=_INDEX)
_SVC = UploadService(adapter=_ADAPTER, meta=_META, cfg=_CFG)

router = APIRouter(
    prefix="/uploads",
    tags=["Uploads"],
    dependencies=[Depends(require_scope("uploads"))],
)


def _record_to_dict(rec) -> dict:
    return {
        "id": rec.id,
        "filename": rec.filename,
        "mime": rec.mime,
        "size": rec.size,
        "url": f"/api/v1/uploads/{rec.id}",
        "created": rec.created.isoformat(),
    }


@router.post("")
async def upload(
    files: Annotated[list[UploadFile], File(...)],
    chat_id: Annotated[str | None, Form()] = None,
) -> dict:
    try:
        result = await _SVC.upload_many(files, owner_id=_OWNER, chat_id=chat_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "uploaded": [_record_to_dict(r) for r in result.uploaded],
        "failed": [asdict(f) for f in result.failed],
    }


@router.get("/{file_id}/grant")
async def grant(
    file_id: str,
    w: int = Query(default=0, ge=0, le=2048, description="Thumbnail width"),
    h: int = Query(default=0, ge=0, le=2048, description="Thumbnail height"),
    q: int = Query(default=80, ge=1, le=100, description="Thumbnail quality"),
    f: str = Query(default="webp", description="Thumbnail format: webp, jpeg, png"),
) -> dict:
    """Mint a short-lived signed URL for ``file_id``.

    When ``w`` or ``h`` is provided, the signed URL points to a resized
    thumbnail served through the ``GET /uploads/{id}`` endpoint with the
    same HMAC token. Thumbnail generation is deferred to the first request
    (cached server-side after that).

    Two signing paths, adapter-driven:

    1. If the configured storage adapter can sign (S3) AND no thumb params
       are set, return the adapter's presigned URL.
    2. Otherwise, return an HMAC-signed proxy URL.
    """
    has_thumb = w > 0 or h > 0
    try:
        _rec, presigned = await _SVC.presigned_get(
            file_id, requester_id=_OWNER, ttl_seconds=DEFAULT_TTL_SECONDS
        )
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e

    # For thumbnails, always use the HMAC proxy path (even with S3) so the
    # server can resize on-the-fly. Thumb bytes are cached locally after first
    # generation, so the server-side overhead is negligible.
    if has_thumb or not presigned:
        token, expires_at = sign_grant(file_id, get_access_token())
        thumb_qs = f"w={w}&h={h}&q={q}&f={f}" if has_thumb else ""
        base_url = f"/api/v1/uploads/{file_id}"
        return {
            "url": f"{base_url}?t={token}{'&' + thumb_qs if thumb_qs else ''}",
            "expires_at": expires_at,
        }

    return {
        "url": presigned,
        "expires_at": int(time.time()) + DEFAULT_TTL_SECONDS,
    }


@router.get("/{file_id}")
async def download(
    file_id: str,
    t: str | None = None,
    w: int = Query(default=0, ge=0, le=2048),
    h: int = Query(default=0, ge=0, le=2048),
    q: int = Query(default=80, ge=1, le=100),
    f: str = Query(default="webp"),
) -> StreamingResponse:
    """Stream a file or a resized thumbnail.

    When ``w`` or ``h`` is > 0, the response is a resized thumbnail in the
    requested format (WebP by default). Otherwise the full original file is
    served with its original MIME type.
    """
    # ``t`` is verified by middleware before this handler runs.
    _ = t
    has_thumb = w > 0 or h > 0

    if has_thumb:
        try:
            rec, mime_type, it = await _SVC.thumbnail(
                file_id,
                requester_id=_OWNER,
                width=w,
                height=h,
                quality=q,
                fmt=f,
            )
        except NotFound as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail="thumbnail generation failed") from e
        return StreamingResponse(
            it,
            media_type=mime_type,
            headers={
                "Cache-Control": "public, max-age=86400, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    try:
        rec, it = await _SVC.stream(file_id, requester_id=_OWNER)
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
async def delete_upload(file_id: str) -> Response:
    try:
        await _SVC.delete(file_id, requester_id=_OWNER)
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e
    return Response(status_code=status.HTTP_204_NO_CONTENT)
