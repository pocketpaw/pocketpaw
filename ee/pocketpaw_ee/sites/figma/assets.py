# ee/pocketpaw_ee/sites/figma/assets.py: make Figma exports safe to put on a site.
#
# ``sanitize_svg`` rebuilds an SVG from an ELEMENT and ATTRIBUTE allowlist. It
# refuses a DOCTYPE or ENTITY outright (no entity expansion, no external DTD), and
# drops, with their subtree: script, foreignObject, style, animation elements
# (``set`` / ``animate`` can rewrite an href), links, and anything not on the list.
# Attributes: every ``on*`` handler goes; ``href`` / ``xlink:href`` survive only as a
# same-document ``#id`` (or, on ``<image>``, a base64 PNG / JPEG / GIF / WebP data
# URI); a ``url(...)`` anywhere (``fill``, ``filter``, ``style``...) must point at
# ``#id``; ``javascript:`` / ``data:`` / ``@import`` / ``expression(`` in any value
# drops the attribute. The output is plain serialised XML, never the input bytes.
#
# ``reencode_raster`` decodes a PNG / JPEG / WebP render after a magic-byte sniff
# and a pixel cap, and writes a fresh PNG or WebP (alpha kept, no metadata), so
# what reaches the public rail is never the bytes Figma's host sent.

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET

MAX_SVG_BYTES = 512 * 1024
MAX_RASTER_PIXELS = 25_000_000
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

_ELEMENTS = frozenset(
    {
        "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
        "defs", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern",
        "symbol", "use", "image", "title", "desc", "filter", "feFlood", "feColorMatrix",
        "feOffset", "feGaussianBlur", "feBlend", "feComposite", "feMorphology",
        "feMerge", "feMergeNode", "feDropShadow",
    }
)  # fmt: skip
_ATTRIBUTES = frozenset(
    {
        "id", "class", "width", "height", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy",
        "r", "rx", "ry", "d", "points", "viewBox", "preserveAspectRatio", "fill",
        "fill-opacity", "fill-rule", "clip-rule", "stroke", "stroke-width",
        "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray",
        "stroke-dashoffset", "stroke-opacity", "opacity", "transform", "clip-path",
        "mask", "filter", "offset", "stop-color", "stop-opacity", "gradientUnits",
        "gradientTransform", "spreadMethod", "fx", "fy", "fr", "patternUnits",
        "patternContentUnits", "patternTransform", "maskUnits", "maskContentUnits",
        "clipPathUnits", "filterUnits", "primitiveUnits", "color-interpolation-filters",
        "flood-color", "flood-opacity", "in", "in2", "result", "type", "values",
        "stdDeviation", "dx", "dy", "mode", "operator", "k1", "k2", "k3", "k4",
        "radius", "style", "href", "visibility", "display", "mix-blend-mode",
        "shape-rendering", "vector-effect", "overflow", "version",
    }
)  # fmt: skip
_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_BAD_VALUE = re.compile(r"javascript:|vbscript:|data:|@import|expression\s*\(", re.IGNORECASE)
_DATA_IMAGE = re.compile(r"data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+")
_LOCAL_ID = re.compile(r"#[A-Za-z_][\w.:-]{0,200}")


class AssetError(ValueError):
    """An export that cannot be made safe. The message is safe to show."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _safe_value(name: str, value: str, element: str) -> str | None:
    if name == "href":
        if _LOCAL_ID.fullmatch(value.strip()):
            return value.strip()
        if element == "image" and _DATA_IMAGE.fullmatch(value.strip()):
            return re.sub(r"\s+", "", value)
        return None
    for match in _URL_RE.finditer(value):
        if not _LOCAL_ID.fullmatch(match.group(2).strip()):
            return None
    if _BAD_VALUE.search(value):
        return None
    if name == "style" and ("<" in value or "\\" in value):
        return None
    return value


def _copy(src: ET.Element) -> ET.Element | None:
    name = _local(src.tag) if isinstance(src.tag, str) else ""
    if name not in _ELEMENTS:
        return None
    out = ET.Element(f"{{{SVG_NS}}}{name}")
    for raw, value in src.attrib.items():
        attr = _local(raw)
        if attr.lower().startswith("on") or attr not in _ATTRIBUTES:
            continue
        if raw.startswith("{") and not raw.startswith(f"{{{XLINK_NS}}}"):
            continue  # some other namespace's attribute
        safe = _safe_value(attr, str(value), name)
        if safe is not None:
            out.set(attr, safe)
    if name in ("title", "desc") and src.text:
        out.text = src.text[:200]
    for child in src:
        kid = _copy(child)
        if kid is not None:
            out.append(kid)
    return out


def sanitize_svg(data: bytes) -> str:
    """A rebuilt, allowlisted SVG document as text, or ``AssetError``."""
    if not data or len(data) > MAX_SVG_BYTES:
        raise AssetError("That vector export is empty or over 512 KB.")
    head = data[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise AssetError("That vector export carries a DOCTYPE and was refused.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise AssetError("That vector export is not valid SVG.") from None
    if _local(root.tag) != "svg":
        raise AssetError("That export is not an SVG.")
    clean = _copy(root)
    if clean is None:  # pragma: no cover - the root is svg
        raise AssetError("That export is not an SVG.")
    ET.register_namespace("", SVG_NS)
    return ET.tostring(clean, encoding="unicode")


def _sniff(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def reencode_raster(data: bytes, *, fmt: str = "png") -> bytes:
    """A fresh PNG or WebP of a PNG / JPEG / WebP render, or ``AssetError``."""
    from PIL import Image

    if fmt not in ("png", "webp"):
        raise AssetError("Rasters are written as png or webp.")
    if _sniff(data[:16]) is None:
        raise AssetError("That render is not a PNG, JPEG or WebP image.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > MAX_RASTER_PIXELS:
                raise AssetError("That render is too large; export it at a smaller scale.")
            image.load()
            mode = "RGBA" if image.mode in ("RGBA", "LA", "P", "PA") else "RGB"
            fresh = image.convert(mode)
    except AssetError:
        raise
    except Exception:  # noqa: BLE001 - a broken image is just refused
        raise AssetError("That render could not be decoded.") from None
    out = io.BytesIO()
    if fmt == "webp":
        fresh.save(out, format="WEBP", quality=88, method=4)
    else:
        fresh.save(out, format="PNG", optimize=True)
    return out.getvalue()
