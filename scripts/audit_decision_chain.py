"""Audit chain-event emission outside the canonical helper.

Created: 2026-05-25 (RFC 09 Slice 1b — feat/rfc-09-slice-1-record-decision-event)

The intended replacement was an `import-linter` contract forbidding
`soul_protocol.spec.decisions` everywhere except
`pocketpaw_ee.cloud.decisions.journal_writer`. Import-linter does not
support sub-packages of external packages in forbidden contracts (it
errors out with "subpackages of external packages are not valid"), and
forbidding the entire `soul_protocol` external package would break the
rest of ee/ that relies on it (the journal engine, EventEntry, the
Actor model, etc.). This script enforces the same intent via a
text-search pass.

What it forbids
---------------

Outside `ee/pocketpaw_ee/cloud/decisions/journal_writer.py`, the
following patterns may not appear in `ee/` source files:

1. ``from soul_protocol.spec.decisions import …`` — the AgentProposal /
   HumanCorrection / DecisionGraduation models + the
   ``build_proposal_event`` / ``build_correction_event`` builders.
2. Any direct EventEntry construction with ``action="agent.proposed"``,
   ``action="human.corrected"``, ``action="policy.evaluated"``, or
   ``action="decision.completed"``. These are the four chain-forming
   actions; emitting them outside the helper bypasses the journal-
   append-before-projection-apply ordering (RFC 09 audit Q11) and the
   warning-on-fold-failure isolation (RFC 09 § Architecture).

Why the script exists
---------------------

RFC 09 § "Producer Design" splits chain-event emission across 9 sites
in 4 files (per the producer audit at
``.claude/worktrees/rfc09-producer-audit/AUDIT-REPORT.md``). The
``record_decision_event`` helper is the chokepoint Slices 2 + 3 will
wire those sites through. Without enforcement, a future agent reading
the existing ``FabricJournalStore`` precedent could reasonably reach
for ``journal.append(entry)`` directly and skip the helper — that
worked for fabric, after all. This script makes the chain-event rule
mechanical: a violator surfaces in CI before review.

Usage
-----

    uv run --group ee python scripts/audit_decision_chain.py

Exit code 0 if clean, 1 if any violations are found. The exit code is
the signal CI consumes — pair this with ``lint-imports`` in the same
make target. Add ``--quiet`` for CI to suppress the per-file diff.

Today's baseline is clean: no module in ``ee/`` imports
``soul_protocol.spec.decisions`` or constructs the chain actions
directly. The helper is the only allowed importer.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The four chain-forming actions per RFC 09. `decision.outcome_attached`
# is intentionally absent — it's a mutation-only event emitted from the
# outcomes service via a separately-audited path.
_CHAIN_ACTIONS = {
    "agent.proposed",
    "human.corrected",
    "policy.evaluated",
    "decision.completed",
}

# Files allowed to do whatever they want with the chain actions / soul-
# protocol builders. The helper itself MUST import the builders;
# documentation files can mention them freely; the test that pins the
# vocabulary swap mentions both old and new names.
_ALLOWED_PATHS = {
    "ee/pocketpaw_ee/cloud/decisions/journal_writer.py",
    "ee/pocketpaw_ee/cloud/decisions/projection.py",  # _TRACKED_ACTIONS membership tests
    "ee/pocketpaw_ee/cloud/decisions/router.py",  # exposes the explain endpoint payload
    "ee/pocketpaw_ee/cloud/decisions/__init__.py",
    "scripts/audit_decision_chain.py",  # this file
}

# Source roots to walk. Tests are intentionally excluded — they
# legitimately construct chain events to exercise the projection
# directly without going through the helper. The audit's job is to
# enforce the production-code rule, not the test-code rule.
_SOURCE_ROOTS = ["ee/pocketpaw_ee/", "src/pocketpaw/"]


_IMPORT_PATTERN = re.compile(
    r"\bfrom\s+soul_protocol\.spec\.decisions\s+import\b"
    r"|\bimport\s+soul_protocol\.spec\.decisions\b"
)


def _build_action_pattern() -> re.Pattern[str]:
    """Match `action="<chain-action>"` and `action='<chain-action>'`
    in any line. Looks for the kwarg form because positional EventEntry
    construction is exotic; the chain helper itself does this in
    journal_writer.py but that file is allow-listed."""
    quoted = "|".join(re.escape(a) for a in _CHAIN_ACTIONS)
    return re.compile(rf'\baction\s*=\s*["\']({quoted})["\']')


_ACTION_PATTERN = _build_action_pattern()


def _scan_file(path: Path, repo_root: Path) -> list[str]:
    """Return human-readable violation strings for `path`, or [] if
    clean. Lines that match either pattern are reported with line number
    + the offending source text trimmed."""
    rel = str(path.relative_to(repo_root))
    if rel in _ALLOWED_PATHS:
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    violations: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            # Comments / docstrings (when single-line) shouldn't count.
            continue
        if _IMPORT_PATTERN.search(line):
            violations.append(
                f"  {rel}:{lineno}: imports soul_protocol.spec.decisions — "
                f"route through record_decision_event helper\n"
                f"    {stripped}"
            )
        if _ACTION_PATTERN.search(line):
            violations.append(
                f"  {rel}:{lineno}: direct chain-action EventEntry construction — "
                f"route through record_decision_event helper\n"
                f"    {stripped}"
            )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-file violation details; print summary only.",
    )
    args = parser.parse_args()

    # Repo root = parent of this script's `scripts/` directory.
    repo_root = Path(__file__).resolve().parent.parent

    all_violations: list[str] = []
    files_scanned = 0
    for src_root in _SOURCE_ROOTS:
        src_dir = repo_root / src_root
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            files_scanned += 1
            all_violations.extend(_scan_file(py_file, repo_root))

    if all_violations:
        if not args.quiet:
            print("Decision-chain audit FAILED:\n", file=sys.stderr)
            for v in all_violations:
                print(v, file=sys.stderr)
            print(
                "\n"
                "Route chain-event emission through "
                "`pocketpaw_ee.cloud.decisions.journal_writer.record_decision_event`. "
                "See RFC 09 Slice 1b and `journal_writer.py`'s module docstring "
                "for the rationale.",
                file=sys.stderr,
            )
        print(
            f"Decision-chain audit: FAILED — "
            f"{len(all_violations)} violation(s) across "
            f"{files_scanned} scanned files.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Decision-chain audit: clean — {files_scanned} files scanned, "
        "no violations."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
