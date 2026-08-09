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
