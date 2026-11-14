# ee/agent/mcp_servers/anatomy_film.py — the full-frame "anatomy reveal" film
# inside a HyperFrames composition: the brief the agent writes it from, and the
# check that runs before a composition is dispatched.
#
# The agent only writes one placeholder element (`data-anatomy-film`) holding a
# JSON spec of an object built from convex parts in real millimetres. The
# browser (paw-enterprise's HyperFrames render) draws the film itself for the
# frames inside the element's window: an object on warm white
# paper, the camera whipping to detail shots, assemblies exploding apart and
# snapping back for a hero shot. While the window runs the film covers the whole
# frame, so nothing else of the composition shows. ANATOMY_FILM_KINDS mirrors
# the part kinds that renderer draws; a kind added there needs its entry here.
# ANATOMY_FILM_EXAMPLES are worked specs the brief carries as a reference pack;
# each one builds with every part intact in paw-enterprise's anatomy.ts.

from __future__ import annotations

import json
import re

ANATOMY_FILM_KINDS = ("box", "cylinder", "ball", "lathe")
MIN_PARTS = 6
MAX_PARTS = 60

ANATOMY_FILM_EXAMPLES: tuple[dict, ...] = (
    {
        "title": "Pour-over kettle",
        "subtitle": "1 L, gooseneck spout",
        "parts": [
            {
                "id": "base",
                "kind": "cylinder",
                "at": [0, 0, 0],
                "r": 82,
                "h": 12,
                "material": "gunmetal",
                "group": "base",
            },
            {
                "id": "body",
                "kind": "lathe",
                "at": [0, 0, 12],
                "profile": [[0, 78], [90, 80], [150, 62], [170, 40]],
                "material": "steel",
                "group": "body",
            },
            {
                "id": "window",
                "kind": "box",
                "at": [0, -84, 40],
                "w": 16,
                "d": 4,
                "h": 60,
                "round": 2,
                "material": "glass",
                "group": "body",
            },
            {
                "id": "spout",
                "kind": "cylinder",
                "at": [76, 0, 40],
                "r": 8,
                "r2": 4,
                "h": 90,
                "axis": "x",
                "material": "steel",
                "group": "body",
            },
            {
                "id": "handle",
                "kind": "box",
                "at": [-98, 0, 50],
                "w": 18,
                "d": 24,
                "h": 110,
                "round": 8,
                "material": "rubber",
                "group": "body",
            },
            {
                "id": "lid",
                "kind": "cylinder",
                "at": [0, 0, 182],
                "r": 40,
                "h": 8,
                "material": "steel",
                "group": "lid",
            },
            {
                "id": "knob",
                "kind": "ball",
                "at": [0, 0, 200],
                "r": 10,
                "material": "brass",
                "group": "lid",
            },
            {
                "id": "switch",
                "kind": "box",
                "at": [0, -80, 2],
                "w": 20,
                "d": 6,
                "h": 8,
                "round": 2,
                "material": "rubber",
                "group": "base",
            },
        ],
        "assemblies": [
            {"id": "lid", "dir": "up", "travel": 90, "delay": 0},
            {"id": "body", "dir": "up", "travel": 40, "delay": 0.3},
            {"id": "base", "dir": "down", "travel": 30, "delay": 0.5},
        ],
        "shots": [
            {"part": "spout", "hint": "Gooseneck spout", "readout": "Pours a thin, slow stream"},
            {"part": "window", "hint": "Water window", "readout": "See the level at a glance"},
            {"part": "base", "hint": "Heating base", "readout": "Lift off and pour"},
        ],
    },
    {
        "title": "Phone",
        "subtitle": "Shell first, then what is inside",
        "parts": [
            {
                "id": "back",
                "kind": "box",
                "at": [0, 0, 0],
                "w": 72,
                "d": 150,
                "h": 1.5,
                "round": 8,
                "material": "glass",
                "group": "back",
            },
            {
                "id": "rail-l",
                "kind": "box",
                "at": [-35, 0, 1.5],
                "w": 2,
                "d": 150,
                "h": 6,
                "material": "steel",
                "group": "frame",
            },
            {
                "id": "rail-r",
                "kind": "box",
                "at": [35, 0, 1.5],
                "w": 2,
                "d": 150,
                "h": 6,
                "material": "steel",
                "group": "frame",
            },
            {
                "id": "rail-t",
                "kind": "box",
                "at": [0, 74, 1.5],
                "w": 68,
                "d": 2,
                "h": 6,
                "material": "steel",
                "group": "frame",
            },
            {
                "id": "rail-b",
                "kind": "box",
                "at": [0, -74, 1.5],
                "w": 68,
                "d": 2,
                "h": 6,
                "material": "steel",
                "group": "frame",
            },
            {
                "id": "battery",
                "kind": "box",
                "at": [0, -12, 1.5],
                "w": 56,
                "d": 70,
                "h": 4,
                "round": 3,
                "material": "gunmetal",
                "group": "inside",
            },
            {
                "id": "board",
                "kind": "box",
                "at": [0, 50, 1.5],
                "w": 56,
                "d": 30,
                "h": 1.5,
                "material": "pcb",
                "group": "inside",
            },
            {
                "id": "chip",
                "kind": "box",
                "at": [-10, 50, 3],
                "w": 12,
                "d": 12,
                "h": 1.5,
                "material": "gunmetal",
                "group": "inside",
            },
            {
                "id": "camera",
                "kind": "cylinder",
                "at": [18, 58, 3],
                "r": 5,
                "h": 3,
                "material": "glass",
                "group": "inside",
            },
            {
                "id": "screen",
                "kind": "box",
                "at": [0, 0, 7.5],
                "w": 72,
                "d": 150,
                "h": 1,
                "round": 8,
                "material": "glass",
                "group": "screen",
            },
        ],
        "assemblies": [
            {"id": "screen", "dir": "up", "travel": 60, "delay": 0},
            {"id": "inside", "dir": "up", "travel": 30, "delay": 0.3},
            {"id": "frame", "dir": "up", "travel": 12, "delay": 0.5},
            {"id": "back", "dir": "down", "travel": 20, "delay": 0.6},
        ],
        "shots": [
            {"part": "chip", "hint": "Main board", "readout": "Where the work happens"},
            {"part": "battery", "hint": "Battery", "readout": "Takes most of the space"},
            {"part": "camera", "hint": "Camera", "readout": "Sits in the top corner"},
        ],
    },
)

