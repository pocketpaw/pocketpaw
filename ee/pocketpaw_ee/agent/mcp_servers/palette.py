# palette.py — in-process MCP server that derives a coherent, FULL 50–900
# role-scaled color palette from a reference image URL, for the claude_agent_sdk
# cloud chat backend.
#
# Created: 2026-07-06 (feat/sites-crew-palette, SC-7). The Svelte-track
# site-authoring skill (pocketpaw-create-svelte-site) runs on the
# claude_agent_sdk backend, which only sees in-process MCP servers — a plain
# BaseTool is invisible to it (same reason media.py / stock_images.py / icons.py
# exist). A generated site needs a coherent color system the same way it needs
# real photography and iconography; the "taste lever" from our design-system
# research is that agents compose better with FULL role-scaled palettes (real
# 50–900 scales + primary/secondary/tertiary/neutral roles) than with three
# stray hexes, so the palette-derivation capability MUST be surfaced here.
#
# What this file does: clones the icons.py / stock_images.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, and the ``_error_response`` /
# ``_success_response`` helpers. Like its siblings this is a PURE READ: it
# downloads a reference image and returns colors, so it needs NO workspace/user
# identity, persists nothing, and binds no session. Tool id namespaces as
# ``mcp__pocketpaw_palette__extract_palette`` so the Claude Code allowlist
# machinery matches it.
#
# Pipeline: httpx (core dep) downloads the image under a short timeout; Pillow
# (core dep) thumbnails it small, quantizes it, and reads the dominant colors;
# those are assigned to roles (primary / secondary / tertiary / neutral) by
# saturation + hue-distinctness + frequency; then each role base color is
# expanded into a full 50–900 lightness scale by the pure, dependency-free,
# unit-testable ``scale_from_base`` helper (fixed HSL lightness stops holding
# the base hue/saturation, so the scale is deterministic and monotonic
# lightest→darkest). Fail-soft: an empty/non-http url, a download/HTTP error, or
# bytes PIL can't open all return an ``_error_response`` and never raise into the
# agent.
"""Agent-side MCP surface for reference-image palette extraction (full scales)."""

from __future__ import annotations

import colorsys
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_palette"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
EXTRACT_PALETTE_TOOL_ID = f"mcp__{SERVER_NAME}__extract_palette"

PALETTE_TOOL_IDS = (EXTRACT_PALETTE_TOOL_ID,)

# Bound the image download so a slow/hanging host can't stall a site build.
_TIMEOUT_SECONDS = 10.0

# Tests inject an ``httpx.MockTransport`` here so the image fetch is exercised
# without live network (same seam icons.py / stock_images.py expose). Production
# leaves it None (real network).
_TRANSPORT: httpx.BaseTransport | None = None

# Thumbnail edge (px) the reference image is shrunk to before quantizing —
# small enough to be fast, large enough to keep the dominant colors.
_THUMBNAIL_EDGE = 64
# How many colors to quantize the thumbnail down to before role assignment.
_QUANTIZE_COLORS = 16
# A color counts as "chromatic" (eligible for primary/secondary/tertiary) only
# above this HSL saturation; below it the color reads as a neutral/gray.
_SATURATION_MIN = 0.15
# Two chromatic roles must sit at least this many hue degrees apart to count as
# distinct — keeps secondary/tertiary from collapsing onto the primary hue.
_HUE_MIN_SEP = 25.0

# Fixed HSL lightness stops for the 50–900 scale. Strictly decreasing so the
# generated scale is guaranteed monotonic (50 lightest → 900 darkest); 500 holds
# the base hue/saturation at a mid lightness so it reads as "the base color".
_SCALE_STOPS: dict[str, float] = {
    "50": 0.95,
    "100": 0.88,
    "200": 0.77,
    "300": 0.66,
    "400": 0.56,
    "500": 0.48,
    "600": 0.40,
    "700": 0.32,
    "800": 0.23,
    "900": 0.14,
}


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: Any) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


