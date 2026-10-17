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

from __future__ import annotations

import json
import re

ANATOMY_FILM_KINDS = ("box", "cylinder", "ball")

ANATOMY_FILM_BRIEF = (
    "Anatomy film: put ONE full-frame element inside the composition root: "
    '<div data-anatomy-film data-start="<second it starts, about 2>" '
    'data-duration="<seconds, so it ends about 2 s before the reel ends>" '
    'style="position:absolute;inset:0"><script type="application/json">{...}</script></div>. '
    "The render draws the anatomy film over the whole frame for exactly that window, so "
    "nothing else of the composition shows then: the hook plays before it and the closing "
    "line after it. The film shows the object on warm white paper, whips the camera to each "
    "detail shot, explodes it into its assemblies and snaps it back for a hero shot; you "
    "only write the spec. Build the real thing from its real parts (the shell of panels "
    "first, then what is inside). When the post is not about one product, pick the single "
    "physical object that best stands for its subject (a building, tool, device or machine).\n"
    "The JSON spec: title (the object's name, 1-4 words); subtitle (a short readout such as "
    "its size or a key fact, under 40 characters); parts: 8 to 40 solids in real "
    "millimetres, x right, y back, z up, standing on z=0, each {id, kind box|cylinder|ball, "
    "at [x,y,z] (the bottom centre of a box, the start of a cylinder's axis, the centre of "
    "a ball), w/d/h and round (corner radius) for a box, r/h/axis x|y|z and r2 (far-end "
    "radius, makes a cone) for a cylinder, r for a ball, material (optional) "
    "steel|chrome|gunmetal|rubber|brass|copper|gold|glass|pcb|porcelain, group: the "
    "assembly it comes apart with}; assemblies: 2 to 6 of {id, dir "
    "up|down|left|right|front|back or [x,y,z], travel (mm), delay 0-1 (outer parts lead), "
    "parent (optional)}; shots: 2 to 4 of {part, hint (1-5 words naming the part), readout "
    "(one short fact, under 40 characters)}, in the order the story is told: the camera "
    "whips to each part in turn."
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
        if not any(
            isinstance(part, dict) and part.get("kind") in ANATOMY_FILM_KINDS
            for part in parsed["parts"]
        ):
            return (
                "The anatomy film's parts need a kind the film draws: "
                f"{', '.join(ANATOMY_FILM_KINDS)}."
            )
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


__all__ = ["ANATOMY_FILM_BRIEF", "ANATOMY_FILM_KINDS", "validate_anatomy_film"]
