# Degenerate-repetition guard for a streamed assistant reply.
#
# A model can fall into a loop and stream the same sentence (or the same short
# paragraph) over and over for minutes; the user cannot tell that apart from
# progress. ``RepetitionGuard`` is fed the reply text chunk by chunk and trips
# when the tail of the reply is one unit, or one block of up to
# ``MAX_PERIOD`` units, repeated back to back. It then reports how much of the
# reply to keep: everything before the loop plus ONE copy of the repeated block.
#
# A "unit" is a sentence or a line. What it deliberately never counts:
#   * text inside fenced code blocks (``` ... ```): closing braces and blank
#     returns legitimately repeat;
#   * list items and table rows: a menu or a placeholder table can repeat a cell;
#   * short units (under ``MIN_UNIT_CHARS``): "Done." and "}" are not loops.
# Fences, list items and table rows also BREAK a run, so repeats on either side
# of one never add up. Short units are skipped without breaking it.
#
# Pure and synchronous: no I/O, no clock. The cloud run loop
# (``pocketpaw_ee.cloud.chat.runs.run_core.execute_run``) owns what happens
# when it trips.

from __future__ import annotations

import re

# How many back-to-back copies trip the guard. A single sentence needs more
# copies than a multi-sentence block: a short status line can legitimately
# recur a couple of times between tool calls, a whole paragraph cannot.
SINGLE_UNIT_REPEATS = 5
BLOCK_REPEATS = 4
MAX_PERIOD = 6
MIN_UNIT_CHARS = 16

REPETITION_STOPPED_CODE = "agent.repetition_stopped"
REPETITION_STOPPED_MESSAGE = (
    "The reply started repeating itself, so I stopped it. Try again or rephrase."
)

# A unit ends at a newline, or at sentence punctuation followed by whitespace or
# by a capital letter (a looping model often drops the space: "...you.I have").
_UNIT_END = re.compile(r"\n|[.!?](?=\s|[A-Z])")
_LIST_OR_TABLE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\|)")
_WS = re.compile(r"\s+")
_BREAK = object()


class RepetitionGuard:
    """Feed streamed text with :meth:`feed`; it returns True once the reply loops."""

    def __init__(self) -> None:
        self._text = ""
        self._scan = 0  # offset where the next unit starts
        self._in_fence = False
        # (normalized unit or _BREAK, end offset in the reply text)
        self._units: list[tuple[object, int]] = []
        self.keep_chars: int | None = None

    @property
    def tripped(self) -> bool:
        return self.keep_chars is not None

    def feed(self, chunk: str) -> bool:
        if self.tripped:
            return True
        if not chunk:
            return False
        self._text += chunk
        while True:
            match = _UNIT_END.search(self._text, self._scan)
            if match is None:
                return False
            end = match.end()
            raw = self._text[self._scan : end]
            self._scan = end
            self._add_unit(raw, end)
            if self._check():
                return True

    def kept_text(self, reply: str) -> str:
        """``reply`` cut to one copy of the loop (unchanged when not tripped)."""
        if self.keep_chars is None:
            return reply
        return reply[: self.keep_chars].rstrip()

    def _add_unit(self, raw: str, end: int) -> None:
        stripped = raw.strip()
        if stripped.startswith("```"):
            self._in_fence = not self._in_fence
            self._units.append((_BREAK, end))
            return
        if self._in_fence or _LIST_OR_TABLE.match(raw):
            if not self._units or self._units[-1][0] is not _BREAK:
                self._units.append((_BREAK, end))
            return
        norm = _WS.sub(" ", stripped).lower()
        if len(norm) < MIN_UNIT_CHARS or not re.search(r"[a-z]{2}", norm):
            return
        self._units.append((norm, end))
        # Only the tail matters; bound the memory on a long reply.
        if len(self._units) > MAX_PERIOD * (SINGLE_UNIT_REPEATS + 1):
            del self._units[: -MAX_PERIOD * (SINGLE_UNIT_REPEATS + 1)]

    def _check(self) -> bool:
        units = self._units
        for period in range(1, MAX_PERIOD + 1):
            repeats = SINGLE_UNIT_REPEATS if period == 1 else BLOCK_REPEATS
            span = period * repeats
            if len(units) < span:
                break
            tail = [u for u, _ in units[-span:]]
            if any(u is _BREAK for u in tail):
                continue
            block = tail[:period]
            if period > 1 and len(set(block)) == 1:
                continue  # a single unit repeated; period 1 owns that case
            if all(tail[i] == block[i % period] for i in range(span)):
                # Keep through the end of the FIRST copy of the block.
                self.keep_chars = units[-span + period - 1][1]
                return True
        return False
