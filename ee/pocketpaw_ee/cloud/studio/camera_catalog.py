# ee/pocketpaw_ee/cloud/studio/camera_catalog.py — camera & lighting controls.
#
# Created 2026-09-03 (studio-camera-lighting).
#
# Image and video models expose no camera or lighting parameters; the only lever
# is the prompt text. This module is the pick-list behind the studio's "Camera &
# lighting" dialog and the renderer that turns a user's picks into the words a
# model actually responds to.
#
# Two rules govern everything here:
#
#   1. The id is a UI handle; the PHRASE is model-facing copy. An entry reads
#      "a 24mm wide-angle lens", never the bare token "24mm". A control the user
#      sets that doesn't change the image is worse than no control at all, so the
#      phrasing is the feature — the string assembly around it is trivia.
#
#   2. Auto means SILENCE. An unset slot contributes no words. Writing "auto
#      focal length" into a prompt actively degrades the result, so every field is
#      optional and an empty spec renders to the empty string.
#
# The catalog lives on the backend and is SERVED to the client (GET
# /studio/camera-catalog) rather than duplicated into TypeScript. The style
# catalog is duplicated TS<->Python by hand and that duplication is exactly what
# let `_apply_style` drift out of sync with `list_styles` — the whole curated
# half of the style menu silently stopped applying. One catalog, one source.

from __future__ import annotations

from typing import Any

# ── Camera groups ───────────────────────────────────────────────────────────
#
# Each group is one slot card in the dialog; `field` names the CameraSpec
# attribute it writes. Ordered as the dialog presents them: the two controls
# that actually move the image first, the gear that only reaches it indirectly
# (through depth of field and rendering character) after.

ANGLES: list[dict[str, Any]] = [
    {
        "id": "eye-level",
        "label": "Eye level",
        "hint": "Neutral",
        "phrase": "from a neutral eye-level angle",
    },
    {
        "id": "low-angle",
        "label": "Low angle",
        "hint": "Power, dominance",
        "phrase": "from a low angle, the camera looking up at the subject",
    },
    {
        "id": "high-angle",
        "label": "High angle",
        "hint": "Vulnerability",
        "phrase": "from a high angle, the camera looking down at the subject",
    },
    {
        "id": "overhead",
        "label": "Overhead",
        "hint": "Bird's eye",
        "phrase": "from a directly overhead bird's-eye view looking straight down",
    },
    {
        "id": "worms-eye",
        "label": "Worm's eye",
        "hint": "Extreme low",
        "phrase": "from an extreme worm's-eye view at ground level",
    },
    {
        "id": "dutch",
        "label": "Dutch angle",
        "hint": "Unease, tension",
        "phrase": "with a tilted dutch angle, the horizon canted",
    },
    {
        "id": "over-shoulder",
        "label": "Over shoulder",
        "hint": "Conversation",
        "phrase": "framed over a foreground shoulder",
    },
    {
        "id": "pov",
        "label": "POV",
        "hint": "First person",
        "phrase": "from a first-person point of view",
    },
]

SHOT_SIZES: list[dict[str, Any]] = [
    {
        "id": "extreme-close-up",
        "label": "Extreme close-up",
        "hint": "Detail only",
        "phrase": "as an extreme close-up filling the frame with a single detail",
    },
    {
        "id": "close-up",
        "label": "Close-up",
        "hint": "Face fills frame",
        "phrase": "as a close-up",
    },
    {
        "id": "medium-close-up",
        "label": "Medium close-up",
        "hint": "Chest up",
        "phrase": "as a medium close-up from the chest up",
    },
    {
        "id": "medium",
        "label": "Medium",
        "hint": "Waist up",
        "phrase": "as a medium shot from the waist up",
    },
    {
        "id": "cowboy",
        "label": "Cowboy",
        "hint": "Mid-thigh up",
        "phrase": "as a cowboy shot from mid-thigh up",
    },
    {
        "id": "wide",
        "label": "Wide",
        "hint": "Full subject",
        "phrase": "as a wide shot showing the full subject in its surroundings",
    },
    {
        "id": "extreme-wide",
        "label": "Extreme wide",
        "hint": "Establishing",
        "phrase": "as an extreme wide establishing shot, the subject small in the frame",
    },
]

