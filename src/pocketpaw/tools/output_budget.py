# Tool-output budget — caps noisy tool results before they reach agent context.
# Created: 2026-05-21 (#1160)
#
# A tool that returns a large blob (a test run, a build log, an HTTP body,
# long command stdout) used to drop the raw blob straight into the agent's
# context window. That burns tokens and buries the signal the agent needs.
#
# cap_tool_output() is the single post-processor applied at the tool return
# boundary (BaseTool._success / BaseTool._error) and at the registry
# chokepoint (ToolRegistry.execute). Small results pass through untouched.
# Oversized results get one of two treatments:
#   - structured formats (pytest, ruff/flake8 lint output) -> a salient-lines
#     extract that keeps failures, errors, and the summary line
#   - everything else -> a deterministic head + tail slice with an elision
#     marker stating how much was dropped
#
# The transform is deterministic (no LLM call) and idempotent: feeding a
# capped string back through cap_tool_output() is a no-op, so applying it at
# two boundaries never double-truncates.

from __future__ import annotations

import re

# Default cap on a single tool result, in characters. Roughly 3k tokens.
# Override per call via the ``cap`` argument — the tool registry passes
# ``Settings.tool_output_char_cap`` so deployments can tune it.
TOOL_OUTPUT_CHAR_CAP = 12_000

# When a blob is sliced, how to split the kept budget between head and tail.
# Head gets the larger share — the start of a log usually carries the command,
# the config, and the first failure; the tail carries the summary line.
_HEAD_FRACTION = 0.6

# Marker dropped between the head and tail slices. Stays recognisable so the
# agent (and a human reading a transcript) can see truncation happened, and so
# a second pass through cap_tool_output() detects an already-capped string.
_ELISION_PREFIX = "... [tool output truncated:"

# A salient-lines extract caps how many lines it keeps so a pathological run
# with thousands of failures still can't blow the budget.
_MAX_SALIENT_LINES = 120

# Lines worth keeping from a structured test / lint blob. Case-insensitive.
_SALIENT_PATTERNS = (
    re.compile(r"\bFAILED\b"),
    re.compile(r"\bERROR\b"),
    re.compile(r"\bPASSED\b.*\bFAILED\b"),  # mixed pytest summary lines
    re.compile(r"^={3,}.*={3,}$"),  # pytest === short test summary === banners
    re.compile(r"^_{3,}"),  # pytest per-failure ____ separators
    re.compile(r"\b\d+ (passed|failed|error|errors|skipped|xfailed|warning)"),
    re.compile(r"^E\s"),  # pytest assertion detail lines
    re.compile(r":\d+:\d+:\s+[EWF]\d"),  # ruff / flake8 "file:line:col: E501 ..."
    re.compile(r"\bwould reformat\b"),  # ruff format check
    re.compile(r"\b(traceback|exception)\b", re.IGNORECASE),
)

# Cheap signal that a blob is a test / lint run rather than free-form text.
# Needs at least one of these before the salient-lines path is taken.
_STRUCTURED_HINTS = (
    re.compile(r"\b\d+ (passed|failed|error|errors)\b"),
    re.compile(r"={3,}\s*(test session starts|short test summary)", re.IGNORECASE),
    re.compile(r":\d+:\d+:\s+[EWF]\d{2,}"),  # ruff/flake8 diagnostic line
    re.compile(r"^(FAILED|ERROR) ", re.MULTILINE),
)


def _looks_structured(text: str) -> bool:
    """True when *text* looks like pytest / ruff / flake8 output.

    Conservative on purpose: a false negative just falls back to the head+tail
    slice, but a false positive could drop the wrong lines from prose.
    """
    return any(hint.search(text) for hint in _STRUCTURED_HINTS)


def _is_salient(line: str) -> bool:
    """True when *line* carries signal worth keeping from a structured blob."""
    return any(pat.search(line) for pat in _SALIENT_PATTERNS)


