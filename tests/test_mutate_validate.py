# tests/test_mutate_validate.py — `scripts/mutate.py --validate`, the anchor-rot check.
#
# Created 2026-08-10 (#1903). Nineteen mutation plans (194 anchors) lived under
# tests/mutations/ with nothing in .github/workflows/ running any of them. The issue says
# twenty; that count included a plan still on an unmerged branch. That is not a gap in the
# tool —
# mutate.py validates every anchor upfront and aborts before applying anything, naming
# the offender — it is a gap in listening. A plan whose anchor has rotted sits at ZERO
# applied mutations while reading as covered, and nothing says so until somebody runs it.
# Two plans were found in exactly that state on the same day (fault_ladder.json,
# site_preview_refresh.json), in unrelated subsystems.
#
# So the check itself needs tests, for the same reason every guard in this repo does: a
# validator nobody has watched fail is not a validator. The three that matter are the two
# failure modes (an anchor matching zero times, and one matching more than once) and the
# healthy case, because a validator that passed everything would also pass the real plans
# and look identical to a working one.
#
# THE LAST TEST IS THE ONE THAT WOULD HAVE CAUGHT #1903 ITSELF: it runs --validate over
# the repo's REAL plans. It is the same command CI runs, so a rotted anchor fails here
# too, on the machine of whoever rotted it.

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MUTATE = REPO_ROOT / "scripts" / "mutate.py"
MARKER = REPO_ROOT / ".mutation-sweep-active"

#: A real repo file with a stable, unique anchor, borrowed from
#: test_mutate_sweep_marker.py's choice for the same reason: plans resolve ``file``
#: against the repo root, so the target has to exist.
_TARGET = "ee/pocketpaw_ee/sites/build_state.py"
_UNIQUE_ANCHOR = 'IN_FLIGHT_STATUSES: frozenset[str] = frozenset({"queued", "building"})'

#: Appears more than once in the same file, which is the second failure mode. Asserted
#: below rather than assumed, so this test cannot quietly stop testing anything if the
#: file changes.
_REPEATED_ANCHOR = "        return True"


def _validate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MUTATE), "--validate", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        encoding="utf-8",
    )


def _plan(tmp_path: Path, find: str, *, label: str = "a planted anchor") -> Path:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            [
                {
                    "label": label,
                    "file": _TARGET,
                    "find": find,
                    "replace": "# mutated",
                    "tests": ["tests/ee/sites/test_build_state.py"],
                }
            ]
        ),
        encoding="utf-8",
    )
    return plan


class TestTheValidatorFires:
    """Both failure modes, demonstrated. A guard nobody has watched break is not a guard."""

    def test_an_anchor_that_matches_nothing_fails(self, tmp_path: Path) -> None:
        plan = _plan(
            tmp_path, "def a_function_that_was_renamed_or_deleted(", label="a rotted anchor"
        )

        result = _validate("--plan", str(plan))

        assert result.returncode != 0
        assert "anchor not found" in result.stdout
        # The label and the file, because "a plan failed" is not actionable and the label
        # is what says which guarantee stopped being proven.
        assert "a rotted anchor" in result.stdout
        assert _TARGET in result.stdout

    def test_an_anchor_that_matches_twice_fails(self, tmp_path: Path) -> None:
        """The near-miss from #1902: a refactor gave a second function a line that had
        been unique to one. Replacing the wrong one produces a meaningless result that
        still looks like a pass, which is why more-than-once is refused rather than
        guessed at."""
        occurrences = (REPO_ROOT / _TARGET).read_text(encoding="utf-8").count(_REPEATED_ANCHOR)
        assert occurrences > 1, "the fixture anchor is no longer repeated - pick another"

        result = _validate("--plan", str(_plan(tmp_path, _REPEATED_ANCHOR)))

        assert result.returncode != 0
        assert f"anchor appears {occurrences} times" in result.stdout

    def test_a_good_anchor_passes(self, tmp_path: Path) -> None:
        result = _validate("--plan", str(_plan(tmp_path, _UNIQUE_ANCHOR)))

        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK:" in result.stdout

    def test_it_applies_nothing_and_leaves_no_marker(self, tmp_path: Path) -> None:
        """Validation is read-only: no mutation applied, no sweep marker written. If it
        wrote one, a validation crash would strand a marker claiming a sweep is running,
        and a marker nobody believes is worse than none."""
        before = (REPO_ROOT / _TARGET).read_bytes()
        marker_existed = MARKER.exists()

        assert _validate("--plan", str(_plan(tmp_path, _UNIQUE_ANCHOR))).returncode == 0

        assert (REPO_ROOT / _TARGET).read_bytes() == before
        assert MARKER.exists() is marker_existed

    def test_it_reports_every_offender_not_just_the_first(self, tmp_path: Path) -> None:
        """A sweep stops at the first bad anchor, because starting half-configured is
        worse than not starting. Validation must not: the fix is per-anchor, and
        fail-fast would cost one CI round trip per rotted entry."""
        plan = tmp_path / "plan.json"
        plan.write_text(
            json.dumps(
                [
                    {
                        "label": f"rotted anchor {n}",
                        "file": _TARGET,
                        "find": f"def a_function_never_written_{n}(",
                        "replace": "# mutated",
                        "tests": ["tests/ee/sites/test_build_state.py"],
                    }
                    for n in range(3)
                ]
            ),
            encoding="utf-8",
        )

        result = _validate("--plan", str(plan))

        assert result.returncode != 0
        for n in range(3):
            assert f"rotted anchor {n}" in result.stdout
        assert "3 of 3 anchors" in result.stdout


class TestTheRepoOwnPlansValidate:
    def test_every_checked_in_plan_resolves_exactly_once(self) -> None:
        """The test that would have caught #1903. Same command CI runs, over the real
        plans, so an anchor rotted by a refactor fails on the machine that rotted it."""
        result = _validate()

        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK:" in result.stdout

    def test_it_covers_every_plan_on_disk_not_a_hand_kept_list(self) -> None:
        """A validator that walked a list would silently stop covering a plan the day
        somebody added one without updating the list, which is the same class of rot it
        exists to catch."""
        plans = sorted((REPO_ROOT / "tests" / "mutations").glob("*.json"))
        expected = sum(len(json.loads(plan.read_text(encoding="utf-8"))) for plan in plans)

        result = _validate()

        assert f"{expected} anchors across {len(plans)} plans" in result.stdout
