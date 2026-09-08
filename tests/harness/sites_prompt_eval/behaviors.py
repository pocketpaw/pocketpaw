"""The scorers — one per behaviour the /sites prompt spends tokens producing.

New file 2026-09-08 (feat/sites-design-skills), part of SD-6.

Each scorer takes the SOURCE of a generated site (HTML, or a Svelte/React
component file — they are all markup plus CSS for these purposes) and returns a
``Verdict``. A scorer answers one question, names the prompt rule it is measuring,
and explains what it saw, because a bare False on a 40 KB page is not actionable.

TWO DESIGN RULES, both learned from gates in this repo that looked fine and were
not:

1. **Score visible text, not source text.** An em-dash inside a CSS comment, a
   class name, or a `<style>` block is not a text tell. ``visible_text`` strips
   script, style, comments, tags and attributes first. A scorer that matched raw
   source would fail a clean page for its own stylesheet.
2. **Absence of the defect is not presence of the behaviour.** ``floor_focus``
   does not pass a page with no focusable elements, and ``floor_reduced_motion``
   does not pass a page with no motion — those return ``n/a``, which the runner
   counts separately. A scorer that returns PASS for "nothing to check" turns an
   empty page into a perfect score.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One scorer's answer about one page.

    ``ok`` is None for "not applicable" — the page gave this scorer nothing to
    judge. That is deliberately NOT a pass: see the module docstring.
    """

    name: str
    ok: bool | None
    detail: str

    @property
    def symbol(self) -> str:
        return {True: "pass", False: "FAIL", None: "n/a"}[self.ok]


def _pass(name: str, detail: str = "") -> Verdict:
    return Verdict(name, True, detail)


def _fail(name: str, detail: str) -> Verdict:
    return Verdict(name, False, detail)


def _na(name: str, detail: str) -> Verdict:
    return Verdict(name, None, detail)


# A page with no copy cannot demonstrate anything about copy. Below this many
# characters of visible text, the copy scorers return n/a rather than the pass
# that "no em-dash was found" would otherwise be. Caught by the runner's
# empty-page control, which scored a blank string at 50% before this existed.
_MIN_COPY_CHARS = 40


def _too_little_copy(text: str) -> bool:
    return len(text.strip()) < _MIN_COPY_CHARS


# --------------------------------------------------------------------------
# Text extraction
# --------------------------------------------------------------------------

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"<[^>]+>", re.S)
_SVELTE_BLOCK = re.compile(r"\{[#/:@][^}]*\}")


def visible_text(source: str) -> str:
    """The words a visitor actually reads.

    Strips script and style bodies, HTML comments, every tag (and therefore every
    attribute), and Svelte/JSX control blocks. What is left is copy.
    """
    s = _SCRIPT_STYLE.sub(" ", source)
    s = _HTML_COMMENT.sub(" ", s)
    s = _TAG.sub(" ", s)
    s = _SVELTE_BLOCK.sub(" ", s)
    return re.sub(r"\s+", " ", s)


def style_text(source: str) -> str:
    """Everything that can carry CSS: `<style>` bodies plus inline style attrs."""
    blocks = re.findall(r"<style\b[^>]*>(.*?)</style>", source, re.S | re.I)
    inline = re.findall(r"style=[\"']([^\"']*)[\"']", source, re.I)
    return "\n".join(blocks + inline)


_RULE = re.compile(r"([^{}]*)\{([^{}]*)\}")


def rules(css: str) -> list[tuple[str, str]]:
    """Every `selector { body }` pair in a stylesheet, as (selector, body).

    ONE splitter, shared by every scorer that needs rules, because the first
    version of `background` anchored on `(?:^|})` and that quietly skipped every
    OTHER rule: consuming the closing brace as the delimiter means the engine has
    to find the NEXT brace to start again. On the control stylesheet it saw
    `:root` and nothing else, so a page whose body rule had been deleted still
    scored a pass. Caught by the clean-page control failing.

    Nested at-rules (`@media { .x { } }`) split imperfectly — the inner rule wins
    and the at-rule wrapper is dropped, which is the behaviour every scorer here
    wants anyway.
    """
    return [(sel.strip(), body) for sel, body in _RULE.findall(css)]


