# ee/pocketpaw_ee/sites/site_references.py — reference images stored on a site
# pocket, and the pixel comparison of a rendered draft against one.
#
# A reference is a design the owner supplied (a screenshot or mockup today, a Figma
# frame render later) that the /sites agent builds to. ``save_reference`` validates
# the bytes, re-encodes them and stores them on the PRIVATE upload adapter;
# ``compare`` scales the reference to the draft's render width and returns
# side-by-side tiles, a diff heatmap, the biggest mismatch regions and a per-tile
# score. ``sites_reference.py`` (the MCP tools) is the only caller today; the Figma
# screenshot tool is meant to reuse ``save_reference``.
#
# Invariants a reader must not break:
#   * bytes are decoded only after a magic-byte sniff (PNG, JPEG, WebP, GIF) and
#     a size and pixel cap; the stored file is always a fresh PNG re-encode
#     (first frame, RGB, no metadata), never the bytes that arrived;
#   * keys are ``sites-references/{workspace}/{pocket}/{ref_id}.png``, with every
#     part checked against a strict pattern, so a key never escapes its tenant;
#   * reference ids are content-addressed (``ref_`` + sha256 prefix of the
#     re-encoded PNG), so saving the same image twice is one reference;
#   * this module never fetches a URL: the caller hands it bytes;
#   * Pillow only. No numpy, no pixel-diff package.

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Bytes accepted before decoding (the public asset rail's image cap).
MAX_REFERENCE_BYTES = 10 * 1024 * 1024
#: Decoded pixel cap. A 2x full-page capture of a long page is ~2880 x 12000.
MAX_REFERENCE_PIXELS = 40_000_000
MAX_REFERENCE_EDGE = 20_000
MIN_REFERENCE_WIDTH = 200
#: References kept per pocket. A design is a handful of frames, not an archive.
MAX_REFERENCES_PER_POCKET = 20

_KEY_PREFIX = "sites-references"
_ID_RE = re.compile(r"ref_[0-9a-f]{16}")
_PART_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: Render widths. ``desktop`` and ``mobile`` match ``preview_site``.
VIEWPORTS = {"desktop": (1280, 800), "mobile": (390, 844)}
MIN_WIDTH, MAX_WIDTH = 320, 1920
#: Tiles compared per call, and their height as a multiple of the width.
MAX_TILES = 3
_TILE_RATIO_DESKTOP = 1.25
_TILE_RATIO_MOBILE = 2.0
#: A grid cell whose mean blurred difference is above this (0-255) is a mismatch.
_CELL_THRESHOLD = 24
_TOP_REGIONS = 5
#: Base64 budget for one tool result, under the SDK's 1 MB message buffer.
RESULT_BUDGET = 700_000
_MAX_EDGE = 1568


class SiteReferenceError(Exception):
    """A reference was refused or could not be read. The message is safe to show."""


@dataclass(frozen=True)
class StoredReference:
    reference_id: str
    width: int
    height: int
    viewport: str  # the width class it most likely shows: "desktop" or "mobile"


@dataclass
class Comparison:
    width: int
    reference_height: int
    build_height: int
    compared_height: int
    match: int  # percent of grid cells under the mismatch threshold
    tile_scores: list[int] = field(default_factory=list)
    regions: list[dict[str, int]] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)  # JPEG: side-by-sides, then heatmap
    dropped_tiles: int = 0


# --- storage -----------------------------------------------------------------


def _key(workspace_id: str, pocket_id: str, reference_id: str) -> str:
    if not (_PART_RE.fullmatch(workspace_id) and _PART_RE.fullmatch(pocket_id)):
        raise SiteReferenceError("that pocket id is not valid.")
    if not _ID_RE.fullmatch(reference_id):
        raise SiteReferenceError(
            f"`reference_id` looks like `ref_` and 16 hex characters; got {reference_id!r}."
        )
    return f"{_KEY_PREFIX}/{workspace_id}/{pocket_id}/{reference_id}.png"


def _adapter() -> Any:
    """The PRIVATE upload adapter. A reference is agent input, never published."""
    from pocketpaw.uploads.factory import build_adapter

    root = Path.home() / ".pocketpaw" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    return build_adapter(root)


