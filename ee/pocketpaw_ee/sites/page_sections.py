# ee/pocketpaw_ee/sites/page_sections.py — cut one site page into the sections
# the site sync ingests as separate kb articles, and read the page index back.
#
# ``split_page`` is pure (no I/O, no LLM). A page's Markdown (``kb_ingest``) is
# cut at its H1/H2 headings; an H2 section still over ``MAX_SECTION_CHARS`` is
# cut at its H3s; a piece under ``MIN_SECTION_CHARS`` joins a neighbour; text
# with no usable heading falls back to ``knowledge_sections.split_into_sections``.
# A page up to ``SHORT_PAGE_CHARS`` is ONE section carrying the page's own
# source and text, which the sync ingests as one article, as it always has.
#
# Invariants a reader must not break:
#   * Sizes: every article this path writes comes from at most 3,000 raw
#     characters, so its compile (restructuring adds roughly 10-30% markdown)
#     still fits the concierge's 4,000-char per-item context whole.
#   * Identity: a section's ``source`` is "<page source>#<heading slug>", with
#     -2, -3 on a repeated slug, and its title is the breadcrumb
#     "Page › Heading". Both are deterministic, so kb-go (v0.3.0 keys an article
#     on its source) replaces a section in place on a re-sync.
#   * The breadcrumb also leads each section's text, so the compile keeps the
#     page context a bare "## Footwear" table would lose.
#   * ``index_sections`` reads both shapes of a ``Site.kb_page_index`` entry:
#     the old ``{"id", "title"}`` and the current one that adds ``sections``.
"""Split a site page into heading sections for per-section kb ingest."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from pocketpaw_ee.cloud.agents.knowledge_sections import Section, split_into_sections

# A page up to this size stays one article (see the header for the sizes).
SHORT_PAGE_CHARS = 3_000
# The cap on one section's raw text, breadcrumb included.
MAX_SECTION_CHARS = 3_000
# A piece smaller than this is a heading and a line or two: a weak search
# document and a paid compile of its own, so it joins a neighbour.
MIN_SECTION_CHARS = 500
# The size split's target for heading-less text, leaving room for the breadcrumb.
_FALLBACK_TARGET_CHARS = 2_400
# Heading slugs are clipped so a source stays a short, readable kb label.
_SLUG_CHARS = 60
_TITLE_CHARS = 120
# Room for a section's body once its breadcrumb line (at most ``_TITLE_CHARS``
# plus a part number) and the blank line after it are counted.
_BODY_CHARS = MAX_SECTION_CHARS - _TITLE_CHARS - 16

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_MARK_RE = re.compile(r"[*_`\\]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")

_SEP = " › "


@dataclass(frozen=True)
class PageSection:
    """One section of a page, ready to ingest.

    ``heading`` is the section's own heading ("" for a whole short page or a
    heading-less piece), ``anchor`` the id that heading carried in the page's
    HTML ("" when it had none), ``text`` the raw text the compile sees."""

    source: str
    title: str
    heading: str
    anchor: str
    text: str

    @property
    def hash(self) -> str:
        """What a re-sync compares to skip an unchanged section's compile."""
        basis = f"{self.source}\n{self.title}\n{self.text}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Piece:
    level: int
    headings: list[str]  # the heading path inside the page (H2, or H2 then H3)
    body: str  # the heading line and everything under it


# --------------------------------------------------------------------------- #
# Heading ids from the page's HTML
# --------------------------------------------------------------------------- #


