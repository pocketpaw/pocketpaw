#!/usr/bin/env python
"""Prove a test suite actually catches the bugs it claims to.

Created: 2026-08-03 (feat/prompt-entity-suffix).

WHY THIS EXISTS. A test that passes tells you nothing on its own — it tells you
the code and the test agree, which is also true when both are wrong. In this
codebase that is not a theoretical worry; it keeps happening, and always in
plausible-looking code:

  * ``TimestampedDocument._set_updated`` was never registered (beanie skips
    ``_``-prefixed hooks), so every ``updatedAt`` holds its construction value.
    Anything keying on it reviewed clean and reported every edit as "unchanged".
  * A ``FunctionModel`` test double advertised native tool search, so a probe
    measuring deferred tool loading reported 0% saving — for the double, not the
    system.
  * ``test_preamble_caps.py``'s fixture widget carried no ``id``, so once rows
    began rendering one it measured them 23 chars shorter than production and
    went on reporting headroom that was not there.
  * A positional-only test in this very sprint passed with the ``/`` removed. It
    was asserting a TypeError that came from a missing argument, not from the
    thing it named.

Every one of those was found by breaking the code on purpose and noticing the
test did not care. This script makes that a command instead of a habit.

WHAT IT DOES. Applies each mutation in a plan, runs the named tests, restores
the file, and reports CAUGHT (tests failed — good) or ESCAPED (tests passed —
the mutation is a bug your suite would ship). Exit code is non-zero if anything
escaped, so it works in CI.

    uv run python scripts/mutate.py --plan tests/mutations/entity_rows.json
    uv run python scripts/mutate.py --plan <plan> --only "positional"

RESTORATION IS THE ONE THING IT MUST NEVER GET WRONG. Original bytes are read
once up front, every apply is wrapped in try/finally, SIGINT is trapped, and a
final pass restores everything again on the way out. A crashed run that left a
mutated file behind would be a far worse bug than any it could find.

A SWEEP IS INVISIBLE TO EVERYONE ELSE, WHICH BURNED US. Mutations are applied
IN PLACE, so for the seconds each one is live the working tree genuinely
contains broken code. Anyone who runs the suite or reads the tree during a
sweep sees a real failure of code that is about to be restored — and nothing
distinguishes it from a regression. That happened on 2026-08-09: a reviewer ran
the tests mid-sweep, saw a bun-PATH assertion fail, and reported it as a
regression. It was this script, working correctly.

So a sweep now leaves ``.mutation-sweep-active`` at the repo root for its
duration, naming the plan, the PID and the mutation currently applied. It is
deliberately NOT gitignored: the point is that ``git status`` surfaces it, so a
second reader has something to see. It is removed by the same ``finally`` that
restores the files, and a leftover marker from a crashed run is reported on the
next start rather than silently overwritten.

PLAN FORMAT — a JSON list of objects:

    [
      {
        "label": "drop the id from the row",
        "file": "src/pocketpaw/prompt/entity.py",
        "find": "parts = [f\"id={ident}\"]",
        "replace": "parts = []",
        "tests": ["tests/test_prompt_entity_line.py"]
      }
    ]

``find`` must appear EXACTLY ONCE in the file. An anchor that matches twice is
rejected rather than guessed at, because replacing the wrong one produces a
meaningless result that still looks like a pass.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Written for the duration of a sweep so a CONCURRENT reader can tell that a test
#: failure is a live mutation rather than a regression. Not gitignored on purpose —
#: ``git status`` surfacing it is the entire mechanism.
MARKER_PATH = REPO_ROOT / ".mutation-sweep-active"


def _write_marker(plan: Path, current: str | None) -> None:
    """Stamp the marker with what is mutated RIGHT NOW.

    Rewritten per mutation rather than once per sweep, because "a sweep is running"
    is much less useful to a reader than "this exact line in this exact file is
    currently broken on purpose". Best-effort: a marker that cannot be written must
    never take down the sweep, since the sweep is the thing with real value.
    """
    body = [
        "A mutation sweep is running in this worktree RIGHT NOW.",
        "",
        "Test failures and unexpected file contents are EXPECTED while this file",
        "exists — mutate.py applies each mutation in place and restores it seconds",
        "later. Do not report them as regressions; re-run once this file is gone.",
        "",
        f"plan    : {plan}",
        f"pid     : {os.getpid()}",
        f"started : {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"current : {current or '(validating the plan)'}",
        "",
        "If this file outlived its sweep, a run crashed hard. Check `git status`/",
        "`git diff` for a file left mutated, then delete this file.",
    ]
    try:
        MARKER_PATH.write_text("\n".join(body) + "\n", encoding="utf-8")
    except OSError:  # pragma: no cover - best effort by design
        pass


def _clear_marker() -> None:
    """Remove the marker. Called from the same ``finally`` that restores files."""
    try:
        MARKER_PATH.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best effort by design
        pass


def _warn_if_stale_marker() -> None:
    """Report a marker left behind by a crashed run instead of overwriting it.

    Silently reusing it would hide the one case where a file may STILL be mutated on
    disk — which is the failure mode this script's restoration discipline exists to
    prevent, so it must be loud.
    """
    if not MARKER_PATH.exists():
        return
    print(
        f"WARNING: {MARKER_PATH.name} already exists — a previous sweep did not clean up.\n"
        "         A file may still be mutated on disk. Check `git diff` before trusting\n"
        "         this run's results.\n"
    )


class Mutation:
    __slots__ = ("file", "find", "label", "replace", "tests")

    def __init__(self, raw: dict, index: int) -> None:
        missing = {"label", "file", "find", "replace", "tests"} - raw.keys()
        if missing:
            raise SystemExit(f"mutation #{index}: missing {', '.join(sorted(missing))}")
        self.label: str = raw["label"]
        self.file: Path = REPO_ROOT / raw["file"]
        self.find: str = raw["find"]
        self.replace: str = raw["replace"]
        self.tests: list[str] = list(raw["tests"])


def _load_plan(path: Path) -> list[Mutation]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"no such plan: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"plan is not valid JSON: {exc}") from None
    if not isinstance(raw, list):
        raise SystemExit("plan must be a JSON list of mutation objects")
    return [Mutation(item, i) for i, item in enumerate(raw)]


def _run_tests(targets: list[str]) -> bool:
    """True when the tests PASS. Mutation testing wants this to be False."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return proc.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--plan", required=True, type=Path, help="path to a JSON mutation plan")
    parser.add_argument("--only", default="", help="run only mutations whose label contains this")
    parser.add_argument("--list", action="store_true", help="print the plan's mutations and exit")
    args = parser.parse_args()

    mutations = _load_plan(args.plan if args.plan.is_absolute() else REPO_ROOT / args.plan)
    if args.only:
        mutations = [m for m in mutations if args.only.lower() in m.label.lower()]
    if not mutations:
        raise SystemExit("no mutations matched")

    if args.list:
        for m in mutations:
            print(f"  {m.label}  ->  {m.file.relative_to(REPO_ROOT)}")
        return 0

    _warn_if_stale_marker()
    _write_marker(args.plan, None)
    # Registered IMMEDIATELY after the marker exists, and via atexit rather than the
    # try/finally below, because the upfront plan validation raises SystemExit — which
    # happens BEFORE that try block is entered. Without this, a plan with a bad anchor
    # would abort correctly and leave a marker claiming a sweep is running forever,
    # which would then warn on every subsequent run and train people to ignore it.
    atexit.register(_clear_marker)

    # Read every original ONCE, before touching anything. If a file is missing
    # or an anchor is bad, fail before the first mutation rather than half way
    # through a run that then has to unwind.
    originals: dict[Path, bytes] = {}
    for m in mutations:
        if not m.file.exists():
            raise SystemExit(f"{m.label}: no such file {m.file}")
        originals.setdefault(m.file, m.file.read_bytes())
        text = originals[m.file].decode("utf-8")
        found = text.count(m.find)
        if found == 0:
            raise SystemExit(f"{m.label}: anchor not found in {m.file.relative_to(REPO_ROOT)}")
        if found > 1:
            raise SystemExit(
                f"{m.label}: anchor appears {found} times in "
                f"{m.file.relative_to(REPO_ROOT)} — make it unique"
            )

    def _restore_all(*_signal_args: object) -> None:
        for path, data in originals.items():
            path.write_bytes(data)
        _clear_marker()

    signal.signal(signal.SIGINT, lambda *a: (_restore_all(), sys.exit(130)))

    escaped: list[str] = []
    try:
        for m in mutations:
            original = originals[m.file]
            mutated = original.decode("utf-8").replace(m.find, m.replace, 1)
            _write_marker(args.plan, f"{m.label}  [{m.file.relative_to(REPO_ROOT)}]")
            try:
                m.file.write_bytes(mutated.encode("utf-8"))
                passed = _run_tests(m.tests)
            finally:
                m.file.write_bytes(original)

            if passed:
                escaped.append(m.label)
                print(f"  ESCAPED  {m.label}")
            else:
                print(f"  caught   {m.label}")
    finally:
        _restore_all()

    print()
    if escaped:
        print(f"{len(escaped)} of {len(mutations)} mutations ESCAPED — the suite would ship these:")
        for label in escaped:
            print(f"  - {label}")
        print("\nA gate is not a gate until a mutation has been observed to break it.")
        return 1

    print(f"all {len(mutations)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
