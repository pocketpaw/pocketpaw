# ee/pocketpaw_ee/cloud/studio/light_rig.py — manual three-point lighting.
#
# Created 2026-09-03 (studio-light-rig).
#
# The Lighting tab's pick-lists cover named setups ("Rembrandt", "high key").
# This module covers the case they can't: a lamp placed exactly where the user
# wants it. The dialog is a stage with the subject at the centre and three
# draggable lamps around it; this file turns that geometry into the sentence a
# model can act on.
#
# The whole problem is quantization. A model cannot read `x: 0.42, y: -0.71`; it
# reads "from the upper right". So every continuous control here is bucketed into
# words a gaffer would actually say, and the buckets are chosen so that a small
# drag does NOT flip the description — the dead zones matter more than the
# precision does.
#
# Three derived facts do most of the work, and none of them is a control the user
# sets directly:
#
#   * WHERE a lamp sits comes from its angle AND its distance from the subject.
#     Near the centre reads as frontal and flat; out at the edge reads as raking
#     or near-backlight. Same angle, completely different picture.
#   * CONTRAST is the key-to-fill ratio, not a slider. It is the single fact that
#     decides whether an image reads as gentle or dramatic, and a user who drags
#     the fill down to 10% has asked for deep shadows whether or not they'd have
#     thought to say so.
#   * An unfilled key is called out explicitly, because "no fill lamp" and "fill
#     lamp at zero" are the same picture and the model needs to be told the
#     shadow side goes dark.

from __future__ import annotations

import math
from typing import Any

# ── Geometry → words ────────────────────────────────────────────────────────
#
# The stage is normalised to [-1, 1] on both axes, subject at the origin,
# +x right and +y UP (the client flips its screen-space y before sending, so the
# maths here reads the way a person would describe it).

# 8-way compass, each entry (upper_bound_degrees, phrase). Angles are measured
# counter-clockwise from screen right. Bands are 45 degrees wide so a lamp has to
# be dragged a real distance before its description changes.
_DIRECTIONS: list[tuple[float, str]] = [
    (22.5, "from the right"),
    (67.5, "from the upper right"),
    (112.5, "from directly above"),
    (157.5, "from the upper left"),
    (202.5, "from the left"),
    (247.5, "from below left"),
    (292.5, "from below"),
    (337.5, "from below right"),
    (360.0, "from the right"),
]


def _direction(x: float, y: float) -> str:
    """Name the compass point a lamp sits on. A lamp parked on the subject has no
    meaningful direction, so it reads as frontal instead of picking one at
    random from a near-zero vector."""
    if math.hypot(x, y) < 0.12:
        return "head-on from the camera position"
    angle = math.degrees(math.atan2(y, x)) % 360.0
    for upper, phrase in _DIRECTIONS:
        if angle < upper:
            return phrase
    return "from the right"


def _throw(x: float, y: float) -> str:
    """How far around the subject the lamp has been pushed.

    Distance is what separates a flat frontal key from a raking edge light at the
    same angle, so it earns its own clause rather than being folded into the
    direction name."""
    r = math.hypot(x, y)
    if r < 0.35:
        return "close to the lens axis, lighting the subject almost flat"
    if r > 0.85:
        return "raking in from the very edge of the scene"
    if r > 0.65:
        return "well off to the side"
    return ""


_STRENGTHS: list[tuple[int, str]] = [
    (85, "dominant"),
    (60, "strong"),
    (35, "moderate"),
    (15, "gentle"),
    (0, "barely perceptible"),
]

_WARMTHS: list[tuple[int, str]] = [
    (80, "very warm tungsten-coloured"),
    (60, "warm"),
    (40, "neutral-white"),
    (20, "cool"),
    (0, "cold blue-toned"),
]


def _bucket(table: list[tuple[int, str]], value: int) -> str:
    for threshold, phrase in table:
        if value >= threshold:
            return phrase
    return table[-1][1]


# ── Colour naming ───────────────────────────────────────────────────────────


def _hue_name(hue: float) -> str:
    """Name a hue in the vocabulary a lighting person uses — "amber", not
    "#FFA500". Bands are deliberately uneven: the warm end of the spectrum is
    where practical lighting actually lives, so it gets finer resolution."""
    bands: list[tuple[float, str]] = [
        (12, "red"),
        (35, "orange"),
        (50, "amber"),
        (68, "yellow"),
        (165, "green"),
        (200, "teal"),
        (250, "blue"),
        (268, "indigo"),
        (310, "violet"),
        (340, "magenta"),
        (360, "red"),
    ]
    for upper, name in bands:
        if hue < upper:
            return name
    return "red"