def _format_marker(dropped_chars: int, dropped_lines: int | None = None) -> str:
    """Build the elision marker describing how much content was removed."""
    if dropped_lines is not None:
        return f"{_ELISION_PREFIX} {dropped_chars:,} chars / {dropped_lines:,} lines elided] ..."
    return f"{_ELISION_PREFIX} {dropped_chars:,} chars elided] ..."


def _head_tail(text: str, cap: int) -> str:
    """Deterministic head + tail slice of *text* with an elision marker.

    The marker itself counts against the budget so the result is always
    ``<= cap`` characters. If the cap is too small to fit the marker plus a
    little content, fall back to a plain head slice.
    """
    marker_estimate = len(_format_marker(len(text)))
    usable = cap - marker_estimate - 2  # 2 for the joining newlines
    if usable <= 0:
        # Cap is pathologically small — just hard-truncate the head.
        return text[:cap]

    head_len = int(usable * _HEAD_FRACTION)
    tail_len = usable - head_len
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len > 0 else ""
    dropped = len(text) - head_len - tail_len
    marker = _format_marker(dropped)
    return f"{head}\n{marker}\n{tail}"


def _salient_extract(text: str, cap: int) -> str:
    """Pull the signal lines out of a structured test / lint blob.

    Keeps lines that match a salient pattern, in original order, up to
    ``_MAX_SALIENT_LINES`` and the character cap. Prepends a marker noting how
    many lines were dropped. Falls back to ``_head_tail`` when no salient line
    is found (so a misclassified blob is still capped sanely).
    """
    lines = text.splitlines()
    kept: list[str] = [ln for ln in lines if _is_salient(ln)]
    if not kept:
        return _head_tail(text, cap)

    # When there are more salient lines than the line budget, keep a head
    # AND a tail of them. The tail matters: a test / lint run's tally line
    # ("2 failed, 1998 passed", "Found 2000 errors.") and short-summary
    # section sit at the very end and are the lines an agent needs most.
    if len(kept) > _MAX_SALIENT_LINES:
        head_count = int(_MAX_SALIENT_LINES * _HEAD_FRACTION)
        tail_count = _MAX_SALIENT_LINES - head_count
        inner_dropped = len(kept) - head_count - tail_count
        kept = (
            kept[:head_count]
            + [f"... [{inner_dropped:,} more salient lines elided] ..."]
            + kept[-tail_count:]
        )

    dropped_lines = len(lines) - sum(1 for ln in kept if not ln.startswith("... ["))
    body = "\n".join(kept)
    marker = _format_marker(len(text) - len(body), dropped_lines)
    extract = f"{marker}\n{body}"

    # The extract can itself exceed the cap on a run with very many failures;
    # head+tail the assembled extract so the contract (<= cap) always holds.
    if len(extract) > cap:
        return _head_tail(extract, cap)
    return extract


def cap_tool_output(
    result: str,
    *,
    cap: int | None = None,
    tool_name: str = "",
) -> str:
    """Cap a single tool result so a noisy blob can't flood agent context.

    Args:
        result: The raw string a tool produced.
        cap: Maximum characters to allow through. Defaults to
            ``TOOL_OUTPUT_CHAR_CAP``. A non-positive cap disables capping.
        tool_name: Optional tool name, kept for future per-tool tuning and
            logging. Unused today.

    Returns:
        ``result`` unchanged when it is within the cap, otherwise a shorter
        string: a salient-lines extract for recognised structured formats, or
        a deterministic head + tail slice for everything else. The result is
        always ``<= cap`` characters and the transform is idempotent.
    """
    if not result:
        return result

    effective_cap = TOOL_OUTPUT_CHAR_CAP if cap is None else cap
    if effective_cap <= 0:
        return result  # capping disabled

    if len(result) <= effective_cap:
        return result  # common case — normal-sized output, untouched

    if _looks_structured(result):
        return _salient_extract(result, effective_cap)
    return _head_tail(result, effective_cap)
