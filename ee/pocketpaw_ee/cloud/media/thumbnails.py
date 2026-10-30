"""EE media thumbnails — server-side downscale cache for gallery tiles.

The /studio gallery grid asks for block-size thumbnails (``GET /api/v1/media/<name>?w=480``)
so tiles never download the full generated PNG. This module resizes raster images
with Pillow and caches the result on backend-local disk
(``~/.pocketpaw/thumbnails/<width>/<name>``) so a repeat request is a plain
FileResponse — no re-decode, no re-upload, no S3 GET.

Generated media filenames are immutable (``<ms>-<uuid>.png``; uploads get a uuid
suffix on collision), so a thumbnail computed once is valid forever — there is no
staleness problem to solve. Cache misses that fail to resize fall back to serving
the original full file (the caller's job), never an error.

Created 2026-08-21 (studio-pagination-thumbs).
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

logger = logging.getLogger(__name__)

# Backend-local thumbnail cache root (both local + S3 adapters write here).
THUMB_ROOT = Path.home() / ".pocketpaw" / "thumbnails"

# Raster formats Pillow can downscale. GIF is excluded (animated) and videos
# simply don't apply — those tiles keep their full URL.
_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

_SAVE_FORMAT = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
}


def local_thumb_path(w: int, name: str) -> Path:
    """Absolute local path for a ``w``-sized thumbnail of ``name`` (exists() when
    already built)."""
    return THUMB_ROOT / str(w) / name


def is_resizable(suffix: str) -> bool:
    """True when Pillow can downscale this media type (raster images only)."""
    return suffix.lower() in _RASTER_SUFFIXES


async def read_all(stream: AsyncIterator[bytes]) -> bytes:
    """Drain an async byte stream into one byte string (for resize input)."""
    return b"".join([chunk async for chunk in stream])


def resize(data: bytes, width: int, suffix: str) -> bytes | None:
    """Downscale an image to fit within ``width``x``width`` (aspect-preserving).

    Returns the re-encoded bytes in the source's raster format, or None when
    Pillow is unavailable / the data isn't decodable — callers then fall back to
    the original file. ``thumbnail()`` never upscales, so a request larger than
    the source costs nothing extra.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow unavailable — serving full-size media")
        return None
    out_format = _SAVE_FORMAT.get(suffix.lower(), "PNG")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.thumbnail((width, width))
            # JPEG can't store alpha / palette modes — flatten before saving.
            if out_format == "JPEG" and img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            save_kwargs: dict[str, object] = {"format": out_format}
            if out_format in ("JPEG", "WEBP"):
                save_kwargs["quality"] = 82
            if out_format == "PNG":
                save_kwargs["optimize"] = True
            img.save(buf, **save_kwargs)
            return buf.getvalue()
    except Exception:  # noqa: BLE001 — a bad tile must never 500 the gallery
        logger.warning(
            "thumbnail resize failed for w=%d suffix=%s (serving original)",
            width,
            suffix,
            exc_info=True,
        )
        return None


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a same-dir temp + ``os.replace`` so a
    concurrent build never leaves a half-written thumbnail (a repeat request may
    race the first one and both read the file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


__all__ = [
    "THUMB_ROOT",
    "local_thumb_path",
    "is_resizable",
    "read_all",
    "resize",
    "write_atomic",
]