ANATOMY_FILM_BRIEF = (
    "Anatomy film: put ONE full-frame element inside the composition root: "
    '<div data-anatomy-film data-start="<second it starts, about 2>" '
    'data-duration="<seconds, so it ends about 2 s before the reel ends>" '
    'style="position:absolute;inset:0"><script type="application/json">{...}</script></div>. '
    "The render draws the anatomy film over the whole frame for exactly that window, so "
    "nothing else of the composition shows then: the hook plays before it and the closing "
    "line after it. The film shows the object on warm white paper, whips the camera to each "
    "detail shot, explodes it into its assemblies and snaps it back for a hero shot; you "
    "only write the spec. Build the company's own product from its real parts. When the "
    "post is not about one product, pick the single physical object that best stands for "
    "its subject (a building, tool, device or machine).\n"
    "The JSON spec: title (the object's name, 1-4 words); subtitle (a short readout, under "
    "40 characters); parts: 8 to 40 solids in real millimetres, x right, y back, z up, "
    "standing on z=0, each {id, kind box|cylinder|ball|lathe, at [x,y,z] (the bottom centre "
    "of a box, the start of a cylinder's or lathe's axis, the centre of a ball), w/d/h and "
    "round (corner radius) for a box, r/h/axis x|y|z and r2 (far-end radius, makes a cone) "
    "for a cylinder, r for a ball, axis and profile [[distance along the axis, radius], ...] "
    "for a lathe (a turned shape: bottles, cups, knobs, kettles, lamp shades; at least two "
    "rows, the outline must bulge outward or stay straight, never pinch in: split a waisted "
    "shape into two lathes), material (optional) "
    "steel|chrome|gunmetal|rubber|brass|copper|gold|glass|pcb|porcelain, group: the "
    "assembly it comes apart with (leave it out for a part that stays put)}; "
    "assemblies: 2 to 6 of {id, dir "
    "up|down|left|right|front|back or [x,y,z], travel (mm), delay 0-1 (outer parts lead), "
    "parent (optional)}; shots: 2 to 4 of {part, hint (1-5 words naming the part), readout "
    "(under 40 characters)}, in the order the story is told: the camera whips to each part "
    "in turn.\n"
    "Build rules: real proportions from the real object; the shell of panels first, then "
    "what is inside; parts meet face to face and never pass through each other (a solid "
    "part cannot hold another inside it, so make a hollow thing from panels); every part "
    "rests on z=0 or on another part, nothing floats; every group is one of the "
    "assemblies; every shot names a part id. Point the shots at the parts that carry the "
    "post's benefits. Readouts and the subtitle say what a part does or a fact from the "
    "company profile; never invent numbers, specs, prices or results.\n"
    "Two worked specs (copy their shape, not their objects): "
    + " ".join(json.dumps(example, separators=(",", ":")) for example in ANATOMY_FILM_EXAMPLES)
)

