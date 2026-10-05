# craft_ops.py — the one validator for every craft studio's closed op vocabulary.
#
# Each studio app (vector, photo, design) has a wire contract,
# ``<app>_ops.contract.json`` beside this file, that paw-enterprise carries
# byte-identical for its op applier. This module loads them and validates an op
# batch against the app's contract: closed op set, param names and types,
# required / anyOf / requiredWhen / oneOf rules, the batch cap, and the
# run_command allow/block rules (each contract carries its own block; photo and
# design share a stricter one than vector's). Adding a verb is a contract edit.
#
# Ids (``ids`` / ``id`` / ``layer`` params) must name an object the agent was
# shown in the page's projection (``surface_meta.<app>``); every int ``id`` in
# the projection counts. Geometry is never checked against document state: the
# engine refuses per op and the refusal rides the next send as
# ``last_edit.failures``.
#
# Batch semantics are shared across apps by op name: a created object
# (``_CREATES``) becomes the selection, so a later op that omits ``ids`` acts on
# it; ``delete`` clears the selection; ops after a ``new_document`` may not name
# ids (the old document is gone). All-or-nothing: errors start ``ops[<i>]``.
#
# OP_EXAMPLES / op_cheatsheet(app): one valid example per op, which each studio
# preamble lists so the agent copies exact op and param names (a test pins that
# every op has one and that it validates).

from __future__ import annotations

import json
import math
import re
from functools import cache
from pathlib import Path
from typing import Any

from pocketpaw_ee.agent.mcp_servers.timeline_ops import _suggest

APPS: tuple[str, ...] = ("vector", "photo", "design")
ROUTES: dict[str, str] = {app: f"/studio/{app}" for app in APPS}
_DIR = Path(__file__).parent

_EFFECT_RE = re.compile(r"^[a-z][A-Za-z0-9]*(\.[A-Za-z][A-Za-z0-9]*)+$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# A free-form ``params`` object (run_command, apply_effect) is capped: it is the
# one place the agent can send an arbitrarily large blob to the browser.
_MAX_OBJECT_CHARS = 20_000
_MAX_POINTS = 2_000
_MAX_CURVE_POINTS = 16

_ID_TYPES = ("ids", "id")
_CREATES = frozenset(
    {"draw_shape", "draw_path", "add_text", "group", "add_text_frame", "add_shape"}
)


def contract_path(app: str) -> Path:
    return _DIR / f"{app}_ops.contract.json"


class Contract:
    """One app's parsed contract."""

    def __init__(self, app: str):
        self.app = app
        self.raw: dict[str, Any] = json.loads(contract_path(app).read_text(encoding="utf-8"))
        self.ops: dict[str, dict[str, Any]] = self.raw["ops"]
        self.kinds: frozenset[str] = frozenset(self.ops)
        self.max_ops: int = self.raw["maxOpsPerBatch"]
        run = self.raw["runCommand"]
        self.command_re = re.compile(run["pattern"])
        self.denied_namespaces = frozenset(run["deniedNamespaces"])
        self.denied_segment_re = re.compile(run["deniedSegmentPattern"])


@cache
def contract(app: str) -> Contract:
    if app not in APPS:
        raise KeyError(f"unknown craft app {app!r}")
    return Contract(app)


class CraftSummary:
    """The page's document projection, reduced to what validation needs."""

    def __init__(self, has_document: bool, ids: set[int] | None = None, selection=()):
        self.has_document = has_document
        self.ids = ids or set()
        self.selection = [i for i in selection if isinstance(i, int) and not isinstance(i, bool)]

    @classmethod
    def from_meta(cls, projection: Any) -> CraftSummary:
        if not isinstance(projection, dict):
            return cls(has_document=False)
        ids: set[int] = set()
        stack: list[Any] = [v for k, v in projection.items() if k != "last_edit"]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                node_id = node.get("id")
                if isinstance(node_id, int) and not isinstance(node_id, bool):
                    ids.add(node_id)
                stack.extend(node.values())
        selection = projection.get("selection")
        return cls(True, ids, selection if isinstance(selection, list) else ())


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: Any, floor: int) -> bool:
    return _is_num(value) and float(value).is_integer() and value >= floor


