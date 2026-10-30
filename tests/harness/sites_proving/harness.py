"""The proving harness: a scenario registry and a machine-readable evidence report.

Created for SG-1 (sites proving harness).

WHAT: ``register`` names a scenario, ``run_scenario`` executes one and captures
its result, ``EvidenceReport`` writes both a JSON file (for later slices and CI
to read) and a readable text summary (for a human).

WHY a registry rather than plain pytest tests: the program's later slices are a
MATRIX (scenarios A1..A8 and beyond, four lanes, ten fallback rungs) and the
question each asks is "which rung served this, with what evidence". A registry
gives every scenario the same record shape, so SG-12's findings report can read
one JSON file instead of scraping test logs. pytest still drives the runs — the
registry is what makes the results comparable across slices.

WHY every record carries ``fallback_rung``: SG-7 exercises all ten rungs, and a
scenario that passed on rung 1 is a different fact from one that passed on rung 4.
Only ``prebuilt-ssr`` exists in SG-1; the field is populated from the bundle so it
is never guessed.

Evidence artifacts (the rendered HTML) are written next to the report so a failure
can be inspected rather than re-run. The report directory is gitignored — evidence
is a build output, not source.
"""

from __future__ import annotations

import json
import platform
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bundle import Bundle

REPORT_DIR = Path(__file__).resolve().parent / "evidence"

# A scenario returns the bundle it produced (if any) plus whatever facts it wants
# on the record. A scenario that proves a FAILURE (A8) returns no bundle.
ScenarioResult = tuple[Bundle | None, Mapping[str, Any]]
ScenarioFn = Callable[[], ScenarioResult]


@dataclass
class Scenario:
    """One registered proving scenario."""

    id: str
    description: str
    fn: ScenarioFn
    # True when PASSING means the scenario's operation FAILED CLOSED (A8). Kept
    # explicit so a fail-closed scenario can never be misread as a broken test.
    expects_failure: bool = False


@dataclass
class ScenarioRecord:
    """The evidence for one scenario run."""

    id: str
    description: str
    passed: bool
    fallback_rung: str | None
    duration_ms: float
    evidence_path: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    traceback: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "passed": self.passed,
            "fallback_rung": self.fallback_rung,
            "duration_ms": round(self.duration_ms, 2),
            "evidence_path": self.evidence_path,
            "details": dict(self.details),
            "error": self.error,
            "traceback": self.traceback,
        }


class ScenarioRegistry:
    """Named scenarios, in registration order."""

    def __init__(self) -> None:
        self._scenarios: dict[str, Scenario] = {}

    def register(
        self, scenario_id: str, description: str, *, expects_failure: bool = False
    ) -> Callable[[ScenarioFn], ScenarioFn]:
        """Decorator: register a scenario under ``scenario_id``."""

        def decorate(fn: ScenarioFn) -> ScenarioFn:
            if scenario_id in self._scenarios:
                raise ValueError(f"scenario {scenario_id!r} is already registered")
            self._scenarios[scenario_id] = Scenario(
                id=scenario_id,
                description=description,
                fn=fn,
                expects_failure=expects_failure,
            )
            return fn

        return decorate

    def get(self, scenario_id: str) -> Scenario:
        try:
            return self._scenarios[scenario_id]
        except KeyError:
            raise KeyError(
                f"no scenario {scenario_id!r} (registered: {sorted(self._scenarios)})"
            ) from None

    def __iter__(self) -> Iterator[Scenario]:
        return iter(self._scenarios.values())

    def __len__(self) -> int:
        return len(self._scenarios)

    def ids(self) -> list[str]:
        return list(self._scenarios)


REGISTRY = ScenarioRegistry()
register = REGISTRY.register


def _write_evidence(scenario_id: str, bundle: Bundle, report_dir: Path) -> str:
    """Write the rendered entry HTML so a result can be inspected, not re-run."""
    artifacts = report_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    path = artifacts / f"{scenario_id}-{bundle.manifest.entry_html}"
    path.write_bytes(bundle.entry_bytes)
    return str(path.relative_to(report_dir).as_posix())


