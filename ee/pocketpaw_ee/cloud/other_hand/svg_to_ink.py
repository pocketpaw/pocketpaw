# ee/pocketpaw_ee/cloud/other_hand/svg_to_ink.py — turn an SVG into pen strokes.
#
# Created 2026-08-28 (feat/other-hand-vector-illustration).
#
# Otherhand draws with ONE pen on cream paper. A generated vector illustration
# has to become ink or it does not belong on the page: pasted as a picture it
# reads as a foreign object, cannot be erased like ink, and does not participate
# in free_y — so the next turn writes over it.
#
# So this converts SVG geometry into the surface's own ``path`` ops: polylines
# in the 1240-wide logical page space. Deliberately narrow:
#
#   * STROKE ONLY. Fills, gradients and opacity are dropped, because a pen has
#     none of them. A filled shape becomes its outline, which is why the caller
#     should ask the generator for line art in the first place.
#   * Curves are FLATTENED, since the renderer draws polylines. Subdivision is
#     proportional to the curve's own size in page space, so a small flourish
#     does not pay for the same 24 segments as a full-page arc.
#   * A hard point budget. A detailed SVG can carry hundreds of thousands of
#     nodes; at some point that is not a drawing, it is a hang. Over budget we
#     drop whole SHAPES from the end rather than thinning every shape, because a
#     complete drawing of fewer things reads better than a smeared version of
#     everything.
#
# No new dependency: the parser is a focused reader of the path grammar and the
# handful of shape elements a generator actually emits. Pulling in a full SVG
# library would buy DOM/CSS/filter support this can never use, and the supply
# chain rule makes a new dep a real cost.

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

# The logical page the surface addresses. Mirrors the frontend PAGE_W/PAGE_H.
PAGE_W = 1240
PAGE_H = 1754

# Above this a "drawing" is a hang, not a picture. Chosen from the renderer's
# side: every point is a lineTo on every frame of the reveal animation.
MAX_POINTS = 12_000
# Below this a segment is invisible at page scale, so subdividing further only
# buys points. ~0.6 logical px is under half a device pixel on a 1240-wide page.
FLATNESS_PX = 0.6
# Ceiling per curve regardless of size — guards a pathological control net.
MAX_SEGMENTS_PER_CURVE = 48

_NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_CMD = re.compile(r"[MmZzLlHhVvCcSsQqTtAa]")
_SVG_NS = "{http://www.w3.org/2000/svg}"


class SvgConvertError(ValueError):
    """The SVG could not be read as geometry."""