def _unit(value: Any) -> bool:
    return _is_num(value) and 0 <= value <= 1


def paint_error(value: Any) -> str | None:
    """Why ``value`` is not a paint, or None when it is one."""
    if isinstance(value, str) and (value == "none" or _HEX_RE.match(value)):
        return None
    if isinstance(value, list) and len(value) == 3 and all(_unit(v) for v in value):
        return None
    if isinstance(value, dict):
        keys = set(value)
        if keys == {"c", "m", "y", "k"} and all(_unit(v) for v in value.values()):
            return None
        if keys == {"gray"} and _unit(value["gray"]):
            return None
        if keys == {"swatch"} and isinstance(value["swatch"], str) and value["swatch"].strip():
            return None
    return (
        'must be a paint: {"c","m","y","k"} with 0..1 values (preferred for print), '
        '"#rrggbb", "none", [r,g,b] in 0..1, {"gray": 0..1} or {"swatch": "name"}'
    )


def _point_ok(p: Any) -> bool:
    if isinstance(p, list):
        return len(p) == 2 and all(_is_num(v) for v in p)
    return isinstance(p, dict) and _is_num(p.get("x")) and _is_num(p.get("y"))


def _object_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def command_error(command: Any, app: str = "vector") -> str | None:
    """Why a run_command id is refused for ``app``, or None when it is allowed."""
    c = contract(app)
    if not isinstance(command, str) or not c.command_re.match(command):
        return "must be an engine command id such as 'object.align'"
    segments = command.split(".")
    if segments[0] in c.denied_namespaces or any(c.denied_segment_re.match(s) for s in segments):
        return (
            f"{command!r} is not allowed from chat: file, export, document, clipboard, "
            f"preference and app commands are blocked ({', '.join(sorted(c.denied_namespaces))})"
        )
    return None


def _range_error(kind: str, value: Any) -> str | None:
    lo, hi = (float(x) for x in kind[6:].split(".."))
    ok = _is_num(value) and lo <= value <= hi
    return None if ok else f"must be a number from {lo:g} to {hi:g}"


def _type_error(kind: str, value: Any, app: str = "vector") -> str | None:
    """Type check one param. ``ids``/``id`` are resolved by the caller."""
    if kind.startswith("enum:"):
        allowed = kind[5:].split("|")
        return None if value in allowed else f"must be one of {', '.join(allowed)}"
    if kind.startswith("range:"):
        return _range_error(kind, value)
    simple = {
        "number": (_is_num(value), "must be a number"),
        "number0": (_is_num(value) and value >= 0, "must be a number >= 0"),
        "number+": (_is_num(value) and value > 0, "must be a number > 0"),
        "int3": (_is_int(value, 3), "must be a whole number >= 3"),
        "int1": (_is_int(value, 1), "must be a whole number >= 1"),
        "index": (_is_int(value, 0), "must be a whole number >= 0 (0 is the first page)"),
        "text": (isinstance(value, str) and bool(value.strip()), "must be a non-empty string"),
        "bool": (isinstance(value, bool), "must be true or false"),
        "hex": (isinstance(value, str) and bool(_HEX_RE.match(value)), 'must be "#rrggbb"'),
    }
    if kind in simple:
        ok, message = simple[kind]
        return None if ok else message
    if kind == "paint":
        return paint_error(value)
    if kind == "points":
        ok = isinstance(value, list) and 2 <= len(value) <= _MAX_POINTS
        if ok and all(_point_ok(p) for p in value):
            return None
        return f"must be 2..{_MAX_POINTS} points, each [x,y] or {{x,y}}"
    if kind == "curve":
        ok = isinstance(value, list) and 2 <= len(value) <= _MAX_CURVE_POINTS
        if ok and all(
            isinstance(p, list) and len(p) == 2 and all(_is_num(v) and 0 <= v <= 255 for v in p)
            for p in value
        ):
            return None
        return f"must be 2..{_MAX_CURVE_POINTS} [in, out] pairs, each 0..255"
    if kind == "object":
        if not isinstance(value, dict):
            return "must be an object"
        if len(json.dumps(value, default=str)) > _MAX_OBJECT_CHARS:
            return f"is larger than {_MAX_OBJECT_CHARS:,} characters"
        return None
    if kind == "effectId":
        ok = isinstance(value, str) and _EFFECT_RE.match(value)
        return None if ok else "must be an effect id such as 'stylize.dropShadow'"
    if kind == "command":
        return command_error(value, app)
    raise ValueError(f"unknown contract type {kind!r}")  # contract bug, not agent error


