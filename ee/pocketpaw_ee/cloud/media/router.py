"""EE media router — serve + list generated media (images, videos).

Generated assets from the /studio surface live in ``~/.pocketpaw/generated/``.
This router serves them over HTTP so the frontend can render ``<img>`` and
``<video>`` tags, and provides a list endpoint for the gallery grid.

Endpoints:
  GET /api/v1/media          — list all generated media files
  POST /api/v1/media         — upload a generated file (canvas "save edited image")
  GET /api/v1/media/{name}   — serve a single media file

Updated: 2026-08-17 (studio-real-backend): added POST upload + excluded the
direct /studio generation outputs from the list (the gallery renders them via
the generation history; without the exclusion each generation would also appear
here as a bare file and the grid would show it twice).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pocketpaw_ee.cloud.studio.service import tracked_generation_filenames

from pocketpaw.config import get_config_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["Media"])


def _generated_dir() -> Path:
    """Get the generated-media directory (same as image_gen.py / media MCP)."""
    d = get_config_dir() / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


_MEDIA_TYPE_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}

# Extension suffix → the frontend's generated-file naming convention. Uploads
# keep their name but are force-named *.png when no extension survives the
# browser's Blob naming (canvas toBlob can hand over a name-less blob).
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


@router.get("")
async def list_media(
    sort: str = Query("newest", description="Sort order: 'newest' or 'oldest'"),
    limit: int = Query(50, description="Max items to return"),
) -> Response:
    """List all generated media files with metadata.

    Returns a JSON array sorted by modification time (newest first by default).
    Each entry has ``{name, url, mime, size, modified}``.
    """
    generated = _generated_dir()
    if not generated.exists():
        return Response(
            content=json.dumps({"media": []}),
            media_type="application/json",
        )

    entries: list[dict] = []
    # Direct /studio generation outputs are excluded from the file list — the
    # gallery already renders them via the generation history, so listing them
    # here too would double the tiles. Agent-side generated files (media MCP)
    # are NOT tracked and therefore still surface here.
    tracked = tracked_generation_filenames()
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
        if f.name in tracked:
            continue
        entries.append(
            {
                "name": f.name,
                "url": f"/api/v1/media/{f.name}",
                "mime": mime,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            }
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
    generated = _generated_dir()
    raw_name = Path(file.filename or "upload.png").name
    safe_name = _SAFE_NAME_RE.sub("-", raw_name).strip("._-") or "upload"

    suffix = Path(safe_name).suffix.lower()
    if suffix not in _MEDIA_TYPE_MAP:
        supported = ", ".join(_MEDIA_TYPE_MAP)
        raise HTTPException(415, f"Unsupported media type '{suffix}'. Supported: {supported}")
    if len(safe_name) > 160:
        stem = Path(safe_name).stem[:120]
        safe_name = f"{stem}{suffix}"

    name = safe_name
    while (generated / name).exists():
        stem = Path(name).stem
        name = f"{stem}-{uuid4().hex[:8]}{suffix}"

    dest = generated / name
    dest.write_bytes(await file.read())
    st = dest.stat()
    return Response(
        content=json.dumps(
            {
                "name": name,
                "url": f"/api/v1/media/{name}",
                "mime": _MEDIA_TYPE_MAP[suffix],
                "size": st.st_size,
                "modified": st.st_mtime,
            }
        ),
        media_type="application/json",
    )


@router.get("/{name:path}")
async def serve_media(name: str) -> Response:
    """Serve a generated media file by name.

    Resolves inside the generated directory to prevent path traversal.
    """
    generated = _generated_dir()
    resolved = (generated / name).resolve()
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    if not str(resolved).startswith(str(generated.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal denied")

    suffix = resolved.suffix.lower()
    media_type = _MEDIA_TYPE_MAP.get(suffix, "application/octet-stream")
    return FileResponse(str(resolved), media_type=media_type)
