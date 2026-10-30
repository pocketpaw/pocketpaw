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
# CDATA, named entities, any processing instruction past a leading XML declaration)
# run BEFORE the parse, so ElementTree never sees a DTD and never expands an entity.
# Any parse error is a violation (a duplicate attribute is one). The walk is
# iterative and stops at the depth cap, so no markup can exhaust the stack. Every
# rule reads PARSED values, so a numeric character reference cannot hide a scheme.
# Attribute values: a backslash is refused outright (the landing and the widget do
# the same, so no CSS escape needs decoding); then whitespace, control and invisible
# format characters are dropped and the rest lowercased before the script-scheme
# rule, and a value holding ``url(`` must be exactly ``url(#id)``. Text content is
# never held to the value rules (``Metadata: 5`` is text, never a link).
# Ids are ``[A-Za-z0-9_-]+``; every id named by an href, a url() or a begin/end
# term must exist in the same SVG (checked after the walk, so a forward reference
# is fine). A ``use`` may not point at a subtree holding a ``use``, and no mask or
# clipPath may reach itself through its subtree's mask / clip-path refs.
# Caps beyond the contract's, shared with ripple: numbers up to 1e6 in magnitude,
# values / keyTimes / keySplines up to 200 entries, ``d`` up to 8,000 chars.
# ``svg_ids(markup)`` gives the ids the widget's rebuild keeps, for an annotation's
# ``target``: ids on ``KEPT_ELEMENTS`` only, mirroring ripple's ILLUSTRATION_ELEMENTS
# (core/src/manifest/illustration-svg.ts), since the rebuild drops any other element
# with its whole subtree.
# Known ceiling: the flash guard reads only ``dur``, so a ``set`` with a ``begin``
# list can still flash; the widget's reduced-motion pause and pause button are the floor.

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
MAX_NUMBER = 1e6
MAX_LIST_ENTRIES = 200
MAX_D_CHARS = 8_000

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
KEPT_ELEMENTS: frozenset[str] = frozenset(
    "svg g defs title desc path rect circle ellipse line polyline polygon text tspan "
    "linearGradient radialGradient stop clipPath mask symbol use animate animateTransform "
    "animateMotion mpath set".split()
)
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
    ("<![cdata[", "a CDATA section"),
)
_ENTITY = re.compile(r"&([^;\s&<]*);")
_KNOWN_ENTITY = re.compile(r"lt|gt|amp|quot|apos|#[0-9]+|#[xX][0-9a-fA-F]+")
_XML_DECL = re.compile(r"<\?xml\s[^?]*\?>")
_ID = re.compile(r"[A-Za-z0-9_-]+")
_ID_REF = re.compile(r"#([A-Za-z0-9_-]+)")
_URL_REF = re.compile(r"url\(#([A-Za-z0-9_-]+)\)")
# A begin/end term naming an element: ``a.end``, ``a.click+1s``, ``a.repeat(2)``.
# The event starts with a letter, so a clock value (``1.5s``) never reads as id ``1``.
_SMIL_REF = re.compile(r"([A-Za-z0-9_-]+)\.(?:[A-Za-z]+|repeat\(\d+\))")
_ANY_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_HASH_TOKEN = re.compile(r"#[A-Za-z0-9_-]+")  # a hex colour or an #id: not a number
_NOT_NUMERIC = frozenset({"id", "href", "attributeName", "begin", "end", "font-family"})
_LISTS = frozenset({"values", "keyTimes", "keySplines"})
_ATTR_SCHEMES = ("javascript:", "data:", "vbscript:", "expression(")
_NUMBER = re.compile(r"\d+(?:\.\d*)?|\.\d+")
_CLOCK = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(h|min|s|ms)?")
_FULL_CLOCK = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)")
_UNIT_SECONDS = {"h": 3600.0, "min": 60.0, "s": 1.0, "ms": 0.001, None: 1.0}


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