# --------------------------------------------------------------------------
# Scorers
# --------------------------------------------------------------------------


def vision_ledger(source: str) -> Verdict:
    """MODULE 1 / PHASE 1: the Creative Direction Declaration is stated up front.

    The one behaviour that proves the agent ran the direction engine at all rather
    than reaching for a default. It is an HTML comment, so it is checked against
    the raw source, not the visible text.
    """
    n = "vision-ledger"
    if re.search(r"creative\s+direction\s+declaration", source, re.I):
        return _pass(n, "declaration comment present")
    if re.search(r"design\s+read\s*:", source, re.I):
        return _pass(n, "design read stated without the comment wrapper")
    return _fail(n, "no Creative Direction Declaration and no Design Read")


_PLAIN_GROUND = re.compile(
    r"(?:^|[;{\s])background(?:-color)?\s*:\s*"
    r"(#fff(?:fff)?\b|#000(?:000)?\b|white\b|black\b)",
    re.I,
)


def background(source: str) -> Verdict:
    """MODULE 2.B: the page ground is styled, and it is not plain #fff / #000.

    Only the *page* ground counts. A white card on a tuned off-white page is fine
    and common, so this looks at rules whose selector reaches the page — body,
    html, :root, .page — rather than at every white in the file.
    """
    n = "background"
    css = style_text(source)
    if not css.strip():
        return _fail(n, "no CSS at all, so the ground is the browser default")
    page_rules = [
        (sel, body)
        for sel, body in rules(css)
        if re.search(r"\bbody\b|\bhtml\b|:root|\.page\b", sel, re.I)
    ]
    # A :root block that only declares custom properties is not a ground. Counting
    # it as one let a page with its body rule deleted still pass — caught by the
    # control that removes the body rule entirely.
    grounds = [
        (sel, body)
        for sel, body in page_rules
        if re.search(r"(?:^|[;{\s])background(?:-color|-image)?\s*:", body, re.I)
    ]
    if not grounds:
        return _fail(n, "no body/html/:root rule declares a background")
    for selector, body in grounds:
        hit = _PLAIN_GROUND.search(body)
        if hit:
            return _fail(n, f"plain ground {hit.group(1)!r} on {selector.strip()!r}")
    return _pass(n, f"{len(grounds)} page-ground rule(s), none plain")


_DASHES = re.compile(r"[—–]")


def no_em_dash(source: str) -> Verdict:
    """MODULE 4: zero em/en dashes in VISIBLE text. The loudest text tell."""
    n = "no-em-dash"
    text = visible_text(source)
    if _too_little_copy(text):
        return _na(n, "no visible copy to judge")
    hits = _DASHES.findall(text)
    if not hits:
        return _pass(n, "none in visible text")
    sample = next((m.group(0) for m in re.finditer(r".{0,32}[—–].{0,32}", text)), "")
    return _fail(n, f"{len(hits)} in visible text, e.g. …{sample.strip()}…")


_FILLER = re.compile(
    r"\b(elevate|revolutioniz\w*|next-gen\w*|empower\w*|supercharg\w*|"
    r"seamless\w*|unleash\w*|unlock your|game-chang\w*|cutting-edge)\b",
    re.I,
)


def no_filler(source: str) -> Verdict:
    """MODULE 4: the banned filler verbs, in visible copy only."""
    n = "no-filler"
    text = visible_text(source)
    if _too_little_copy(text):
        return _na(n, "no visible copy to judge")
    found = sorted({m.lower() for m in _FILLER.findall(text)})
    if not found:
        return _pass(n, "none")
    return _fail(n, "filler verbs: " + ", ".join(found))


_PLACEHOLDER = re.compile(
    r"\b(john\s+doe|jane\s+doe|lorem\s+ipsum|acme\b|nexus\b|smartflow\b|"
    r"your\s+company\s+name|company\s+name\s+here)\b",
    re.I,
)


def no_placeholder(source: str) -> Verdict:
    """MODULE 4: no John Doe, no Acme, no lorem ipsum in visible copy."""
    n = "no-placeholder"
    text = visible_text(source)
    if _too_little_copy(text):
        return _na(n, "no visible copy to judge")
    found = sorted({m.strip().lower() for m in _PLACEHOLDER.findall(text)})
    if not found:
        return _pass(n, "none")
    return _fail(n, "placeholder copy: " + ", ".join(found))