@dataclass(frozen=True)
class Box:
    """Where the drawing lands on the page, in logical page units."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise SvgConvertError("the target box must have positive width and height")


def _numbers(chunk: str) -> list[float]:
    return [float(m.group()) for m in _NUM.finditer(chunk)]


def _flatten_cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    scale: float,
) -> list[tuple[float, float]]:
    """Sample a cubic bezier finely enough that the result reads as a curve.

    Segment count comes from the control net's length in PAGE units, not from a
    fixed number: the same constant then serves a 20px flourish and a 900px arc
    without over-sampling the first or faceting the second.
    """
    net = (
        math.dist(p0, p1) + math.dist(p1, p2) + math.dist(p2, p3)
    ) * scale
    n = max(2, min(MAX_SEGMENTS_PER_CURVE, int(net / max(FLATNESS_PX, 0.01)) ** 0.5 * 2))
    out: list[tuple[float, float]] = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        x = u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0]
        y = u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]
        out.append((x, y))
    return out


def _flatten_quadratic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    scale: float,
) -> list[tuple[float, float]]:
    """A quadratic is a cubic with its control points raised — convert and reuse
    one flattener rather than maintain two samplers that can drift apart."""
    c1 = (p0[0] + 2 / 3 * (p1[0] - p0[0]), p0[1] + 2 / 3 * (p1[1] - p0[1]))
    c2 = (p2[0] + 2 / 3 * (p1[0] - p2[0]), p2[1] + 2 / 3 * (p1[1] - p2[1]))
    return _flatten_cubic(p0, c1, c2, p2, scale)


def _arc_to_cubics(
    p0: tuple[float, float],
    rx: float,
    ry: float,
    phi_deg: float,
    large_arc: bool,
    sweep: bool,
    p1: tuple[float, float],
) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    """Endpoint-parameterised elliptical arc → a list of cubic segments.

    Implements the conversion in the SVG spec's implementation notes (F.6). Arcs
    appear in generated art often enough (rounded corners, dials, petals) that
    dropping them leaves visible gaps in the outline.
    """
    if rx == 0 or ry == 0 or p0 == p1:
        return []
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)

    dx2, dy2 = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1p = cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2

    # Scale the radii up when they are too small to span the endpoints (F.6.6).
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s

    denom = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    if denom == 0:
        return []
    num = max(0.0, rx * rx * ry * ry - denom)
    coef = math.sqrt(num / denom)
    if large_arc == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (p0[0] + p1[0]) / 2
    cy = sin_p * cxp + cos_p * cyp + (p0[1] + p1[1]) / 2

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        n = math.hypot(ux, uy) * math.hypot(vx, vy)
        if n == 0:
            return 0.0
        a = math.acos(max(-1.0, min(1.0, dot / n)))
        return -a if ux * vy - uy * vx < 0 else a

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi

    # A cubic approximates at most a quarter turn well; split accordingly.
    n_segs = max(1, int(math.ceil(abs(dtheta) / (math.pi / 2))))
    delta = dtheta / n_segs
    t = 4 / 3 * math.tan(delta / 4)
    out = []
    th = theta1
    for _ in range(n_segs):
        cos1, sin1 = math.cos(th), math.sin(th)
        cos2, sin2 = math.cos(th + delta), math.sin(th + delta)
        e1 = (
            cx + rx * cos_p * cos1 - ry * sin_p * sin1,
            cy + rx * sin_p * cos1 + ry * cos_p * sin1,
        )
        e2 = (
            cx + rx * cos_p * cos2 - ry * sin_p * sin2,
            cy + rx * sin_p * cos2 + ry * cos_p * sin2,
        )
        d1 = (
            -rx * cos_p * sin1 - ry * sin_p * cos1,
            -rx * sin_p * sin1 + ry * cos_p * cos1,
        )
        d2 = (
            -rx * cos_p * sin2 - ry * sin_p * cos2,
            -rx * sin_p * sin2 + ry * cos_p * cos2,
        )
        out.append(
            (
                (e1[0] + t * d1[0], e1[1] + t * d1[1]),
                (e2[0] - t * d2[0], e2[1] - t * d2[1]),
                e2,
            )
        )
        th += delta
    return out


def parse_path(d: str, scale_hint: float = 1.0) -> list[list[tuple[float, float]]]:
    """Read one ``d`` attribute into subpaths of points, in the SVG's own units.

    ``scale_hint`` is how many PAGE units one SVG unit becomes, passed down so
    curve subdivision is decided at the size the reader will actually see.

    Unknown commands are skipped rather than raised on: generators emit the odd
    oddity, and losing one segment beats losing the drawing.
    """
    subpaths: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = 0.0
    start_x = start_y = 0.0
    # Reflection points for the S/T shorthands.
    prev_c2: tuple[float, float] | None = None
    prev_q1: tuple[float, float] | None = None

    tokens = [t for t in _CMD.split(d)]
    cmds = _CMD.findall(d)
    # split() yields a leading chunk before the first command; drop it.
    args = tokens[1:] if len(tokens) == len(cmds) + 1 else tokens

    for cmd, chunk in zip(cmds, args, strict=False):
        nums = _numbers(chunk)
        rel = cmd.islower()
        c = cmd.upper()

        if c == "M":
            for i in range(0, len(nums) - 1, 2):
                nx, ny = nums[i], nums[i + 1]
                px, py = (x + nx, y + ny) if rel else (nx, ny)
                if i == 0:
                    if len(cur) > 1:
                        subpaths.append(cur)
                    cur = [(px, py)]
                    start_x, start_y = px, py
                else:
                    # Extra pairs after an M are implicit LINETOs, per the spec.
                    cur.append((px, py))
                x, y = px, py
            prev_c2 = prev_q1 = None
        elif c == "L":
            for i in range(0, len(nums) - 1, 2):
                nx, ny = nums[i], nums[i + 1]
                x, y = (x + nx, y + ny) if rel else (nx, ny)
                cur.append((x, y))
            prev_c2 = prev_q1 = None
        elif c == "H":
            for nx in nums:
                x = x + nx if rel else nx
                cur.append((x, y))
            prev_c2 = prev_q1 = None
        elif c == "V":
            for ny in nums:
                y = y + ny if rel else ny
                cur.append((x, y))
            prev_c2 = prev_q1 = None
        elif c in ("C", "S"):
            step = 6 if c == "C" else 4
            for i in range(0, len(nums) - step + 1, step):
                if c == "C":
                    c1 = (nums[i], nums[i + 1])
                    c2 = (nums[i + 2], nums[i + 3])
                    end = (nums[i + 4], nums[i + 5])
                    if rel:
                        c1 = (x + c1[0], y + c1[1])
                        c2 = (x + c2[0], y + c2[1])
                        end = (x + end[0], y + end[1])
                else:
                    # S reflects the previous cubic's second control point.
                    c1 = (2 * x - prev_c2[0], 2 * y - prev_c2[1]) if prev_c2 else (x, y)
                    c2 = (nums[i], nums[i + 1])
                    end = (nums[i + 2], nums[i + 3])
                    if rel:
                        c2 = (x + c2[0], y + c2[1])
                        end = (x + end[0], y + end[1])
                if not cur:
                    cur = [(x, y)]
                cur.extend(_flatten_cubic((x, y), c1, c2, end, scale_hint))
                x, y = end
                prev_c2 = c2
            prev_q1 = None
        elif c in ("Q", "T"):
            step = 4 if c == "Q" else 2
            for i in range(0, len(nums) - step + 1, step):
                if c == "Q":
                    q1 = (nums[i], nums[i + 1])
                    end = (nums[i + 2], nums[i + 3])
                    if rel:
                        q1 = (x + q1[0], y + q1[1])
                        end = (x + end[0], y + end[1])
                else:
                    q1 = (2 * x - prev_q1[0], 2 * y - prev_q1[1]) if prev_q1 else (x, y)
                    end = (nums[i], nums[i + 1])
                    if rel:
                        end = (x + end[0], y + end[1])
                if not cur:
                    cur = [(x, y)]
                cur.extend(_flatten_quadratic((x, y), q1, end, scale_hint))
                x, y = end
                prev_q1 = q1
            prev_c2 = None
        elif c == "A":
            for i in range(0, len(nums) - 6, 7):
                rx, ry, rot = nums[i], nums[i + 1], nums[i + 2]
                large = bool(nums[i + 3])
                sweep = bool(nums[i + 4])
                end = (nums[i + 5], nums[i + 6])
                if rel:
                    end = (x + end[0], y + end[1])
                if not cur:
                    cur = [(x, y)]
                for c1, c2, e in _arc_to_cubics((x, y), rx, ry, rot, large, sweep, end):
                    cur.extend(_flatten_cubic((x, y), c1, c2, e, scale_hint))
                    x, y = e
                x, y = end
            prev_c2 = prev_q1 = None
        elif c == "Z":
            if cur:
                cur.append((start_x, start_y))
                subpaths.append(cur)
                cur = []
            x, y = start_x, start_y
            prev_c2 = prev_q1 = None

    if len(cur) > 1:
        subpaths.append(cur)
    return [s for s in subpaths if len(s) > 1]


def _viewbox(root: ET.Element) -> tuple[float, float, float, float]:
    """The SVG's own coordinate space. Falls back to width/height, then to a
    square, because a generator occasionally omits the viewBox and a drawing
    scaled to nothing is worse than one scaled by a guess."""
    vb = (root.get("viewBox") or "").strip()
    if vb:
        n = _numbers(vb)
        if len(n) == 4 and n[2] > 0 and n[3] > 0:
            return n[0], n[1], n[2], n[3]
    w = _numbers(root.get("width") or "")
    h = _numbers(root.get("height") or "")
    if w and h and w[0] > 0 and h[0] > 0:
        return 0.0, 0.0, w[0], h[0]
    return 0.0, 0.0, 100.0, 100.0


def _shapes(root: ET.Element, scale_hint: float) -> list[list[tuple[float, float]]]:
    """Every drawable element, as polylines in the SVG's own units.

    Covers the elements a vector generator actually emits. Anything else — text,
    images, filters, use/defs — is skipped: a pen cannot draw them, and silently
    dropping them is the honest behaviour when the alternative is a wrong shape.
    """
    out: list[list[tuple[float, float]]] = []
    for el in root.iter():
        tag = el.tag.replace(_SVG_NS, "")
        try:
            if tag == "path":
                out.extend(parse_path(el.get("d") or "", scale_hint))
            elif tag == "line":
                out.append(
                    [
                        (float(el.get("x1", 0)), float(el.get("y1", 0))),
                        (float(el.get("x2", 0)), float(el.get("y2", 0))),
                    ]
                )
            elif tag in ("polyline", "polygon"):
                n = _numbers(el.get("points") or "")
                pts = [(n[i], n[i + 1]) for i in range(0, len(n) - 1, 2)]
                if tag == "polygon" and len(pts) > 2:
                    pts.append(pts[0])
                if len(pts) > 1:
                    out.append(pts)
            elif tag == "rect":
                x, y = float(el.get("x", 0)), float(el.get("y", 0))
                w, h = float(el.get("width", 0)), float(el.get("height", 0))
                if w > 0 and h > 0:
                    out.append([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)])
            elif tag in ("circle", "ellipse"):
                cx, cy = float(el.get("cx", 0)), float(el.get("cy", 0))
                if tag == "circle":
                    rx = ry = float(el.get("r", 0))
                else:
                    rx, ry = float(el.get("rx", 0)), float(el.get("ry", 0))
                if rx > 0 and ry > 0:
                    steps = max(12, min(64, int((rx + ry) * scale_hint / 3)))
                    out.append(
                        [
                            (
                                cx + rx * math.cos(2 * math.pi * i / steps),
                                cy + ry * math.sin(2 * math.pi * i / steps),
                            )
                            for i in range(steps + 1)
                        ]
                    )
        except (TypeError, ValueError):
            # One malformed attribute loses one shape, never the drawing.
            continue
    return out


def svg_to_ops(svg: str, box: Box) -> list[dict[str, Any]]:
    """Convert an SVG into ``path`` page-ops fitted inside ``box``.

    The drawing is scaled UNIFORMLY and centred, so a generator's square canvas
    landing in a wide box keeps its proportions instead of being stretched — a
    stretched illustration is the tell that something machine-processed it.

    Returns ops ready to go straight into a page-ops block. An SVG with no
    drawable geometry returns an empty list rather than raising: the caller's
    turn should continue without the picture, not fail.
    """
    try:
        root = ET.fromstring(svg)  # noqa: S314 — see the note below.
    except ET.ParseError as exc:
        raise SvgConvertError(f"could not parse the SVG: {exc}") from exc
    # ET is used rather than defusedxml because the input is a generated SVG we
    # fetched ourselves over TLS, and ET has had entity expansion disabled by
    # default since Python 3.8 — the billion-laughs vector this warning is about
    # does not apply. It never parses user-uploaded XML.

    vx, vy, vw, vh = _viewbox(root)
    # One scale for both axes: fit, never stretch.
    scale = min(box.w / vw, box.h / vh)
    # Centre the fitted drawing in the box it was given.
    off_x = box.x + (box.w - vw * scale) / 2
    off_y = box.y + (box.h - vh * scale) / 2

    ops: list[dict[str, Any]] = []
    total = 0
    for shape in _shapes(root, scale):
        pts: list[list[float]] = []
        last: tuple[float, float] | None = None
        for sx, sy in shape:
            px = off_x + (sx - vx) * scale
            py = off_y + (sy - vy) * scale
            # Drop points that land on top of the previous one after scaling —
            # a curve sampled finely in SVG units can collapse at page scale,
            # and they cost the same to draw as real ones.
            if last is not None and abs(px - last[0]) < 0.25 and abs(py - last[1]) < 0.25:
                continue
            # Never emit a coordinate off the page; the validator drops the
            # whole op if any point is out of range.
            px = max(0.0, min(float(PAGE_W), px))
            py = max(0.0, min(float(PAGE_H * 30), py))
            pts.append([round(px, 1), round(py, 1)])
            last = (px, py)
        if len(pts) < 2:
            continue
        if total + len(pts) > MAX_POINTS:
            # Budget spent. Stop at a shape boundary — see the module note on
            # why whole shapes are dropped rather than every shape thinned.
            break
        total += len(pts)
        ops.append({"t": "path", "pts": pts})
    return ops