def _sniff(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def normalize(data: bytes) -> tuple[bytes, int, int]:
    """Validate an image and re-encode it as a clean PNG: ``(png, width, height)``.

    Raises :class:`SiteReferenceError` for anything that is not a raster image inside
    the size and pixel caps. The dimensions are read from the header before the
    pixels are decoded, so an oversized image is refused without being expanded.
    """
    from PIL import Image

    if not data:
        raise SiteReferenceError("the image is empty.")
    if len(data) > MAX_REFERENCE_BYTES:
        raise SiteReferenceError(
            f"the image is {len(data) // (1024 * 1024)} MB; references are capped at "
            f"{MAX_REFERENCE_BYTES // (1024 * 1024)} MB."
        )
    if _sniff(data[:16]) is None:
        raise SiteReferenceError("a reference must be a PNG, JPEG, WebP or GIF image.")
    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
    except Exception as exc:  # noqa: BLE001 — any header failure is "not an image"
        raise SiteReferenceError("could not read the image.") from exc
    if width * height > MAX_REFERENCE_PIXELS or max(width, height) > MAX_REFERENCE_EDGE:
        raise SiteReferenceError(
            f"the image is {width}x{height}; references are capped at "
            f"{MAX_REFERENCE_PIXELS // 1_000_000} megapixels and {MAX_REFERENCE_EDGE}px a side."
        )
    if width < MIN_REFERENCE_WIDTH:
        raise SiteReferenceError(
            f"the image is {width}px wide; a page reference needs at least {MIN_REFERENCE_WIDTH}px."
        )
    try:
        image.seek(0)
        image.load()
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            image = flat
        else:
            image = image.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise SiteReferenceError("could not decode the image.") from exc
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), width, height


#: Pixel widths of common phone screenshots at 2x and 3x. A desktop capture at 1x
#: is 1280, 1366, 1440 or 1920 wide, so none of these collide with one.
_PHONE_CAPTURE_WIDTHS = frozenset(
    {750, 780, 828, 1080, 1125, 1170, 1179, 1206, 1242, 1284, 1290, 1320}
)


def guess_viewport(width: int, height: int) -> str:
    """The width class a screenshot most likely shows: ``mobile`` for a 1x phone
    width, or a tall image at a known 2x/3x phone width; ``desktop`` otherwise."""
    if width <= 500:
        return "mobile"
    if width in _PHONE_CAPTURE_WIDTHS and height >= width * 1.6:
        return "mobile"
    return "desktop"


async def save_reference(
    data: bytes, *, workspace_id: str, pocket_id: str, adapter: Any | None = None
) -> StoredReference:
    """Validate, re-encode and store one reference on the pocket."""
    png, width, height = normalize(data)
    reference_id = "ref_" + hashlib.sha256(png).hexdigest()[:16]
    key = _key(workspace_id, pocket_id, reference_id)
    adapter = adapter or _adapter()
    if not await adapter.exists(key):
        existing = await adapter.list_prefix(f"{_KEY_PREFIX}/{workspace_id}/{pocket_id}/")
        if len(existing) >= MAX_REFERENCES_PER_POCKET:
            raise SiteReferenceError(
                f"this site already has {MAX_REFERENCES_PER_POCKET} references; reuse one "
                "of them rather than adding more."
            )

        async def _body() -> AsyncIterator[bytes]:
            yield png

        await adapter.put(key, _body(), "image/png")
    return StoredReference(reference_id, width, height, guess_viewport(width, height))


async def load_reference(
    *, workspace_id: str, pocket_id: str, reference_id: str, adapter: Any | None = None
) -> bytes:
    """Read a stored reference back. Raises :class:`SiteReferenceError` when missing."""
    from pocketpaw.uploads.errors import NotFound

    key = _key(workspace_id, pocket_id, reference_id)
    adapter = adapter or _adapter()
    buf = bytearray()
    try:
        async for chunk in adapter.open(key):
            buf.extend(chunk)
            if len(buf) > MAX_REFERENCE_BYTES * 4:
                raise SiteReferenceError("the stored reference is unexpectedly large.")
    except NotFound as exc:
        raise SiteReferenceError(
            f"no reference {reference_id} on this site. Save it first with save_site_reference."
        ) from exc
    if not buf:
        raise SiteReferenceError(f"no reference {reference_id} on this site.")
    return bytes(buf)