_MEASURE = re.compile(r"max-width\s*:\s*([0-9.]+)\s*(ch|rem|em|px)", re.I)
# Roughly 75 characters, the top of design-taste 2.F's 60-75 range: 75ch, or about
# 42rem / 672px at a 16px root. Anything wider is not a measure cap.
_MEASURE_CEIL = {"ch": 78.0, "rem": 46.0, "em": 46.0, "px": 760.0}


def measure_capped(source: str) -> Verdict:
    """design-taste 2.F + craft §1: body copy carries a real measure cap.

    A `max-width: 1200px` container is a layout width, not a measure, so the value
    has to actually be in reading range for this to pass.
    """
    n = "measure-capped"
    css = style_text(source)
    if not css.strip():
        return _fail(n, "no CSS, so no measure cap")
    for value, unit in _MEASURE.findall(css):
        if float(value) <= _MEASURE_CEIL[unit.lower()]:
            return _pass(n, f"max-width: {value}{unit}")
    widest = _MEASURE.findall(css)
    if widest:
        return _fail(
            n, "max-width present but all are layout widths: " + ", ".join(v + u for v, u in widest)
        )
    return _fail(n, "no max-width anywhere")


def not_centered_hero(source: str) -> Verdict:
    """MODULE 5: the hero is not centred text over a gradient.

    The single most-cited AI tell for a landing page, and cheap to detect: a rule
    whose selector mentions the hero, carrying both `text-align: center` and a
    gradient background.
    """
    n = "not-centered-hero"
    css = style_text(source)
    hero = [(s, b) for s, b in rules(css) if re.search(r"hero|banner|masthead", s, re.I)]
    if not hero:
        return _na(n, "no hero/banner selector to judge")
    for selector, body in hero:
        centered = re.search(r"text-align\s*:\s*center", body, re.I)
        gradient = re.search(r"gradient\s*\(", body, re.I)
        if centered and gradient:
            return _fail(n, f"centred over a gradient on {selector.strip()!r}")
    return _pass(n, f"{len(hero)} hero rule(s), none centred-over-gradient")


_THREE_UP = re.compile(r"grid-template-columns\s*:\s*repeat\(\s*3\s*,\s*1fr\s*\)", re.I)


def no_three_equal_cards(source: str) -> Verdict:
    """MODULE 5: no three-equal-card feature row.

    `repeat(3, 1fr)` is the literal shape the rule bans. `repeat(auto-fit, ...)`
    and an explicitly asymmetric track list are fine and do not match.
    """
    n = "no-three-equal-cards"
    css = style_text(source)
    if not css.strip():
        return _na(n, "no CSS to judge")
    hits = _THREE_UP.findall(css)
    if hits:
        return _fail(n, f"{len(hits)} × repeat(3, 1fr)")
    return _pass(n, "none")


_MIN_TARGET = re.compile(r"min-(?:height|width)\s*:\s*([0-9.]+)\s*px", re.I)
_INTERACTIVE = re.compile(r"<(button|a|input|select|textarea)\b", re.I)


def floor_hit_area(source: str) -> Verdict:
    """craft §5: interactive targets reach 44px.

    n/a when the page has no interactive elements at all, because a page with no
    buttons has not demonstrated anything about hit areas.
    """
    n = "floor-hit-area"
    if not _INTERACTIVE.search(source):
        return _na(n, "no interactive elements to judge")
    css = style_text(source)
    sizes = [float(v) for v in _MIN_TARGET.findall(css)]
    if any(v >= 44 for v in sizes):
        return _pass(n, f"min-height/width ≥ 44px present ({max(sizes):g}px)")
    if sizes:
        return _fail(n, f"min-height/width present but all under 44px: {sizes}")
    return _fail(n, "interactive elements with no min-height/min-width at all")


