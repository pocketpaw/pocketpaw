# ee/pocketpaw_ee/cloud/belt/develop_station.py — the factory's develop station.
#
# ``ClaudeCodeDevelop`` is the production ``DevelopFn`` behind the headless
# mandate dispatcher: it turns one approved plan task into a checked, reviewed
# unified diff that ``HeadlessDevelopRunner`` attaches to the PENDING run (the
# per-diff Instinct gate still decides; this module never commits, pushes or
# merges anything outside a throwaway worktree).
#
# A small explicit state machine, written as sequential steps:
#   PREPARE  ``git worktree add --detach`` of the bound repo at ``origin/<base>``
#            (after a fetch) when an origin exists, else the local ``<base>``.
#   WORK     a charter recipe → run that command; else DEVELOP → ``claude -p``.
#   CHECK    run every charter check; keep exit code + output tail.
#   FIX      a red check (or a failed review) with attempts left → ``claude -p``
#            with the failure, then CHECK again. Recipes get no LLM fix.
#   REVIEW   checks green → an independent read-only ``claude -p`` judges the
#            task against ``git diff``: strict ``{"verdict", "notes"}`` JSON.
#   DONE     ``git add -A`` + ``git diff --cached --binary <base sha>``.
#   CLEANUP  always: remove the temp dir, then ``git worktree prune`` (finally).
# Any dead end raises ``DevelopStationError`` naming the step; the runner records
# it as ``headless_error`` on the queued run's blob.
#
# Safety: every subprocess goes through ONE injectable ``Runner`` with an argv
# list (never a shell); charter commands are ``shlex.split``. The default runner
# passes only an allow-listed env (``_ENV_KEYS``: no tokens, URIs or API keys)
# and kills the whole process group on timeout or cancellation. Station git
# calls run with fsmonitor and hooks disabled. ``claude`` runs
# with an allow-listed tool set (Read/Edit/Write/Glob/Grep + Bash limited to the
# charter's check prefixes) and gets its prompt on stdin. The binary and model
# come from ``foreman.claude_cli_argv`` (the system CLI, never the SDK copy).
# Wired at startup by ``wire_from_env`` when ``POCKETPAW_MANDATE_DISPATCHER=
# headless`` and ``POCKETPAW_FACTORY_DEVELOP=claude``; off by default.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import signal
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pocketpaw_ee.cloud.belt.headless import DevelopRequest, DevelopResult
from pocketpaw_ee.cloud.mandates.foreman import claude_cli_argv, claude_result_text

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 120.0
_TAIL_LINES = 80
_TAIL_CHARS = 2000  # cap on what a failure carries into the blob / a prompt
_REVIEW_DIFF_CHARS = 60_000
_DEVELOP_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]
_REVIEW_TOOLS = ["Read", "Glob", "Grep"]
# Every station git call: no fsmonitor command, no hooks (the worktree is agent
# territory; a planted hook or fsmonitor would run on ``git add``).
_GIT = ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
# The only env every factory subprocess (claude, checks, recipes, git) sees:
# enough for PATH lookups, the CLI's keychain/OAuth under HOME, and locale.
# Everything else (Mongo URI, tokens, API keys, POCKETPAW_* secrets) is dropped.
_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL")


def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


class Runner(Protocol):
    """The ONE subprocess seam: argv in, ``(returncode, stdout, stderr)`` out.
    A timeout returns a non-zero code with the reason in stderr."""

    async def __call__(
        self, argv: list[str], *, cwd: Path, timeout: float, stdin: str | None = None
    ) -> tuple[int, str, str]: ...


def scrubbed_env() -> dict[str, str]:
    """The allow-listed env (``_ENV_KEYS``, only those present).
    ``PYTHONDONTWRITEBYTECODE`` keeps check runs from leaving ``__pycache__``
    files for ``git add -A`` to sweep into the diff."""
    env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


