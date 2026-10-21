# Degenerate-repetition guard for a streamed assistant reply.
#
# A model can fall into a loop and stream the same sentence (or the same short
# paragraph) over and over for minutes; the user cannot tell that apart from
# progress. ``RepetitionGuard`` is fed the reply text chunk by chunk and trips
# only on an unmistakable loop: one unit repeated ``SINGLE_UNIT_REPEATS`` times
# back to back, or a block of up to ``MAX_PERIOD`` units repeated
# ``BLOCK_REPEATS`` times, AND the looped text spanning at least
# ``MIN_LOOP_CHARS``. The thresholds are deliberately high: the loop this exists
# for ran to 1,555 copies, while a song chorus, "write it ten times" or a short
# log excerpt repeats a handful of times and must never be cut.
#
# A "unit" is a sentence (Latin or CJK punctuation) or a line. Fenced code
# (``` or ~~~), indented code, list items, table rows, blockquotes and lines
# with no letters never count and BREAK a run, as does :meth:`break_run` (the
# caller calls it on every tool call, so a status line recurring across tool
# calls never adds up). Blank lines neither count nor break.
#
# Memory and time are bounded: only the unterminated tail of the text is kept,
# a scan never revisits text it already searched, and an over-long unterminated
# tail is dropped as a break. Pure and synchronous. The cloud run loop
# (``pocketpaw_ee.cloud.chat.runs.run_core.execute_run``) owns what happens
# when it trips; :meth:`kept_text` says how much of the reply to keep (all text
# before the loop plus one copy).

from __future__ import annotations

import re

SINGLE_UNIT_REPEATS = 12
BLOCK_REPEATS = 8
MAX_PERIOD = 6
MIN_LOOP_CHARS = 400
# An unterminated tail longer than this is not a sentence; drop it as a break.
_MAX_PENDING = 4000

REPETITION_STOPPED_CODE = "agent.repetition_stopped"
REPETITION_STOPPED_MESSAGE = (
    "The reply started repeating itself, so I stopped it. Try again or rephrase."
)

# A unit ends at a newline, at CJK/fullwidth sentence punctuation, or at Latin
# sentence punctuation followed by whitespace or a capital letter (a looping
# model often drops the space: "...you.I have").
_UNIT_END = re.compile(r"\n|[。！？]|[.!?](?=\s|[A-Z])")
_FENCE = re.compile(r"^ {0,3}(?:```|~~~)")
_NOT_PROSE = re.compile(r"^(?: {4}|\t|\s*(?:[-*+]\s|\d+[.)]\s|\||>))")
_WS = re.compile(r"\s+")
_BREAK = object()


class RepetitionGuard:
    """Feed streamed text with :meth:`feed`; it returns True once the reply loops."""

    def __init__(self) -> None:
        self._pending = ""  # text after the last unit end
        self._pending_start = 0  # absolute offset of ``_pending[0]``
        self._search_from = 0  # index in ``_pending`` not yet searched
        self._line_start = True  # does ``_pending`` start at a line start?
        self._in_fence = False
        # (normalized unit or _BREAK, absolute start offset, absolute end offset)
        self._units: list[tuple[object, int, int]] = []
        self.keep_chars: int | None = None

    @property
    def tripped(self) -> bool:
        return self.keep_chars is not None

    def break_run(self) -> None:
        """End any run in progress (a tool call sits between two stretches of text)."""
        self._push_break(self._pending_start)

    def feed(self, chunk: str) -> bool:
        if self.tripped:
            return True
        if not chunk:
            return False
        self._pending += chunk
        while True:
            match = _UNIT_END.search(self._pending, self._search_from)
            if match is None or (
                match.end() == len(self._pending) and self._pending[match.start()] in ".!?"
            ):
                # No definite unit end yet. Next time search only the new text,
                # plus the last character, whose lookahead was undecidable.
                self._search_from = max(0, len(self._pending) - 1)
                if len(self._pending) > _MAX_PENDING:
                    self._pending_start += len(self._pending)
                    self._pending = ""
                    self._search_from = 0
                    self._line_start = False
                    self._push_break(self._pending_start)
                return False
            end = match.end()
            raw = self._pending[:end]
            at_line_start = self._line_start
            self._line_start = raw.endswith("\n")
            start = self._pending_start
            self._pending = self._pending[end:]
            self._pending_start = start + end
            self._search_from = 0
            self._add_unit(raw, start, start + end, at_line_start)
            if self._check():
                return True

    def kept_text(self, reply: str) -> str:
        """``reply`` cut to one copy of the loop (unchanged when not tripped)."""
        if self.keep_chars is None:
            return reply
        return reply[: self.keep_chars].rstrip()

    def _push_break(self, at: int) -> None:
        if not self._units or self._units[-1][0] is not _BREAK:
            self._units.append((_BREAK, at, at))

    def _add_unit(self, raw: str, start: int, end: int, at_line_start: bool) -> None:
        if at_line_start and _FENCE.match(raw):
            self._in_fence = not self._in_fence
            self._push_break(end)
            return
        if self._in_fence or (at_line_start and _NOT_PROSE.match(raw)):
            self._push_break(end)
            return
        norm = _WS.sub(" ", raw.strip()).lower()
        if not norm:
            return
        if not any(c.isalpha() for c in norm):
            self._push_break(end)
            return
        self._units.append((norm, start, end))
        limit = MAX_PERIOD * BLOCK_REPEATS + SINGLE_UNIT_REPEATS
        if len(self._units) > limit:
            del self._units[:-limit]

    def _check(self) -> bool:
        units = self._units
        for period in range(1, MAX_PERIOD + 1):
            repeats = SINGLE_UNIT_REPEATS if period == 1 else BLOCK_REPEATS
            span = period * repeats
            if len(units) < span:
                continue
            tail = units[-span:]
            if any(u is _BREAK for u, _, _ in tail):
                continue
            block = [u for u, _, _ in tail[:period]]
            if period > 1 and len(set(block)) == 1:
                continue  # a single unit repeated; period 1 owns that case
            if not all(tail[i][0] == block[i % period] for i in range(span)):
                continue
            if tail[-1][2] - tail[0][1] < MIN_LOOP_CHARS:
                continue
            # Keep through the end of the FIRST copy of the block.
            self.keep_chars = tail[period - 1][2]
            return True
        return False