class _HeadingIds(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[tuple[str, str]] = []
        self._open: tuple[str, str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            anchor = next((v for k, v in attrs or [] if k == "id" and v), "")
            self._open, self._text = (tag, anchor.strip()), []

    def handle_endtag(self, tag: str) -> None:
        if self._open and tag == self._open[0]:
            key = heading_key("".join(self._text))
            if key and self._open[1]:
                self.ids.append((key, self._open[1]))
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open:
            self._text.append(data)


def heading_anchors(html: str) -> tuple[tuple[str, str], ...]:
    """``(heading key, id)`` for each heading of ``html`` that has an id, first
    one per key. Never raises: a parse failure yields what was read so far."""
    parser = _HeadingIds()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — anchors are a nicety, never a failed sync
        pass
    seen: dict[str, str] = {}
    for key, anchor in parser.ids:
        seen.setdefault(key, anchor)
    return tuple(seen.items())


def heading_key(text: str) -> str:
    """A heading's text folded for matching HTML against Markdown."""
    plain = _MD_MARK_RE.sub("", _MD_LINK_RE.sub(r"\1", text))
    return _WS_RE.sub(" ", plain).strip().lower()


# --------------------------------------------------------------------------- #
# The split
# --------------------------------------------------------------------------- #


def split_page(doc: Any) -> list[PageSection]:
    """``doc`` (a ``kb_ingest.SiteDocument``) as the sections to ingest, in page
    order. A short page is one section with the page's own source and text."""
    text = str(doc.text or "").strip()
    anchors = dict(getattr(doc, "anchors", ()) or ())
    title = page_title(text, str(doc.path))
    if len(text) <= SHORT_PAGE_CHARS:
        return [PageSection(source=doc.source, title=title, heading="", anchor="", text=text)]

    pieces: list[_Piece] = []
    for piece in _cut(text, levels=(1, 2), path=[]):
        if len(piece.body) > _BODY_CHARS and piece.level:
            pieces.extend(_merge_small(list(_cut(piece.body, levels=(3,), path=piece.headings))))
        else:
            pieces.append(piece)
    pieces = _merge_small(_drop_title_heading(pieces, title))

    sections: list[PageSection] = []
    used: dict[str, int] = {}
    for piece in pieces:
        heading = piece.headings[-1] if piece.headings else ""
        crumbs = [title, *piece.headings]
        for part, chunk in enumerate(_fit(piece.body), start=1):
            label = heading or chunk.title_hint
            slug = _slug(label) or "part"
            used[slug] = used.get(slug, 0) + 1
            if used[slug] > 1:
                slug = f"{slug}-{used[slug]}"
            crumb = _SEP.join(c for c in [*crumbs, *([] if heading else [label])] if c)
            crumb = crumb[:_TITLE_CHARS].rstrip()
            if part > 1 or not label:
                # A piece of a cut section: the part number keeps titles distinct.
                crumb = f"{crumb} ({part})" if heading else f"{crumb} (part {part})"
            sections.append(
                PageSection(
                    source=f"{doc.source}#{slug}",
                    title=crumb,
                    heading=label,
                    anchor=anchors.get(heading_key(heading), "") if heading else "",
                    text=f"{crumb}\n\n{chunk.text}",
                )
            )
    return sections


def page_title(text: str, path: str) -> str:
    """The page's name for breadcrumbs: its first H1, else the <title> line
    ``html_to_markdown`` puts first, else a name made from the path."""
    first: str | None = None  # the first non-empty line; "" when it is not a title
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            first = "" if first is None else first
            continue
        if in_fence or not line.strip():
            continue
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 1:
            return _clean_heading(match.group(2))[:_TITLE_CHARS]
        if first is None:
            first = "" if match else line.strip()
    if first:
        return first[:_TITLE_CHARS]
    name = re.sub(r"\.(html?|svelte|md|svx)$", "", path.strip("/").lower())
    name = re.sub(r"(^|/)(index|\+page|src/routes)$", "", name).strip("/")
    return name.replace("/", " / ").replace("-", " ").strip().capitalize() or "Home"


def _cut(text: str, *, levels: tuple[int, ...], path: list[str]) -> list[_Piece]:
    """``text`` cut before each heading whose level is in ``levels`` (never inside
    a code fence). The text before the first such heading is a level-0 piece."""
    pieces: list[_Piece] = [_Piece(level=0, headings=list(path), body="")]
    lines: list[str] = []
    in_fence = False

    def close() -> None:
        pieces[-1].body = "\n".join(lines).strip()

    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING_RE.match(line)
        if match and len(match.group(1)) in levels:
            close()
            lines = []
            heading = _clean_heading(match.group(2))
            pieces.append(_Piece(level=len(match.group(1)), headings=[*path, heading], body=""))
        lines.append(line)
    close()
    return [p for p in pieces if p.body]


def _drop_title_heading(pieces: list[_Piece], title: str) -> list[_Piece]:
    """An H1 that repeats the page title is the page, not a section: its piece
    reads as heading-less so the breadcrumb does not say the title twice."""
    for piece in pieces:
        if piece.level == 1 and heading_key(piece.headings[-1]) == heading_key(title):
            piece.level, piece.headings = 0, piece.headings[:-1]
    return pieces


def _merge_small(pieces: list[_Piece]) -> list[_Piece]:
    """Pieces under ``MIN_SECTION_CHARS`` joined to a neighbour: the previous one
    when the result fits ``MAX_SECTION_CHARS``, else the next (taking its
    heading when the small piece had none). One that fits neither stays."""
    out: list[_Piece] = []
    carry: _Piece | None = None
    for piece in pieces:
        if carry is not None:
            if len(carry.body) + len(piece.body) + 2 <= _BODY_CHARS:
                headings = piece.headings if carry.level == 0 else carry.headings
                level = piece.level if carry.level == 0 else carry.level
                piece = _Piece(level=level, headings=headings, body=f"{carry.body}\n\n{piece.body}")
            else:
                out.append(carry)
            carry = None
        if len(piece.body) < MIN_SECTION_CHARS:
            if out and len(out[-1].body) + len(piece.body) + 2 <= _BODY_CHARS:
                out[-1].body = f"{out[-1].body}\n\n{piece.body}"
                continue
            carry = piece
            continue
        out.append(piece)
    if carry is not None:
        out.append(carry)
    return out


def _fit(body: str) -> list[Section]:
    """``body`` as one piece when it fits beside a breadcrumb, else the size
    split's sections (``knowledge_sections``), each with its own heading hint."""
    if len(body) <= _BODY_CHARS:
        return [Section(title_hint="", text=body)]
    return split_into_sections(body, target=_FALLBACK_TARGET_CHARS, hard_max=_FALLBACK_TARGET_CHARS)


def _clean_heading(text: str) -> str:
    plain = _MD_MARK_RE.sub("", _MD_LINK_RE.sub(r"\1", text))
    return _WS_RE.sub(" ", plain).strip()


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:_SLUG_CHARS].strip("-")


# --------------------------------------------------------------------------- #
# The page index
# --------------------------------------------------------------------------- #


def index_sections(entry: Any) -> list[dict[str, str]]:
    """A ``Site.kb_page_index`` entry's articles in page order, as
    ``{"id", "title", "source", "hash", "anchor"}`` dicts. An old-shape entry
    (``{"id", "title"}``) reads as one section; anything unusable as none."""
    if not isinstance(entry, dict):
        return []
    out: list[dict[str, str]] = []
    for section in entry.get("sections") or []:
        if isinstance(section, dict) and section.get("id"):
            out.append({k: str(section.get(k) or "") for k in _SECTION_KEYS})
    if out:
        return out
    if entry.get("id"):
        return [{"id": str(entry["id"]), "title": str(entry.get("title") or ""),
                 "source": "", "hash": "", "anchor": ""}]  # fmt: skip
    return []


_SECTION_KEYS = ("id", "title", "source", "hash", "anchor")


__all__ = [
    "MAX_SECTION_CHARS",
    "MIN_SECTION_CHARS",
    "SHORT_PAGE_CHARS",
    "PageSection",
    "heading_anchors",
    "heading_key",
    "index_sections",
    "page_title",
    "split_page",
]
