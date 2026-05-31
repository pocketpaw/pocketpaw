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
async def grant(file_id: str) -> dict:
    """Mint a short-lived signed URL for ``file_id``.

    Two signing paths, adapter-driven:

    1. If the configured storage adapter can sign (S3 and friends), return
       the adapter's presigned URL — the browser fetches bytes directly
       from object storage, bypassing the app server.
    2. Otherwise, return an HMAC-signed proxy URL routed through this
       service's ``GET /uploads/{id}?t=...`` endpoint. Lets local-disk
       deployments still power ``<img src>`` and ``<a download>``.
    """
    try:
        _rec, presigned = await _SVC.presigned_get(
            file_id, requester_id=_OWNER, ttl_seconds=DEFAULT_TTL_SECONDS
        )
    except NotFound as e:
        raise HTTPException(status_code=404, detail="not found") from e

    if presigned:
        return {
            "url": presigned,
            "expires_at": int(time.time()) + DEFAULT_TTL_SECONDS,
        }

    token, expires_at = sign_grant(file_id, get_access_token())
    return {
        "url": f"/api/v1/uploads/{file_id}?t={token}",
        "expires_at": expires_at,
    }


@router.get("/{file_id}")
async def download(file_id: str, t: str | None = None) -> StreamingResponse:
    # ``t`` is accepted purely so FastAPI ignores it (the signature is verified
    # by middleware before this handler runs). Keeping it in the signature
    # also documents the public contract.
    _ = t
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
            # Prevent browsers from MIME-sniffing past the declared type.
            # Defense-in-depth against content-type confusion for uploads that
            # are labeled as text/* but hold unexpected bytes.
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
