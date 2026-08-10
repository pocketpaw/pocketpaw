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
duration, naming the plan and the mutation currently applied. It is removed by
the same ``finally`` that restores the files.

AND A PRE-EXISTING MARKER REFUSES THE RUN, rather than warning and continuing.
That is not caution for its own sake — continuing is the step that makes a
crashed sweep's damage permanent. This script snapshots each file's CURRENT bytes
as "the original", so if a previous run died with a file mutated, the next run
adopts the mutation as the original and faithfully restores it forever, then
deletes the marker that was the only evidence. Two files in this repo were found
in exactly that state. ``--force`` is the escape hatch, for a marker whose files
have already been verified clean.

The marker IS gitignored — a sweep artifact must never be committable. Discovery
therefore does not rely on ``git status``: ``tests/conftest.py`` prints a loud
banner in pytest's own report header whenever the marker exists, which is the
path a reader actually takes. Someone chasing a failure runs the suite; they may
never run ``git status`` at all.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Written for the duration of a sweep so a CONCURRENT reader can tell that a test
#: failure is a live mutation rather than a regression. Gitignored (a sweep artifact
#: must never be committable); the reader-facing signal is the pytest report-header
#: banner in ``tests/conftest.py``, which fires on the path a reader actually takes.
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
        f"pid     : {os.getpid()}   (INFORMATIONAL ONLY - see below)",
        f"started : {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"current : {current or '(validating the plan)'}",
        "",
        "THE PRESENCE OF THIS FILE IS THE AUTHORITATIVE SIGNAL. Do not decide",
        '"live vs stranded" from the pid: checking it is racy (the process can exit',
        "between reading this file and looking the pid up), and a dead-looking pid",
        "on a live sweep is exactly the combination that gets someone to clobber a",
        "running job. Treat this file as busy until it disappears.",
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


#: Past this age a marker is treated as definitely abandoned rather than possibly live.
#: A sweep is bounded by (mutations x test-suite time) and the plans in this repo finish
#: in seconds to a couple of minutes, so an hour is already far outside any real run.
#: Erring LONG is deliberate: calling a live sweep "stale" is the expensive mistake,
#: because it tells a reader to trust results taken while a file was mutated. Calling an
#: abandoned one "possibly live" only costs someone a wait.
STALE_MARKER_AFTER = timedelta(hours=1)


def marker_age(text: str, *, now: datetime | None = None) -> timedelta | None:
    """Age of a marker from its ``started :`` line, or ``None`` if undatable.

    Undatable reads as ``None`` and callers treat that as POSSIBLY LIVE, not as stale —
    a marker we cannot date is exactly when we should be most cautious.
    """
    for line in text.splitlines():
        if line.startswith("started"):
            _, _, stamp = line.partition(":")
            try:
                started = datetime.fromisoformat(stamp.strip())
            except ValueError:
                return None
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            return (now or datetime.now(UTC)) - started
    return None