def colour_name(hex_colour: str | None) -> str:
    """Turn a hex swatch into a describable colour, or "" when it is close enough
    to white that naming it would only add noise to the prompt."""
    if not hex_colour:
        return ""
    value = hex_colour.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        return ""
    try:
        r, g, b = (int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return ""

    high, low = max(r, g, b), min(r, g, b)
    delta = high - low
    # Near-grey: a "white" lamp is the default, and saying so wastes tokens.
    if delta < 0.08:
        return ""

    if high == r:
        hue = (60 * ((g - b) / delta)) % 360
    elif high == g:
        hue = 60 * (((b - r) / delta) + 2)
    else:
        hue = 60 * (((r - g) / delta) + 4)

    name = _hue_name(hue)
    saturation = delta / high if high else 0.0
    if saturation > 0.75:
        return f"saturated {name}"
    if saturation < 0.3:
        return f"faintly {name}"
    return name


# ── Lamp → clause ───────────────────────────────────────────────────────────


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _lamp_clause(lamp: Any, role: str) -> str:
    """One lamp as a noun phrase: "a soft warm key from the upper left"."""
    if not _field(lamp, "enabled", False):
        return ""
    strength = int(_field(lamp, "strength", 70))
    if strength <= 0:
        return ""

    parts = [
        _bucket(_STRENGTHS, strength),
        "soft" if _field(lamp, "quality", "soft") == "soft" else "hard-edged",
    ]
    # An explicit colour REPLACES the warmth word rather than joining it. Warmth
    # is the white-balance axis and only describes a lamp nobody has gelled; run
    # them together and you get "cold blue-toned saturated magenta", which asks
    # for two different colours in one breath.
    tint = colour_name(_field(lamp, "colour"))
    parts.append(tint or _bucket(_WARMTHS, int(_field(lamp, "warmth", 50))))

    x = float(_field(lamp, "x", 0.0))
    y = float(_field(lamp, "y", 0.0))
    clause = f"a {' '.join(parts)} {role} {_direction(x, y)}"
    throw = _throw(x, y)
    return f"{clause}, {throw}" if throw else clause


# ── Derived facts ───────────────────────────────────────────────────────────


def _contrast_clause(key: Any, fill: Any) -> str:
    """Describe the key-to-fill ratio, which is what actually decides whether the
    picture reads gentle or dramatic. Users adjust two sliders; the thing they
    mean by it is this one sentence."""
    key_on = _field(key, "enabled", False) and int(_field(key, "strength", 0)) > 0
    if not key_on:
        return ""
    key_strength = int(_field(key, "strength", 0))
    fill_on = _field(fill, "enabled", False) and int(_field(fill, "strength", 0)) > 0
    if not fill_on:
        return "unfilled, so the shadow side falls away into darkness"

    ratio = key_strength / max(int(_field(fill, "strength", 1)), 1)
    if ratio >= 4:
        return "very high contrast, the shadow side dropping close to black"
    if ratio >= 2.5:
        return "high contrast with deep, defined shadows"
    if ratio >= 1.5:
        return "moderate contrast, shadows present but open"
    return "low contrast, the subject evenly filled"


_AMBIENCE: dict[str, str] = {
    "day": "bright daylight ambience opening up the shadows",
    "dusk": "dim cool dusk ambience",
    "night": "near-black night ambience with almost no ambient fill",
}


def compose_light_rig_phrase(rig: Any) -> str:
    """Render a light rig into lighting direction. "" when the rig is off or has
    no lamp doing anything — the same Auto-means-silence rule the pick-lists
    follow."""
    if rig is None or not _field(rig, "enabled", False):
        return ""

    key = _field(rig, "key")
    fill = _field(rig, "fill")
    rim = _field(rig, "rim")

    lamps = [
        _lamp_clause(key, "key light"),
        _lamp_clause(fill, "fill"),
        _lamp_clause(rim, "rim light"),
    ]
    lit = [clause for clause in lamps if clause]
    if not lit:
        return ""

    sentences = [f"Lit with {_join(lit)}."]
    contrast = _contrast_clause(key, fill)
    if contrast:
        sentences.append(f"{contrast[0].upper()}{contrast[1:]}.")

    ambience = _AMBIENCE.get(str(_field(rig, "ambience", "day")))
    environment = _field(rig, "environment")
    tone = _field(environment, "dominantTone") if environment else None
    if ambience:
        if tone:
            sentences.append(
                f"{ambience[0].upper()}{ambience[1:]}, "
                f"the scene picking up {tone} colour from its surroundings."
            )
        else:
            sentences.append(f"{ambience[0].upper()}{ambience[1:]}.")
    elif tone:
        sentences.append(f"The scene picking up {tone} colour from its surroundings.")

    return " ".join(sentences)


def _join(parts: list[str]) -> str:
    """Oxford-comma join. Three lamps read as a list, not as three sentences."""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


# ── Presets ─────────────────────────────────────────────────────────────────
#
# Backend-owned for the same reason the camera catalog is: a preset is just a rig
# snapshot, and having the client carry its own copy is how the two drift. Each
# one is a real setup, positioned the way it would be on a stage — the y values
# are positive-up, so a key "above and left" is (-0.6, 0.5).


def _lamp(
    x: float,
    y: float,
    strength: int,
    warmth: int = 50,
    quality: str = "soft",
    colour: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "x": x,
        "y": y,
        "strength": strength,
        "warmth": warmth,
        "quality": quality,
        "colour": colour,
    }


_OFF = _lamp(0.0, 0.0, 0, enabled=False)

LIGHT_RIG_PRESETS: list[dict[str, Any]] = [
    {
        "id": "key-left",
        "label": "Key left",
        "swatch": "#F5F0E6",
        "rig": {
            "enabled": True,
            "ambience": "day",
            "key": _lamp(-0.62, 0.42, 90, warmth=55),
            "fill": _lamp(0.45, 0.05, 45, warmth=50),
            "rim": _OFF,
        },
    },
    {
        "id": "rembrandt",
        "label": "Rembrandt",
        "swatch": "#E8D9BC",
        "rig": {
            "enabled": True,
            "ambience": "dusk",
            # High and 45 off-axis: the placement that makes the cheek triangle.
            "key": _lamp(-0.55, 0.62, 85, warmth=68),
            "fill": _lamp(0.5, -0.1, 22, warmth=45),
            "rim": _OFF,
        },
    },
    {
        "id": "rim-glow",
        "label": "Rim glow",
        "swatch": "#7FD8E8",
        "rig": {
            "enabled": True,
            "ambience": "night",
            "key": _lamp(-0.5, 0.2, 40, warmth=40),
            "fill": _OFF,
            "rim": _lamp(0.92, 0.35, 95, warmth=25, quality="hard"),
        },
    },
    {
        "id": "golden-hour",
        "label": "Golden hour",
        "swatch": "#F0B060",
        "rig": {
            "enabled": True,
            "ambience": "dusk",
            # Low and raking, the way the sun sits an hour before it goes.
            "key": _lamp(-0.88, 0.18, 92, warmth=90),
            "fill": _lamp(0.55, 0.0, 35, warmth=60),
            "rim": _lamp(0.75, 0.5, 55, warmth=88),
        },
    },
    {
        "id": "neon-night",
        "label": "Neon night",
        "swatch": "#B36BE8",
        "rig": {
            "enabled": True,
            "ambience": "night",
            "key": _lamp(-0.7, 0.15, 80, warmth=15, quality="hard", colour="#FF3DA6"),
            "fill": _lamp(0.6, -0.2, 30, warmth=10, quality="hard", colour="#2BD4FF"),
            "rim": _lamp(0.88, 0.45, 70, warmth=12, quality="hard", colour="#7A3DFF"),
        },
    },
    {
        "id": "softbox",
        "label": "Softbox",
        "swatch": "#FFFFFF",
        "rig": {
            "enabled": True,
            "ambience": "day",
            # Big, close and nearly frontal — the flat, forgiving beauty setup.
            "key": _lamp(-0.28, 0.3, 88, warmth=50),
            "fill": _lamp(0.3, 0.1, 70, warmth=50),
            "rim": _OFF,
        },
    },
]


__all__ = [
    "LIGHT_RIG_PRESETS",
    "colour_name",
    "compose_light_rig_phrase",
]