# --- comparison ----------------------------------------------------------------


def resolve_width(viewport: Any, ref_width: int, ref_height: int) -> tuple[int, int, str]:
    """``(width, height, label)`` to render the draft at. ``viewport`` is
    ``"desktop"``, ``"mobile"``, a CSS width in pixels, or ``None`` (guess from the
    reference). Raises :class:`SiteReferenceError` on anything else."""
    if viewport is None or viewport == "":
        viewport = guess_viewport(ref_width, ref_height)
    if isinstance(viewport, str) and viewport in VIEWPORTS:
        w, h = VIEWPORTS[viewport]
        return w, h, viewport
    if isinstance(viewport, bool) or not isinstance(viewport, int | float):
        raise SiteReferenceError(
            f"`viewport` is `desktop`, `mobile` or a width in px ({MIN_WIDTH}-{MAX_WIDTH}); "
            f"got {viewport!r}."
        )
    w = int(viewport)
    if not MIN_WIDTH <= w <= MAX_WIDTH:
        raise SiteReferenceError(f"`viewport` width must be {MIN_WIDTH}-{MAX_WIDTH}px; got {w}.")
    return w, (844 if w < 600 else 800), f"{w}px"


def _rgb(data: bytes) -> Any:
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001
        raise SiteReferenceError("could not decode an image to compare.") from exc
    return image.convert("RGB")


def _to_width(image: Any, width: int) -> Any:
    from PIL import Image

    if image.width == width:
        return image
    height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _regions(cells: list[list[int]], cell: int, width: int, height: int) -> list[dict[str, int]]:
    """Group mismatched cells into 4-connected regions, biggest difference first."""
    rows, cols = len(cells), len(cells[0]) if cells else 0
    seen = [[False] * cols for _ in range(rows)]
    found: list[tuple[int, dict[str, int]]] = []
    for r in range(rows):
        for c in range(cols):
            if seen[r][c] or cells[r][c] <= _CELL_THRESHOLD:
                continue
            stack, members = [(r, c)], []
            seen[r][c] = True
            while stack:
                y, x = stack.pop()
                members.append((y, x))
                for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                    if (
                        0 <= ny < rows
                        and 0 <= nx < cols
                        and not seen[ny][nx]
                        and cells[ny][nx] > _CELL_THRESHOLD
                    ):
                        seen[ny][nx] = True
                        stack.append((ny, nx))
            weight = sum(cells[y][x] for y, x in members)
            ys = [y for y, _ in members]
            xs = [x for _, x in members]
            x0, y0 = min(xs) * cell, min(ys) * cell
            x1 = min(width, (max(xs) + 1) * cell)
            y1 = min(height, (max(ys) + 1) * cell)
            mean = weight / len(members)
            found.append(
                (
                    weight,
                    {
                        "x": x0,
                        "y": y0,
                        "w": x1 - x0,
                        "h": y1 - y0,
                        "score": round(100 * (1 - mean / 255)),
                    },
                )
            )
    found.sort(key=lambda item: item[0], reverse=True)
    return [region for _, region in found[:_TOP_REGIONS]]


