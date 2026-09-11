# verify.py — the develop station's MECHANICAL gate: does this diff actually work?
# Created: 2026-09-12 (feat/belt-gate).
#
# Why this exists: the belt develop station ran ZERO mechanical checks. The only
# thing proven anywhere was ``git apply --3way`` in executor.py — and that proves
# a patch APPLIES, not that it WORKS. The station preamble said "run targeted
# tests", which is prose with no enforcement and no result channel, so the human
# at the Instinct gate was approving unverified work.
#
# The design decision (captain's, BS-gate): gate BEFORE the human, not after.
# ``belt_propose_change`` calls ``verify_diff`` after its structural validations
# and BEFORE it files the Instinct Action. A red result REFUSES the propose and
# hands the failure text back to the agent — that is the feedback loop. A green
# (or "nothing to run") result rides onto the ``_code_change`` blob under
# ``verification`` so the Tray/console can show the human what was proven.
#
# How it works:
#   1. Throwaway git worktree of ``repo`` at ``base_branch``, mirroring
#      executor.py's worktree + ``git apply --3way`` pattern (same base-ref
#      resolution, same diff-as-a-FILE discipline, same ``_force_remove_worktree``
#      teardown). ALWAYS torn down, including on exception.
#   2. DISCOVER the checks from the applied tree — pytest (pyproject.toml),
#      the package.json ``test`` script (package manager detected from lockfile),
#      and ``pulley doctor`` (belt.lock). No stack is hardcoded as mandatory.
#   3. Run each check with a per-check timeout, capture the tail of its output.
#
# Fail-CLOSED discipline (the whole point of a gate):
#   * ``verify_diff`` NEVER raises. An internal error becomes
#     ``CheckResult(name="verify_internal", ok=False)`` so an exploding verifier
#     refuses the propose instead of waving it through.
#   * A check that cannot RUN (no runner on PATH, no node_modules in the fresh
#     worktree, pytest collected nothing) is ``skipped``, never a pass and never
#     a failure — it does not count as proof.
#   * A suite that exits 0 having PASSED nothing is demoted to skipped too
#     (``_require_evidence``). This is not hypothetical: ``uv run pytest`` in a
#     throwaway pocketpaw worktree syncs default groups only, so ``pocketpaw_ee``
#     is absent, every ``tests/ee`` module ``importorskip``s, and the run exits 0
#     having exercised nothing. On the exit code alone the gate would hand the
#     approver a green stamp for the change it is least able to verify.
#   * ``status="no_checks"`` is its own third state: not a pass, not a failure.
#     The propose proceeds (a docs repo has nothing to run) but the human sees
#     that nothing was proven.

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pocketpaw_ee.cloud.belt.executor import (
    _force_remove_worktree,
    _has_origin,
    _run,
    _suppress,
)

logger = logging.getLogger(__name__)

# Tail of each check's output kept. A full test log would bloat the error
# response and (if it ever leaked there) the Instinct blob; the tail is where
# pytest/vitest put the failure summary.
_OUTPUT_CAP = 4000