async def run_subprocess(
    argv: list[str], *, cwd: Path, timeout: float, stdin: str | None = None
) -> tuple[int, str, str]:
    """Default ``Runner`` — ``create_subprocess_exec`` (never a shell) with the
    scrubbed env, in its own session so a timeout or a cancelled run kills the
    whole process group (a check's grandchildren too), not just the child."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=scrubbed_env(),
        start_new_session=True,
    )
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
        )
    except TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return -1, "", f"timed out after {timeout:.0f}s"
    except BaseException:
        _kill_group(proc)
        raise
    return proc.returncode or 0, out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")


class DevelopStationError(RuntimeError):
    """A develop run that cannot produce an attachable diff. The message starts
    with the failing step (PREPARE / WORK / CHECK / REVIEW / DONE)."""


@dataclass(frozen=True)
class CheckResult:
    command: str
    code: int
    tail: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def _tail(text: str) -> str:
    lines = text.strip().splitlines()[-_TAIL_LINES:]
    return "\n".join(lines)[-_TAIL_CHARS:]


async def _default_charter_for(workspace_id: str, mandate_id: str) -> dict[str, Any] | None:
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    return await mandate_service.charter_for_mandate(workspace_id, mandate_id)


@dataclass
class ClaudeCodeDevelop:
    """Production ``DevelopFn``: worktree → develop/recipe → checks → fix loop →
    review → diff. ``run`` and ``charter_for`` are injectable for tests."""

    run: Runner = run_subprocess
    charter_for: Callable[[str, str], Awaitable[dict[str, Any] | None]] = _default_charter_for
    max_fix_attempts: int = 2

    async def __call__(self, request: DevelopRequest) -> DevelopResult:
        found = await self.charter_for(request.workspace_id, request.mandate_id)
        if found is None:
            raise DevelopStationError(f"PREPARE: mandate {request.mandate_id!r} not found")
        charter: dict[str, Any] = found.get("charter") or {}
        checks = [str(c) for c in charter.get("checks") or []]
        recipes: dict[str, str] = dict(charter.get("recipes") or {})

        repo = self._resolve_repo(request.repo or str(found.get("repo") or ""))
        base_branch, start_ref = await self._resolve_base(repo, request.base_branch)

        tmp = Path(tempfile.mkdtemp(prefix="belt-develop-"))
        worktree = tmp / "wt"
        try:
            # PREPARE
            await self._git(repo, "worktree", "add", "--detach", str(worktree), start_ref)
            base_sha = (await self._git(worktree, "rev-parse", "HEAD")).strip()

            # WORK
            if request.recipe:
                command = recipes.get(request.recipe)
                if command is None:
                    raise DevelopStationError(
                        f"WORK: recipe {request.recipe!r} is not declared in the charter"
                    )
                code, out, err = await self.run(
                    shlex.split(command),
                    cwd=worktree,
                    timeout=_env_seconds("POCKETPAW_FACTORY_CHECK_TIMEOUT", 600),
                )
                if code != 0:
                    raise DevelopStationError(
                        f"WORK: recipe {request.recipe!r} ({command}) exited {code}:\n"
                        f"{_tail(out + err)}"
                    )
            else:
                await self._claude(
                    _develop_prompt(request, charter),
                    tools=_DEVELOP_TOOLS + [f"Bash({c}:*)" for c in checks],
                    cwd=worktree,
                    step="DEVELOP",
                )

            # CHECK → FIX → REVIEW (one attempts counter for both fix reasons)
            attempts = 0
            review_notes: list[str] = []
            while True:
                results = [await self._check(worktree, c) for c in checks]
                failed = [r for r in results if not r.ok]
                if failed:
                    if request.recipe or attempts >= self.max_fix_attempts:
                        first = failed[0]
                        raise DevelopStationError(
                            f"CHECK: `{first.command}` still failing (exit {first.code}) "
                            f"after {attempts} fix attempt(s):\n{first.tail}"
                        )
                    attempts += 1
                    await self._claude(
                        _fix_prompt(request, checks, _check_failures(failed)),
                        tools=_DEVELOP_TOOLS + [f"Bash({c}:*)" for c in checks],
                        cwd=worktree,
                        step="FIX",
                    )
                    continue
                if request.recipe:
                    verdict = "skipped (recipe)"
                    break
                await self._git(worktree, "add", "-A")
                diff = await self._git(worktree, "diff", "--cached", base_sha)
                passed, review_notes = await self._review(request, diff, worktree)
                if passed:
                    verdict = "pass"
                    break
                if attempts >= self.max_fix_attempts:
                    raise DevelopStationError(
                        f"REVIEW: still failing after {attempts} fix attempt(s): "
                        + "; ".join(review_notes)[:_TAIL_CHARS]
                    )
                attempts += 1
                await self._claude(
                    _fix_prompt(request, checks, "Review notes:\n- " + "\n- ".join(review_notes)),
                    tools=_DEVELOP_TOOLS + [f"Bash({c}:*)" for c in checks],
                    cwd=worktree,
                    step="FIX",
                )

            # DONE
            await self._git(worktree, "add", "-A")
            diff = await self._git(worktree, "diff", "--cached", "--binary", base_sha)
            if not diff.strip():
                raise DevelopStationError("DONE: the change produced an empty diff")
            names = await self._git(worktree, "diff", "--cached", "--name-only", base_sha)
            files_changed = len([n for n in names.splitlines() if n.strip()])

            lines = [request.summary or request.task.splitlines()[0][:120]]
            if request.recipe:
                lines.append(f"recipe: {request.recipe}")
            lines += [f"check `{r.command}`: {'pass' if r.ok else 'fail'}" for r in results]
            lines.append(f"review: {verdict}")
            lines += [f"  - {n}" for n in review_notes]
            lines.append(f"fix attempts: {attempts}")
            return DevelopResult(
                diff=diff,
                base_branch=base_branch,
                summary="\n".join(lines),
                files_changed=files_changed,
            )
        finally:
            # CLEANUP — the temp dir goes first (synchronous, so a cancelled run
            # still drops it), then prune unregisters the vanished worktree.
            shutil.rmtree(tmp, ignore_errors=True)
            await self.run([*_GIT, "worktree", "prune"], cwd=repo, timeout=_GIT_TIMEOUT)

    # -- steps ---------------------------------------------------------------

    @staticmethod
    def _resolve_repo(repo: str) -> Path:
        from pocketpaw_ee.cloud.belt.executor import _re_resolve_repo

        path, err = _re_resolve_repo(repo)
        if path is None:
            raise DevelopStationError(f"PREPARE: {err}")
        return path

    async def _resolve_base(self, repo: Path, base_branch: str) -> tuple[str, str]:
        """``(branch name, worktree start ref)``. The branch defaults to the
        repo's checked-out branch; the start ref is ``origin/<branch>`` (after a
        fetch) when an origin exists — the same rule as the belt executor."""
        base = base_branch.strip()
        if not base:
            base = (await self._git(repo, "rev-parse", "--abbrev-ref", "HEAD")).strip()
            if base == "HEAD":
                raise DevelopStationError("PREPARE: repo is on a detached HEAD; no base branch")
        code, _out, _err = await self.run(
            [*_GIT, "remote", "get-url", "origin"], cwd=repo, timeout=_GIT_TIMEOUT
        )
        if code == 0:
            await self._git(repo, "fetch", "origin", base)
            return base, f"origin/{base}"
        return base, base

    async def _git(self, cwd: Path, *args: str) -> str:
        code, out, err = await self.run([*_GIT, *args], cwd=cwd, timeout=_GIT_TIMEOUT)
        if code != 0:
            raise DevelopStationError(f"git {args[0]} failed (exit {code}): {_tail(err or out)}")
        return out

    async def _check(self, cwd: Path, command: str) -> CheckResult:
        code, out, err = await self.run(
            shlex.split(command),
            cwd=cwd,
            timeout=_env_seconds("POCKETPAW_FACTORY_CHECK_TIMEOUT", 600),
        )
        return CheckResult(command=command, code=code, tail=_tail(out + "\n" + err))

    async def _claude(
        self, prompt: str, *, tools: list[str], cwd: Path, step: str, edits: bool = True
    ) -> str:
        mode = ["--permission-mode", "acceptEdits"] if edits else []
        argv = claude_cli_argv(*mode, "--allowedTools", *tools)
        code, out, err = await self.run(
            argv,
            cwd=cwd,
            timeout=_env_seconds("POCKETPAW_FACTORY_DEVELOP_TIMEOUT", 900),
            stdin=prompt,
        )
        if code != 0:
            raise DevelopStationError(f"{step}: claude exited {code}: {_tail(err or out)}")
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict) and envelope.get("is_error"):
            raise DevelopStationError(f"{step}: claude reported an error: {_tail(out)}")
        return claude_result_text(out)

    async def _review(
        self, request: DevelopRequest, diff: str, cwd: Path
    ) -> tuple[bool, list[str]]:
        text = await self._claude(
            _review_prompt(request, diff),
            tools=_REVIEW_TOOLS,
            cwd=cwd,
            step="REVIEW",
            edits=False,
        )
        start, end = text.find("{"), text.rfind("}")
        try:
            verdict = json.loads(text[start : end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            verdict = None
        if not isinstance(verdict, dict) or verdict.get("verdict") not in ("pass", "fail"):
            raise DevelopStationError(f"REVIEW: unparseable verdict: {text[:300]!r}")
        notes = [str(n) for n in verdict.get("notes") or []]
        return verdict["verdict"] == "pass", notes


# -- prompts ------------------------------------------------------------------


def _charter_block(charter: dict[str, Any]) -> str:
    return (
        f"Mandate goal: {charter.get('goal') or '(none)'}\n"
        f"Boundaries (never cross): {json.dumps(charter.get('boundaries') or [])}\n"
        f"The mandate says no to: {json.dumps(charter.get('says_no') or [])}\n"
        f"Checks that must pass: {json.dumps(charter.get('checks') or [])}"
    )


def _develop_prompt(request: DevelopRequest, charter: dict[str, Any]) -> str:
    return (
        "You are the develop station of an engineering mandate, working in a "
        "throwaway git worktree (the current directory).\n\n"
        f"TASK:\n{request.task}\n\nEXPECTED OUTCOME:\n{request.summary}\n\n"
        f"{_charter_block(charter)}\n\n"
        "Make the change. Add or update tests that cover it. Run the checks if you "
        "can. Do NOT commit, push or create branches. Stay inside the boundaries."
    )


def _fix_prompt(request: DevelopRequest, checks: list[str], failure: str) -> str:
    return (
        "You are the develop station fixing your change in this worktree (the "
        "current directory). The task was:\n"
        f"{request.task}\n\nIt is not done yet:\n{failure}\n\n"
        f"Fix it so these checks pass: {json.dumps(checks)}. Keep the change "
        "focused on the task. Do NOT commit, push or create branches."
    )


def _check_failures(failed: list[CheckResult]) -> str:
    return "\n\n".join(
        f"Check `{r.command}` failed (exit {r.code}). Last output:\n{r.tail}" for r in failed
    )


def _review_prompt(request: DevelopRequest, diff: str) -> str:
    shown = diff[:_REVIEW_DIFF_CHARS]
    if len(diff) > _REVIEW_DIFF_CHARS:
        shown += "\n[diff truncated; read the files for the rest]"
    return (
        "You are an independent code reviewer. Judge whether this diff does the "
        "task, is correct, and carries tests for the change. You may read files in "
        "the current directory; you cannot edit.\n\n"
        f"TASK:\n{request.task}\n\nEXPECTED OUTCOME:\n{request.summary}\n\n"
        f"DIFF:\n{shown}\n\n"
        'Reply with STRICT JSON only: {"verdict": "pass" | "fail", "notes": ["..."]}. '
        '"fail" only for real problems: the task is not done, a bug, or missing tests.'
    )


def wire_from_env() -> bool:
    """Wire ``ClaudeCodeDevelop`` as the production develop loop when
    ``POCKETPAW_MANDATE_DISPATCHER=headless`` and ``POCKETPAW_FACTORY_DEVELOP=
    claude``. Returns whether it wired. Called once at app startup."""
    from pocketpaw_ee.cloud.belt.headless import set_production_develop_fn

    dispatcher = (os.environ.get("POCKETPAW_MANDATE_DISPATCHER") or "").strip().lower()
    develop = (os.environ.get("POCKETPAW_FACTORY_DEVELOP") or "").strip().lower()
    if dispatcher != "headless" or develop != "claude":
        return False
    set_production_develop_fn(ClaudeCodeDevelop())
    logger.info("belt: headless develop station wired (system claude CLI)")
    return True


__all__ = [
    "CheckResult",
    "ClaudeCodeDevelop",
    "DevelopStationError",
    "Runner",
    "run_subprocess",
    "wire_from_env",
]