# Focal lengths — the set from the design mockup, plus Custom. The `mm` value is
# what CameraSpec.focalLengthMm stores; `id` stays a string so the whole catalog
# renders through one generic code path on the client.
FOCAL_LENGTHS: list[dict[str, Any]] = [
    {
        "id": "8",
        "mm": 8,
        "label": "8mm",
        "hint": "Fisheye",
        "phrase": "an 8mm fisheye lens with extreme barrel distortion",
    },
    {
        "id": "14",
        "mm": 14,
        "label": "14mm",
        "hint": "Ultra-wide",
        "phrase": "a 14mm ultra-wide lens",
    },
    {
        "id": "16",
        "mm": 16,
        "label": "16mm",
        "hint": "Super-wide",
        "phrase": "a 16mm super-wide lens",
    },
    {
        "id": "24",
        "mm": 24,
        "label": "24mm",
        "hint": "Wide / establishing",
        "phrase": "a 24mm wide-angle lens",
    },
    {
        "id": "35",
        "mm": 35,
        "label": "35mm",
        "hint": "Natural / docu",
        "phrase": "a 35mm lens with a natural documentary field of view",
    },
    {
        "id": "50",
        "mm": 50,
        "label": "50mm",
        "hint": "Standard / human eye",
        "phrase": "a 50mm standard lens matching human-eye perspective",
    },
    {
        "id": "85",
        "mm": 85,
        "label": "85mm",
        "hint": "Portrait",
        "phrase": "an 85mm portrait lens with flattering compression",
    },
    {
        "id": "135",
        "mm": 135,
        "label": "135mm",
        "hint": "Tele / compressed",
        "phrase": "a 135mm telephoto lens compressing the background",
    },
    {
        "id": "200",
        "mm": 200,
        "label": "200mm",
        "hint": "Long telephoto",
        "phrase": "a 200mm long telephoto lens with heavily compressed perspective",
    },
    {
        "id": "custom",
        "label": "Custom",
        "hint": "Describe it yourself",
        "custom": True,
        "phrase": "",
    },
]

APERTURES: list[dict[str, Any]] = [
    {
        "id": "f-1-2",
        "label": "f/1.2",
        "hint": "Razor-thin focus",
        "phrase": "wide open at f/1.2 with a razor-thin plane of focus and creamy bokeh",
    },
    {
        "id": "f-1-4",
        "label": "f/1.4",
        "hint": "Very shallow",
        "phrase": "at f/1.4 with very shallow depth of field and soft bokeh",
    },
    {
        "id": "f-2",
        "label": "f/2",
        "hint": "Shallow",
        "phrase": "at f/2 with shallow depth of field",
    },
    {
        "id": "f-2-8",
        "label": "f/2.8",
        "hint": "Subject separation",
        "phrase": "at f/2.8, the subject cleanly separated from a soft background",
    },
    {
        "id": "f-4",
        "label": "f/4",
        "hint": "Balanced",
        "phrase": "at f/4 with balanced depth of field",
    },
    {
        "id": "f-5-6",
        "label": "f/5.6",
        "hint": "Most of frame sharp",
        "phrase": "at f/5.6 with most of the frame in focus",
    },
    {
        "id": "f-8",
        "label": "f/8",
        "hint": "Deep focus",
        "phrase": "at f/8 with deep focus front to back",
    },
    {
        "id": "f-16",
        "label": "f/16",
        "hint": "Everything sharp",
        "phrase": "at f/16, everything from foreground to horizon razor sharp",
    },
]