def _resolve_ids(
    raw: Any, kind: str, i: int, key: str, summary: CraftSummary
) -> tuple[Any, str | None]:
    values = [raw] if kind == "id" else raw
    if kind == "ids" and (not isinstance(raw, list) or not raw):
        return None, f"ops[{i}].{key} must be a non-empty list of ids."
    out: list[int] = []
    for value in values:
        obj = _object_id(value)
        if obj is None:
            return None, f"ops[{i}].{key}: {value!r} is not an id (ids are integers)."
        if obj not in summary.ids:
            hint = _suggest(str(obj), {str(x) for x in summary.ids})
            return None, (
                f"ops[{i}].{key}: {obj} is not in the document you were shown.{hint} "
                "Copy ids from the document in your context; an object created in this "
                "batch has no id yet, but it is the selection, so omit ids to act on it."
            )
        out.append(obj)
    return (out[0] if kind == "id" else out), None


def _one_of_error(spec: dict[str, Any], op: dict[str, Any], i: int, kind: str) -> str | None:
    groups: list[list[str]] = spec.get("oneOf") or []
    if not groups:
        return None
    complete = [g for g in groups if all(k in op for k in g)]
    options = " or ".join(f"({', '.join(g)})" for g in groups)
    if not complete:
        return f"ops[{i}] ({kind}) needs exactly one of: {options}."
    stray = sorted({k for g in groups for k in g} - set(complete[0]) & set(op))
    if len(complete) > 1 or stray:
        return f"ops[{i}] ({kind}) takes one of {options}, not both; drop {', '.join(stray)}."
    return None