def floor_focus(source: str) -> Verdict:
    """craft §5: a visible focus style survives.

    Fails on the specific defect the rule names — `outline: none` with nothing put
    back — rather than on the absence of a `:focus-visible` block, since a page can
    legitimately rely on the UA ring.
    """
    n = "floor-focus"
    if not _INTERACTIVE.search(source):
        return _na(n, "no focusable elements to judge")
    css = style_text(source)
    killed = re.findall(r"outline\s*:\s*(?:none|0)\b", css, re.I)
    # The replacement has to be a VISIBLE style. Matching `outline` alone accepted
    # `.btn:focus { outline: none }` as its own replacement, which is the exact
    # defect this scorer exists to catch — caught by the focus control.
    # Read each declaration's VALUE and judge it, rather than trying to express
    # "an outline that is not none" as a lookahead. The lookahead version passed
    # `outline: none` as its own replacement, because `\s*` backtracks to zero
    # width and then `(?!none)` is standing in front of a SPACE, which is not
    # "none". Caught by the focus control.
    replaced = False
    for sel, body in rules(css):
        if not re.search(r":focus(?:-visible)?", sel, re.I):
            continue
        for prop, value in re.findall(r"([a-z-]+)\s*:\s*([^;]+)", body, re.I):
            prop = prop.lower().strip()
            value = value.strip().lower()
            if prop in ("box-shadow", "border", "border-color", "background"):
                if value not in ("none", "0"):
                    replaced = True
            elif prop == "outline" and value not in ("none", "0"):
                replaced = True
        if replaced:
            break
    if killed and not replaced:
        return _fail(n, f"{len(killed)} × outline:none with no focus style put back")
    if killed:
        return _pass(n, "outline reset, and a :focus style replaces it")
    return _pass(n, "no outline reset; the UA focus ring survives")


# `transition-property` is the form design-taste and sites-craft both TEACH
# ("name the properties, never transition: all"), so a scorer that only matched
# the `transition:` shorthand reported "no motion" on exactly the pages that
# followed the rule. Caught by the reduced-motion control.
_MOTION = re.compile(
    r"(transition(?:-property|-duration)?\s*:|animation(?:-name)?\s*:|@keyframes\b)", re.I
)


def floor_reduced_motion(source: str) -> Verdict:
    """craft §5: motion is gated on prefers-reduced-motion.

    n/a on a page with no motion — nothing to honour. That distinction is the
    whole point: a still page must not score a free pass on a motion rule.
    """
    n = "floor-reduced-motion"
    css = style_text(source)
    if not _MOTION.search(css):
        return _na(n, "no transition/animation to gate")
    if re.search(r"@media[^{]*prefers-reduced-motion", css, re.I):
        return _pass(n, "motion is gated")
    return _fail(n, "page animates and never mentions prefers-reduced-motion")


PAGE_SCORERS: tuple[Callable[[str], Verdict], ...] = (
    vision_ledger,
    background,
    no_em_dash,
    no_filler,
    no_placeholder,
    measure_capped,
    not_centered_hero,
    no_three_equal_cards,
    floor_hit_area,
    floor_focus,
    floor_reduced_motion,
)


# --------------------------------------------------------------------------
# Cross-page scorer
# --------------------------------------------------------------------------

_HEX = re.compile(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", re.I)


def _accents(source: str) -> set[str]:
    """Every hex colour in the CSS, normalised to 6 digits lowercase."""
    out = set()
    for h in _HEX.findall(style_text(source)):
        h = h.lower()
        out.add("".join(c * 2 for c in h) if len(h) == 3 else h)
    return out


def rotation(first: str, second: str) -> Verdict:
    """MODULE 2.G + the repetition ban: two briefs do not resolve to one look.

    This is the behaviour `sites-theme-system` exists to enforce and the one a cut
    to the rotation language in PHASE 2 would remove first. It is also the only
    scorer that needs two pages, which is why the fixture set is built in pairs.

    Judged on the palette, because that is the axis with a machine-checkable
    answer. Identical palettes across two unrelated briefs is the failure; a couple
    of shared neutrals is not.
    """
    n = "rotation"
    a, b = _accents(first), _accents(second)
    if not a or not b:
        return _na(n, "one of the pair declares no hex colours")
    shared = a & b
    union = a | b
    overlap = len(shared) / len(union)
    if a == b:
        return _fail(n, f"identical palettes ({len(a)} colours, byte for byte)")
    if overlap > 0.6:
        return _fail(n, f"palettes {overlap:.0%} identical ({len(shared)}/{len(union)})")
    return _pass(n, f"palettes differ ({overlap:.0%} shared)")
