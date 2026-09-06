"""EE media router — list + serve + upload generated media (images, videos).

Generated assets from the /studio surface are stored through the shared storage
swap (``media.storage`` → ``pocketpaw.uploads.build_adapter``): local disk in
dev, S3 in a POCKETPAW_UPLOAD_ADAPTER=s3 deployment. This router lists them for
the gallery grid, serves them over HTTP so the frontend can render ``<img>`` /
``<video>`` tags, and accepts the canvas editor's "save edited image" upload.

Endpoints:
  GET  /api/v1/media          — list all generated media files
  POST /api/v1/media          — upload a generated file (canvas "save edited image")
  GET  /api/v1/media/{name}   — serve a single media file

Updated: 2026-09-06 (BR-4, feat/browser-surface-extract): /browser screenshots
are saved through this same storage so they have a URL an image widget can
render. They are NOT gallery items, so both listings skip them — and because
they belong to one tenant, ``serve_media`` refuses a capture whose owner token
(baked into the filename by ``storage.capture_name_prefix``) does not match the
caller's active workspace. The workspace comes from an OPTIONAL user dependency:
every non-capture name keeps serving exactly as before, with or without a
session, so the studio gallery is untouched.

Updated: 2026-08-17 (studio-media-s3): list/serve/upload now go through the
storage adapter (local → on-disk mtime listing; S3 → browse + timestamp-from-key
listing, streaming reads). Direct /studio generation outputs are still excluded
from the list (the gallery renders them via the generation history).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pocketpaw_ee.cloud.auth.core import fastapi_users
from pocketpaw_ee.cloud.media import storage
from pocketpaw_ee.cloud.studio.service import tracked_generation_filenames

from pocketpaw.uploads.errors import NotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["Media"])

# Optional, so an unauthenticated read of an ordinary gallery file behaves
# exactly as it did before this route learned about captures.
_optional_user = fastapi_users.current_user(active=True, optional=True)


async def optional_workspace_id(user=Depends(_optional_user)) -> str | None:
    """The caller's active workspace, or None when there is no session."""
    return getattr(user, "active_workspace", None) if user is not None else None


_MEDIA_TYPE_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}

# Uploads keep their name but are force-named when no extension survives the
# browser's Blob naming (canvas toBlob can hand over a name-less blob).
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

# The on-disk generated dir is the SAME path the local adapter writes (and the
# agent-side media MCP still writes directly), so local-mode listings keep
# surfacing those files — only S3 mode stops seeing them (that agent path still
# writes local disk).


def _local_entries(generated: Path, sort: str, tracked: set[str]) -> list[dict]:
    """List one local generated directory (mtime-sorted, media-only, excluding
    generation-tracked files) as MediaFile dicts."""
    if not generated.exists():
        return []
    entries: list[dict] = []
    for f in sorted(generated.iterdir(), key=os.path.getmtime, reverse=(sort == "newest")):
        if not f.is_file():
            continue
        # Skip hidden /system files (e.g. .DS_Store, ._* resource forks)
        # and anything without a known media extension — only images and
        # videos belong in the gallery.
        if f.name.startswith("."):
            continue
        suffix = f.suffix.lower()
        mime = _MEDIA_TYPE_MAP.get(suffix)
        if mime is None:
            continue
        if f.name in tracked or storage.capture_owner_of(f.name):
            continue
        entries.append(
            {
                "name": f.name,
                "url": f"/api/v1/media/{f.name}",
                "mime": mime,
                "size": f.stat().st_size,
                "modified": int(f.stat().st_mtime * 1000),
            }
        )
    return entries


async def _remote_entries(sort: str, tracked: set[str]) -> list[dict]:
    """List S3 keys under the media prefix as MediaFile dicts. Remote listings
    carry no mtime, so ``modified`` comes from the timestamp baked into generated
    filenames (``<ms>-<uuid>.png``); uploads without one report 0."""
    entries: list[dict] = []
    for item in await storage.get_adapter().browse(storage.MEDIA_KEY_PREFIX + "/"):
        if item.is_dir:
            continue
        if item.name.startswith("."):
            continue
        suffix = Path(item.name).suffix.lower()
        mime = _MEDIA_TYPE_MAP.get(suffix)
        if mime is None:
            continue
        if item.name in tracked or storage.capture_owner_of(item.name):
            continue
        entries.append(
            {
                "name": item.name,
                "url": f"/api/v1/media/{item.name}",
                "mime": mime,
                "size": item.size,
                "modified": storage.modified_from_name(item.name),
            }
        )
    entries.sort(key=lambda e: e["modified"], reverse=(sort == "newest"))
    return entries