def _attr_violation(tag: str, name: str, value: str, refs: list[str]) -> str | None:
    """Why one attribute is refused, else None. Every id it names goes on ``refs``
    (checked against the document's ids after the walk: a reference may come first)."""
    ns, local = _split(name)
    low = local.lower()
    if low.startswith("on"):
        return f"an event handler attribute ({local})"
    if low == "style":
        return "a style attribute"
    if low == "href" and ns in (None, XLINK_NS):
        if tag not in HREF_ELEMENTS:
            return f"an href on {tag}"
        if not (m := _ID_REF.fullmatch(value)):
            return "an href that is not #id"
        refs.append(m.group(1))
    if ns is None:
        if local == "attributeType":
            return "an attributeType attribute"
        if local == "id" and not _ID.fullmatch(value):
            return "an id outside [A-Za-z0-9_-]"
        if local == "d" and len(value) > MAX_D_CHARS:
            return f"a d over {MAX_D_CHARS} characters"
        if local in _LISTS and len(value.strip().strip(";").split(";")) > MAX_LIST_ENTRIES:
            return f"a {local} list over {MAX_LIST_ENTRIES} entries"
        if local not in _NOT_NUMERIC:
            numbers = _ANY_NUMBER.findall(_HASH_TOKEN.sub(" ", value))
            if any(abs(float(n)) > MAX_NUMBER for n in numbers):
                return f"a number over {MAX_NUMBER:g}"
    if tag in ANIMATION_ELEMENTS and ns is None:
        if local in ("begin", "end"):
            refs.extend(m.group(1) for t in value.split(";") if (m := _SMIL_REF.match(t.strip())))
        if local == "attributeName" and value.strip() not in ATTRIBUTE_NAMES:
            return f"an animation of {value.strip()!r}"
        if local == "dur" and value.strip() not in ("indefinite", "media"):
            seconds = _seconds(value)
            if seconds is None or seconds < MIN_DUR_SECONDS:
                return f"a dur under {MIN_DUR_SECONDS}s"
        if local == "repeatCount" and value.strip() != "indefinite":
            if not _NUMBER.fullmatch(value.strip()) or float(value) > MAX_REPEAT_COUNT:
                return f"a repeatCount over {MAX_REPEAT_COUNT}"
    if "\\" in value:
        return "a backslash in a value"
    folded = _folded(value)
    if any(s in folded for s in _ATTR_SCHEMES):
        return "a script or data link"
    if "url(" in folded:
        if not (m := _URL_REF.fullmatch(value)):
            return "a url() that is not url(#id)"
        refs.append(m.group(1))
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
    body = markup.lstrip("\ufeff")
    if decl := _XML_DECL.match(body):
        body = body[decl.end() :]
    if "<?" in body:
        return "a processing instruction"
    try:
        root = ET.fromstring(markup)
    except (ET.ParseError, ValueError):
        return "markup that does not parse"
    elements = animations = 0
    uses: list[ET.Element] = []
    refs: list[str] = []
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
        if "href" in el.attrib and f"{{{XLINK_NS}}}href" in el.attrib:
            return "both href and xlink:href on one element"
        for name, value in el.attrib.items():
            if reason := _attr_violation(tag, name, value, refs):
                return reason
        if tag == "use":
            uses.append(el)
            if len(uses) > MAX_USES:
                return f"more than {MAX_USES} use elements"
        if isinstance(el.get("id"), str):
            ids.setdefault(el.get("id"), el)
        stack.extend((kid, depth + 1) for kid in el)
    if missing := next((ref for ref in refs if ref not in ids), None):
        return f"a reference to a missing id (#{missing})"
    for use in uses:
        ref = use.get("href") or use.get(f"{{{XLINK_NS}}}href") or ""
        target = ids.get(ref.strip()[1:])
        if target is not None and any(_split(e.tag)[1] == "use" for e in target.iter()):
            return "a use pointing at a use"
    return _clip_cycle(ids)


def _clip_cycle(ids: dict[str, ET.Element]) -> str | None:
    """A ``mask`` or ``clipPath`` reaching itself through the ``mask`` /
    ``clip-path`` refs of its subtree, at any number of hops."""
    clips = {i: e for i, e in ids.items() if _split(e.tag)[1] in ("mask", "clipPath")}
    edges = {
        i: {
            m.group(1)
            for node in e.iter()
            for attr in ("mask", "clip-path")
            if (m := _URL_REF.fullmatch(node.get(attr) or "")) and m.group(1) in clips
        }
        for i, e in clips.items()
    }
    for start, first in edges.items():
        seen: set[str] = set()
        todo = list(first)
        while todo:
            ref = todo.pop()
            if ref == start:
                return "a mask or clipPath that references itself"
            if ref not in seen:
                seen.add(ref)
                todo.extend(edges[ref])
    return None


def svg_ids(markup: str) -> frozenset[str]:
    """The ids the widget's rebuild keeps: on a ``KEPT_ELEMENTS`` element whose
    ancestors are all kept, and on an animation element only when it names an
    attributeName (``animateMotion`` needs none). Ask it only of markup that
    ``svg_violation`` passed (parsed, namespaced, within the caps)."""
    ids: set[str] = set()
    stack = [ET.fromstring(markup)]
    while stack:
        el = stack.pop()
        tag = _split(el.tag)[1] if isinstance(el.tag, str) else ""
        if tag not in KEPT_ELEMENTS:
            continue
        if (
            tag in ANIMATION_ELEMENTS
            and tag != "animateMotion"
            and "attributeName" not in el.attrib
        ):
            continue
        if isinstance(el.get("id"), str):
            ids.add(el.get("id"))
        stack.extend(el)
    return frozenset(ids)