def run_scenario(scenario_id: str, *, report_dir: Path = REPORT_DIR) -> ScenarioRecord:
    """Run one scenario and build its record.

    A scenario's own exception is CAUGHT and recorded as a failure — the harness
    reports on scenarios, it does not crash with them. The one thing it will not
    do is call a scenario passed: ``expects_failure`` scenarios must themselves
    assert that the operation raised, and they still fail here if they raise
    something unplanned.
    """
    scenario = REGISTRY.get(scenario_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    try:
        bundle, details = scenario.fn()
    except Exception as exc:
        return ScenarioRecord(
            id=scenario.id,
            description=scenario.description,
            passed=False,
            fallback_rung=None,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
        )

    duration_ms = (time.perf_counter() - started) * 1000.0
    evidence_path: str | None = None
    rung: str | None = None
    payload = dict(details)

    if bundle is not None:
        evidence_path = _write_evidence(scenario.id, bundle, report_dir)
        rung = bundle.manifest.fallback_rung
        payload["manifest"] = bundle.manifest.as_dict()
        payload["bundle_files"] = sorted(bundle.files)
    elif scenario.expects_failure:
        # A fail-closed scenario has no bundle by design; its rung is the rung
        # that refused to produce one.
        rung = str(payload.get("fallback_rung") or "") or None

    return ScenarioRecord(
        id=scenario.id,
        description=scenario.description,
        passed=True,
        fallback_rung=rung,
        duration_ms=duration_ms,
        evidence_path=evidence_path,
        details=payload,
    )


class EvidenceReport:
    """Collects scenario records plus free-form measurements, then writes both files."""

    def __init__(self, *, report_dir: Path = REPORT_DIR, slice_id: str = "SG-1") -> None:
        self.report_dir = report_dir
        self.slice_id = slice_id
        self.records: list[ScenarioRecord] = []
        self.measurements: dict[str, Any] = {}
        self.notes: dict[str, Any] = {}

    def add(self, record: ScenarioRecord) -> ScenarioRecord:
        self.records.append(record)
        return record

    def run(self, scenario_id: str) -> ScenarioRecord:
        return self.add(run_scenario(scenario_id, report_dir=self.report_dir))

    def measure(self, name: str, value: Any) -> None:
        self.measurements[name] = value

    def note(self, name: str, value: Any) -> None:
        self.notes[name] = value

    @property
    def all_passed(self) -> bool:
        return bool(self.records) and all(r.passed for r in self.records)

    def as_dict(self) -> dict[str, Any]:
        return {
            "slice": self.slice_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "all_passed": self.all_passed,
            "scenarios": [r.as_dict() for r in self.records],
            "measurements": self.measurements,
            "notes": self.notes,
        }

    def _summary_text(self) -> str:
        lines = [
            f"{self.slice_id} proving harness — evidence summary",
            f"generated {time.strftime('%Y-%m-%d %H:%M:%S')}  python {sys.version.split()[0]}",
            "",
            f"scenarios: {sum(1 for r in self.records if r.passed)}/{len(self.records)} passed",
            "",
        ]
        for record in self.records:
            lines.append(
                f"  [{'PASS' if record.passed else 'FAIL'}] {record.id}  "
                f"rung={record.fallback_rung or '-'}  {record.duration_ms:.0f}ms"
            )
            lines.append(f"         {record.description}")
            if record.evidence_path:
                lines.append(f"         evidence: {record.evidence_path}")
            if record.error:
                lines.append(f"         error: {record.error}")

        if self.measurements:
            lines += ["", "measurements:"]
            for name, value in self.measurements.items():
                lines.append(f"  {name}:")
                if isinstance(value, Mapping):
                    for key, val in value.items():
                        lines.append(f"    {key}: {val}")
                else:
                    lines.append(f"    {value}")

        if self.notes:
            lines += ["", "notes:"]
            for name, value in self.notes.items():
                lines.append(f"  {name}: {value}")

        return "\n".join(lines) + "\n"

    def write(self) -> tuple[Path, Path]:
        """Write ``report.json`` + ``summary.txt``. Returns both paths."""
        self.report_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.report_dir / "report.json"
        text_path = self.report_dir / "summary.txt"
        json_path.write_text(
            json.dumps(self.as_dict(), indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        text_path.write_text(self._summary_text(), encoding="utf-8")
        return json_path, text_path