# ---------------------------------------------------------------------------
# Pure color helpers (no deps, no I/O — unit-testable in isolation)
# ---------------------------------------------------------------------------


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse a ``#rrggbb`` (or ``rrggbb``) hex string into an (r, g, b) tuple."""
    v = value.strip().lstrip("#")
    if len(v) == 3:  # shorthand ``#abc`` → ``#aabbcc``
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6:
        raise ValueError(f"invalid hex color: {value!r}")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    """Render an (r, g, b) tuple as an uppercase ``#RRGGBB`` hex string."""
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _rgb_to_hsl(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """Convert (r, g, b) 0–255 to HSL with hue in degrees [0, 360)."""
    r, g, b = (c / 255.0 for c in rgb)
    h, lightness, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360.0, s, lightness


def _hsl_to_rgb(h: float, s: float, lightness: float) -> tuple[int, int, int]:
    """Convert HSL (hue in degrees) back to an (r, g, b) 0–255 tuple."""
    r, g, b = colorsys.hls_to_rgb((h % 360.0) / 360.0, lightness, s)
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def _hue_distance(a: float, b: float) -> float:
    """Shortest angular distance between two hues, in degrees [0, 180]."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def scale_from_base(hex_color: str) -> dict[str, str]:
    """Expand a single base hex color into a full 50–900 lightness scale.

    Pure math, no dependencies: holds the base hue and saturation constant and
    walks a fixed ladder of decreasing HSL lightness stops (``_SCALE_STOPS``), so
    the returned scale is deterministic and monotonic — ``"50"`` is the lightest
    and ``"900"`` the darkest. ``"500"`` keeps the base hue/saturation at a mid
    lightness so it reads as the base color. Unit-testable on its own (the
    acceptance criterion): every step is a valid ``#RRGGBB`` hex string.
    """
    h, s, _ = _rgb_to_hsl(_hex_to_rgb(hex_color))
    return {
        step: _rgb_to_hex(_hsl_to_rgb(h, s, lightness)) for step, lightness in _SCALE_STOPS.items()
    }


# ---------------------------------------------------------------------------
# Extraction (Pillow) — dominant colors → role base colors
# ---------------------------------------------------------------------------


def _dominant_colors(data: bytes) -> list[dict[str, Any]]:
    """Decode ``data`` with Pillow, shrink + quantize it, and return the
    dominant colors sorted most-frequent first.

    Each entry is ``{"rgb", "count", "h", "s", "l"}``. Raises if PIL can't open
    the bytes — the caller maps that to a soft ``_error_response``.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as im:
        img = im.convert("RGB")
        img.thumbnail((_THUMBNAIL_EDGE, _THUMBNAIL_EDGE))
        # Quantize to a small palette so near-identical pixels collapse into a
        # single dominant color, then read the (count, rgb) pairs back out.
        quantized = img.quantize(colors=_QUANTIZE_COLORS)
        counted = quantized.convert("RGB").getcolors(maxcolors=_THUMBNAIL_EDGE * _THUMBNAIL_EDGE)

    if not counted:
        raise ValueError("no colors could be read from the image")

    out: list[dict[str, Any]] = []
    for count, rgb in counted:
        h, s, lightness = _rgb_to_hsl(rgb)
        out.append({"rgb": rgb, "count": count, "h": h, "s": s, "l": lightness})
    out.sort(key=lambda c: c["count"], reverse=True)
    return out


def _extract_role_colors(data: bytes) -> dict[str, str]:
    """Assign the dominant colors to roles and return each role's base hex.

    Deterministic: ``primary`` is the most frequent chromatic (saturated) color,
    ``secondary`` / ``tertiary`` the next most frequent chromatic colors at a
    distinct hue (falling back to a hue-rotation of the primary when the image
    lacks distinct hues), and ``neutral`` the least-saturated dominant color.
    Always returns all four roles.
    """
    colors = _dominant_colors(data)
    chromatic = [c for c in colors if c["s"] >= _SATURATION_MIN]

    # neutral: the least-saturated dominant color (a gray/near-gray anchor).
    neutral_rgb = min(colors, key=lambda c: c["s"])["rgb"]

    roles: dict[str, tuple[int, int, int]] = {}
    picked_hues: list[float] = []

    def _hue_is_distinct(h: float) -> bool:
        return all(_hue_distance(h, ph) >= _HUE_MIN_SEP for ph in picked_hues)

    # Fill primary → secondary → tertiary from distinct-hue chromatic colors.
    for role in ("primary", "secondary", "tertiary"):
        for c in chromatic:
            if c["rgb"] in roles.values():
                continue
            if _hue_is_distinct(c["h"]):
                roles[role] = c["rgb"]
                picked_hues.append(c["h"])
                break

    # Fallbacks: if the image had no chromatic color at all, seed primary from
    # the most frequent color; then derive any missing role by hue-rotating the
    # primary so the palette is still coherent and fully populated.
    if "primary" not in roles:
        roles["primary"] = colors[0]["rgb"]
    for offset, role in ((40.0, "secondary"), (-40.0, "tertiary")):
        if role not in roles:
            h, s, lightness = _rgb_to_hsl(roles["primary"])
            roles[role] = _hsl_to_rgb(h + offset, max(s, _SATURATION_MIN), lightness)

    roles["neutral"] = neutral_rgb
    return {role: _rgb_to_hex(rgb) for role, rgb in roles.items()}


async def _fetch_image(url: str) -> bytes:
    """Download the reference image over the (mockable) transport under a short
    timeout. Raises on transport/HTTP error — the handler maps that to a soft
    ``_error_response``."""
    async with httpx.AsyncClient(transport=_TRANSPORT, timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def _extract_handler(args: dict) -> dict:
    """MCP handler for ``palette__extract_palette``.

    Pure read: downloads the reference image and derives a full role-scaled
    palette. No identity/tenant context is required (nothing is persisted).
    Returns ``{ok:true, palette:{primary:{"50":..,"900":..}, secondary, tertiary,
    neutral}}``. Fail-soft at every step — an empty/non-http url, a download/HTTP
    error, or bytes PIL can't open all return an ``_error_response`` and never
    raise into the agent.
    """
    url = args.get("image_url")
    if not isinstance(url, str) or not url.strip():
        return _error_response("extract_palette requires a non-empty `image_url`.")
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return _error_response("extract_palette requires an http(s) `image_url`.")

    try:
        data = await _fetch_image(url)
    except Exception as exc:  # noqa: BLE001 — fail soft, never raise into the agent
        logger.warning("palette: image download failed for %r", url, exc_info=True)
        return _error_response(f"palette extraction failed: image download error: {exc}")

    try:
        base_colors = _extract_role_colors(data)
    except Exception as exc:  # noqa: BLE001 — PIL open/decode failure → soft error
        logger.warning("palette: extraction failed for %r", url, exc_info=True)
        return _error_response(f"palette extraction failed: {exc}")

    palette = {role: scale_from_base(base_hex) for role, base_hex in base_colors.items()}
    return _success_response({"ok": True, "palette": palette})


def build_palette_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for palette extraction, or return
    ``None`` if the Claude Agent SDK isn't installed. Matches the ``(name,
    server)`` / ``None`` shape of ``build_icons_server`` so the backend's MCP
    registration loop treats it identically."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_palette MCP disabled")
        return None

    @tool(
        "extract_palette",
        (
            "Derive a coherent, FULL role-scaled color palette from a reference "
            "image URL. Use when building a site or page that should match a "
            "brand photo, logo, or moodboard — feed the image and get back real "
            "color scales to theme the page with. Args: `image_url` (required — "
            "an http(s) URL to the reference image, e.g. a hero photo or logo). "
            "Returns {ok, palette:{primary, secondary, tertiary, neutral}} where "
            "EACH role is a full 50–900 scale ({'50':'#..', '100':'#..', ..., "
            "'900':'#..'}) — 50 is the lightest tint, 500 the base color, 900 the "
            "darkest shade. Wire the scales straight into your design tokens "
            "(e.g. primary-500 for buttons, neutral-100 for surfaces, "
            "neutral-900 for text). An error means the image couldn't be fetched "
            "or read — proceed with a sensible default palette, do not retry "
            "blindly."
        ),
        {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "minLength": 1,
                    "description": "http(s) URL of the reference image to derive the palette from.",
                },
            },
            "required": ["image_url"],
            "additionalProperties": False,
        },
    )
    async def extract_palette_tool(args):  # type: ignore[no-untyped-def]
        return await _extract_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[extract_palette_tool],
    )
    return SERVER_NAME, server


__all__ = [
    "EXTRACT_PALETTE_TOOL_ID",
    "PALETTE_TOOL_IDS",
    "SERVER_NAME",
    "scale_from_base",
    "build_palette_server",
]
