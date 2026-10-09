# ee/pocketpaw_ee/paw_bar/illustration_svg.py: the server's SVG policy for Ripple's
# ``illustration`` widget (model-written animated SVG in a concierge card).
#
# ``svg_violation(markup)`` returns a short reason when the markup holds anything in
# the contract's hostile set or goes over a cap, else None. The contract is
# docs/design/drafts/2026-10-09-ripple-illustration-svg.md in paw-workspace; the
# ripple widget and the landing's card policy implement the same lists, so do not
# add or drop an entry here alone. Harmless unknowns (a ``filter`` element, a
# ``class`` attribute) pass: the widget rebuilds by allowlist and drops them.
#
# Order matters: the string pre-checks (length, DOCTYPE, ENTITY, xml-stylesheet,
# named entities, CDATA with markup) run BEFORE the parse, so ElementTree never
# sees a DTD and never expands an entity. Any parse error is a violation. The walk
# is iterative and stops at the depth cap, so no markup can exhaust the stack.
# Attribute values are read the way a browser would: CSS escapes undone, then
# whitespace, control and invisible format characters dropped and lowercased,
# before the ``url(`` and script-scheme rules; text content is never held to them
# (``Metadata: 5`` is text, never a link). A ``use`` may not point at a subtree
# holding a ``use`` (no fan-out past the element cap).

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET

MAX_SVG_CHARS = 24_000
MAX_ELEMENTS = 400
MAX_DEPTH = 24
MAX_ANIMATIONS = 40
MAX_USES = 40
MIN_DUR_SECONDS = 0.5
MAX_REPEAT_COUNT = 1000

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

HOSTILE_ELEMENTS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "foreignobject",
        "iframe",
        "object",
        "embed",
        "a",
        "image",
        "feimage",
        "audio",
        "video",
        "canvas",
    }
)
ANIMATION_ELEMENTS: frozenset[str] = frozenset(
    {"animate", "animateTransform", "animateMotion", "set"}
)
HREF_ELEMENTS: frozenset[str] = frozenset({"use", "mpath"})
ATTRIBUTE_NAMES: frozenset[str] = frozenset(
    {
        "fill",
        "fill-opacity",
        "stroke",
        "stroke-width",
        "stroke-opacity",
        "stroke-dasharray",
        "stroke-dashoffset",
        "opacity",
        "transform",
        "d",
        "points",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "width",
        "height",
        "offset",
        "stop-color",
        "stop-opacity",
        "visibility",
        "display",
        "font-size",
        "letter-spacing",
    }
)

_PRE_REFUSED = (
    ("<!doctype", "a DOCTYPE"),
    ("<!entity", "an ENTITY"),
    ("<?xml-stylesheet", "a stylesheet"),
)
_ENTITY = re.compile(r"&([^;\s&<]*);")
_KNOWN_ENTITY = re.compile(r"lt|gt|amp|quot|apos|#[0-9]+|#[xX][0-9a-fA-F]+")
_CDATA = re.compile(r"<!\[CDATA\[(.*?)(?:\]\]>|$)", re.DOTALL)
_ID_REF = re.compile(r"#[A-Za-z_][\w.\-]*")
_URL_REF = re.compile(r"url\(\s*#[A-Za-z_][\w.\-]*\s*\)", re.IGNORECASE)
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\r\n\f]?|\\(.)", re.DOTALL)
_ATTR_SCHEMES = ("javascript:", "data:", "vbscript:", "expression(")
_NUMBER = re.compile(r"\d+(?:\.\d*)?|\.\d+")
_CLOCK = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(h|min|s|ms)?")
_FULL_CLOCK = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)")
_UNIT_SECONDS = {"h": 3600.0, "min": 60.0, "s": 1.0, "ms": 0.001, None: 1.0}


def _css_char(match: re.Match[str]) -> str:
    if match.group(2) is not None:
        return match.group(2)
    point = int(match.group(1), 16)
    return chr(point) if 0 < point <= 0x10FFFF and not 0xD800 <= point <= 0xDFFF else "�"


