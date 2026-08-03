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
import json
import signal
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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

    signal.signal(signal.SIGINT, lambda *a: (_restore_all(), sys.exit(130)))

    escaped: list[str] = []
    try:
        for m in mutations:
            original = originals[m.file]
            mutated = original.decode("utf-8").replace(m.find, m.replace, 1)
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
