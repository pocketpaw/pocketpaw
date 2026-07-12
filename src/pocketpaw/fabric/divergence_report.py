# src/pocketpaw/fabric/divergence_report.py
# Created: 2026-07-11 (FST-8 — the operational proof kit).
#
# The shadow-divergence report: parse the store's grep-stable divergence
# lines out of a log capture, summarize them, and answer the one rollout
# question that gates enforce — "is shadow clean enough to enforce?".
# Read-only and dependency-free (stdlib argparse + json + re): it never
# imports the store, never touches a database, and is safe to run against
# production logs. CLI: ``python -m pocketpaw.fabric.divergence_report
# <logfile> [logfile2 ...]`` (or stdin).
"""Parse + summarize Fabric shadow-divergence log lines (FST-8).

The contract (emitted by ``pocketpaw.fabric.store`` at every merge site,
one single line per statement-producing property)::

    fabric shadow: object=<id> property=<p> lww=<json> resolver=<json>
        diverged=<True|False> disputed=<True|False> unresolvable=<True|False>
        freshness=<fresh|aging|stale|none>

``lww`` and ``resolver`` are ``json.dumps``'d (strings quoted), so the
line never wraps and both values round-trip losslessly. The failure-shield
warning uses a distinct prefix (``fabric shadow: statement pass failed for
object=...``) so ``fabric shadow: object=`` isolates divergence lines
exactly.

Coverage caveat (FST-4): shadow recording is **at-most-once** per journal
event — replays and retries are not double-counted, but a report is a
lower bound on write traffic, not an exact ledger. An empty report can
also mean shadow mode never ran; confirm the mode before reading "no
lines" as "no divergence".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The exact prefix of a divergence line. The failure-shield warning starts
#: with "fabric shadow: statement pass failed for object=" — it never
#: matches this, so the prefix alone isolates divergence lines.
DIVERGENCE_PREFIX = "fabric shadow: object="

# Head: everything before the JSON-encoded lww value. object ids and
# property names are token-shaped (no whitespace) — the same assumption the
# FST-3/5 contract-guard regexes in the test suite make.
_HEAD_RE = re.compile(r"^fabric shadow: object=(?P<object_id>\S+) property=(?P<property>\S+) lww=")

# Tail: the four fixed flags after the JSON-encoded resolver value.
_TAIL_RE = re.compile(
    r"^ diverged=(?P<diverged>True|False)"
    r" disputed=(?P<disputed>True|False)"
    r" unresolvable=(?P<unresolvable>True|False)"
    r" freshness=(?P<freshness>fresh|aging|stale|none)\s*$"
)

_DECODER = json.JSONDecoder()


@dataclass(frozen=True)
class DivergenceRecord:
    """One parsed divergence line (or one malformed line, kept for counting).

    A line that starts with :data:`DIVERGENCE_PREFIX` but does not parse
    becomes a record with ``parse_error`` set (and neutral field values) —
    the tolerant-parser contract: malformed lines are *counted*, never
    raised, and never silently dropped.
    """

    object_id: str
    property: str
    lww: Any
    resolver: Any
    diverged: bool
    disputed: bool
    unresolvable: bool
    freshness: str  # "fresh" | "aging" | "stale" | "none"
    raw: str
    parse_error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the line parsed cleanly."""
        return self.parse_error is None

    @property
    def unexplained(self) -> bool:
        """The enforce-gate heuristic, per divergence line (PRD metric).

        A divergence is *explained* when the line itself shows why the
        resolver disagreed with LWW: the conflict is flagged
        (``disputed=True``), the resolver fell back on an unresolvable tie
        (``unresolvable=True``), or freshness demotion was in play
        (``freshness != "fresh"``). Those are multi-source ordering doing
        its job — expected in shadow, safe to enforce.

        *Unexplained* = ``diverged=True`` with none of those markers: the
        resolver overrode the freshest, undisputed data and nothing on the
        line says why. Target ZERO before flipping to enforce — each one is
        either a trust-ladder surprise or a bug, and both need a human look.

        Honest limits: this is a line-local heuristic. It cannot see
        PIN/IGNORE curation, per-type ladder overrides, or cross-line
        context — an "explained" line can still be wrong data winning for
        a legible reason, and coverage is at-most-once per journal event
        (see the module docstring).
        """
        return (
            self.ok
            and self.diverged
            and not (self.disputed or self.unresolvable or self.freshness != "fresh")
        )


def _malformed(raw: str, why: str) -> DivergenceRecord:
    return DivergenceRecord(
        object_id="",
        property="",
        lww=None,
        resolver=None,
        diverged=False,
        disputed=False,
        unresolvable=False,
        freshness="none",
        raw=raw,
        parse_error=why,
    )


def parse_divergence_lines(lines: Iterable[str]) -> list[DivergenceRecord]:
    """Tolerantly parse divergence lines out of arbitrary log text.

    * Lines that do not start with :data:`DIVERGENCE_PREFIX` (other log
      output, the failure-shield warning, blanks) are skipped entirely.
    * Lines that carry the prefix but violate the contract yield a record
      with ``parse_error`` set — counted by :func:`summarize` as
      ``parse_errors``. This function NEVER raises on input content.

    The ``lww``/``resolver`` values are decoded with a JSON-aware scan
    (``JSONDecoder.raw_decode``), not whitespace splitting, so values
    containing spaces (quoted strings, objects, lists) parse exactly.
    """
    records: list[DivergenceRecord] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line.startswith(DIVERGENCE_PREFIX):
            continue
        head = _HEAD_RE.match(line)
        if head is None:
            records.append(_malformed(line, "head does not match the contract"))
            continue
        rest = line[head.end() :]
        try:
            lww, end = _DECODER.raw_decode(rest)
        except ValueError:
            records.append(_malformed(line, "lww value is not valid JSON"))
            continue
        rest = rest[end:]
        if not rest.startswith(" resolver="):
            records.append(_malformed(line, "missing resolver= field after lww"))
            continue
        rest = rest[len(" resolver=") :]
        try:
            resolver, end = _DECODER.raw_decode(rest)
        except ValueError:
            records.append(_malformed(line, "resolver value is not valid JSON"))
            continue
        tail = _TAIL_RE.match(rest[end:])
        if tail is None:
            records.append(_malformed(line, "tail flags do not match the contract"))
            continue
        records.append(
            DivergenceRecord(
                object_id=head.group("object_id"),
                property=head.group("property"),
                lww=lww,
                resolver=resolver,
                diverged=tail.group("diverged") == "True",
                disputed=tail.group("disputed") == "True",
                unresolvable=tail.group("unresolvable") == "True",
                freshness=tail.group("freshness"),
                raw=line,
                parse_error=None,
            )
        )
    return records


