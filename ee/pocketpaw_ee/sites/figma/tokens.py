# ee/pocketpaw_ee/sites/figma/tokens.py: design tokens from a Figma frame, by role.
#
# ``derive`` turns what a design actually uses into the roles a Paw Site template
# themes: colours (canvas, surface, ink, muted, accent, border), a radius scale, a
# spacing scale, a type ramp, the display and body fonts and one shadow. Two
# sources, in order:
#   1. Enterprise local variables (``/files/:key/variables/local``) when the grant
#      has ``file_variables:read``: a variable whose NAME says its role wins, valued
#      in its collection's default mode, aliases followed one level;
#   2. otherwise (and for every role variables left open) the frame's own nodes:
#      solid fills weighted by area, text fills weighted by text length and size,
#      the most saturated fill for the accent, strokes for borders, and the
#      distinct corner radii, auto-layout gaps / paddings and font sizes in use.
# Every token says ``observed`` (with where it came from) or ``substituted`` (not
# in the design, value null: the template keeps its own). ``css`` renders the
# observed ones as a ``:root`` block and a Tailwind 4 ``@theme`` block for the
# agent to map onto the template's own names. Layer names never reach the output;
# font family names are cleaned and capped like any design text.

from __future__ import annotations

import colorsys
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from pocketpaw_ee.sites.figma.outline import clean, hex_color

MAX_NODES = 2000
COLOR_ROLES = ("canvas", "surface", "ink", "muted", "accent", "border")
_ROLE_WORDS = {
    "canvas": ("background", "canvas", "bg", "page"),
    "surface": ("surface", "card", "panel", "elevated"),
    "ink": ("text", "ink", "foreground", "fg", "on-surface", "body"),
    "muted": ("muted", "subtle", "secondary-text", "text-secondary", "tertiary"),
    "accent": ("accent", "primary", "brand", "action", "cta"),
    "border": ("border", "stroke", "divider", "outline"),
}
_SIZE_NAMES = ("display", "h1", "h2", "h3", "body", "small", "caption")
_RADIUS_NAMES = ("sm", "md", "lg", "xl")