def _refuse_on_existing_marker(*, force: bool) -> None:
    """REFUSE to run when a marker already exists, unless explicitly forced.

    This used to only warn, and warning is not enough — the failure it must stop is both
    worse than a confusing test run and completely silent.

    THE FAILURE, observed for real on 2026-08-09. Sweep A is killed hard, leaving a file
    mutated and the marker behind. Sweep B starts and reads its ``originals`` snapshot
    FROM DISK — which now contains A's mutation — runs, and on the way out faithfully
    "restores" that mutation as though it were the original. B's cleanup then deletes the
    marker, destroying the only remaining evidence. The mutation is now permanent,
    indistinguishable from real code, and nothing will ever warn about it again. Two
    files in this repo were found in exactly that state with no marker left to explain
    them.

    So a second run must not proceed on its own judgement: PROCEEDING IS THE STEP THAT
    BAKES THE MUTATION IN. The operator has to look at the diff first — seconds of work,
    and the only thing that distinguishes "a crashed sweep left junk" from "this is real
    code".

    Refusing also must not delete the marker: it is the only thing that explains a
    mutated file, so destroying it here would reproduce the incident.

    ``--force`` covers the one legitimate case — a marker whose files are already
    verified clean — and is deliberately awkward to reach for.
    """
    if not MARKER_PATH.exists():
        return
    try:
        text = MARKER_PATH.read_text(encoding="utf-8")
    except OSError:
        text = ""
    age = marker_age(text)
    age_note = (
        f"{int(age.total_seconds() // 60)}m old"
        if age is not None
        else "undatable (treat as possibly live)"
    )
    live_or_dead = (
        "A PREVIOUS SWEEP CRASHED without cleaning up."
        if age is not None and age > STALE_MARKER_AFTER
        else "ANOTHER SWEEP MAY BE RUNNING RIGHT NOW."
    )

    if force:
        print(f"NOTE: proceeding past {MARKER_PATH.name} ({age_note}) because --force was given.\n")
        return

    raise SystemExit(
        f"REFUSING TO RUN: {MARKER_PATH.name} already exists ({age_note}).\n"
        f"  {live_or_dead}\n"
        "\n"
        "  Running now is not safe. This script snapshots each file's CURRENT bytes as\n"
        "  the original, so if a crashed sweep left a file mutated, this run would treat\n"
        "  that mutation as the original and restore it permanently.\n"
        "\n"
        "  Do this instead:\n"
        "    1. git status / git diff     <- look for a file left mutated\n"
        "    2. git checkout -- <file>   <- restore anything that is a mutation\n"
        f"    3. rm {MARKER_PATH.name}\n"
        "    4. re-run\n"
        "\n"
        "  If you have already verified the tree is clean, pass --force.\n"
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


def _marker_field(text: str, name: str) -> str:
    """Value of a ``name : value`` line in the marker, or ``""``."""
    for line in text.splitlines():
        if line.startswith(name):
            return line.partition(":")[2].strip()
    return ""


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=REPO_ROOT, encoding="utf-8"
    )


