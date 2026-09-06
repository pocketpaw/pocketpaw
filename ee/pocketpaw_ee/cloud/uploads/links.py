# links.py — pure parser for note links and tags in text files.
# Created: 2026-09-05 (feat/files-links, B1). Turns a markdown or plain-text
#   body into the wikilink names it points at plus the #hashtags and
#   frontmatter tags it carries. No I/O, no YAML library; the FileReady
#   listener feeds it extracted text and every link resolver goes through
#   ``normalize_link_name`` so a link and a filename compare the same way.
"""Wikilink / hashtag / frontmatter-tag parser for library notes."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Both fence styles. The preview renderer (core/markdown/wikilinks.ts) skips
# ``` and ~~~ alike; parsing only ``` here made a [[link]] inside a ~~~ block
# a real edge and a phantom backlink that the preview never rendered.
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
# ``#word`` at line start or after whitespace. ``# Heading`` fails because the
# first char must be a letter; ``https://x/y#z`` fails because ``/`` precedes.
_HASHTAG_RE = re.compile(r"(?:^|(?<=\s))#([A-Za-z][\w/-]*)", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class NoteLinks:
    link_names: tuple[str, ...]
    hashtags: tuple[str, ...]
    frontmatter_tags: tuple[str, ...]


def normalize_link_name(name: str) -> str:
    """The one normalizer: strip, casefold, drop a single trailing ``.md``."""
    name = name.strip().casefold()
    if name.endswith(".md"):
        name = name[:-3]
    return name


def _dedupe(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _split_tag_value(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [t.strip().strip("\"'").lstrip("#") for t in value.split(",")]


def _frontmatter_tags(text: str) -> tuple[list[str], str]:
    """Return (tags, text without the frontmatter block)."""
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return [], text
    tags: list[str] = []
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not re.match(r"tags?\s*:", line, re.IGNORECASE):
            continue
        value = line.split(":", 1)[1]
        if value.strip():
            tags.extend(_split_tag_value(value))
            continue
        # Block list form: following lines that look like ``- a``.
        while i < len(lines) and re.match(r"\s*-\s*\S", lines[i]):
            tags.extend(_split_tag_value(lines[i].split("-", 1)[1]))
            i += 1
    return tags, text[m.end() :]


def parse_note_links(text: str) -> NoteLinks:
    """Extract wikilink targets, ``#hashtags`` and frontmatter ``tags:``.

    Fenced and inline code are skipped for all three. Results are deduped in
    order of first appearance; link names are normalized, tags are casefolded.
    """
    fm_tags, body = _frontmatter_tags(text or "")
    body = _FENCE_RE.sub(" ", body)
    body = _INLINE_CODE_RE.sub(" ", body)

    links: list[str] = []
    for raw in _WIKILINK_RE.findall(body):
        name = raw.split("|", 1)[0].split("#", 1)[0]
        links.append(normalize_link_name(name))

    hashtags = [t.casefold() for t in _HASHTAG_RE.findall(body)]
    return NoteLinks(
        link_names=_dedupe(links),
        hashtags=_dedupe(hashtags),
        frontmatter_tags=_dedupe([t.casefold() for t in fm_tags]),
    )


__all__ = ["NoteLinks", "normalize_link_name", "parse_note_links"]