def _rgb(hex_value: str) -> tuple[int, int, int]:
    return tuple(int(hex_value[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _close(a: str, b: str, limit: int = 10) -> bool:
    return sum(abs(x - y) for x, y in zip(_rgb(a), _rgb(b), strict=True)) <= limit


def _saturation(hex_value: str) -> float:
    r, g, b = (c / 255 for c in _rgb(hex_value))
    _, lightness, sat = colorsys.rgb_to_hls(r, g, b)
    return sat if 0.12 < lightness < 0.9 else 0.0


def _luminance(hex_value: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(hex_value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return round((la + 0.05) / (lb + 0.05), 2)


class _Weights:
    """Colour weights with near-identical shades folded into the heaviest one."""

    def __init__(self) -> None:
        self.weight: dict[str, float] = defaultdict(float)

    def add(self, colour: str, weight: float) -> None:
        if not re.fullmatch(r"#[0-9a-f]{6}", colour) or weight <= 0:
            return
        for known in self.weight:
            if _close(known, colour):
                self.weight[known] += weight
                return
        self.weight[colour] += weight

    def ranked(self) -> list[str]:
        return [c for c, _ in sorted(self.weight.items(), key=lambda kv: -kv[1])]


def _solid(paints: Any) -> str:
    for paint in paints if isinstance(paints, list) else []:
        if isinstance(paint, Mapping) and paint.get("type") == "SOLID":
            if paint.get("visible") is False:
                continue
            colour = hex_color(paint.get("color"))
            if colour:
                return colour[:7]
    return ""


def _area(node: Mapping[str, Any]) -> float:
    box = node.get("absoluteBoundingBox")
    if not isinstance(box, Mapping):
        return 0.0
    try:
        return max(0.0, float(box.get("width", 0))) * max(0.0, float(box.get("height", 0)))
    except (TypeError, ValueError):
        return 0.0


class _Collect:
    def __init__(self) -> None:
        self.seen = 0
        self.fills = _Weights()
        self.text = _Weights()
        self.accent = _Weights()
        self.strokes = _Weights()
        self.root_fill = ""
        self.radii: Counter[float] = Counter()
        self.gaps: Counter[float] = Counter()
        self.sizes: Counter[float] = Counter()
        self.size_family: dict[float, Counter[str]] = defaultdict(Counter)
        self.family_chars: Counter[str] = Counter()
        self.shadows: Counter[str] = Counter()

    def walk(self, node: Mapping[str, Any], root: bool = False) -> None:
        if self.seen >= MAX_NODES or node.get("visible") is False:
            return
        self.seen += 1
        fill = _solid(node.get("fills"))
        if node.get("type") == "TEXT":
            chars = len(str(node.get("characters") or ""))
            style = node.get("style") if isinstance(node.get("style"), Mapping) else {}
            size = style.get("fontSize")
            if fill:
                self.text.add(fill, chars * float(size or 16))
            if isinstance(size, int | float) and size > 0:
                size = round(float(size), 1)
                self.sizes[size] += chars or 1
                family = clean(style.get("fontFamily"), 60)
                if family:
                    self.size_family[size][family] += chars or 1
                    self.family_chars[family] += chars or 1
        elif fill:
            if root:
                self.root_fill = fill
            area = _area(node)
            self.fills.add(fill, area)
            if _saturation(fill) > 0.35:
                self.accent.add(fill, 1 + min(area, 40_000) / 40_000)
        stroke = _solid(node.get("strokes"))
        if stroke:
            self.strokes.add(stroke, 1)
        radius = node.get("cornerRadius")
        if isinstance(radius, int | float) and radius > 0:
            self.radii[round(float(radius), 1)] += 1
        for key in ("itemSpacing", "paddingLeft", "paddingRight", "paddingTop", "paddingBottom"):
            value = node.get(key)
            if isinstance(value, int | float) and value > 0:
                self.gaps[round(float(value), 1)] += 1
        for effect in node.get("effects") or []:
            if (
                isinstance(effect, Mapping)
                and effect.get("type") == "DROP_SHADOW"
                and effect.get("visible") is not False
            ):
                offset = effect.get("offset") if isinstance(effect.get("offset"), Mapping) else {}
                colour = hex_color(effect.get("color")) or "#0000001a"
                self.shadows[
                    f"{_px(offset.get('x'))} {_px(offset.get('y'))} "
                    f"{_px(effect.get('radius'))} {_px(effect.get('spread'))} {colour}"
                ] += 1
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                self.walk(child)


def _px(value: Any) -> str:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{round(float(value), 1):g}px"
    return "0px"


def _token(value: Any, source: str) -> dict[str, Any]:
    return {"value": value, "status": "observed", "source": source}


def _missing(note: str = "not in the design: keep the template's value") -> dict[str, Any]:
    return {"value": None, "status": "substituted", "source": note}


# ---------------------------------------------------------------- variables


def _variable_value(var: Mapping[str, Any], variables: Mapping[str, Any], modes: Mapping) -> Any:
    collection = str(var.get("variableCollectionId") or "")
    mode = modes.get(collection)
    values = var.get("valuesByMode") if isinstance(var.get("valuesByMode"), Mapping) else {}
    value = values.get(mode) if mode in values else next(iter(values.values()), None)
    if isinstance(value, Mapping) and value.get("type") == "VARIABLE_ALIAS":
        target = variables.get(str(value.get("id") or ""))
        if isinstance(target, Mapping):
            return _variable_value(target, {}, modes)
        return None
    return value


def from_variables(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    meta = body.get("meta") if isinstance(body.get("meta"), Mapping) else {}
    variables = meta.get("variables") if isinstance(meta.get("variables"), Mapping) else {}
    collections = meta.get("variableCollections")
    collections = collections if isinstance(collections, Mapping) else {}
    modes = {
        cid: str(c.get("defaultModeId") or "")
        for cid, c in collections.items()
        if isinstance(c, Mapping)
    }
    out: dict[str, dict[str, Any]] = {}
    radii: list[float] = []
    gaps: list[float] = []
    for var in list(variables.values())[:MAX_NODES]:
        if not isinstance(var, Mapping) or var.get("hiddenFromPublishing"):
            continue
        name = clean(var.get("name"), 80).lower().replace(" ", "-")
        value = _variable_value(var, variables, modes)
        kind = var.get("resolvedType")
        if kind == "COLOR" and isinstance(value, Mapping):
            colour = hex_color(value)
            for role in COLOR_ROLES:
                key = f"color.{role}"
                if key not in out and any(w in name for w in _ROLE_WORDS[role]):
                    out[key] = _token(colour, f"variable {clean(var.get('name'), 80)}")
                    break
        elif kind == "FLOAT" and isinstance(value, int | float):
            if "radius" in name or "corner" in name:
                radii.append(float(value))
            elif any(w in name for w in ("spacing", "space", "gap", "padding")):
                gaps.append(float(value))
    if radii:
        out["radius"] = _token(_scale(sorted(set(radii)), _RADIUS_NAMES), "variables")
    if gaps:
        out["space"] = _token(sorted(set(gaps))[:8], "variables")
    return out


# ------------------------------------------------------------------- nodes


def _scale(values: list[float], names: tuple[str, ...]) -> dict[str, float]:
    small = [v for v in values if v < 999][: len(names)]
    out = dict(zip(names, small, strict=False))
    if any(v >= 999 for v in values):
        out["full"] = 9999
    return out


def from_nodes(nodes_body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    collect = _Collect()
    nodes = nodes_body.get("nodes") if isinstance(nodes_body.get("nodes"), Mapping) else {}
    for entry in nodes.values():
        doc = entry.get("document") if isinstance(entry, Mapping) else None
        if isinstance(doc, Mapping):
            collect.walk(doc, root=True)
    out: dict[str, dict[str, Any]] = {}
    fills = collect.fills.ranked()
    canvas = collect.root_fill or (fills[0] if fills else "")
    if canvas:
        out["color.canvas"] = _token(canvas, "the frame's own fill")
    surface = next((c for c in fills if canvas and not _close(c, canvas, 6)), "")
    if surface and collect.fills.weight[surface] > 0:
        out["color.surface"] = _token(surface, "the largest fill after the canvas")
    text = collect.text.ranked()
    if text:
        out["color.ink"] = _token(text[0], "the colour of most text")
        if canvas:
            out["color.ink"]["contrast_on_canvas"] = contrast(text[0], canvas)
    if len(text) > 1:
        out["color.muted"] = _token(text[1], "the second text colour")
    accent = collect.accent.ranked()
    if accent:
        out["color.accent"] = _token(accent[0], "the most used saturated fill")
    strokes = collect.strokes.ranked()
    if strokes:
        out["color.border"] = _token(strokes[0], "the most used stroke")
    if collect.radii:
        out["radius"] = _token(
            _scale(sorted(collect.radii), _RADIUS_NAMES), "corner radii in the frame"
        )
    if collect.gaps:
        common = [v for v, _ in collect.gaps.most_common(8)]
        out["space"] = _token(sorted(common), "auto-layout gaps and padding")
    if collect.sizes:
        sizes = sorted(collect.sizes, reverse=True)[: len(_SIZE_NAMES)]
        body_size = collect.sizes.most_common(1)[0][0]
        ramp: dict[str, float] = {}
        larger = [s for s in sizes if s > body_size]
        smaller = [s for s in sizes if s < body_size]
        for name, size in zip(("display", "h1", "h2", "h3"), larger, strict=False):
            ramp[name] = size
        ramp["body"] = body_size
        for name, size in zip(("small", "caption"), sorted(smaller, reverse=True), strict=False):
            ramp[name] = size
        out["type"] = _token(ramp, "font sizes in use; body is the most used")
        largest = sizes[0]
        display = collect.size_family[largest].most_common(1)
        if display:
            out["font.display"] = _token(display[0][0], f"the {largest:g}px text")
        body_family = collect.family_chars.most_common(1)
        if body_family:
            out["font.body"] = _token(body_family[0][0], "the face of most text")
    if collect.shadows:
        out["shadow"] = _token(collect.shadows.most_common(1)[0][0], "the most used drop shadow")
    return out


# ------------------------------------------------------------------- merge

_ALL_ROLES = (
    *(f"color.{r}" for r in COLOR_ROLES),
    "radius",
    "space",
    "type",
    "font.display",
    "font.body",
    "shadow",
)


def derive(
    nodes_body: Mapping[str, Any], variables_body: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Every role, observed from variables first, then from the nodes, else substituted."""
    from_vars = from_variables(variables_body) if variables_body else {}
    from_tree = from_nodes(nodes_body)
    return {role: from_vars.get(role) or from_tree.get(role) or _missing() for role in _ALL_ROLES}


def _family(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9 \-]", "", name)[:60].strip()
    return f'"{safe}"' if safe else ""


def css(tokens: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """The observed tokens as ``:root`` and Tailwind 4 ``@theme`` declarations."""
    root: list[str] = []
    theme: list[str] = []
    for role in COLOR_ROLES:
        value = tokens.get(f"color.{role}", {}).get("value")
        if isinstance(value, str) and re.fullmatch(r"#[0-9a-f]{6,8}", value):
            root.append(f"  --color-{role}: {value};")
            theme.append(f"  --color-{role}: {value};")
    radius = tokens.get("radius", {}).get("value")
    if isinstance(radius, Mapping):
        for name, value in radius.items():
            root.append(f"  --radius-{name}: {value:g}px;")
            theme.append(f"  --radius-{name}: {value:g}px;")
    space = tokens.get("space", {}).get("value")
    if isinstance(space, list):
        for n, value in enumerate(space, start=1):
            root.append(f"  --space-{n}: {value:g}px;")
        if space:
            theme.append(f"  --spacing: {min(space):g}px;")
    ramp = tokens.get("type", {}).get("value")
    if isinstance(ramp, Mapping):
        for name, value in ramp.items():
            root.append(f"  --text-{name}: {value:g}px;")
            theme.append(f"  --text-{name}: {value:g}px;")
    for role in ("display", "body"):
        value = tokens.get(f"font.{role}", {}).get("value")
        family = _family(value) if isinstance(value, str) else ""
        if family:
            serif = "serif" in value.lower() and "sans" not in value.lower()
            generic = "ui-serif, serif" if serif else "ui-sans-serif, system-ui, sans-serif"
            stack = f"{family}, {generic}"
            root.append(f"  --font-{role}: {stack};")
            theme.append(f"  --font-{role}: {stack};")
    shadow = tokens.get("shadow", {}).get("value")
    if isinstance(shadow, str) and re.fullmatch(r"[0-9a-f#. px-]+", shadow):
        root.append(f"  --shadow-card: {shadow};")
        theme.append(f"  --shadow-card: {shadow};")
    return {
        "root": ":root {\n" + "\n".join(root) + "\n}",
        "theme": "@theme {\n" + "\n".join(theme) + "\n}",
    }
