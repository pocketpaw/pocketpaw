# ee/pocketpaw_ee/sites/design_extract.py — read a source site's design language
# out of its own stylesheets and fill the crew's ``DesignSystem`` with it.
#
# Created 2026-09-04 (IR-4, feat/sites-import-design-tokens).
#
# WHY STYLESHEETS AND NOT A SCREENSHOT: the IR-1 spike measured five sites
# spanning token-driven, utility-first, WordPress-era, a client-rendered app and
# pre-tokens plain CSS. Every one yielded palette, fonts, type scale and spacing
# from its stylesheets alone; radii on four, and the fifth (Hacker News) has no
# rounded corners anywhere, so reporting none is correct rather than a gap. That
# takes the metered browser call out of the critical path for tokens.
#
# WHY IT IS NOT JUST A LIST OF EVERY VALUE: a real site declares far more than a
# brief can use — 312 colour tokens on tailwindcss.com, 91 on excalidraw.com,
# against the six or so a design brief needs. Extraction is the easy half; the
# work is SELECTION. Ranking a token by how often the rest of the CSS references
# it picked the right head on every site tested, down to the accent.
#
# FOUR THINGS THAT DEFEAT A NAIVE READ, each measured rather than assumed:
#   * ``var()`` indirection — ``--default-font-family: var(--font-sans)``, and 71
#     of Excalidraw's 235 properties are pure indirection. Unresolved, the brief
#     records the string "var(--font-inter)" as a typeface.
#   * ``@font-face`` is where the real typeface lives. On the probe site
#     ``--font-sans`` resolves to a generic system stack and the actual face,
#     Roobert Mono, appears only in an ``@font-face`` rule and a preload link.
#   * shadow tokens carry colour-like values, so ``--tw-shadow`` outranks real
#     colours unless excluded by name.
#   * custom properties alone left three of the five sites short on at least one
#     family, and covered nothing at all on the pre-tokens site, so mining
#     ordinary declarations is a first-class path and not a degraded one.
"""Extract a source site's design tokens from its stylesheets."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from pocketpaw_ee.sites_crew.models import ColorScale, DesignSystem, Typography

# How many of each family survive selection. A brief needs a palette, not a
# design system dump: the agent is being asked to match a look, and a hundred
# near-identical greys describe nothing.
MAX_PALETTE = 8
MAX_TYPE_STEPS = 8
MAX_SPACING = 8
MAX_RADII = 5
MAX_SHADOWS = 3
# Cap what rides into the prompt. The whole point is that these values reach the
# model, so the budget is real: ~4KB of custom properties is a generous ceiling
# for one page's design language and still small beside the preamble.
MAX_TOKENS_CSS_CHARS = 4000
# Depth cap on var() chains. Real chains are two or three deep; anything longer
# is a cycle or a mistake, and either way the literal is not worth more passes.
_VAR_DEPTH = 6

_CUSTOM_PROP = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;{}]+)")
_VAR_REF = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,([^()]*))?\)")
_FONT_FACE = re.compile(r"@font-face\s*\{([^}]*)\}", re.I)
_FONT_FAMILY = re.compile(r"font-family\s*:\s*([^;}]+)", re.I)
_FONT_SIZE = re.compile(r"font-size\s*:\s*([^;}]+)", re.I)
_FONT_WEIGHT = re.compile(r"font-weight\s*:\s*([^;}]+)", re.I)
_RADIUS = re.compile(r"border-radius\s*:\s*([^;}]+)", re.I)
_SHADOW = re.compile(r"box-shadow\s*:\s*([^;}]+)", re.I)
_SPACING = re.compile(
    r"\b(?:padding|margin|gap|row-gap|column-gap)"
    r"(?:-(?:top|right|bottom|left|inline|block))?\s*:\s*([^;{}]+)",
    re.I,
)
_COLOR_DECL = re.compile(
    r"\b(?:color|background-color|background|border-color|fill|stroke)\s*:\s*([^;{}]+)", re.I
)
_COLORISH = re.compile(
    r"(#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)|oklch\([^)]*\)"
    r"|oklab\([^)]*\)|lab\([^)]*\))"
)
_LENGTH = re.compile(r"(-?\d*\.?\d+)(rem|em|px|pt)\b")

# Names whose VALUES look like colours but are not palette entries. Shadows are
# the measured offender; transitions and gradients are the same class.
_NOT_A_COLOUR_TOKEN = re.compile(r"(shadow|transition|gradient|filter|outline|glow)", re.I)
# Generic families are fallbacks, not the brand's typeface.
_GENERIC_FAMILIES = {
    "sans-serif",
    "serif",
    "monospace",
    "system-ui",
    "ui-sans-serif",
    "ui-monospace",
    "ui-serif",
    "ui-rounded",
    "cursive",
    "fantasy",
    "inherit",
    "initial",
    "unset",
    "-apple-system",
    "blinkmacsystemfont",
    "segoe ui",
    "roboto",
    "helvetica",
    "helvetica neue",
    "arial",
    # Emoji and symbol faces ride at the tail of nearly every modern font stack.
    # They are not anyone's brand typeface, and taking the first NON-generic name
    # picked "Apple Color Emoji" as the body font on the first site tested.
    "apple color emoji",
    "segoe ui emoji",
    "segoe ui symbol",
    "noto color emoji",
    "android emoji",
    "emojisymbols",
}


def _resolve(value: str, props: dict[str, str], depth: int = 0) -> str:
    """Chase ``var()`` references to a literal, honouring the fallback argument."""
    if depth >= _VAR_DEPTH:
        return value
    match = _VAR_REF.search(value)
    if not match:
        return value
    target = props.get(match.group(1))
    if target is None:
        target = (match.group(2) or "").strip()
    return _resolve(value[: match.start()] + target + value[match.end() :], props, depth + 1)


def _first_family(value: str) -> str:
    """The first named family in a stack, or "" when it is only generics."""
    for part in value.split(","):
        name = part.strip().strip("'\"")
        if name and name.lower() not in _GENERIC_FAMILIES and not name.startswith("var("):
            return name
    return ""


def _hex_to_rgb(value: str) -> tuple[float, float, float] | None:
    """Parse a hex colour to 0..1 RGB. Only hex — comparing an oklch() to a
    hex() honestly needs a colour library, and guessing at it would merge
    colours that are not alike."""
    text = value.strip()
    if not text.startswith("#"):
        return None
    digits = text[1:]
    if len(digits) in (3, 4):
        digits = "".join(c * 2 for c in digits[:3])
    elif len(digits) in (6, 8):
        digits = digits[:6]
    else:
        return None
    try:
        return tuple(int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def _oklab(value: str) -> tuple[float, float, float] | None:
    """sRGB hex to OKLab, for merging colours that differ by less than the eye
    can see. The market's tools cluster in this space rather than in RGB because
    RGB distance does not track perceived difference — two greys a hair apart in
    OKLab really are the same grey, and two blues the same RGB distance apart
    are not."""
    rgb = _hex_to_rgb(value)
    if rgb is None:
        return None

    def _linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_linear(c) for c in rgb)
    lms = (
        0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
        0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
        0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
    )
    l_, m_, s_ = (c ** (1 / 3) if c > 0 else -((-c) ** (1 / 3)) for c in lms)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _merge_near_duplicates(
    ranked: list[tuple[str, str]], threshold: float = 0.02
) -> list[tuple[str, str]]:
    """Drop colours that are perceptually the same as one already kept.

    Ranked input, so the first occurrence wins and the near-duplicate behind it
    goes. A palette of eight that is really three colours and five shades of one
    of them tells the agent nothing about the brand.
    """
    kept: list[tuple[str, str]] = []
    kept_lab: list[tuple[float, float, float]] = []
    for name, value in ranked:
        lab = _oklab(value)
        if lab is None:
            # Not a hex we can compare. Keep it rather than guess: an oklch()
            # accent dropped for resembling nothing is worse than one extra.
            kept.append((name, value))
            continue
        if any(
            sum((a - b) ** 2 for a, b in zip(lab, other, strict=True)) ** 0.5 < threshold
            for other in kept_lab
        ):
            continue
        kept.append((name, value))
        kept_lab.append(lab)
    return kept


def _to_px(value: str) -> float:
    """One length in approximate pixels, for ordering a scale. Approximate is
    the right precision here: the job is "which of these is bigger", not layout."""
    match = _LENGTH.match(value)
    if not match:
        return 0.0
    number, unit = float(match.group(1)), match.group(2)
    return number * {"rem": 16, "em": 16, "pt": 1.333, "px": 1}.get(unit, 1)


def _cluster_lengths(values: list[str], limit: int, *, positive_only: bool = False) -> list[str]:
    """Order length literals by how often they appear, then by size.

    Frequency first because the values a site leans on are its scale; the
    one-off 13px is noise. Sorted by magnitude after selection so the result
    reads as a scale rather than as a histogram.
    """
    if positive_only:
        # Negative margins pull things around; they are not a spacing SCALE, and
        # -100px at the head of one reads as an instruction to overlap.
        values = [v for v in values if not v.startswith("-") and not v.startswith("0")]
    counts = Counter(v for v in values if v)
    head = [v for v, _ in counts.most_common(limit * 3)][: limit * 3]

    return sorted(set(head), key=_to_px)[:limit]


def extract_design_system(
    stylesheets: dict[str, str], *, name: str = "source"
) -> tuple[DesignSystem, list[str]]:
    """Read a design system out of a site's own CSS.

    ``stylesheets`` maps a path to its text; inline ``<style>`` blocks belong in
    here too. Returns the system plus notes worth surfacing on the import report
    — a substituted font, a family we could not name.
    """
    css = "\n".join(stylesheets.values())
    notes: list[str] = []
    if not css.strip():
        notes.append("the source shipped no stylesheet we could read")
        return DesignSystem(name=name), notes

    props: dict[str, str] = {}
    for key, raw in _CUSTOM_PROP.findall(css):
        props[key] = raw.strip()
    resolved = {k: _resolve(v, props) for k, v in props.items()}

    # ---- Palette: declared tokens ranked by reference count, else mined. ----
    colour_tokens = {
        k: v
        for k, v in resolved.items()
        if _COLORISH.search(v) and not _NOT_A_COLOUR_TOKEN.search(k)
    }
    ranked: list[tuple[str, str]] = []
    if colour_tokens:
        refs = {k: len(re.findall(re.escape(k) + r"\s*[,)]", css)) for k in colour_tokens}
        ranked = [(k, colour_tokens[k]) for k in sorted(colour_tokens, key=lambda k: (-refs[k], k))]
    else:
        # No custom properties at all — a pre-tokens site. Mine the declarations
        # themselves; the rules a site emitted are still the colours it uses.
        used: Counter[str] = Counter()
        for decl in _COLOR_DECL.findall(css):
            used.update(m.lower() for m in _COLORISH.findall(_resolve(decl, props)))
        ranked = [
            (f"color-{i + 1}", v) for i, (v, _) in enumerate(used.most_common(MAX_PALETTE * 3))
        ]
        if ranked:
            notes.append(
                "the source declares no design tokens; its palette was read "
                "from the rules it emitted"
            )

    palette = _merge_near_duplicates(ranked)[:MAX_PALETTE]
    colors: dict[str, ColorScale] = {}
    for index, (token, value) in enumerate(palette):
        # The steps are a shape the crew model already speaks; a real site does
        # not publish a 50..900 ramp, so each colour lands on its own 500 with
        # the token's own name kept as the role. Renaming them "primary" and
        # "secondary" would be inventing a hierarchy the source never stated.
        role = token.lstrip("-") or f"color-{index + 1}"
        colors[role] = ColorScale(**{"500": value.strip()})

    # ---- Typography: @font-face first, because that is the real typeface. ----
    faces: list[str] = []
    for body in _FONT_FACE.findall(css):
        found = _FONT_FAMILY.search(body)
        if found:
            family = found.group(1).strip().strip("'\"")
            if family and family not in faces:
                faces.append(family)
    stack_families = [
        f for f in (_first_family(_resolve(v, props)) for v in _FONT_FAMILY.findall(css)) if f
    ]
    sizes = _cluster_lengths(
        [
            f"{n}{u}"
            for v in _FONT_SIZE.findall(css)
            for n, u in _LENGTH.findall(_resolve(v, props))
        ],
        MAX_TYPE_STEPS,
    )
    weights = [w.strip() for v in _FONT_WEIGHT.findall(css) for w in [v] if v.strip().isdigit()]
    # ``sizes`` is the frequency-ranked SCALE, which is what a body size should
    # come from. A display size is rare by definition, so it falls out of that
    # ranking — the first site tested reported a 14px heading. Take the largest
    # size the stylesheet sets anywhere instead.
    all_sizes = [
        f"{n}{u}" for v in _FONT_SIZE.findall(css) for n, u in _LENGTH.findall(_resolve(v, props))
    ]
    display = max(all_sizes, key=_to_px, default="")

    heading = faces[0] if faces else (stack_families[0] if stack_families else "")
    body_family = ""
    for candidate in [*faces[1:], *stack_families]:
        if candidate != heading:
            body_family = candidate
            break
    typography: dict[str, Typography] = {}
    if heading:
        typography["heading"] = Typography(
            family=heading,
            size=display or (sizes[-1] if sizes else ""),
            weight=max(weights, default="") or "",
        )
    if body_family or heading:
        typography["body"] = Typography(
            family=body_family or heading,
            size=sizes[len(sizes) // 2] if sizes else "",
        )
    if not typography:
        notes.append("the source names no webfont; the generated site keeps the default type")

    # ---- Spacing, radii, elevation. ----
    spacing_values = _cluster_lengths(
        [f"{n}{u}" for v in _SPACING.findall(css) for n, u in _LENGTH.findall(_resolve(v, props))],
        MAX_SPACING,
        positive_only=True,
    )
    spacing = {f"s{i + 1}": v for i, v in enumerate(spacing_values)}
    radii_values = _cluster_lengths(
        [f"{n}{u}" for v in _RADIUS.findall(css) for n, u in _LENGTH.findall(_resolve(v, props))],
        MAX_RADII,
    )
    rounded = {f"r{i + 1}": v for i, v in enumerate(radii_values)}
    shadows = [
        r
        for r in (_resolve(v, props).strip() for v in _SHADOW.findall(css))
        # A shadow still naming a var() after resolution points at a property the
        # source computes elsewhere (an -rgb channel split, typically). Emitting
        # it gives the agent a value that resolves to nothing.
        if r and "var(" not in r and r != "none"
    ]
    elevation = {f"e{i + 1}": v for i, v in enumerate(list(dict.fromkeys(shadows))[:MAX_SHADOWS])}

    tokens_css = _compile_tokens_css(colors, typography, spacing, rounded, elevation)
    rationale = _describe(colors, heading, faces)

    return (
        DesignSystem(
            name=name,
            colors=colors,
            typography=typography,
            spacing=spacing,
            rounded=rounded,
            elevation=elevation,
            rationale=rationale,
            tokens_css=tokens_css,
        ),
        notes,
    )


def _compile_tokens_css(
    colors: dict[str, ColorScale],
    typography: dict[str, Typography],
    spacing: dict[str, str],
    rounded: dict[str, str],
    elevation: dict[str, str],
) -> str:
    """The compiled custom properties, which the crew model calls the single
    source of truth downstream. Emitted from the SELECTED tokens rather than
    passed through from the source, so what the agent applies is the same set
    the rest of the brief describes."""
    lines = [":root {"]
    for role, scale in colors.items():
        value = scale.s500 or ""
        if value:
            lines.append(f"  --{role}: {value};")
    for role, face in typography.items():
        if face.family:
            lines.append(f"  --font-{role}: {face.family};")
        if face.size:
            lines.append(f"  --text-{role}: {face.size};")
    for key, value in spacing.items():
        lines.append(f"  --space-{key}: {value};")
    for key, value in rounded.items():
        lines.append(f"  --radius-{key}: {value};")
    for key, value in elevation.items():
        lines.append(f"  --shadow-{key}: {value};")
    lines.append("}")
    css = "\n".join(lines)
    return css[:MAX_TOKENS_CSS_CHARS]


def _describe(colors: dict[str, ColorScale], heading: str, faces: list[str]) -> str:
    """A short prose read of the extracted look, for the preamble's rationale
    line. It exists because the token values alone do not say "dark" or
    "monospace", and that framing is what stops the agent theming a dark site
    like a light one."""
    parts: list[str] = []
    # Judge dark vs light from the BACKGROUND, never from the whole palette. A
    # dark site's text colours are light, and on the first site tested those
    # outnumbered the two dark grounds and called a #121212 page "a light
    # palette" — which would have had the agent theme it inside out.
    ground = ""
    for role, scale in colors.items():
        if scale.s500 and re.search(r"(bg|background|surface|canvas|paper)", role, re.I):
            ground = scale.s500
            break
    if not ground:
        # No token says which is the ground, so fall back to the most-referenced
        # colour, which ranking already put first.
        first = next((s.s500 for s in colors.values() if s.s500), "")
        ground = first
    if ground:
        parts.append("a dark palette" if _is_dark(ground) else "a light palette")
    if heading:
        parts.append(f"headings set in {heading}")
    if faces:
        parts.append(f"webfonts the source ships: {', '.join(faces[:3])}")
    if not parts:
        return ""
    return (
        "Extracted from the source page: " + "; ".join(parts) + ". "
        "Match this, do not substitute a direction of your own."
    )


def _is_dark(value: str) -> bool:
    rgb = _hex_to_rgb(value)
    if rgb is None:
        return False
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 0.4


def stylesheets_from_crawl(files: dict[str, bytes], html: str) -> dict[str, str]:
    """The CSS a harvest carries: every ``.css`` file plus the page's inline
    ``<style>`` blocks. Inline blocks matter — the probe site keeps its whole
    design system in one, and reading only linked files would find nothing."""
    sheets: dict[str, str] = {}
    for path, blob in files.items():
        if path.lower().endswith(".css") and blob:
            sheets[path] = blob.decode("utf-8", "replace")
    inline = re.findall(r"<style\b[^>]*>(.*?)</style>", html or "", re.I | re.S)
    if inline:
        sheets["<inline>"] = "\n".join(inline)
    return sheets


def apply_to_brief(brief: Any, stylesheets: dict[str, str], *, name: str) -> list[str]:
    """Fill a brief's branding layer in place. Returns notes for the report."""
    system, notes = extract_design_system(stylesheets, name=name)
    brief.branding.design_system = system
    return notes
