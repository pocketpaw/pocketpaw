# executor.py — applies an approved Belt code-change Action and opens a PR.
# Created: 2026-06-10 (feat/belt-gate, BS-3).
#
# Updated: 2026-06-10 (feat/belt-trace, BS-4 — Decision-Graph chain close) —
#   ``execute_approved_change`` now CLOSES the Decision-Graph chain the
#   propose path opened (RFC 09). It reads the ``correlation_id`` off the
#   schema-2 ``_code_change`` blob and emits the terminal
#   ``decision.completed`` event:
#     * SUCCESS (mark_executed) → ``passed=True, action_outcome="landed"``
#       with the ``pr_url`` / ``branch`` / ``files_changed`` on the payload.
#     * FAILURE (any mark_failed branch) → ``passed=False,
#       action_outcome="failed"`` with the ``error_class`` / ``reason`` so the
#       explain narrator can say WHY it failed.
#   The router threads the ``human.corrected`` event id it just emitted into
#   ``execute_approved_change(..., human_event_id=...)`` so the terminal event
#   chains its ``causation_id`` back to the human approval — one clean causal
#   walk ``agent.proposed → human.corrected → decision.completed``. Exactly ONE
#   terminal fires per run: every error path RETURNS right after its single
#   ``_emit_chain_close`` + ``mark_failed`` pair, and the success path emits
#   once at the end — no doubled terminals. The schema literal is bumped 1 → 2
#   to match belt.py's schema-2 blob (a stale schema-1 blob approved post-deploy
#   still fails loud on the mismatch guard). Both the emit and the read are
#   best-effort: a Decision-Graph wiring failure must never break the approve
#   response (the Slice 4 abandon-sweeper closes any chain left open).
#
# What this module does (the apply-on-approve half of the Belt code-change
# gate): the ``pocketpaw_belt`` MCP server proposes a unified diff THROUGH
# Instinct (the human approve/reject layer). After a human approves the Action,
# the ee instinct router's ``approve_action`` fires ``execute_approved_change``
# here — exactly mirroring how ``instinct_bridge.execute_approved_write`` is
# fired for a parked pocket write. This function:
#
#   1. Reads the ``_code_change`` blob from ``action.parameters``. A missing or
#      schema-mismatched blob → mark_failed, return.
#   2. RE-resolves the repo path against the allowlist (defense in depth — the
#      allowlist may have tightened between propose and approve).
#   3. Creates a FRESH git worktree (one per action id, under a tmp dir; NEVER
#      the repo's live checkout) at ``origin/<base_branch>`` after a fetch.
#   4. ``git apply --3way`` the diff (written to a temp FILE — never echoed/
#      interpolated into a shell).
#   5. Branches ``feat/belt-<action-id-short>``, commits (Conventional Commits;
#      the agent's summary as the body; NO AI attribution), pushes.
#   6. Opens a PR via an injectable opener (default shells ``gh pr create``;
#      tests inject a fake).
#   7. ``mark_executed`` with outcome ``{pr_url, branch, files_changed}``.
#   8. ALWAYS removes the worktree — on success or any failure. On apply
#      conflict / any error → ``mark_failed`` with a clear outcome (the agent /
#      user can re-propose); never leave half-state.
#
# Security (this code moves diffs into git + runs subprocesses):
#   * subprocess arg LISTS only — never ``shell=True``, never string-interpolate
#     user input into a command. Repo path, branch, diff path are all argv
#     elements.
#   * the diff is DATA — written to a temp file and fed to ``git apply <file>``;
#     never echoed, eval'd, or passed on a command line.
#   * the repo path is re-resolved INSIDE the allowlist; a path that escaped the
#     boundary (or the boundary tightened) is refused, not applied.
#   * NO secrets in logs — only action ids, branch names, and file counts. Diff
#     content is never logged.
#   * destructive ops are confined to a throwaway worktree dir that is removed
#     in a finally block; the live checkout is never touched.
#
# Why a separate module (not in pockets/): the Belt code-change path is its own
# subsystem — it doesn't touch backend credentials or the pockets service. It
# mirrors instinct_bridge's propose/execute SHAPE without sharing its plumbing.

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Schema version stamped on the ``_code_change`` blob. Bump when the blob shape
# changes so a stale pending Action approved after a deploy fails loud instead
# of applying a misinterpreted diff (same discipline as the pocket-write
# bridge's ``_POCKET_WRITE_SCHEMA``).
#
# Schema 2 (BS-4) — the blob carries the Decision-Graph ``correlation_id`` +
# ``proposed_event_id`` set by belt.py at propose time. Kept in sync with the
# MCP server's ``CODE_CHANGE_SCHEMA`` literal; duplicated here so the executor
# has no import dependency on the agent-side MCP module.
_CODE_CHANGE_SCHEMA = 2

