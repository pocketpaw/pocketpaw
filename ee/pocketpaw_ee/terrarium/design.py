# ee/pocketpaw_ee/terrarium/design.py
#
# The design language a citizen writes once it holds the ``workshop``: a
# footprint in map cells and a list of parts (box, prism, cylinder, cone) in a
# fixed palette of sixteen named colours, each part optionally carrying a few
# named features (door, window, chimney, banner, lamp). It is DATA — nothing
# here is evaluated — and the frontend's headless renderer turns a valid design
# into a sprite.
#
# MIRRORS paw-enterprise ``src/lib/core/terrarium/design.ts`` (PALETTE, the
# limits, and the validation reasons) and the two must stay in lockstep: a
# design this validator accepts is a design that file draws. Change one, change
# the other in the same wave.
#
# ``validate_design`` rebuilds the design from ONLY the known keys, so the
# canonical body a citizen pays for carries no smuggled text: ``name`` is the
# one free-text field, and it is the one the service moderates.

"""The citizen design language: schema, validator, canonical JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PALETTE: tuple[str, ...] = (
    "stone", "slate", "brick", "clay", "sand", "oak", "pine", "moss",
    "ivy", "sky", "sea", "ink", "snow", "gold", "rust", "plum",
)  # fmt: skip
PART_KINDS: tuple[str, ...] = ("box", "prism", "cylinder", "cone")
FEATURE_KINDS: tuple[str, ...] = ("door", "window", "chimney", "banner", "lamp")
FACES: tuple[str, ...] = ("front", "right")
ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)
MAX_PARTS = 24
MAX_FEATURES = 8
MAX_HEIGHT = 4
MAX_FOOTPRINT = 3
MAX_NAME = 40
_EPS = 1e-9


@dataclass(frozen=True)
class Design:
    """A validated design. ``parts`` holds only known keys (see the header)."""

    name: str
    footprint: dict[str, int]
    parts: list[dict[str, Any]]

    def canonical(self) -> str:
        """The body an artifact stores: sorted keys, no whitespace."""
        obj = {"version": 1, "name": self.name, "footprint": self.footprint, "parts": self.parts}
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class DesignError:
    reasons: list[str]

    def __str__(self) -> str:
        return "; ".join(self.reasons)


def _num(v: Any, lo: float, hi: float) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and lo <= v <= hi


def _vec(v: Any) -> bool:
    return (
        isinstance(v, list)
        and len(v) == 3
        and all(isinstance(n, int | float) and not isinstance(n, bool) for n in v)
    )


def _feature(f: Any, at: str, j: int, reasons: list[str]) -> dict[str, Any] | None:
    if not isinstance(f, dict) or f.get("kind") not in FEATURE_KINDS:
        reasons.append(f"{at} feature {j}: kind is one of {', '.join(FEATURE_KINDS)}")
        return None
    face, u, v = f.get("face"), f.get("u"), f.get("v")
    if (
        (face is not None and face not in FACES)
        or (u is not None and not _num(u, 0, 1))
        or (v is not None and not _num(v, 0, 1))
    ):
        reasons.append(f"{at} feature {j}: face is front or right, u and v are 0 to 1")
        return None
    out: dict[str, Any] = {"kind": f["kind"]}
    for key, val in (("face", face), ("u", u), ("v", v)):
        if val is not None:
            out[key] = val
    return out


def _part(p: Any, i: int, fw: int, fd: int, reasons: list[str]) -> dict[str, Any] | None:
    at = f"part {i}"
    if not isinstance(p, dict):
        reasons.append(f"{at} is not an object")
        return None
    if p.get("kind") not in PART_KINDS:
        reasons.append(f"{at}: kind is one of {', '.join(PART_KINDS)}")
    if p.get("color") not in PALETTE:
        reasons.append(f"{at}: color is a palette name, not {json.dumps(p.get('color'))}")
    rotate = p.get("rotate")
    if rotate is not None and (isinstance(rotate, bool) or rotate not in ROTATIONS):
        reasons.append(f"{at}: rotate is 0, 90, 180 or 270")
    if not _vec(p.get("at")) or not _vec(p.get("size")):
        reasons.append(f"{at}: at and size are [x, y, z] numbers")
        return None
    turned = rotate in (90, 270)
    sx, sy, sz = p["size"]
    w, h, d = (sz, sy, sx) if turned else (sx, sy, sz)
    if w <= 0 or h <= 0 or d <= 0:
        reasons.append(f"{at}: size is positive")
    if fw and fd and (w > fw or d > fd):
        reasons.append(f"{at}: no part is larger than the footprint")
    if h > MAX_HEIGHT:
        reasons.append(f"{at}: no part is taller than {MAX_HEIGHT} cells")
    x, y, z = p["at"]
    if x < 0 or y < 0 or z < 0 or (fw and x + w > fw + _EPS) or (fd and z + d > fd + _EPS):
        reasons.append(f"{at}: sits outside the footprint")
    if y + h > MAX_HEIGHT + _EPS:
        reasons.append(f"{at}: rises above {MAX_HEIGHT} cells")
    out: dict[str, Any] = {
        "kind": p.get("kind"),
        "at": list(p["at"]),
        "size": list(p["size"]),
        "color": p.get("color"),
    }
    if rotate is not None:
        out["rotate"] = rotate
    feats = p.get("features")
    if feats is not None:
        if not isinstance(feats, list) or len(feats) > MAX_FEATURES:
            reasons.append(f"{at}: at most {MAX_FEATURES} features")
        else:
            cleaned = [_feature(f, at, j + 1, reasons) for j, f in enumerate(feats)]
            out["features"] = [c for c in cleaned if c is not None]
    return out


def validate_design(obj: Any) -> Design | DesignError:
    """Validate anything a citizen (or a wire) handed over.

    Same rules and same reasons as the frontend's ``validateDesign``; every
    violation is collected so a citizen learns all of them at once.
    """
    if not isinstance(obj, dict):
        return DesignError(["a design is an object"])
    reasons: list[str] = []
    if obj.get("version") != 1:
        reasons.append("version must be 1")
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
        reasons.append(f"name is 1 to {MAX_NAME} characters")
    fp = obj.get("footprint")
    fw = int(fp["w"]) if isinstance(fp, dict) and _num(fp.get("w"), 1, MAX_FOOTPRINT) else 0
    fd = int(fp["d"]) if isinstance(fp, dict) and _num(fp.get("d"), 1, MAX_FOOTPRINT) else 0
    if not fw or not fd:
        reasons.append(f"footprint w and d are each 1 to {MAX_FOOTPRINT} cells")
    parts_in = obj.get("parts")
    parts: list[dict[str, Any]] = []
    if not isinstance(parts_in, list) or not parts_in:
        reasons.append("parts is a non-empty list")
    elif len(parts_in) > MAX_PARTS:
        reasons.append(f"at most {MAX_PARTS} parts")
    else:
        cleaned = [_part(p, i + 1, fw, fd, reasons) for i, p in enumerate(parts_in)]
        parts = [c for c in cleaned if c is not None]
    if reasons:
        return DesignError(reasons)
    return Design(name=str(name), footprint={"w": fw, "d": fd}, parts=parts)


__all__ = [
    "FACES",
    "FEATURE_KINDS",
    "MAX_FEATURES",
    "MAX_FOOTPRINT",
    "MAX_HEIGHT",
    "MAX_PARTS",
    "PALETTE",
    "PART_KINDS",
    "ROTATIONS",
    "Design",
    "DesignError",
    "validate_design",
]
