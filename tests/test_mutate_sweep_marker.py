# tests/test_mutate_sweep_marker.py — the sweep marker in scripts/mutate.py.
#
# Created 2026-08-09. mutate.py had no tests, which is a little ironic for the tool
# whose whole argument is that untested code agrees with untested tests.
#
# WHY THE MARKER EXISTS, since that is what these assert. Mutations are applied IN
# PLACE, so while one is live the working tree really does contain broken code. On
# 2026-08-09 a reviewer ran the suite during a sweep, saw a bun-PATH assertion fail,
# and reported it as a regression — it was this script working correctly. The marker
# gives a concurrent reader something `git status` will surface.
#
# The marker must never OUTLIVE its sweep, because a permanent marker trains people to
# ignore it, and the one case it needs to be believed is a crashed run that left a file
# mutated. So the tests below care most about the cleanup paths — including the
# SystemExit path, which does not pass through the try/finally and needed atexit.

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MUTATE = REPO_ROOT / "scripts" / "mutate.py"
MARKER = REPO_ROOT / ".mutation-sweep-active"


def _run(plan_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MUTATE), "--plan", str(plan_path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def _write_plan(tmp_path: Path, mutations: list[dict]) -> Path:
    # Plans resolve `file` against REPO_ROOT, so the target must be a real repo file;
    # the plan itself can live anywhere.
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(mutations), encoding="utf-8")
    return plan


def _target() -> str:
    """A repo file with a stable, unique anchor. build_state.py is this sprint's and
    its frozenset literal appears exactly once."""
    return "ee/pocketpaw_ee/sites/build_state.py"


_ANCHOR = 'IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"queued", "building"})'


class TestTheMarkerNeverOutlivesItsSweep:
    def test_cleared_after_a_sweep_where_the_mutation_is_caught(self, tmp_path: Path) -> None:
        plan = _write_plan(
            tmp_path,
            [
                {
                    "label": "queued stops counting as in-flight",
                    "file": _target(),
                    "find": _ANCHOR,
                    "replace": 'IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"building"})',
                    "tests": ["tests/ee/sites/test_build_state.py"],
                }
            ],
        )
        proc = _run(plan)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not MARKER.exists()

    def test_cleared_when_the_plan_aborts_on_a_bad_anchor(self, tmp_path: Path) -> None:
        """The path that needed ``atexit``. Plan validation raises SystemExit BEFORE the
        try/finally is entered, so a marker written just beforehand would otherwise
        survive forever — and a permanent marker trains people to ignore it."""
        plan = _write_plan(
            tmp_path,
            [
                {
                    "label": "anchor that does not exist",
                    "file": _target(),
                    "find": "this text is definitely not in the file",
                    "replace": "nor is this",
                    "tests": ["tests/ee/sites/test_build_state.py"],
                }
            ],
        )
        proc = _run(plan)
        assert proc.returncode != 0
        assert "anchor not found" in (proc.stdout + proc.stderr)
        assert not MARKER.exists()

    def test_the_file_is_restored_after_the_sweep(self, tmp_path: Path) -> None:
        """Restoration is the thing this script must never get wrong; the marker is
        only the signal. Asserted together because a leftover marker and a leftover
        mutation are the same incident."""
        target = REPO_ROOT / _target()
        before = target.read_bytes()
        plan = _write_plan(
            tmp_path,
            [
                {
                    "label": "queued stops counting as in-flight",
                    "file": _target(),
                    "find": _ANCHOR,
                    "replace": 'IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"building"})',
                    "tests": ["tests/ee/sites/test_build_state.py"],
                }
            ],
        )
        _run(plan)
        assert target.read_bytes() == before
        assert not MARKER.exists()


class TestABadAnchorIsLoud:
    def test_an_unmatched_anchor_exits_non_zero_and_runs_nothing(self, tmp_path: Path) -> None:
        """This already worked — recorded as a test because I reported it as broken.

        The real trap is on the CALLER's side: chaining sweeps with ``;`` and piping
        through ``tail`` discards the non-zero exit, so an aborted plan looks like a
        pass next to another plan's success line. Use ``&&``, or check ``$?`` per plan.
        """
        plan = _write_plan(
            tmp_path,
            [
                {
                    "label": "good anchor that would be caught",
                    "file": _target(),
                    "find": _ANCHOR,
                    "replace": 'IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"building"})',
                    "tests": ["tests/ee/sites/test_build_state.py"],
                },
                {
                    "label": "bad anchor later in the same plan",
                    "file": _target(),
                    "find": "definitely absent",
                    "replace": "x",
                    "tests": ["tests/ee/sites/test_build_state.py"],
                },
            ],
        )
        proc = _run(plan)
        assert proc.returncode != 0
        # Validation is upfront, so NOTHING ran — not even the valid first mutation.
        assert "caught" not in proc.stdout
        assert "ESCAPED" not in proc.stdout