@dataclass(frozen=True)
class DivergenceSummary:
    """Aggregate view over parsed divergence records — the go/no-go input."""

    total: int  # cleanly parsed lines
    parse_errors: int  # prefix-matching lines that violated the contract
    diverged: int
    diverged_rate: float  # diverged / total (0.0 when total == 0)
    disputed: int
    unresolvable: int
    unexplained: int  # see DivergenceRecord.unexplained — target ZERO
    top_diverged_properties: tuple[tuple[str, int], ...]  # (property, diverged-count), descending
    freshness: dict[str, int] = field(default_factory=dict)  # over all parsed lines

    @property
    def enforce_ready(self) -> bool:
        """The go/no-go verdict: zero unexplained divergences AND zero
        parse errors (an unparseable line could hide an unexplained
        divergence, so malformed input blocks the green light too)."""
        return self.unexplained == 0 and self.parse_errors == 0


def summarize(records: Iterable[DivergenceRecord], *, top_n: int = 5) -> DivergenceSummary:
    """Fold parsed records into a :class:`DivergenceSummary`.

    Malformed records (``parse_error`` set) count toward ``parse_errors``
    only; every other statistic is computed over cleanly parsed lines.
    """
    materialized = list(records)  # records may be a one-shot generator
    valid = [r for r in materialized if r.ok]
    parse_errors = len(materialized) - len(valid)

    diverged = [r for r in valid if r.diverged]
    per_property = Counter(r.property for r in diverged)
    freshness = Counter(r.freshness for r in valid)
    total = len(valid)
    return DivergenceSummary(
        total=total,
        parse_errors=parse_errors,
        diverged=len(diverged),
        diverged_rate=(len(diverged) / total) if total else 0.0,
        disputed=sum(1 for r in valid if r.disputed),
        unresolvable=sum(1 for r in valid if r.unresolvable),
        unexplained=sum(1 for r in valid if r.unexplained),
        top_diverged_properties=tuple(per_property.most_common(top_n)),
        freshness=dict(freshness),
    )


def format_report(summary: DivergenceSummary) -> str:
    """A compact human-readable block ending in the verdict line."""
    pct = f"{summary.diverged_rate * 100:.1f}%"
    lines = [
        "fabric divergence report",
        f"  lines:         {summary.total} parsed, {summary.parse_errors} malformed",
        f"  diverged:      {summary.diverged}/{summary.total} ({pct})",
        f"  disputed:      {summary.disputed}",
        f"  unresolvable:  {summary.unresolvable}",
        f"  unexplained:   {summary.unexplained}",
    ]
    if summary.freshness:
        dist = " ".join(
            f"{k}={summary.freshness[k]}"
            for k in ("fresh", "aging", "stale", "none")
            if k in summary.freshness
        )
        lines.append(f"  freshness:     {dist}")
    if summary.top_diverged_properties:
        lines.append("  top diverged properties:")
        width = max(len(p) for p, _ in summary.top_diverged_properties)
        lines.extend(f"    {p:<{width}}  {n}" for p, n in summary.top_diverged_properties)
    if summary.total == 0 and summary.parse_errors == 0:
        lines.append("  note: no divergence lines found — confirm shadow mode actually ran")
    verdict = "yes" if summary.enforce_ready else "no"
    detail = f"{summary.unexplained} unexplained"
    if summary.parse_errors:
        detail += f", {summary.parse_errors} parse errors"
    lines.append(f"ENFORCE-READY: {verdict} ({detail})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: report over one or more log files (or stdin).

    Exit code 0 when ENFORCE-READY, 1 when not — usable as a CI/rollout
    gate. Exit 2 on unreadable input files.
    """
    parser = argparse.ArgumentParser(
        prog="python -m pocketpaw.fabric.divergence_report",
        description=(
            "Summarize Fabric shadow-divergence log lines and answer the "
            "enforce go/no-go. Reads the given log files, or stdin when no "
            "files (or '-') are given."
        ),
    )
    parser.add_argument("logfiles", nargs="*", help="log files to scan ('-' or none = stdin)")
    parser.add_argument(
        "--top", type=int, default=5, metavar="N", help="top-N diverged properties (default 5)"
    )
    args = parser.parse_args(argv)

    lines: list[str] = []
    sources = args.logfiles or ["-"]
    for name in sources:
        if name == "-":
            lines.extend(sys.stdin.read().splitlines())
            continue
        try:
            with open(name, encoding="utf-8", errors="replace") as fh:
                lines.extend(fh.read().splitlines())
        except OSError as exc:
            print(f"error: cannot read {name}: {exc}", file=sys.stderr)
            return 2

    summary = summarize(parse_divergence_lines(lines), top_n=args.top)
    print(format_report(summary))
    return 0 if summary.enforce_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
