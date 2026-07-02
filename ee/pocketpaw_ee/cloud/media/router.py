"""EE media router — serve + list generated media (images, videos).

Generated assets from the /studio surface live in ``~/.pocketpaw/generated/``.
This router serves them over HTTP so the frontend can render ``<img>`` and
``<video>`` tags, and provides a list endpoint for the gallery grid.

Endpoints:
  GET /api/v1/media          — list all generated media files
  GET /api/v1/media/{name}   — serve a single media file

Created: 2026-06-16  (moved from OSS core to EE)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

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