class TestMarkerAgeDecidesLiveVsAbandoned:
    """A pre-existing marker is always reported, but the age changes WHAT it says:
    recent means another sweep may be racing this one, old means a crashed run left it.
    Biased toward "possibly live" because that is the cheap mistake — calling a live
    sweep stale tells a reader to trust results taken while a file was mutated."""

    def test_our_own_stamp_format_parses(self) -> None:
        """Pinned because the stamp is an ISO timestamp full of colons and the parser
        splits on the first one. It works because the writer emits ``started : <iso>``
        with a space before the colon — a formatting change would silently break dating
        and every marker would read as undatable."""
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from datetime import UTC, datetime

        import mutate

        line = f"started : {datetime.now(UTC).isoformat(timespec='seconds')}"
        age = mutate.marker_age(line)
        assert age is not None
        assert age.total_seconds() < 60

    def test_an_undatable_marker_is_not_treated_as_stale(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import mutate

        assert mutate.marker_age("no start line here") is None

    def test_a_recent_marker_warns_about_a_concurrent_sweep(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from datetime import UTC, datetime, timedelta

        import mutate

        recent = datetime.now(UTC) - timedelta(minutes=2)
        age = mutate.marker_age(f"started : {recent.isoformat(timespec='seconds')}")
        assert age is not None and age < mutate.STALE_MARKER_AFTER

    def test_an_old_marker_is_past_the_abandoned_threshold(self) -> None:
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from datetime import UTC, datetime, timedelta

        import mutate

        old = datetime.now(UTC) - timedelta(hours=3)
        age = mutate.marker_age(f"started : {old.isoformat(timespec='seconds')}")
        assert age is not None and age > mutate.STALE_MARKER_AFTER


class TestPytestSeesTheMarker:
    """The compensating channel for gitignoring the marker: pytest's own report header,
    which fires on the path a reader actually takes."""

    def test_report_header_is_silent_when_no_sweep_is_running(self) -> None:
        from tests.conftest import pytest_report_header

        assert not MARKER.exists()
        assert pytest_report_header() is None

    def test_report_header_shouts_when_a_marker_exists(self) -> None:
        from tests.conftest import pytest_report_header

        MARKER.write_text("current : some-mutation [a/b.py]\n", encoding="utf-8")
        try:
            header = pytest_report_header()
            assert header is not None
            assert "MUTATION SWEEP IS RUNNING" in header
            assert "NOT regressions" in header
            assert "some-mutation" in header
        finally:
            MARKER.unlink(missing_ok=True)


class TestAPreExistingMarkerRefusesTheRun:
    """The serious one, from a real incident on 2026-08-09.

    Two files were found permanently mutated with NO marker to explain them. The
    mechanism: sweep A is killed leaving a file mutated and the marker behind. Sweep B
    starts, snapshots its "originals" FROM DISK — now containing A's mutation — runs, and
    faithfully restores the mutation as though it were the original. B's cleanup then
    deletes the marker, destroying the only evidence.

    Warning was not enough, because CONTINUING is the step that bakes the mutation in.
    So a pre-existing marker now refuses, and refusing must not destroy the evidence.
    """

    def _plan(self, tmp_path: Path) -> Path:
        return _write_plan(
            tmp_path,
            [
                {
                    "label": "queued stops counting as in-flight",
                    "file": _target(),
                    "find": _ANCHOR,
                    "replace": 'IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"building"})',
                    "tests": ["tests/ee/sites/test_build_state.py"],
                }
            ],
        )

    def test_refuses_and_exits_non_zero(self, tmp_path: Path) -> None:
        MARKER.write_text("started : 2026-08-09T10:00:00+00:00\ncurrent : x\n", encoding="utf-8")
        try:
            proc = _run(self._plan(tmp_path))
            assert proc.returncode != 0
            assert "REFUSING TO RUN" in (proc.stdout + proc.stderr)
        finally:
            MARKER.unlink(missing_ok=True)

    def test_refusing_preserves_the_evidence(self, tmp_path: Path) -> None:
        """The marker is the only thing that explains a mutated file. Deleting it on
        refusal would reproduce the exact incident this guard exists to stop."""
        MARKER.write_text("started : 2026-08-09T10:00:00+00:00\ncurrent : x\n", encoding="utf-8")
        try:
            _run(self._plan(tmp_path))
            assert MARKER.exists(), "refusal must preserve the marker for inspection"
        finally:
            MARKER.unlink(missing_ok=True)

    def test_refusing_applies_no_mutation(self, tmp_path: Path) -> None:
        target = REPO_ROOT / _target()
        before = target.read_bytes()
        MARKER.write_text("started : 2026-08-09T10:00:00+00:00\ncurrent : x\n", encoding="utf-8")
        try:
            _run(self._plan(tmp_path))
            assert target.read_bytes() == before
        finally:
            MARKER.unlink(missing_ok=True)

    def test_force_proceeds_past_the_marker(self, tmp_path: Path) -> None:
        """The escape hatch, for a marker whose files are already verified clean.
        Deliberately explicit — the default must be refusal."""
        MARKER.write_text("started : 2026-08-09T10:00:00+00:00\ncurrent : x\n", encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(MUTATE), "--plan", str(self._plan(tmp_path)), "--force"],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr
            assert "caught" in proc.stdout
        finally:
            MARKER.unlink(missing_ok=True)


class TestTheMarkerTellsReadersNotToTrustThePid:
    def test_pid_is_labelled_informational(self) -> None:
        """A reviewer used the pid to decide "live or stranded", found it not running,
        and was about to restore a file from under a healthy sweep. Any pid check is racy
        by construction — the process can exit between reading the file and looking it up
        — so the file's presence has to be the authority."""
        import sys as _sys

        _sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import mutate

        mutate._write_marker(Path("some/plan.json"), "a-mutation")
        try:
            text = MARKER.read_text(encoding="utf-8")
            assert "INFORMATIONAL ONLY" in text
            assert "PRESENCE OF THIS FILE IS THE AUTHORITATIVE SIGNAL" in text
            assert "racy" in text
        finally:
            MARKER.unlink(missing_ok=True)