def validate_ops(
    app: str, ops: Any, summary: CraftSummary
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Validate ``app``'s op batch. Returns ``(clean_ops, None)`` or ``(None, error)``."""
    c = contract(app)
    if not summary.has_document:
        return None, (
            f"No {app} document is open. The {ROUTES[app]} editor must be open "
            "before it can be edited. Ask the user to open it."
        )
    if not isinstance(ops, list):
        return None, "`ops` must be a list of operations."
    if not ops:
        return None, "`ops` was empty — nothing to do."
    if len(ops) > c.max_ops:
        return None, (
            f"`ops` had {len(ops)} operations; the maximum is {c.max_ops}. "
            "Split the work across turns."
        )

    clean: list[dict[str, Any]] = []
    has_selection = bool(summary.selection)
    replaced = False  # a new_document earlier in this batch

    for i, raw in enumerate(ops):
        if not isinstance(raw, dict):
            return None, f"ops[{i}] must be an object."
        kind = raw.get("op")
        if not isinstance(kind, str):
            return None, f"ops[{i}].op is missing."
        if kind not in c.kinds:
            return None, (
                f"ops[{i}].op {kind!r} is not a {app} operation.{_suggest(kind, set(c.kinds))} "
                f"Valid operations: {', '.join(sorted(c.kinds))}."
            )
        spec = c.ops[kind]
        params: dict[str, str] = spec["params"]
        # None means "not given": models send explicit nulls for optional fields.
        op = {k: v for k, v in raw.items() if v is not None}

        for key in op:
            if key != "op" and key not in params:
                allowed = ", ".join(params) or "none"
                return None, (
                    f"ops[{i}] ({kind}) has no param {key!r}.{_suggest(key, set(params))} "
                    f"Allowed: {allowed}."
                )
        required = list(spec.get("required", []))
        for field, table in spec.get("requiredWhen", {}).items():
            required += table.get(op.get(field), [])
        for key in required:
            if key not in op:
                return None, f"ops[{i}] ({kind}) is missing required param {key!r}."
        any_of = spec.get("anyOf")
        if any_of and not any(k in op for k in any_of):
            return None, f"ops[{i}] ({kind}) needs at least one of: {', '.join(any_of)}."
        err = _one_of_error(spec, op, i, kind)
        if err:
            return None, err

        for key, value in list(op.items()):
            if key == "op":
                continue
            ptype = params[key]
            if ptype in _ID_TYPES:
                if replaced:
                    return None, (
                        f"ops[{i}].{key}: a new_document earlier in this batch replaced the "
                        "document, so no id from before it exists. Omit ids to act on "
                        "the selection, or split the work across turns."
                    )
                resolved, err = _resolve_ids(value, ptype, i, key, summary)
                if err:
                    return None, err
                op[key] = resolved
                continue
            err = _type_error(ptype, value, app)
            if err:
                return None, f"ops[{i}].{key} {err}."

        if "ids" in params and "ids" not in op and not has_selection:
            return None, (f"ops[{i}] ({kind}) has no `ids` and nothing is selected. Pass the ids.")

        if kind in _CREATES:
            has_selection = True
        elif kind == "delete":
            has_selection = False
        elif kind == "new_document":
            has_selection, replaced = False, True
        clean.append(op)
    return clean, None


# One valid example per op, rendered into each studio preamble so the agent
# copies exact op and param names instead of guessing (on vector it tried
# set_fill and stroke_width before the list existed). A test pins that every
# contract op has an example and that each one passes validate_ops.
_K = {"c": 0, "m": 0, "y": 0, "k": 1}
OP_EXAMPLES: dict[str, dict[str, dict[str, Any]]] = {
    "vector": {
        "draw_shape": {"op": "draw_shape", "shape": "rectangle", "x": 9, "y": 9, "width": 234,
                       "height": 126, "fill": "none",
                       "stroke": {"c": 0, "m": 0.2, "y": 0.7, "k": 0.15}, "strokeWidth": 2},
        "draw_path": {"op": "draw_path", "points": [[0, 0], [40, 0], [20, 30]], "closed": True,
                      "fill": "#e53935"},
        "add_text": {"op": "add_text", "text": "शुभ विवाह", "x": 126, "y": 60, "size": 24,
                     "color": _K, "align": "center"},
        "set_paint": {"op": "set_paint", "ids": [2], "fill": {"c": 0, "m": 1, "y": 1, "k": 0},
                      "stroke": "none", "strokeWidth": 0},
        "transform": {"op": "transform", "ids": [2], "dx": 10, "dy": -4, "rotate": 15},
        "align": {"op": "align", "ids": [3], "align": "center", "to": "artboard"},
        "arrange": {"op": "arrange", "ids": [2], "order": "back"},
        "group": {"op": "group", "ids": [2, 3]},
        "ungroup": {"op": "ungroup", "ids": [2]},
        "delete": {"op": "delete", "ids": [3]},
        "set_text": {"op": "set_text", "id": 3, "text": "Sharma Prints", "size": 18,
                     "color": "#1a237e"},
        "apply_effect": {"op": "apply_effect", "ids": [2], "effect": "stylize.dropShadow"},
        "pathfinder": {"op": "pathfinder", "ids": [2, 3], "operation": "unite"},
        "new_document": {"op": "new_document", "width": 252, "height": 144, "units": "Inches",
                         "colorMode": "cmyk"},
        "set_artboard": {"op": "set_artboard", "width": 360, "height": 504},
        "run_command": {"op": "run_command", "command": "select.all", "params": {}},
    },
    "photo": {
        "adjust_levels": {"op": "adjust_levels", "inBlack": 12, "inWhite": 240, "gamma": 1.1},
        "adjust_curves": {"op": "adjust_curves", "points": [[0, 0], [128, 150], [255, 255]]},
        "brightness_contrast": {"op": "brightness_contrast", "brightness": 15, "contrast": 10},
        "hue_saturation": {"op": "hue_saturation", "saturation": 12},
        "sharpen": {"op": "sharpen", "amount": 60, "radius": 1.2},
        "auto_tone": {"op": "auto_tone"},
        "crop": {"op": "crop", "preset": "passport_35x45"},
        "resize": {"op": "resize", "width": 1200, "height": 1800, "resolution": 300},
        "rotate": {"op": "rotate", "by": "90cw"},
        "flip": {"op": "flip", "axis": "horizontal"},
        "select_subject": {"op": "select_subject"},
        "select_all": {"op": "select_all"},
        "deselect": {"op": "deselect"},
        "fill_selection": {"op": "fill_selection", "color": "#ffffff", "opacity": 1},
        "add_text": {"op": "add_text", "text": "राम स्टूडियो", "x": 40, "y": 520, "size": 28,
                     "color": "#ffffff", "align": "left"},
        "new_layer": {"op": "new_layer", "name": "Background fill"},
        "delete_layer": {"op": "delete_layer", "layer": 3},
        "set_layer_opacity": {"op": "set_layer_opacity", "layer": 2, "opacity": 0.6},
        "set_layer_visibility": {"op": "set_layer_visibility", "layer": 2, "visible": False},
        "rename_layer": {"op": "rename_layer", "layer": 2, "name": "Retouch"},
        "run_command": {"op": "run_command", "command": "filter.noise.reduceNoise", "params": {}},
    },
    "design": {
        "new_document": {"op": "new_document", "size": "card", "bleed": 3, "pages": 2},
        "add_page": {"op": "add_page", "after": 0},
        "delete_page": {"op": "delete_page", "page": 1},
        "add_text_frame": {"op": "add_text_frame", "page": 0, "x": 8, "y": 8, "width": 74,
                           "height": 12, "text": "शर्मा प्रिंटर्स", "size": 16,
                           "font": "Noto Sans Devanagari", "color": _K, "align": "left"},
        "add_shape": {"op": "add_shape", "page": 0, "shape": "rectangle", "x": -3, "y": -3,
                      "width": 96, "height": 18, "fill": {"c": 0, "m": 0.6, "y": 1, "k": 0},
                      "stroke": "none"},
        "set_text": {"op": "set_text", "id": 7, "text": "Sharma Prints", "size": 14},
        "set_paint": {"op": "set_paint", "ids": [5], "fill": {"c": 1, "m": 0.5, "y": 0, "k": 0},
                      "stroke": "none"},
        "transform": {"op": "transform", "ids": [5], "dx": 2, "dy": -1.5},
        "align": {"op": "align", "ids": [7], "align": "center", "to": "page"},
        "arrange": {"op": "arrange", "ids": [5], "order": "back"},
        "delete": {"op": "delete", "ids": [7]},
        "set_bleed": {"op": "set_bleed", "bleed": 3},
        "run_command": {"op": "run_command", "command": "object.qrCode",
                        "params": {"content": "https://example.com"}},
    },
}  # fmt: skip


def op_cheatsheet(app: str) -> str:
    """``app``'s exact op vocabulary, one example line per op (contract order)."""
    return "\n".join(
        f"  {json.dumps(OP_EXAMPLES[app][op], ensure_ascii=False, separators=(',', ':'))}"
        for op in contract(app).ops
    )


__all__ = [
    "APPS",
    "OP_EXAMPLES",
    "ROUTES",
    "Contract",
    "CraftSummary",
    "command_error",
    "contract",
    "contract_path",
    "op_cheatsheet",
    "paint_error",
    "validate_ops",
]