@router.get("")
async def list_media(
    sort: str = Query("newest", description="Sort order: 'newest' or 'oldest'"),
    limit: int = Query(50, description="Max items to return"),
) -> Response:
    """List all generated media files with metadata.

    Returns a JSON array sorted by modification time (newest first by default).
    Each entry has ``{name, url, mime, size, modified}``.

    Direct /studio generation outputs are excluded — the gallery already renders
    them via the generation history, so listing them here too would double the
    tiles. Agent-side generated files (media MCP) are NOT tracked and therefore
    still surface here (in local mode).
    """
    tracked = tracked_generation_filenames()
    generated = storage.local_generated_dir()
    entries = (
        _local_entries(generated, sort, tracked)
        if generated is not None
        else await _remote_entries(sort, tracked)
    )
    return Response(
        content=json.dumps({"media": entries[:limit]}),
        media_type="application/json",
    )


@router.post("")
async def upload_media(file: UploadFile = File(...)) -> Response:
    """Upload a generated file into the gallery (used by the canvas editor's
    "save edited image"). Returns the MediaFile JSON so the frontend can select
    it after re-listing.

    The filename is sanitized (basename + safe chars only, extension must be a
    known media type) and made unique so a re-save never overwrites an existing
    tile. The direct-generation tracked set does NOT include uploads, so an
    uploaded file always surfaces in the /media list."""
    raw_name = Path(file.filename or "upload.png").name
    safe_name = _SAFE_NAME_RE.sub("-", raw_name).strip("._-") or "upload"

    suffix = Path(safe_name).suffix.lower()
    if suffix not in _MEDIA_TYPE_MAP:
        supported = ", ".join(_MEDIA_TYPE_MAP)
        raise HTTPException(415, f"Unsupported media type '{suffix}'. Supported: {supported}")
    if len(safe_name) > 160:
        stem = Path(safe_name).stem[:120]
        safe_name = f"{stem}{suffix}"

    adapter = storage.get_adapter()
    name = safe_name
    while await adapter.exists(storage.media_key(name)):
        stem = Path(name).stem
        name = f"{stem}-{uuid4().hex[:8]}{suffix}"

    data = await file.read()
    mime = _MEDIA_TYPE_MAP[suffix]
    stored = await adapter.put(storage.media_key(name), storage.bytes_stream(data), mime)
    return Response(
        content=json.dumps(
            {
                "name": name,
                "url": f"/api/v1/media/{name}",
                "mime": mime,
                "size": stored.size,
                "modified": storage.modified_from_name(name),
            }
        ),
        media_type="application/json",
    )


@router.get("/{name:path}")
async def serve_media(
    name: str,
    workspace_id: str | None = Depends(optional_workspace_id),
) -> Response:
    """Serve a generated media file by name.

    Local adapter → FileResponse off disk (preserves Range requests for video).
    Remote adapter (S3) → streams the bytes back through the backend so the
    frontend's backend-relative ``/api/v1/media/<name>`` URL keeps working with
    no bucket exposure or CORS setup.
    """
    if not name or name != Path(name).name or ".." in name:
        raise HTTPException(status_code=403, detail="Path traversal denied")

    # A /browser capture belongs to the workspace whose token is in its name.
    owner = storage.capture_owner_of(name)
    if owner is not None and (
        workspace_id is None or owner != storage.capture_owner_token(workspace_id)
    ):
        raise HTTPException(status_code=404, detail="Media file not found")

    adapter = storage.get_adapter()
    key = storage.media_key(name)
    suffix = Path(name).suffix.lower()
    media_type = _MEDIA_TYPE_MAP.get(suffix, "application/octet-stream")

    local = adapter.local_path(key)
    if local is not None:
        if not local.exists() or not local.is_file():
            raise HTTPException(status_code=404, detail="Media file not found")
        return FileResponse(str(local), media_type=media_type)

    # Remote adapter — probe existence, then stream.
    if not await adapter.exists(key):
        raise HTTPException(status_code=404, detail="Media file not found")
    try:
        return StreamingResponse(adapter.open(key), media_type=media_type)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="Media file not found") from exc