CAMERA_BODIES: list[dict[str, Any]] = [
    {
        "id": "red-v-raptor",
        "label": "RED V-Raptor",
        "hint": "Digital cinema",
        "phrase": "a RED V-Raptor digital cinema camera with crisp high-resolution rendering",
    },
    {
        "id": "arri-alexa-35",
        "label": "ARRI Alexa 35",
        "hint": "Filmic highlights",
        "phrase": "an ARRI Alexa 35 with filmic highlight roll-off and natural skin tones",
    },
    {
        "id": "sony-venice-2",
        "label": "Sony Venice 2",
        "hint": "Rich color",
        "phrase": "a Sony Venice 2 with rich, deep color rendition",
    },
    {
        "id": "panavision-dxl2",
        "label": "Panavision DXL2",
        "hint": "Large format",
        "phrase": "a large-format Panavision Millennium DXL2 with shallow, dimensional depth",
    },
    {
        "id": "film-35mm",
        "label": "35mm film",
        "hint": "Analog grain",
        "phrase": "35mm motion picture film with visible organic grain and halation",
    },
    {
        "id": "film-16mm",
        "label": "16mm film",
        "hint": "Coarse, gritty",
        "phrase": "grainy 16mm film with a coarse, gritty texture",
    },
    {
        "id": "polaroid",
        "label": "Polaroid",
        "hint": "Instant film",
        "phrase": "instant Polaroid film with soft focus, milky contrast and a faded cast",
    },
    {
        "id": "smartphone",
        "label": "Smartphone",
        "hint": "Casual, candid",
        "phrase": "a smartphone camera with a casual, candid snapshot quality",
    },
]

LENSES: list[dict[str, Any]] = [
    {
        "id": "arri-master-prime",
        "label": "ARRI Master Prime",
        "hint": "Spherical, clean",
        "phrase": "an ARRI Master Prime spherical lens, clinically sharp and neutral",
    },
    {
        "id": "cooke-s4",
        "label": "Cooke S4",
        "hint": "Warm, gentle",
        "phrase": "a Cooke S4 prime with warm rendering and gentle, flattering falloff",
    },
    {
        "id": "zeiss-supreme",
        "label": "Zeiss Supreme",
        "hint": "Neutral, modern",
        "phrase": "a Zeiss Supreme prime with neutral modern contrast",
    },
    {
        "id": "anamorphic",
        "label": "Anamorphic",
        "hint": "Oval bokeh, flares",
        "phrase": "an anamorphic lens with oval bokeh and horizontal blue streak flares",
    },
    {
        "id": "vintage-uncoated",
        "label": "Vintage uncoated",
        "hint": "Soft, glowing",
        "phrase": "a vintage uncoated lens that blooms and glows in the highlights",
    },
    {
        "id": "tilt-shift",
        "label": "Tilt-shift",
        "hint": "Selective plane",
        "phrase": "a tilt-shift lens with a steeply angled plane of focus",
    },
    {
        "id": "macro",
        "label": "Macro",
        "hint": "Extreme detail",
        "phrase": "a macro lens resolving extreme surface detail",
    },
]

# ── Lighting groups ─────────────────────────────────────────────────────────

LIGHTING_SETUPS: list[dict[str, Any]] = [
    {
        "id": "three-point",
        "label": "Three-point",
        "hint": "Key, fill, back",
        "phrase": "a classic three-point setup with key, fill and separation backlight",
    },
    {
        "id": "rembrandt",
        "label": "Rembrandt",
        "hint": "Cheek triangle",
        "phrase": "Rembrandt lighting casting a small triangle of light on the shadow-side cheek",
    },
    {
        "id": "butterfly",
        "label": "Butterfly",
        "hint": "Glamour",
        "phrase": "butterfly lighting from directly above, a small shadow under the nose",
    },
    {
        "id": "split",
        "label": "Split",
        "hint": "Half in shadow",
        "phrase": "split lighting leaving half the face in shadow",
    },
    {
        "id": "rim",
        "label": "Rim / backlit",
        "hint": "Edge glow",
        "phrase": "strong rim lighting tracing a bright edge around the subject",
    },
    {
        "id": "high-key",
        "label": "High key",
        "hint": "Bright, airy",
        "phrase": "high-key lighting, bright and airy with almost no shadow",
    },
    {
        "id": "low-key",
        "label": "Low key",
        "hint": "Dark, moody",
        "phrase": "low-key lighting with deep shadows and a small pool of light",
    },
    {
        "id": "chiaroscuro",
        "label": "Chiaroscuro",
        "hint": "Extreme contrast",
        "phrase": "dramatic chiaroscuro with extreme contrast between light and dark",
    },
    {
        "id": "practical-only",
        "label": "Practicals only",
        "hint": "In-scene sources",
        "phrase": "lit only by practical sources visible in the scene",
    },
    {
        "id": "silhouette",
        "label": "Silhouette",
        "hint": "Subject dark",
        "phrase": "backlit into near silhouette, the subject reading as a dark shape",
    },
]