_FILM_RE = re.compile(r"<[a-zA-Z][^>]*\bdata-anatomy-film\b[^>]*>", re.IGNORECASE)
_SPEC_RE = re.compile(
    r"<script\b[^>]*application/json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)


def _attr(tag: str, name: str) -> float | None:
    match = re.search(rf"\b{name}\s*=\s*([\"']?)([^\"'\s>]*)\1", tag, re.IGNORECASE)
    try:
        return float(match.group(2)) if match else None
    except ValueError:
        return None


def _lint_spec(spec: dict) -> str | None:
    parts = [part for part in spec["parts"] if isinstance(part, dict)]
    drawn = [part for part in parts if part.get("kind") in ANATOMY_FILM_KINDS]
    if not drawn:
        return (
            f"The anatomy film's parts need a kind the film draws: {', '.join(ANATOMY_FILM_KINDS)}."
        )
    if not MIN_PARTS <= len(drawn) <= MAX_PARTS:
        return (
            f"The anatomy film has {len(drawn)} usable parts; build the object from "
            f"{MIN_PARTS} to {MAX_PARTS} (8 to 40 reads best)."
        )
    ids = {part.get("id") for part in drawn}
    assemblies = {
        a.get("id") for a in spec.get("assemblies") or [] if isinstance(a, dict) and a.get("id")
    }
    if assemblies:
        stray = sorted(
            {str(p["group"]) for p in drawn if p.get("group") and p["group"] not in assemblies}
        )
        if stray:
            return (
                f"Parts use groups {', '.join(stray)} that are not assemblies. Give every "
                "grouped part a group that names one of the assemblies."
            )
    missing = [
        str(shot.get("part"))
        for shot in spec.get("shots") or []
        if isinstance(shot, dict) and shot.get("part") not in ids
    ]
    if missing:
        return f"Shots name parts that do not exist: {', '.join(missing)}. Use part ids."
    bad_lathes = [
        str(part.get("id"))
        for part in drawn
        if part.get("kind") == "lathe"
        and not (isinstance(part.get("profile"), list) and len(part["profile"]) >= 2)
    ]
    if bad_lathes:
        return f"Lathe parts {', '.join(bad_lathes)} need a profile of at least two [s, r] rows."
    lows = [p["at"][2] for p in drawn if isinstance(p.get("at"), list) and len(p["at"]) == 3]
    if lows and all(isinstance(z, (int, float)) for z in lows) and min(lows) > 5:
        return "No part stands on the ground: the lowest part should sit at z=0."
    return None


def validate_anatomy_film(html: str, duration_s: float | None = None) -> str | None:
    """Check each anatomy film element carries a usable spec and fits the composition."""
    for film in _FILM_RE.finditer(html):
        spec = _SPEC_RE.search(html, film.end())
        try:
            parsed = json.loads(spec.group(1)) if spec else None
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("parts"), list):
            return (
                'A data-anatomy-film element needs a <script type="application/json"> child '
                'holding {"title": ..., "parts": [...], "assemblies": [...], "shots": [...]} '
                "as valid JSON."
            )
        spec_error = _lint_spec(parsed)
        if spec_error is not None:
            return spec_error
        tag = film.group(0)
        start = _attr(tag, "data-start")
        length = _attr(tag, "data-duration")
        if start is None or start < 0:
            return (
                "A data-anatomy-film element needs a numeric data-start (the second it "
                'starts, e.g. data-start="2").'
            )
        if length is None or length < 4:
            return (
                "A data-anatomy-film element needs a numeric data-duration of at least 4 seconds."
            )
        if duration_s is not None and start + length > duration_s + 0.01:
            return (
                f"The anatomy film runs {start:g}-{start + length:g}s but the composition "
                f"ends at {duration_s:g}s. Shorten data-duration so it ends about 2 s before "
                "the end."
            )
    return None


__all__ = [
    "ANATOMY_FILM_BRIEF",
    "ANATOMY_FILM_EXAMPLES",
    "ANATOMY_FILM_KINDS",
    "validate_anatomy_film",
]