# The parameters key the blob rides under — kept in sync with the MCP server's
# ``CODE_CHANGE_PARAM_KEY``. Duplicated as a literal here so the executor has no
# import dependency on the agent-side MCP module.
_CODE_CHANGE_PARAM_KEY = "_code_change"

# Subprocess timeout for any single git / gh call (seconds). A hung remote
# operation must not wedge the approve path forever.
_SUBPROCESS_TIMEOUT = 120.0


class PrOpener(Protocol):
    """Injectable PR-opening interface.

    The default implementation shells ``gh pr create``; tests inject a fake to
    assert the call args without touching GitHub. Returns the PR URL (or a
    best-effort placeholder string if ``gh`` printed nothing parseable)."""

    async def open_pr(
        self,
        *,
        repo_path: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str: ...


class GhCliPrOpener:
    """Default ``PrOpener`` — shells ``gh pr create`` from the worktree.

    ``gh`` reads the repo's remote + the authenticated user's token from the
    environment; no secret is passed on the command line. The title/body are
    argv elements (never interpolated into a shell string)."""

    async def open_pr(
        self,
        *,
        repo_path: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        code, out, err = await _run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=repo_path,
        )
        if code != 0:
            raise RuntimeError(f"gh pr create failed (exit {code}): {err.strip() or out.strip()}")
        # `gh pr create` prints the PR URL on stdout. Take the last non-empty
        # line that looks like a URL.
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("http"):
                return line
        return out.strip() or "<pr-created>"


async def _run(
    argv: list[str], *, cwd: Path | None = None, stdin: bytes | None = None
) -> tuple[int, str, str]:
    """Run a subprocess from an ARG LIST (never a shell), bounded by a timeout.

    Returns ``(returncode, stdout, stderr)``. NEVER uses ``shell=True`` and
    never interpolates user input into a command string — every element of
    ``argv`` is passed literally. This is the single subprocess chokepoint for
    the executor."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(input=stdin), timeout=_SUBPROCESS_TIMEOUT
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        sub = argv[1] if len(argv) > 1 else ""
        raise RuntimeError(
            f"command timed out after {_SUBPROCESS_TIMEOUT}s: {argv[0]} {sub}"
        ) from None
    return (
        proc.returncode or 0,
        out_b.decode("utf-8", "replace"),
        err_b.decode("utf-8", "replace"),
    )


def _re_resolve_repo(repo: str) -> tuple[Path | None, str | None]:
    """Re-resolve + re-allowlist the repo path at EXECUTE time (defense in
    depth). Reuses the MCP server's resolver so propose and execute share one
    boundary definition. Returns ``(path, None)`` or ``(None, error)``."""
    try:
        from pocketpaw_ee.agent.mcp_servers.belt import _resolve_repo
    except Exception:  # noqa: BLE001 — agent module shouldn't fail to import, but be defensive
        # Fall back to a minimal inline check if the agent module is absent.
        candidate = Path(repo).expanduser().resolve()
        if not candidate.is_dir() or not (candidate / ".git").exists():
            return None, f"repo path {repo!r} is not a git repository"
        return candidate, None
    return _resolve_repo(repo)


def _short_id(action_id: str) -> str:
    """A short, branch-safe slug from an action id (drop any ``act-`` prefix,
    keep the last 12 hex chars)."""
    raw = action_id.split("-", 1)[-1] if "-" in action_id else action_id
    safe = "".join(ch for ch in raw if ch.isalnum())
    return safe[-12:] or "change"


def _coerce_uuid(raw: Any) -> Any | None:
    """Coerce a value to a ``UUID``, or ``None`` if it can't be. Accepts an
    existing ``UUID`` (returned as-is) or a string; anything else → None."""
    from uuid import UUID

    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


def _blob_correlation_id(blob: dict[str, Any]) -> Any | None:
    """Pull the Decision-Graph chain ``correlation_id`` off a schema-2
    ``_code_change`` blob, or ``None`` if missing / malformed. Without it the
    chain-close emit no-ops — the Slice 4 abandon-sweeper closes any orphan."""
    return _coerce_uuid(blob.get("correlation_id"))


def _emit_chain_close(
    *,
    passed: bool,
    action_outcome: str,
    error_class: str | None,
    reason: str | None,
    correlation_id: Any | None,
    workspace_id: str,
    user_id: str,
    causation_id: Any | None,
    pr_url: str | None = None,
    branch: str | None = None,
    files_changed: int | None = None,
) -> None:
    """Emit the ``decision.completed`` chain-close for a Belt code-change run.

    Mirrors ``instinct_bridge._emit_bridge_chain_close`` — the executor owns
    the chain close on the apply path, exactly as the pocket-write bridge owns
    it on its re-entry path. ``correlation_id`` is read off the schema-2 blob;
    ``causation_id`` is the ``human.corrected`` event the router emitted just
    before approval so the terminal chains back to the human approval.

    Returns early when ``correlation_id`` is None (a blob with a malformed /
    missing id, or a schema-1 blob): there is no chain to close. The Slice 4
    abandon-sweeper will close any chain that accumulates without a terminal.

    Best-effort: a Decision-Graph wiring failure must never break the approve
    response — the journal write is the source of truth; the Slice 4 reconciler
    is the safety net.
    """
    if correlation_id is None:
        return

    # Late imports — keep the executor's import surface small and avoid a
    # circular import with the decisions package.
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_decision_completed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    payload: dict[str, Any] = {
        "passed": passed,
        "action_outcome": action_outcome,
    }
    if error_class:
        payload["error_class"] = error_class
    if reason:
        payload["reason"] = reason
    if pr_url:
        payload["pr_url"] = pr_url
    if branch:
        payload["branch"] = branch
    if files_changed is not None:
        payload["files_changed"] = files_changed

    try:
        record_decision_completed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
            causation_id=causation_id,
        )
    except Exception:  # noqa: BLE001 — chain close is best-effort
        logger.warning(
            "belt decision.completed emit failed for correlation_id=%s "
            "(action_outcome=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            action_outcome,
            exc_info=True,
        )


async def execute_approved_change(
    action: Any,
    *,
    pr_opener: PrOpener | None = None,
    human_event_id: Any | None = None,
) -> None:
    """Apply the code change carried by a freshly-approved Instinct Action.

    Called best-effort from the instinct router's ``approve_action`` after
    ``store.approve()`` succeeds — the same hook shape the pocket-write bridge
    uses. ``action`` is the approved Action. ``pr_opener`` lets tests inject a
    fake; production passes ``None`` and gets the ``gh pr create`` opener.

    ``human_event_id`` (BS-4) is the id of the ``human.corrected`` event the
    router emitted just before calling this — threaded through so the terminal
    ``decision.completed`` event can chain its ``causation_id`` back to the
    approval, completing the causal walk ``agent.proposed → human.corrected →
    decision.completed``. ``None`` is tolerated (the chain still folds via the
    shared ``correlation_id``).

    Never raises — a failure here must not break the approve response. The
    router wraps the call too; this is belt-and-braces. The worktree is ALWAYS
    cleaned up (success or failure); the Action is marked executed on success
    or failed with a clear outcome on any error. BS-4: every terminal path
    (success or any failure) closes the Decision-Graph chain exactly once.
    """
    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store()
    opener: PrOpener = pr_opener or GhCliPrOpener()

    params = getattr(action, "parameters", None) or {}
    blob = params.get(_CODE_CHANGE_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not a Belt code-change Action at all — no chain was ever opened for
        # it, so there is nothing to close. Return without a terminal emit.
        logger.warning("approved action %s carries no _code_change blob", action.id)
        return

    # BS-4 — read the chain correlation_id off the schema-2 blob up front so
    # EVERY terminal path below can close the chain it opened. Defensive: a
    # malformed / missing id falls through to None and the chain-close helper
    # no-ops (the Slice 4 abandon-sweeper closes any chain left open).
    correlation_id = _blob_correlation_id(blob)
    workspace_id = str(blob.get("workspace_id") or "")
    requested_by = str(blob.get("requested_by") or "")
    causation = _coerce_uuid(human_event_id)

    async def _fail(reason: str, *, error_class: str) -> None:
        """Mark the Action failed AND close the chain with one terminal —
        the single failure-path chokepoint so a path can never both fail
        and double-fire the terminal."""
        await store.mark_failed(action.id, reason)
        _emit_chain_close(
            passed=False,
            action_outcome="failed",
            error_class=error_class,
            reason=reason,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=requested_by,
            causation_id=causation,
        )

    if blob.get("schema") != _CODE_CHANGE_SCHEMA:
        await _fail(
            "code-change schema mismatch — the change blob is from an "
            "incompatible build and cannot be applied",
            error_class="SchemaMismatch",
        )
        return

    repo = str(blob.get("repo") or "")
    base_branch = str(blob.get("base_branch") or "")
    diff = blob.get("diff")
    summary = str(blob.get("summary") or "")
    task = str(blob.get("task") or "")

    if not base_branch or not isinstance(diff, str) or not diff.strip():
        await _fail(
            "code-change blob is missing base_branch or diff",
            error_class="MalformedBlob",
        )
        return

    # Defense in depth — re-resolve + re-allowlist the repo at execute time.
    repo_path, repo_err = _re_resolve_repo(repo)
    if repo_err is not None or repo_path is None:
        await _fail(
            f"repo no longer valid at approval time: {repo_err}",
            error_class="RepoInvalid",
        )
        return

    branch = f"feat/belt-{_short_id(str(action.id))}"

    # One throwaway worktree dir per action id, under a tmp/belt-actions root.
    # NEVER the repo's live checkout. Cleaned up in the finally block below.
    tmp_root = Path(tempfile.gettempdir()) / "belt-actions"
    tmp_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = tmp_root / f"act-{_short_id(str(action.id))}"
    diff_file: Path | None = None
    worktree_created = False

    try:
        # 1. Fetch the latest base so we branch off the freshest origin tip.
        code, _out, err = await _run(["git", "fetch", "origin", base_branch], cwd=repo_path)
        if code != 0:
            await _fail(
                f"git fetch origin {base_branch} failed: {err.strip()[:300]}",
                error_class="GitFetchFailed",
            )
            return

        # 2. Fresh worktree at origin/<base_branch>. If the dir somehow exists
        #    from a prior crash, remove it first so add doesn't refuse.
        if worktree_dir.exists():
            await _force_remove_worktree(repo_path, worktree_dir)
        code, _out, err = await _run(
            ["git", "worktree", "add", str(worktree_dir), f"origin/{base_branch}"],
            cwd=repo_path,
        )
        if code != 0:
            await _fail(
                f"git worktree add failed: {err.strip()[:300]}",
                error_class="GitWorktreeAddFailed",
            )
            return
        worktree_created = True

        # 3. Branch off the detached worktree head.
        code, _out, err = await _run(["git", "checkout", "-b", branch], cwd=worktree_dir)
        if code != 0:
            await _fail(
                f"git checkout -b {branch} failed: {err.strip()[:300]}",
                error_class="GitCheckoutFailed",
            )
            return

        # 4. Write the diff to a temp FILE and apply it — the diff is DATA, it
        #    never touches a command line beyond the file path argument.
        fd_path = worktree_dir / ".belt-change.diff"
        fd_path.write_text(diff, encoding="utf-8")
        diff_file = fd_path
        code, _out, err = await _run(
            ["git", "apply", "--3way", "--whitespace=nowarn", str(fd_path)], cwd=worktree_dir
        )
        # Remove the diff file before committing so it never lands in the PR.
        with _suppress():
            fd_path.unlink()
            diff_file = None
        if code != 0:
            await _fail(
                "diff did not apply cleanly (conflict or stale base) — "
                f"re-propose against the current {base_branch}. git apply: {err.strip()[:300]}",
                error_class="ApplyConflict",
            )
            return

        # 5. Stage everything the diff touched, capture the changed-file list,
        #    then commit. Conventional Commits; the agent's summary as the body;
        #    NO AI attribution.
        code, _out, err = await _run(["git", "add", "-A"], cwd=worktree_dir)
        if code != 0:
            await _fail(f"git add failed: {err.strip()[:300]}", error_class="GitAddFailed")
            return

        files_changed = await _changed_files(worktree_dir)
        if not files_changed:
            await _fail(
                "diff produced no staged changes — nothing to commit",
                error_class="NothingToCommit",
            )
            return

        commit_title = _commit_title(task, summary)
        commit_body = summary or task
        code, _out, err = await _run(
            ["git", "commit", "-m", commit_title, "-m", commit_body], cwd=worktree_dir
        )
        if code != 0:
            await _fail(f"git commit failed: {err.strip()[:300]}", error_class="GitCommitFailed")
            return

        # 6. Push the branch.
        code, _out, err = await _run(
            ["git", "push", "-u", "origin", branch], cwd=worktree_dir
        )
        if code != 0:
            await _fail(f"git push failed: {err.strip()[:300]}", error_class="GitPushFailed")
            return

        # 7. Open the PR via the injectable opener.
        try:
            pr_url = await opener.open_pr(
                repo_path=worktree_dir,
                branch=branch,
                base_branch=base_branch,
                title=commit_title,
                body=commit_body,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("belt: PR open failed for action %s", action.id, exc_info=True)
            # The branch is pushed but no PR — record the partial so a human can
            # open the PR by hand. This is NOT a clean success.
            await _fail(
                f"branch '{branch}' pushed but PR open failed: {exc}. "
                "Open the PR manually or re-propose.",
                error_class="PrOpenFailed",
            )
            return

        # 8. Mark executed with the structured outcome.
        await store.mark_executed(
            action.id,
            f"PR opened: {pr_url} (branch '{branch}', {len(files_changed)} file(s) changed)",
        )
        # BS-4 — close the chain on the SUCCESS path. ``action_outcome="landed"``
        # + the PR url / branch / file count ride on the payload for the explain
        # narrator. This is the ONLY terminal on the happy path (every failure
        # path above closed via ``_fail`` and returned), so exactly one
        # ``decision.completed`` lands per run.
        _emit_chain_close(
            passed=True,
            action_outcome="landed",
            error_class=None,
            reason=None,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            user_id=requested_by,
            causation_id=causation,
            pr_url=pr_url,
            branch=branch,
            files_changed=len(files_changed),
        )
        logger.info(
            "belt: applied code_change action %s → branch %s, %d file(s), PR %s",
            action.id,
            branch,
            len(files_changed),
            pr_url,
        )
    except Exception:  # noqa: BLE001 — never let an executor crash break approve
        logger.warning(
            "belt: code_change execution crashed for action %s", action.id, exc_info=True
        )
        with _suppress():
            await _fail("code-change executor crashed — re-propose", error_class="ExecutorCrash")
    finally:
        # ALWAYS clean up — leave no half-state. Remove the temp diff file (if
        # the apply path bailed before unlinking it) and the worktree.
        if diff_file is not None:
            with _suppress():
                diff_file.unlink()
        if worktree_created or worktree_dir.exists():
            await _force_remove_worktree(repo_path, worktree_dir)


async def _changed_files(worktree_dir: Path) -> list[str]:
    """Return the staged file paths in the worktree (``git diff --cached
    --name-only``). Empty list on any error — the caller treats empty as
    'nothing to commit'."""
    code, out, _err = await _run(
        ["git", "diff", "--cached", "--name-only"], cwd=worktree_dir
    )
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


async def _force_remove_worktree(repo_path: Path, worktree_dir: Path) -> None:
    """Remove a worktree and prune the registration — best-effort, never
    raises. Falls back to an ``rmtree`` if ``git worktree remove`` refuses."""
    with _suppress():
        await _run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=repo_path
        )
    with _suppress():
        await _run(["git", "worktree", "prune"], cwd=repo_path)
    # If git left the directory behind (e.g. the add half-failed), nuke it.
    if worktree_dir.exists():
        with _suppress():
            shutil.rmtree(worktree_dir, ignore_errors=True)


def _commit_title(task: str, summary: str) -> str:
    """Build a Conventional-Commits title from the task / summary.

    Keeps it under ~72 chars and prefixes ``feat(belt):`` so the commit reads as
    a Belt-applied change. The first sentence of the summary (falling back to
    the task) becomes the subject. NO AI attribution anywhere."""
    source = (summary or task or "apply code change").strip()
    # First sentence / first line only.
    first = source.replace("\n", " ").split(". ", 1)[0].strip().rstrip(".")
    subject = first[:60].strip() or "apply code change"
    return f"feat(belt): {subject}"


class _suppress:
    """Tiny context manager that swallows any exception — for best-effort
    cleanup / mark_failed in the crash + finally paths where a second failure
    must never mask the first."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return True


__all__ = [
    "GhCliPrOpener",
    "PrOpener",
    "execute_approved_change",
]