def _jpeg(image: Any, quality: int) -> bytes:
    from PIL import Image

    if max(image.size) > _MAX_EDGE:
        image = image.copy()
        image.thumbnail((_MAX_EDGE, _MAX_EDGE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _b64_len(data: bytes) -> int:
    return 4 * ((len(data) + 2) // 3)


def compare(reference_png: bytes, build_png: bytes, *, width: int, mobile: bool) -> Comparison:
    """Compare a draft render against a reference at one CSS ``width``.

    Both images are scaled to ``width`` and cut to the shorter of the two, capped at
    ``MAX_TILES`` tiles. The difference is taken on lightly blurred copies so
    anti-aliasing does not register, then averaged over a grid of square cells.
    Returns the match (share of cells under the threshold), a score per tile, the
    top mismatch regions in CSS pixels, and JPEG images: one reference | build
    side-by-side per tile, then the heatmap. Images that would push the result past
    ``RESULT_BUDGET`` are dropped from the bottom up (the heatmap always survives).
    """
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    ref = _to_width(_rgb(reference_png), width)
    build = _to_width(_rgb(build_png), width)
    tile_h = max(1, round(width * (_TILE_RATIO_MOBILE if mobile else _TILE_RATIO_DESKTOP)))
    height = min(ref.height, build.height, tile_h * MAX_TILES)
    ref_c = ref.crop((0, 0, width, height))
    build_c = build.crop((0, 0, width, height))

    blur = ImageFilter.GaussianBlur(1.5)
    diff = ImageChops.difference(ref_c.filter(blur), build_c.filter(blur)).convert("L")

    cell = max(16, width // 16)
    cols, rows = -(-width // cell), -(-height // cell)
    grid = diff.resize((cols, rows), Image.Resampling.BOX)
    values = list(grid.tobytes())  # mode "L": one byte per cell
    cells = [values[r * cols : (r + 1) * cols] for r in range(rows)]
    flat = [v for row in cells for v in row]
    match = round(100 * sum(1 for v in flat if v <= _CELL_THRESHOLD) / max(1, len(flat)))

    tiles = -(-height // tile_h)
    tile_scores: list[int] = []
    for t in range(tiles):
        top, bottom = t * tile_h, min(height, (t + 1) * tile_h)
        band = [v for row in cells[top // cell : -(-bottom // cell)] for v in row]
        ok = sum(1 for v in band if v <= _CELL_THRESHOLD)
        tile_scores.append(round(100 * ok / max(1, len(band))))

    regions = _regions(cells, cell, width, height)

    # Heatmap: the reference washed out, red where the pixels differ, numbered boxes
    # around the top regions so the text and the picture name the same places.
    base = Image.blend(
        ref_c.convert("L").convert("RGB"), Image.new("RGB", ref_c.size, "white"), 0.55
    )
    mask = diff.point(lambda v: min(255, v * 4))
    heat = Image.composite(Image.new("RGB", ref_c.size, (220, 30, 30)), base, mask)
    draw = ImageDraw.Draw(heat)
    for n, region in enumerate(regions, start=1):
        box = (
            region["x"],
            region["y"],
            region["x"] + region["w"] - 1,
            region["y"] + region["h"] - 1,
        )
        draw.rectangle(box, outline=(20, 20, 160), width=max(2, width // 400))
        draw.text((region["x"] + 4, region["y"] + 4), str(n), fill=(20, 20, 160))

    gap = max(8, width // 80)
    sides: list[Any] = []
    for t in range(tiles):
        top, bottom = t * tile_h, min(height, (t + 1) * tile_h)
        pair = Image.new("RGB", (width * 2 + gap, bottom - top), (128, 128, 128))
        pair.paste(ref_c.crop((0, top, width, bottom)), (0, 0))
        pair.paste(build_c.crop((0, top, width, bottom)), (width + gap, 0))
        label = ImageDraw.Draw(pair)
        label.text((6, 6), "REFERENCE", fill=(220, 30, 30))
        label.text((width + gap + 6, 6), "BUILD", fill=(220, 30, 30))
        sides.append(pair)

    heat_bytes = _jpeg(heat, 72)
    if _b64_len(heat_bytes) > RESULT_BUDGET // 2:
        heat_bytes = _jpeg(heat, 55)
    budget = RESULT_BUDGET - _b64_len(heat_bytes)
    images: list[bytes] = []
    for pair in sides:
        encoded = _jpeg(pair, 72)
        if _b64_len(encoded) > budget:
            encoded = _jpeg(pair, 55)
        if _b64_len(encoded) > budget:
            break
        images.append(encoded)
        budget -= _b64_len(encoded)
    dropped = len(sides) - len(images)
    images.append(heat_bytes)

    return Comparison(
        width=width,
        reference_height=ref.height,
        build_height=build.height,
        compared_height=height,
        match=match,
        tile_scores=tile_scores,
        regions=regions,
        images=images,
        dropped_tiles=dropped,
    )


__all__ = [
    "MAX_REFERENCES_PER_POCKET",
    "MAX_REFERENCE_BYTES",
    "MAX_REFERENCE_PIXELS",
    "Comparison",
    "SiteReferenceError",
    "StoredReference",
    "compare",
    "guess_viewport",
    "load_reference",
    "normalize",
    "resolve_width",
    "save_reference",
]
