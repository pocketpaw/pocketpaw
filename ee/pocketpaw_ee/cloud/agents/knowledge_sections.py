# knowledge_sections.py — split a document into sections small enough to
# compile one at a time without losing a fact.
#
# ``split_into_sections`` is pure (no I/O, no LLM). The sectioned ingest in
# ``knowledge.py`` compiles each section into its own kb article. kb-go only
# searches compiled articles, and its ``search --context`` output carries an
# article's body only while that body is under 2,000 bytes, so a section has
# to come out of its compile small enough to be both searchable and quotable.
#
# Invariants a reader must not break:
#   * Every word of the input lands in exactly one section, in order (blank
#     lines and page breaks become paragraph breaks). Nothing is dropped and
#     nothing is repeated, except a
#     markdown table's header row, which is repeated on each piece of a table
#     split across sections so every piece still reads as a table.
#   * A cut never falls inside a line unless that line alone is over
#     ``target``. Then prose is cut at sentence ends, and a sentence that is
#     still too long is cut at spaces.
#   * A document of at most ``hard_max`` characters is ONE section, so a
#     short document keeps the whole-document compile it always had.
"""Split a document into fact-preserving sections for per-section compile."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    """One piece of a document: its text, and the nearest heading above or at
    its start (``""`` when the document has none) as a hint for the compiler."""

    title_hint: str
    text: str


_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_SETEXT_UNDERLINE = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_MD_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
# Two or more column gaps of 2+ spaces: a plain-text table row.
_SPACED_COLUMNS = re.compile(r"\S {2,}\S.* {2,}\S")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# Characters that mark a line as data (a price row, a percentage), never a heading.
_DATA_CHARS = set("$%€£|\t")
_HEADING_MAX_CHARS = 80
# A line ending in one of these is prose that wraps onto the next line.
_CONTINUATION_WORDS = frozenset(
    "a an and as at by for from in into of on or our the to with your".split()
)
_HEADING_MAX_WORDS = 10
# A heading flushes the section being built only once that section holds this
# share of ``target``, so a run of short headed blocks packs together instead of
# becoming many tiny sections (each one a paid compile).
_HEADING_FLUSH_SHARE = 0.5


def split_into_sections(text: str, *, target: int = 1800, hard_max: int = 2400) -> list[Section]:
    """Split ``text`` into sections of about ``target`` characters.

    Boundaries are, in order of preference: headings (markdown, setext, and
    heading-like lines such as ``2. Caring for your gear``), blank-line
    paragraph groups, page breaks (``\\f``) and table blocks. Paragraphs are
    packed up to ``target``; a paragraph or table over ``target`` is split at
    line ends (tables) or sentence-ending lines and sentences (prose). Each
    section carries the nearest heading as ``title_hint``.

    A document of at most ``hard_max`` characters is returned as one section.
    """
    if target <= 0 or hard_max < target:
        raise ValueError("need 0 < target <= hard_max")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if len(text) <= hard_max:
        return [Section(title_hint=_first_heading(text), text=text)]

    sections: list[Section] = []
    parts: list[str] = []
    size = 0
    has_body = False
    hint = ""
    last_heading = ""

    def flush() -> None:
        nonlocal parts, size, has_body, hint
        if parts:
            sections.append(Section(title_hint=hint, text="\n\n".join(parts)))
        parts, size, has_body, hint = [], 0, False, last_heading

    def add(part: str, *, body: bool) -> None:
        nonlocal size, has_body
        size += len(part) + (2 if parts else 0)
        parts.append(part)
        has_body = has_body or body

    for kind, lines in _blocks(text):
        if kind == "heading":
            heading = lines[0]
            if has_body and size >= target * _HEADING_FLUSH_SHARE:
                last_heading = _heading_text(heading)
                flush()
            else:
                if not parts:
                    hint = _heading_text(heading)
                last_heading = _heading_text(heading)
            add(heading, body=False)
            continue
        block = "\n".join(lines)
        units = [block] if len(block) <= target else _split_block(kind, lines, target)
        for unit in units:
            if has_body and size + 2 + len(unit) > target:
                flush()
            add(unit, body=True)
    flush()
    return sections


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """``(kind, lines)`` blocks: ``heading`` (one line), ``para`` or ``table``.
    A page break ends a block like a blank line does."""
    blocks: list[tuple[str, list[str]]] = []
    para: list[str] = []
    table: list[str] = []
    prev = ""  # the previous line, "" after a blank line or a break
    prev_was_heading = False

    def end(buffer: list[str], kind: str) -> None:
        if buffer:
            blocks.append((kind, list(buffer)))
            buffer.clear()

    for line in text.replace("\f", "\n\n").split("\n"):
        stripped = line.strip()
        if not stripped:
            end(para, "para")
            end(table, "table")
            prev, prev_was_heading = "", False
            continue
        if _SETEXT_UNDERLINE.match(line) and len(para) == 1 and not table:
            # "Title\n=====": the one-line paragraph above was a heading.
            blocks.append(("heading", [para.pop()]))
            prev, prev_was_heading = stripped, True
            continue
        after_break = not prev or prev_was_heading or prev[-1] in ".!?:"
        if _MD_HEADING.match(line) or (after_break and _looks_like_heading(stripped)):
            end(para, "para")
            end(table, "table")
            blocks.append(("heading", [line.rstrip()]))
            prev, prev_was_heading = stripped, True
            continue
        if _looks_like_table_row(line):
            end(para, "para")
            table.append(line.rstrip())
        else:
            end(table, "table")
            para.append(line.rstrip())
        prev, prev_was_heading = stripped, False
    end(para, "para")
    end(table, "table")
    return blocks


def _looks_like_heading(line: str) -> bool:
    """A short, unpunctuated, capitalised line with no data characters."""
    if len(line) > _HEADING_MAX_CHARS or len(line.split()) > _HEADING_MAX_WORDS:
        return False
    if line[-1] in ".!?,;:" or _DATA_CHARS & set(line):
        return False
    if line.rsplit(" ", 1)[-1] in _CONTINUATION_WORDS:
        return False  # a wrapped line of prose, cut after "and", "the", ...
    first = line.lstrip("0123456789. ")[:1] if line[0].isdigit() else line[0]
    return first.isalpha() and first.isupper()


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") or "\t" in stripped or bool(_SPACED_COLUMNS.search(stripped))


def _heading_text(line: str) -> str:
    return line.strip().lstrip("#").strip()


def _first_heading(text: str) -> str:
    for line in text.split("\n"):
        if _MD_HEADING.match(line):
            return _heading_text(line)
    return ""


def _split_block(kind: str, lines: list[str], target: int) -> list[str]:
    """Pieces of one oversized block, each at most ``target`` characters."""
    if kind == "table":
        header: list[str] = []
        if len(lines) > 2 and _MD_TABLE_SEPARATOR.match(lines[1]):
            header, lines = lines[:2], lines[2:]
        head = "\n".join(header)
        room = target - (len(head) + 1 if head else 0)
        if room < target // 2:
            # A header too big to repeat: split the table as plain lines.
            return _pack(_fit_lines(header + lines, target), target, "\n")
        rows = _pack(_fit_lines(lines, room), room, "\n")
        return [f"{head}\n{rows_text}" if head else rows_text for rows_text in rows]
    # Prose: prefer cuts after lines that end a sentence, then any line end,
    # then sentence ends inside a line, then spaces.
    runs: list[str] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        if line.rstrip()[-1:] in ".!?:":
            runs.append("\n".join(current))
            current = []
    if current:
        runs.append("\n".join(current))
    pieces: list[str] = []
    for run in runs:
        if len(run) <= target:
            pieces.append(run)
        else:
            pieces.extend(_pack(_fit_lines(run.split("\n"), target), target, "\n"))
    return _pack(pieces, target, "\n")


def _fit_lines(lines: list[str], limit: int) -> list[str]:
    """``lines`` with any line over ``limit`` cut at sentence ends, then spaces,
    then (a single unbroken run) every ``limit`` characters."""
    out: list[str] = []
    for line in lines:
        if len(line) <= limit:
            out.append(line)
            continue
        sentences: list[str] = []
        for sentence in _SENTENCE_END.split(line):
            if len(sentence) <= limit:
                sentences.append(sentence)
                continue
            words: list[str] = []
            for word in sentence.split(" "):
                while len(word) > limit:
                    words.append(word[:limit])
                    word = word[limit:]
                words.append(word)
            sentences.extend(_pack(words, limit, " "))
        out.extend(_pack(sentences, limit, " "))
    return out


def _pack(parts: list[str], limit: int, joiner: str) -> list[str]:
    """Greedily join consecutive ``parts`` with ``joiner`` while within ``limit``."""
    packed: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        candidate = f"{current}{joiner}{part}" if current else part
        if current and len(candidate) > limit:
            packed.append(current)
            current = part
        else:
            current = candidate
    if current:
        packed.append(current)
    return packed


__all__ = ["Section", "split_into_sections"]
