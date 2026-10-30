"""Run the SD-6 fixtures through any producer and score the result.

New file 2026-09-08 (feat/sites-design-skills), part of SD-6.

The producer is injected rather than imported, so this package never has to know
which backend, provider, or surface produced the page. That is what lets the same
fixtures score every model in the routing table, which both diet plans require and
neither can do with a hard-wired client.

    def produce(prompt: str) -> str:
        ...  # call a model through whatever path you are measuring
        return site_source

    report = run(produce)
    print(report.render())
    write_baseline(report, Path("tests/harness/sites_prompt_eval/baseline.json"))

WHY "n/a" IS COUNTED SEPARATELY AND NOT AS A PASS. A page with no buttons cannot
demonstrate a hit-area rule, and scoring that as a pass is how an eval reports a
rising score while the pages get emptier. ``Report.score`` is passes over
passes+fails; the n/a count is reported beside it so a jump in n/a is visible.

WHY A FAILED PRODUCE IS A HARD ERROR ROW AND NOT A SKIP. If the model errors on
two of six briefs, a report that silently scores the remaining four is a report
that says the prompt got better.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import behaviors, fixtures
from .behaviors import Verdict

Producer = Callable[[str], str]


@dataclass
class PageResult:
    key: str
    verdicts: tuple[Verdict, ...] = ()
    error: str | None = None


@dataclass
class Report:
    pages: list[PageResult] = field(default_factory=list)
    cross: list[Verdict] = field(default_factory=list)
    label: str = ""

    def _all(self) -> list[Verdict]:
        out: list[Verdict] = []
        for p in self.pages:
            out.extend(p.verdicts)
        out.extend(self.cross)
        return out

    @property
    def passes(self) -> int:
        return sum(1 for v in self._all() if v.ok is True)

    @property
    def fails(self) -> int:
        return sum(1 for v in self._all() if v.ok is False)

    @property
    def not_applicable(self) -> int:
        return sum(1 for v in self._all() if v.ok is None)

    @property
    def errors(self) -> int:
        return sum(1 for p in self.pages if p.error)

    @property
    def score(self) -> float:
        judged = self.passes + self.fails
        return self.passes / judged if judged else 0.0

    def render(self) -> str:
        lines = [f"sites prompt eval — {self.label or 'unlabelled run'}"]
        for p in self.pages:
            if p.error:
                lines.append(f"  {p.key}: ERROR {p.error}")
                continue
            for v in p.verdicts:
                lines.append(f"  {p.key:<16} {v.symbol:<4} {v.name:<22} {v.detail}")
        for v in self.cross:
            lines.append(f"  {'(pair)':<16} {v.symbol:<4} {v.name:<22} {v.detail}")
        lines.append(
            f"  score {self.passes}/{self.passes + self.fails} "
            f"({self.score:.0%}), {self.not_applicable} n/a, {self.errors} errors"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "score": round(self.score, 4),
            "passes": self.passes,
            "fails": self.fails,
            "not_applicable": self.not_applicable,
            "errors": self.errors,
            "pages": {
                p.key: ({"error": p.error} if p.error else {v.name: v.symbol for v in p.verdicts})
                for p in self.pages
            },
            "cross": {v.name: v.symbol for v in self.cross},
        }


def score_page(source: str) -> tuple[Verdict, ...]:
    """Every page scorer against one generated page."""
    return tuple(scorer(source) for scorer in behaviors.PAGE_SCORERS)


def run(produce: Producer, label: str = "") -> Report:
    """Produce every create fixture, score each page, then score each pair."""
    report = Report(label=label)
    rendered: dict[str, str] = {}

    for brief in fixtures.CREATE_BRIEFS:
        try:
            source = produce(brief.prompt)
        except Exception as exc:  # noqa: BLE001 — an error row, never a skip
            report.pages.append(PageResult(brief.key, error=f"{type(exc).__name__}: {exc}"))
            continue
        rendered[brief.key] = source
        report.pages.append(PageResult(brief.key, verdicts=score_page(source)))

    for pair_name, briefs in fixtures.pairs().items():
        keys = [b.key for b in briefs if b.key in rendered]
        if len(keys) < 2:
            continue
        v = behaviors.rotation(rendered[keys[0]], rendered[keys[1]])
        report.cross.append(Verdict(f"{v.name}:{pair_name}", v.ok, v.detail))

    return report


def write_baseline(report: Report, path: Path) -> None:
    """Record a run so the next one can be diffed against it.

    A slice from the diet plan ships when the score is unchanged against the
    recorded baseline, and does not when it moves — however good the reasoning
    looked. Never hand-edit this file to make a diff go away.
    """
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")


def read_baseline(path: Path) -> dict | None:
    """The recorded run, or None when no baseline has been taken yet."""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