# Env keys DROPPED before running a check. ``uv run`` honours VIRTUAL_ENV /
# UV_PROJECT_ENVIRONMENT, so an inherited value would make it sync the throwaway
# tree's editable install into the LIVE server venv — which we then delete.
# PYTHONPATH/PYTHONHOME would let the caller's paths shadow the applied tree, and
# PYTEST_ADDOPTS would inject the OUTER suite's flags into the inner run.
_ENV_STRIP = frozenset(
    {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS"}
)

# pytest exit 5 = "no tests collected". Nothing was proven, so it is a SKIP —
# never a pass. Exit 0 on an all-SKIPPED suite is the same lie wearing a
# different exit code; ``_require_evidence`` reads the counts line to catch it.
_PYTEST_NO_TESTS = 5


@dataclass(frozen=True)
class CheckResult:
    """One mechanical check. ``skipped`` means it could not run at all — it
    carries ``ok=True`` so it never trips the failed status, but it does not
    count as proof either (see ``_status``)."""

    name: str
    ok: bool
    skipped: bool
    output: str
    duration_s: float


@dataclass(frozen=True)
class VerifyResult:
    status: Literal["passed", "failed", "no_checks"]
    checks: tuple[CheckResult, ...]
    summary: str


def _tail(text: str) -> str:
    """Last ``_OUTPUT_CAP`` chars of ``text``, flagged when truncated."""
    clean = text.strip()
    if len(clean) <= _OUTPUT_CAP:
        return clean
    return f"…(truncated to the last {_OUTPUT_CAP} chars)…\n{clean[-_OUTPUT_CAP:]}"


def _check_env() -> dict[str, str]:
    """The environment a check subprocess gets — the server's, minus the vars
    that would point it at the live venv or shadow the applied tree."""
    return {k: v for k, v in os.environ.items() if k not in _ENV_STRIP}


def _status(checks: tuple[CheckResult, ...]) -> Literal["passed", "failed", "no_checks"]:
    """The three-state rule, in one place.

    any check failed → failed; at least one check RAN and all ran ok → passed;
    nothing runnable → no_checks (neither a pass nor a failure)."""
    if any(not c.ok for c in checks):
        return "failed"
    if any(not c.skipped for c in checks):
        return "passed"
    return "no_checks"


def _summarize(status: str, checks: tuple[CheckResult, ...]) -> str:
    if not checks:
        return "no mechanical checks discovered for this repo"
    parts = [
        f"{c.name} {'skipped' if c.skipped else 'ok' if c.ok else 'FAILED'} ({c.duration_s:.1f}s)"
        for c in checks
    ]
    return f"{status}: " + ", ".join(parts)


def _result(checks: tuple[CheckResult, ...]) -> VerifyResult:
    status = _status(checks)
    return VerifyResult(status=status, checks=checks, summary=_summarize(status, checks))


def _internal(message: str) -> VerifyResult:
    """Fail CLOSED. An internal error is a FAILED verification, not a skip —
    a verifier that cannot prove the work must not let it through."""
    return _result(
        (
            CheckResult(
                name="verify_internal",
                ok=False,
                skipped=False,
                output=_tail(message),
                duration_s=0.0,
            ),
        )
    )


def _skip(name: str, why: str) -> CheckResult:
    return CheckResult(name=name, ok=True, skipped=True, output=why, duration_s=0.0)


async def _timed(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    env: dict[str, str],
    skip_codes: tuple[int, ...] = (),
) -> CheckResult:
    """Run one check, bounded by ``timeout_s``. A timeout is ``ok=False`` — an
    unbounded suite proves nothing and must not be waved through."""
    start = time.monotonic()
    try:
        code, out, err = await _run(argv, cwd=cwd, timeout=float(timeout_s), env=env)
    except Exception as exc:  # noqa: BLE001 — _run raises RuntimeError on timeout
        return CheckResult(
            name=name,
            ok=False,
            skipped=False,
            output=_tail(f"{type(exc).__name__}: {exc}"),
            duration_s=time.monotonic() - start,
        )
    duration = time.monotonic() - start
    body = _tail(f"$ {' '.join(argv)}\n{out}\n{err}")
    if code in skip_codes:
        return CheckResult(name=name, ok=True, skipped=True, output=body, duration_s=duration)
    return CheckResult(name=name, ok=code == 0, skipped=False, output=body, duration_s=duration)


def _locks_pytest(lock: Path) -> bool:
    """True when ``uv.lock`` resolves a pytest package. Without it ``uv run
    pytest`` exits non-zero on a missing command and reds a good diff."""
    try:
        return re.search(r'^name = "pytest"$', lock.read_text(encoding="utf-8"), re.M) is not None
    except OSError:
        return False


def _require_evidence(check: CheckResult) -> CheckResult:
    """A pytest run that exits 0 having passed NOTHING proved nothing — demote it
    to a skip.

    Exit 0 covers "everything passed" AND "every test was skipped", and the live
    case is pocketpaw itself: ``uv run pytest`` in a throwaway worktree syncs the
    default groups only, so ``pocketpaw_ee`` is absent, every ``tests/ee`` module
    ``importorskip``s, and the suite exits 0 having run nothing. On the exit code
    alone that is a PASS, and the approver gets a green rubber stamp on a change
    nothing exercised. ``-q`` still prints the counts line, so read it."""
    if not check.ok or check.skipped:
        return check
    passed = re.search(r"\b(\d+) passed\b", check.output)
    if passed and int(passed.group(1)) > 0:
        return check
    return replace(
        check,
        skipped=True,
        output=_tail(
            f"{check.output}\n\nNo test PASSED — the suite exited 0 without proving "
            "anything (everything skipped, or nothing installed to run), so this "
            "counts as SKIPPED, not a pass."
        ),
    )


def _pytest_targets(tree: Path, diff: str) -> list[str]:
    """Test files the diff touches, filtered to what EXISTS in the applied tree.

    A path that doesn't exist would make pytest exit 4 (usage error) and red a
    good diff, so the filter is load-bearing, not tidiness. Empty list → the
    caller runs the repo default."""
    targets: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        raw = line[4:].strip().split("\t", 1)[0]
        if raw == "/dev/null":
            continue
        if raw.startswith("b/"):
            raw = raw[2:]
        name = Path(raw).name
        if not name.endswith(".py"):
            continue
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        if (tree / raw).exists():
            targets.append(raw)
    return sorted(set(targets))


async def _pytest_check(
    tree: Path, diff: str, *, timeout_s: int, env: dict[str, str]
) -> CheckResult | None:
    """Python: run the repo's tests, targeted at the diff's test files when it
    has any. ``None`` = not a Python repo, no check discovered."""
    if not (tree / "pyproject.toml").exists():
        return None
    targets = _pytest_targets(tree, diff)

    lock = tree / "uv.lock"
    if lock.exists() and shutil.which("uv"):
        # Correct by construction: uv resolves the tree's OWN dependencies.
        if not _locks_pytest(lock):
            # Running it would exit non-zero on "unknown command pytest" and red
            # a perfectly good diff. A missing runner is a SKIP, not a failure.
            return _skip("pytest(uv)", "the repo's uv.lock does not include pytest")
        return _require_evidence(
            await _timed(
                "pytest(uv)",
                ["uv", "run", "pytest", *targets, "-q"],
                cwd=tree,
                timeout_s=timeout_s,
                env=env,
                skip_codes=(_PYTEST_NO_TESTS,),
            )
        )

    try:
        import pytest  # noqa: F401
    except ImportError:
        return _skip("pytest", "pytest is not importable and the repo has no uv.lock to run it")

    # Ambient interpreter. Its editable installs would otherwise resolve
    # ``import <pkg>`` to the ORIGINAL checkout — the diff's source changes would
    # never be exercised — so the applied tree goes on PYTHONPATH ahead of them.
    roots = [str(tree / "src")] if (tree / "src").is_dir() else []
    roots.append(str(tree))
    return _require_evidence(
        await _timed(
            "pytest(ambient)",
            [sys.executable, "-m", "pytest", *targets, "-q"],
            cwd=tree,
            timeout_s=timeout_s,
            env={**env, "PYTHONPATH": os.pathsep.join(roots)},
            skip_codes=(_PYTEST_NO_TESTS,),
        )
    )


async def _node_check(tree: Path, *, timeout_s: int, env: dict[str, str]) -> CheckResult | None:
    """Node/Bun: the package.json ``test`` script, run with the package manager
    the lockfile names. ``None`` = no package.json or no test script."""
    pkg = tree / "package.json"
    if not pkg.exists():
        return None
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return None
    if not scripts.get("test"):
        return None

    if (tree / "bun.lock").exists() or (tree / "bun.lockb").exists():
        pm = "bun"
    elif (tree / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    else:
        pm = "npm"
    name = f"{pm} test"

    if not shutil.which(pm):
        return _skip(name, f"{pm} is not on PATH")
    if not (tree / "node_modules").is_dir():
        # A fresh worktree has no install. Running it would fail on missing
        # modules and red a good diff — an install-state failure is not a code
        # failure. ponytail: no install step; add one if node repos become the
        # common case and the install cost is acceptable inside the gate.
        return _skip(name, "no node_modules in the throwaway worktree — the suite cannot run")
    return await _timed(name, [pm, "run", "test"], cwd=tree, timeout_s=timeout_s, env=env)


async def _pulley_check(tree: Path, *, timeout_s: int, env: dict[str, str]) -> CheckResult | None:
    """Pulley app (belt.lock at the app root): ``pulley doctor``. Skipped, never
    failed, when no pulley CLI is resolvable."""
    if not (tree / "belt.lock").exists():
        return None
    pulley = shutil.which("pulley")
    if not pulley:
        return _skip("pulley doctor", "no pulley CLI on PATH")
    return await _timed("pulley doctor", [pulley, "doctor"], cwd=tree, timeout_s=timeout_s, env=env)


async def verify_diff(
    *, repo: str, base_branch: str, diff: str, timeout_s: int = 600
) -> VerifyResult:
    """Apply ``diff`` in a throwaway worktree of ``repo`` at ``base_branch`` and
    run whatever mechanical checks that tree offers.

    NEVER raises: every failure path — including an internal one — comes back as
    a ``VerifyResult``. The worktree is ALWAYS removed."""
    repo_path = Path(repo)
    tmp_dir: Path | None = None
    worktree_dir: Path | None = None
    try:
        tmp_root = Path(tempfile.gettempdir()) / "belt-verify"
        tmp_root.mkdir(parents=True, exist_ok=True)
        # mkdtemp for a unique dir per call — unlike the executor there is no
        # action id at propose time, and two concurrent proposes must not
        # collide. git creates the ``tree`` subdir itself (worktree add refuses
        # an existing destination).
        tmp_dir = Path(tempfile.mkdtemp(prefix="verify-", dir=tmp_root))
        worktree_dir = tmp_dir / "tree"

        # Base ref, exactly as the executor resolves it: with a remote, fetch and
        # use ``origin/<base>``; local-only, resolve the LOCAL branch to a sha so
        # the worktree checks it out DETACHED (git refuses to check out a branch
        # that is already live in the repo's working tree).
        if await _has_origin(repo_path):
            code, _out, err = await _run(["git", "fetch", "origin", base_branch], cwd=repo_path)
            if code != 0:
                return _internal(f"git fetch origin {base_branch} failed: {err.strip()[:300]}")
            base_ref = f"origin/{base_branch}"
        else:
            code, out, err = await _run(
                ["git", "rev-parse", "--verify", base_branch], cwd=repo_path
            )
            if code != 0:
                return _internal(
                    f"local base branch {base_branch!r} not found: {err.strip()[:300]}"
                )
            base_ref = out.strip()

        code, _out, err = await _run(
            ["git", "worktree", "add", "--detach", str(worktree_dir), base_ref], cwd=repo_path
        )
        if code != 0:
            return _internal(f"git worktree add failed: {err.strip()[:300]}")

        # The diff is DATA — written to a FILE, never interpolated into a command.
        diff_file = worktree_dir / ".belt-verify.diff"
        diff_file.write_text(diff, encoding="utf-8")
        start = time.monotonic()
        code, _out, err = await _run(
            ["git", "apply", "--3way", "--whitespace=nowarn", str(diff_file)], cwd=worktree_dir
        )
        with _suppress():
            diff_file.unlink()
        if code != 0:
            # A named check, not an internal error: "your diff doesn't apply,
            # re-propose against the current base" is actionable for the agent.
            return _result(
                (
                    CheckResult(
                        name="git_apply",
                        ok=False,
                        skipped=False,
                        output=_tail(
                            "the diff did not apply cleanly (conflict or stale base) — "
                            f"re-propose against the current {base_branch}.\n{err}"
                        ),
                        duration_s=time.monotonic() - start,
                    ),
                )
            )

        env = _check_env()
        checks = [
            await _pytest_check(worktree_dir, diff, timeout_s=timeout_s, env=env),
            await _node_check(worktree_dir, timeout_s=timeout_s, env=env),
            await _pulley_check(worktree_dir, timeout_s=timeout_s, env=env),
        ]
        return _result(tuple(c for c in checks if c is not None))
    except Exception as exc:  # noqa: BLE001 — a verifier that explodes fails CLOSED
        logger.warning("belt verify: internal error", exc_info=True)
        return _internal(f"{type(exc).__name__}: {exc}")
    finally:
        if worktree_dir is not None:
            with _suppress():
                await _force_remove_worktree(repo_path, worktree_dir)
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


__all__ = ["CheckResult", "VerifyResult", "verify_diff"]