def restore_from_marker() -> int:
    """``--restore``: undo what a hard-killed sweep left behind.

    WHY THIS HAS TO EXIST AND CANNOT BE SOLVED IN-PROCESS. A hard kill bypasses both
    cleanup paths BY CONSTRUCTION — the ``finally`` never runs and neither does the
    ``atexit`` handler. An agent interrupt is a hard kill; one was observed on
    2026-08-09, a second after a sweep started, leaving ``daytona_build.py`` carrying the
    "timeout floor is dropped" mutation and the marker stranded. So in-place restore can
    never be made crash-safe, and the durable protection can only ever be
    DETECT-AND-RECOVER. The marker was already the detect half; this is the recover half.

    Restores from GIT, never from an in-memory or on-disk copy — a copy is one more thing
    that dies with the process, while ``git checkout --`` is idempotent and survives
    anything.

    REFUSES rather than guesses in the two cases where guessing is destructive:

    * the marker looks LIVE — recovering a running sweep is worse than not recovering a
      dead one, so the age logic is reused and the bias is toward "leave it alone";
    * the file carries changes BEYOND the mutation — someone's real edits are mixed in,
      and this cannot know which lines were theirs. Blowing them away to save three
      manual steps is a bad trade, so it explains and stops.

    Safe to run blind: no marker means nothing to do and exit 0.
    """
    if not MARKER_PATH.exists():
        print("Nothing to restore: no sweep marker.")
        return 0

    try:
        text = MARKER_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Cannot read {MARKER_PATH.name}: {exc}")
        return 1

    age = marker_age(text)
    if age is None or age <= STALE_MARKER_AFTER:
        age_note = "undatable" if age is None else f"{int(age.total_seconds() // 60)}m old"
        print(
            f"REFUSING: {MARKER_PATH.name} looks LIVE ({age_note}).\n"
            "  A sweep may be running right now, and restoring under a live sweep would\n"
            "  fight it over the same file. Wait for the marker to disappear.\n"
            f"  If you are certain it is abandoned, delete {MARKER_PATH.name} and re-run."
        )
        return 1

    current = _marker_field(text, "current")
    plan_field = _marker_field(text, "plan")
    if not current or "[" not in current:
        print(
            f"Marker names no applied mutation (current: {current!r}).\n"
            "  The sweep died before applying one, so nothing was mutated. Clearing."
        )
        _clear_marker()
        return 0

    label = current.split("[")[0].strip()
    rel = current.split("[")[-1].rstrip("]").strip().replace("\\", "/")
    target = REPO_ROOT / rel
    print(f"Marker says the live mutation was:\n  {label}\n  in {rel}\n")

    if not target.exists():
        print(f"REFUSING: {rel} does not exist.")
        return 1

    head = _git("show", f"HEAD:{rel}")
    if head.returncode != 0:
        print(f"REFUSING: cannot read {rel} from HEAD ({head.stderr.strip()}).")
        return 1
    head_text = head.stdout
    current_text = target.read_text(encoding="utf-8")

    if current_text == head_text:
        print(f"  {rel} already matches HEAD — nothing to restore.")
        _clear_marker()
        print(f"\nCleared {MARKER_PATH.name}. Re-run the plan to confirm the gate still fires.")
        return 0

    # Is the ONLY difference the mutation itself? Un-apply it and compare to HEAD. This is
    # what distinguishes "sweep debris" from "someone's real edit", and it is exact rather
    # than heuristic — no line counting, no diff parsing.
    mutation = None
    if plan_field:
        plan_path = Path(plan_field)
        plan_path = plan_path if plan_path.is_absolute() else REPO_ROOT / plan_path
        try:
            for raw in json.loads(plan_path.read_text(encoding="utf-8")):
                if raw.get("label") == label:
                    mutation = raw
                    break
        except (OSError, json.JSONDecodeError, TypeError):
            mutation = None

    if mutation is None:
        print(
            f"REFUSING: could not find the mutation {label!r} in plan {plan_field!r},\n"
            f"  so there is no way to prove {rel}'s only change is the mutation.\n"
            f"  Inspect it yourself: git diff -- {rel}"
        )
        return 1

    unapplied = current_text.replace(mutation["replace"], mutation["find"], 1)
    if unapplied != head_text:
        print(
            f"REFUSING: {rel} has changes BEYOND the mutation.\n"
            "  Someone's real edits are mixed in with sweep debris, and this cannot tell\n"
            "  which lines are theirs. Restoring would delete real work.\n"
            f"  Do it by hand: git diff -- {rel}, keep what is yours, drop the mutation."
        )
        return 1

    checkout = _git("checkout", "--", rel)
    if checkout.returncode != 0:
        print(f"REFUSING: git checkout failed ({checkout.stderr.strip()}).")
        return 1

    print(f"  RESTORED {rel} from HEAD (the mutation was its only change).")
    _clear_marker()
    print(
        f"  CLEARED  {MARKER_PATH.name}\n"
        "\nRe-run the plan now. A restore is not proof the gate still catches:\n"
        f"  uv run python scripts/mutate.py --plan {plan_field}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--plan", type=Path, help="path to a JSON mutation plan")
    parser.add_argument("--only", default="", help="run only mutations whose label contains this")
    parser.add_argument("--list", action="store_true", help="print the plan's mutations and exit")
    parser.add_argument(
        "--force",
        action="store_true",
        help="run despite an existing sweep marker (only after verifying the tree is clean)",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="undo what a hard-killed sweep left behind, then clear the marker",
    )
    args = parser.parse_args()

    # --restore reads the plan path out of the marker, so it needs no --plan. Checked
    # here rather than with argparse groups so the error names the actual requirement.
    if args.restore:
        return restore_from_marker()
    if args.plan is None:
        parser.error("--plan is required (or use --restore)")

    mutations = _load_plan(args.plan if args.plan.is_absolute() else REPO_ROOT / args.plan)
    if args.only:
        mutations = [m for m in mutations if args.only.lower() in m.label.lower()]
    if not mutations:
        raise SystemExit("no mutations matched")

    if args.list:
        for m in mutations:
            print(f"  {m.label}  ->  {m.file.relative_to(REPO_ROOT)}")
        return 0

    _refuse_on_existing_marker(force=args.force)
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