LIGHTING_SOURCES: list[dict[str, Any]] = [
    {
        "id": "golden-hour",
        "label": "Golden hour",
        "hint": "Warm, low sun",
        "phrase": "warm low golden-hour sun casting long shadows",
    },
    {
        "id": "blue-hour",
        "label": "Blue hour",
        "hint": "Cool dusk",
        "phrase": "cool blue-hour dusk light after sunset",
    },
    {
        "id": "harsh-noon",
        "label": "Harsh noon",
        "hint": "Hard overhead",
        "phrase": "harsh overhead midday sun with hard-edged shadows",
    },
    {
        "id": "overcast",
        "label": "Overcast",
        "hint": "Flat, even",
        "phrase": "flat even overcast daylight with no visible shadows",
    },
    {
        "id": "window",
        "label": "Window light",
        "hint": "Soft, directional",
        "phrase": "soft directional daylight through a nearby window",
    },
    {
        "id": "night-neon",
        "label": "Neon night",
        "hint": "Colored city",
        "phrase": "colored neon signage lighting the scene at night",
    },
    {
        "id": "candlelight",
        "label": "Candlelight",
        "hint": "Warm flicker",
        "phrase": "warm flickering candlelight",
    },
    {
        "id": "firelight",
        "label": "Firelight",
        "hint": "Orange, dancing",
        "phrase": "dancing orange firelight",
    },
    {
        "id": "moonlight",
        "label": "Moonlight",
        "hint": "Cool, dim",
        "phrase": "cool dim moonlight",
    },
    {
        "id": "studio-strobe",
        "label": "Studio strobe",
        "hint": "Crisp specular",
        "phrase": "studio strobes with crisp specular highlights",
    },
    {
        "id": "softbox",
        "label": "Softbox",
        "hint": "Wrapped, even",
        "phrase": "a large softbox wrapping the subject in even light",
    },
    {
        "id": "fluorescent",
        "label": "Fluorescent",
        "hint": "Institutional",
        "phrase": "cold overhead fluorescent tubes with a slight green cast",
    },
]

LIGHTING_QUALITIES: list[dict[str, Any]] = [
    {
        "id": "hard",
        "label": "Hard",
        "hint": "Sharp shadows",
        "phrase": "hard light with sharply defined shadow edges",
    },
    {
        "id": "soft",
        "label": "Soft",
        "hint": "Gradual falloff",
        "phrase": "soft diffused light with gradual shadow falloff",
    },
]

LIGHTING_DIRECTIONS: list[dict[str, Any]] = [
    {"id": "front", "label": "Front", "hint": "Flat", "phrase": "lit from the front"},
    {"id": "side", "label": "Side", "hint": "Sculpting", "phrase": "lit from the side"},
    {"id": "back", "label": "Back", "hint": "Separation", "phrase": "lit from behind"},
    {"id": "top", "label": "Top", "hint": "Overhead", "phrase": "lit from directly above"},
    {
        "id": "under",
        "label": "Under",
        "hint": "Unsettling",
        "phrase": "lit from below for an unsettling effect",
    },
]

# ── Group assembly (what the dialog renders) ────────────────────────────────
#
# `field` is the CameraSpec / LightingSpec attribute the group writes, so the
# client renders every group through one generic component instead of eight
# hand-written ones.

