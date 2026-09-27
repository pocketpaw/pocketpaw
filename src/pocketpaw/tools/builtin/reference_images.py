# Reference images — turn a screenshot (a design reference, or a render of the
# agent's own draft) into image blocks a vision model can actually read.
#
# Created: 2026-09-27 (feat/sites-visual-research). Site research used to hand
# the agent TEXT only: a Refero style write-up, an Inspo DESIGN.md, and image
# URLs it had no way to open. Reproducing a rejected waitlist page by hand showed
# the fix was looking at the pictures — the shared pattern across the good
# references (light page, calm headline, a large product visual in a gradient
# panel) never appeared in any text description. The same gap closed the loop on
# the draft: nothing let the agent see its own page before handing it over.
#
# WHY TILES: a full-page screenshot is ~1280 x 6000-12000px. The model API caps
# an image's long edge and quietly downsamples anything past ~1568px, so one tall
# image arrives as an unreadable sliver. Cutting it into a few near-landscape
# tiles keeps every section legible at a bounded, predictable token cost.
#
# FETCHING: only from hosts the caller names. These URLs come back from third-
# party archives, and a helper that fetched any URL it was handed would be an
# SSRF primitive on the API host.

from __future__ import annotations

import base64
import io
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Tests inject an ``httpx.MockTransport`` here.
_TRANSPORT: httpx.BaseTransport | None = None
_TIMEOUT_SECONDS = 20.0

# Bytes accepted off the wire before decoding. A full-page capture is a few MB
# at most; anything bigger is not a screenshot.
_MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024

# The model reads an image's long edge at ~1568px; wider only costs tokens.
_MAX_EDGE = 1568
# Screenshots are normalised to this width before tiling, so text keeps roughly
# the size it has in a desktop browser.
_BASE_WIDTH = 1280


class ReferenceImageError(RuntimeError):
    """The image could not be fetched or decoded."""


def host_allowed(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    """HTTPS, and a host equal to or under one of ``allowed_hosts``."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


async def fetch_image(url: str, allowed_hosts: tuple[str, ...]) -> bytes:
    """Download an image from an allow-listed host. Raises ``ReferenceImageError``."""
    if not host_allowed(url, allowed_hosts):
        raise ReferenceImageError(f"refusing to fetch an image from {urlparse(url).hostname!r}")
    kwargs: dict[str, Any] = {"timeout": _TIMEOUT_SECONDS, "follow_redirects": False}
    if _TRANSPORT is not None:
        kwargs["transport"] = _TRANSPORT
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ReferenceImageError(f"image fetch failed: {exc}") from exc
    if response.status_code != 200:
        raise ReferenceImageError(f"image fetch returned HTTP {response.status_code}")
    if not response.headers.get("content-type", "").startswith("image/"):
        raise ReferenceImageError("the URL did not return an image")
    if len(response.content) > _MAX_DOWNLOAD_BYTES:
        raise ReferenceImageError("the image is too large to read")
    return response.content


def to_tiles(data: bytes, *, max_tiles: int = 4, tile_ratio: float = 1.25) -> list[bytes]:
    """Normalise a screenshot and cut it into at most ``max_tiles`` JPEG tiles.

    The image is scaled to at most ``_BASE_WIDTH`` wide, then cut top to bottom
    into tiles ``tile_ratio`` times as tall as they are wide (a mobile capture
    passes a larger ratio). A page taller than ``max_tiles`` tiles keeps its top
    ``max_tiles`` — the fold and the first sections are where design decisions
    live. Each tile is then capped at ``_MAX_EDGE`` on its long edge.
    Raises ``ReferenceImageError`` when the bytes are not a readable image.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pillow is a core dependency; this is belt and braces
        raise ReferenceImageError("image support is not installed") from exc

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 — any decode failure is "not an image"
        raise ReferenceImageError("could not decode the image") from exc

    image = image.convert("RGB")
    if image.width > _BASE_WIDTH:
        height = round(image.height * _BASE_WIDTH / image.width)
        image = image.resize((_BASE_WIDTH, height), Image.Resampling.LANCZOS)

    tile_height = max(1, round(image.width * tile_ratio))
    tiles: list[bytes] = []
    top = 0
    while top < image.height and len(tiles) < max(1, max_tiles):
        tile = image.crop((0, top, image.width, min(image.height, top + tile_height)))
        if max(tile.size) > _MAX_EDGE:
            tile.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        tile.save(buffer, format="JPEG", quality=82, optimize=True)
        tiles.append(buffer.getvalue())
        top += tile_height
    return tiles


def image_blocks(tiles: list[bytes]) -> list[dict[str, Any]]:
    """MCP ``image`` content blocks for a list of JPEG tiles."""
    return [
        {"type": "image", "data": base64.b64encode(t).decode("ascii"), "mimeType": "image/jpeg"}
        for t in tiles
    ]


__all__ = [
    "ReferenceImageError",
    "fetch_image",
    "host_allowed",
    "image_blocks",
    "to_tiles",
]