def _folded(text: str) -> str:
    """``text`` NFKC-folded, with whitespace, control and invisible format
    characters dropped, lowercased."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in text if ch > " " and ch != "\x7f" and unicodedata.category(ch) != "Cf"
    ).lower()


def _seconds(value: str) -> float | None:
    """A SMIL clock value in seconds; None when it is not one."""
    text = value.strip()
    if m := _CLOCK.fullmatch(text):
        return float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    if m := _FULL_CLOCK.fullmatch(text):
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return None


def _split(name: str) -> tuple[str | None, str]:
    """``{ns}local`` as (ns, local); (None, name) when it has no namespace."""
    if name.startswith("{"):
        ns, _, local = name[1:].partition("}")
        return ns, local
    return None, name


def _attr_violation(tag: str, name: str, value: str) -> str | None:
    ns, local = _split(name)
    low = local.lower()
    if low.startswith("on"):
        return f"an event handler attribute ({local})"
    if low == "style":
        return "a style attribute"
    if low == "href" and ns in (None, XLINK_NS):
        if tag not in HREF_ELEMENTS:
            return f"an href on {tag}"
        if not _ID_REF.fullmatch(value.strip()):
            return "an href that is not #id"
    if tag in ANIMATION_ELEMENTS and ns is None:
        if local == "attributeName" and value.strip() not in ATTRIBUTE_NAMES:
            return f"an animation of {value.strip()!r}"
        if local == "dur" and value.strip() not in ("indefinite", "media"):
            seconds = _seconds(value)
            if seconds is None or seconds < MIN_DUR_SECONDS:
                return f"a dur under {MIN_DUR_SECONDS}s"
        if local == "repeatCount" and value.strip() != "indefinite":
            if not _NUMBER.fullmatch(value.strip()) or float(value) > MAX_REPEAT_COUNT:
                return f"a repeatCount over {MAX_REPEAT_COUNT}"
    plain = _CSS_ESCAPE.sub(_css_char, value)
    folded = _folded(plain)
    if any(s in folded for s in _ATTR_SCHEMES):
        return "a script or data link"
    if "url(" in folded and not _URL_REF.fullmatch(plain.strip()):
        return "a url() that is not url(#id)"
    return None


def svg_violation(markup: str) -> str | None:
    """Why ``markup`` is refused (hostile or over a cap), or None when it passes."""
    if len(markup) > MAX_SVG_CHARS:
        return f"more than {MAX_SVG_CHARS} characters"
    low = markup.lower()
    for token, what in _PRE_REFUSED:
        if token in low:
            return what
    for m in _ENTITY.finditer(markup):
        if not _KNOWN_ENTITY.fullmatch(m.group(1)):
            return f"the entity &{m.group(1)};"
    if any("<" in m.group(1) for m in _CDATA.finditer(markup)):
        return "CDATA holding markup"
    try:
        root = ET.fromstring(markup)
    except (ET.ParseError, ValueError):
        return "markup that does not parse"
    elements = animations = 0
    uses: list[ET.Element] = []
    ids: dict[str, ET.Element] = {}
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        el, depth = stack.pop()
        if depth > MAX_DEPTH:
            return f"nesting deeper than {MAX_DEPTH}"
        elements += 1
        if elements > MAX_ELEMENTS:
            return f"more than {MAX_ELEMENTS} elements"
        if not isinstance(el.tag, str):
            return "a node that is not an element"
        ns, tag = _split(el.tag)
        if ns not in (None, SVG_NS):
            return "an element outside the SVG namespace"
        if tag.lower() in HOSTILE_ELEMENTS:
            return f"a {tag} element"
        if depth == 1 and tag != "svg":
            return "a root that is not svg"
        if tag in ANIMATION_ELEMENTS:
            animations += 1
            if animations > MAX_ANIMATIONS:
                return f"more than {MAX_ANIMATIONS} animation elements"
        for name, value in el.attrib.items():
            if reason := _attr_violation(tag, name, value):
                return reason
        if tag == "use":
            uses.append(el)
            if len(uses) > MAX_USES:
                return f"more than {MAX_USES} use elements"
        if isinstance(el.get("id"), str):
            ids.setdefault(el.get("id"), el)
        stack.extend((kid, depth + 1) for kid in el)
    for use in uses:
        ref = use.get("href") or use.get(f"{{{XLINK_NS}}}href") or ""
        target = ids.get(ref.strip()[1:])
        if target is not None and any(_split(e.tag)[1] == "use" for e in target.iter()):
            return "a use pointing at a use"
    return None