CAMERA_GROUPS: list[dict[str, Any]] = [
    {"id": "angle", "field": "angle", "label": "Angle", "options": ANGLES},
    {"id": "shotSize", "field": "shotSize", "label": "Shot size", "options": SHOT_SIZES},
    {
        "id": "focalLength",
        "field": "focalLengthMm",
        "label": "Focal length",
        "options": FOCAL_LENGTHS,
    },
    {"id": "aperture", "field": "aperture", "label": "Aperture", "options": APERTURES},
    {"id": "body", "field": "body", "label": "Camera", "options": CAMERA_BODIES},
    {"id": "lens", "field": "lens", "label": "Lens", "options": LENSES},
]

LIGHTING_GROUPS: list[dict[str, Any]] = [
    {"id": "setup", "field": "setup", "label": "Setup", "options": LIGHTING_SETUPS},
    {"id": "source", "field": "source", "label": "Source", "options": LIGHTING_SOURCES},
    {"id": "quality", "field": "quality", "label": "Quality", "options": LIGHTING_QUALITIES},
    {
        "id": "direction",
        "field": "direction",
        "label": "Direction",
        "options": LIGHTING_DIRECTIONS,
    },
]


def _phrase(options: list[dict[str, Any]], value: Any) -> str:
    """Look one option's model-facing phrase up by id. Unknown ids render to the
    empty string rather than raising — a stale client sending a retired id should
    lose that one control, not fail the whole generation."""
    if value is None or value == "":
        return ""
    wanted = str(value)
    for opt in options:
        if str(opt["id"]) == wanted:
            return str(opt.get("phrase") or "")
    return ""


def compose_camera_phrase(camera: Any) -> str:
    """Render a CameraSpec into one or two sentences of camera direction.

    Returns "" for a spec that is absent or entirely unset — the Auto case. The
    gear clause and the framing clause are separate sentences because they answer
    different questions ("what shot this" vs "where is it standing"), and a model
    handles two short sentences better than one long comma chain.
    """
    if camera is None:
        return ""

    def field(name: str) -> Any:
        return getattr(camera, name, None) if not isinstance(camera, dict) else camera.get(name)

    # A custom focal length is free text the user typed; trust it as written
    # rather than trying to parse a number back out of it.
    custom_focal = (field("customFocalLength") or "").strip()
    focal_phrase = (
        f"a {custom_focal} lens" if custom_focal else _phrase(FOCAL_LENGTHS, field("focalLengthMm"))
    )

    gear = [
        _phrase(CAMERA_BODIES, field("body")),
        _phrase(LENSES, field("lens")),
        focal_phrase,
        _phrase(APERTURES, field("aperture")),
    ]
    framing = [
        _phrase(ANGLES, field("angle")),
        _phrase(SHOT_SIZES, field("shotSize")),
    ]

    sentences: list[str] = []
    gear_parts = [g for g in gear if g]
    if gear_parts:
        sentences.append(f"Shot on {', '.join(gear_parts)}.")
    framing_parts = [f for f in framing if f]
    if framing_parts:
        sentences.append(f"Framed {', '.join(framing_parts)}.")
    return " ".join(sentences)


def compose_lighting_phrase(lighting: Any) -> str:
    """Render a LightingSpec into one sentence. "" when absent or unset."""
    if lighting is None:
        return ""

    def field(name: str) -> Any:
        return (
            getattr(lighting, name, None) if not isinstance(lighting, dict) else lighting.get(name)
        )

    parts = [
        p
        for p in (
            _phrase(LIGHTING_SETUPS, field("setup")),
            _phrase(LIGHTING_SOURCES, field("source")),
            _phrase(LIGHTING_QUALITIES, field("quality")),
            _phrase(LIGHTING_DIRECTIONS, field("direction")),
        )
        if p
    ]
    if not parts:
        return ""
    return f"Lit with {', '.join(parts)}."


__all__ = [
    "ANGLES",
    "APERTURES",
    "CAMERA_BODIES",
    "CAMERA_GROUPS",
    "FOCAL_LENGTHS",
    "LENSES",
    "LIGHTING_DIRECTIONS",
    "LIGHTING_GROUPS",
    "LIGHTING_QUALITIES",
    "LIGHTING_SETUPS",
    "LIGHTING_SOURCES",
    "SHOT_SIZES",
    "compose_camera_phrase",
    "compose_lighting_phrase",
]
