# executor.py — applies an approved Belt code-change Action and opens a PR.
# Created: 2026-06-10 (feat/belt-gate, BS-3).
#
# Updated: 2026-06-11 (feat/belt-autopilot) — refuses a QUEUED station run loud.
#   A ``code_change`` blob carrying ``station_pending=True`` (filed by the
#   mandate ``StationTaskDispatcher`` with the task text but NO diff) is a
#   placeholder run waiting for a human to drive the develop station to a diff —
#   it is never auto-applyable. The executor fails it with error_class
#   ``StationPending`` if it is ever (mistakenly) approved.
#
# Updated: 2026-06-11 (feat/belt-repo-init — local-only gate mode) — the executor
#   now lands a change on a repo with NO ``origin`` remote WITHOUT pushing or
#   opening a PR. ``_has_origin`` is checked once up front (step 0): with a remote
#   the worktree bases on ``origin/<base>`` and the push + PR path runs as before;
#   with no remote the worktree bases on the LOCAL ``<base>`` ref, the push + PR
#   steps are SKIPPED, and ``_land_local_only`` records the executed outcome
#   carrying the ``branch`` + ``commit_sha`` instead of a ``pr_url``. The branch is
#   promoted into the real repo (``git branch <branch> <sha>``) so it survives the
#   worktree teardown. ``_persist_run_result`` back-writes branch + commit_sha (and
#   NOT pr_url) so the runs read model emits ``pr_url=None`` and the page renders a
#   branch chip; ``belt_run_updated`` still fires (landed/done, no pr_url); the
#   Decision-Graph chain still closes once. The with-remote path is unchanged.
#
# Updated: 2026-06-10 (feat/belt-console-backend, SC-2 — runs read model + SSE) —
#   the executor now feeds the /belt console two things:
#     * STRUCTURED outcome on the blob — on a SUCCESSFUL apply it back-writes
#       ``pr_url`` + ``branch`` + ``files_changed`` onto the persisted
#       ``_code_change`` blob (``_persist_run_result``), so the runs read model
#       (``ee.cloud.belt.service.get_run`` / ``list_runs``) reads them
#       structurally instead of regex-parsing the free-text ``mark_executed``
#       outcome. The free-text outcome stays for The Tray.
#     * ``belt_run_updated`` realtime event — published at every terminal:
#       ``landed`` (stage done) on success, ``failed`` (stage done) on any
#       failure path (the ``_fail`` chokepoint emits once). Routes through
#       ``belt_service.emit_belt_run_updated``, whose PRIMARY path is the
#       WORKSPACE REALTIME BUS (the executor runs AFTER the chat turn, so the
#       per-session SSE drain is gone — only the bus reaches the page). The
#       blob's ``workspace_id`` drives the workspace-scoped fan-out. Best-effort:
#       a bus / blob-write failure never breaks the approve response.
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
#      the repo's live checkout). WITH a remote: at ``origin/<base_branch>`` after
#      a fetch. LOCAL-ONLY (no origin): DETACHED at the local ``<base_branch>``
#      commit (never the branch name, which the live working tree holds).
#   4. ``git apply --3way`` the diff (written to a temp FILE — never echoed/
#      interpolated into a shell).
#   5. Branches ``feat/belt-<action-id-short>``, commits (Conventional Commits;
#      the agent's summary as the body; NO AI attribution).
#   6a. WITH a remote — pushes, opens a PR via an injectable opener (default
#       shells ``gh pr create``; tests inject a fake), ``mark_executed`` with
#       outcome ``{pr_url, branch, files_changed}``.
#   6b. LOCAL-ONLY (no origin) — NO push, NO PR. Promotes the belt branch into
#       the real repo and ``mark_executed`` with outcome ``{branch, commit_sha,
#       files_changed}`` (no pr_url). ``belt_run_updated`` still fires (landed).
#   7. ALWAYS removes the worktree — on success or any failure. On apply
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
    commit_sha: str | None = None,
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
    if commit_sha:
        payload["commit_sha"] = commit_sha
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


async def _emit_run_updated(
    *,
    workspace_id: str,
    action_id: str,
    status: str,
    stage: str,
    pr_url: str | None = None,
) -> None:
    """Publish ``belt_run_updated`` for an executor lifecycle terminal.

    Thin wrapper over ``belt_service.emit_belt_run_updated`` (the WORKSPACE
    REALTIME BUS path + an in-turn SSE) so the executor has one call site per
    terminal. The bus is the path that actually reaches the /belt page — the
    executor runs AFTER the chat turn, so there's no per-session SSE sink in
    scope. Best-effort: an import / bus / SSE failure can never bubble into the
    approve response."""
    try:
        from pocketpaw_ee.cloud.belt import service as belt_service

        await belt_service.emit_belt_run_updated(
            workspace_id=workspace_id,
            action_id=action_id,
            status=status,
            stage=stage,
            pr_url=pr_url,
        )
    except Exception:  # noqa: BLE001 — emit must never break the apply path
        logger.debug("belt: belt_run_updated emit failed (non-fatal)", exc_info=True)


async def _persist_run_result(
    *,
    store: Any,
    action_id: str,
    branch: str,
    files_changed: int,
    pr_url: str | None = None,
    commit_sha: str | None = None,
) -> None:
    """Back-write the apply result onto the persisted ``_code_change`` blob.

    The runs read model reads ``pr_url`` / ``branch`` / ``commit_sha`` /
    ``files_changed`` off the blob STRUCTURALLY rather than parsing the free-text
    ``mark_executed`` outcome. Direct SQL update — the same pattern belt.py's
    ``_persist_chain_ids`` uses for the propose-time chain ids. Best-effort: a
    write failure leaves the run without the structured fields (the read model
    falls back to None) but never breaks the approve response.

    Two landing shapes share this writer:
      * WITH-REMOTE — ``pr_url`` + ``branch`` are set; ``commit_sha`` is omitted.
      * LOCAL-ONLY (no ``origin``) — ``branch`` + ``commit_sha`` are set; ``pr_url``
        stays absent so the read model emits ``pr_url=None`` and the page renders
        a branch chip instead of a PR link.
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(_CODE_CHANGE_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["branch"] = branch
        blob["files_changed"] = files_changed
        # Only set the fields that apply to THIS landing shape — never write a
        # null pr_url over a real one (and vice-versa). The read model treats an
        # absent key the same as None.
        if pr_url is not None:
            blob["pr_url"] = pr_url
        if commit_sha is not None:
            blob["commit_sha"] = commit_sha
        params[_CODE_CHANGE_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — back-write is best-effort
        logger.warning(
            "belt: failed to persist PR result onto action %s — runs read model "
            "will show no pr_url/branch for it",
            action_id,
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
        and double-fire the terminal. SC-2: also pushes the ``belt_run_updated``
        SSE (status=failed, stage=done) so the /belt page reflects the failure
        live — best-effort, never raises."""
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
        await _emit_run_updated(
            workspace_id=workspace_id,
            action_id=str(action.id),
            status="failed",
            stage="done",
        )

    if blob.get("schema") != _CODE_CHANGE_SCHEMA:
        await _fail(
            "code-change schema mismatch — the change blob is from an "
            "incompatible build and cannot be applied",
            error_class="SchemaMismatch",
        )
        return

    # A QUEUED STATION RUN (filed by the mandate StationTaskDispatcher) carries
    # the task text but NO diff — it is waiting for a human to drive the develop
    # station to a diff, which files a FRESH applyable code_change Action. It must
    # never auto-apply (there is nothing to apply). The normal flow never approves
    # a queued run, but a stray bulk-approve would land here — refuse it loud.
    if blob.get("station_pending"):
        await _fail(
            "this is a QUEUED station run, not an applyable change — open the "
            "develop station to produce a diff first, then approve that proposal",
            error_class="StationPending",
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
        # 0. LOCAL-ONLY DETECTION — does the repo have an ``origin`` remote? A
        #    repo with no origin is a local-only landing: we branch off the LOCAL
        #    base ref (never ``origin/<base>``, which doesn't exist) and skip the
        #    push + PR entirely (handled after the commit, step 6).
        has_origin = await _has_origin(repo_path)

        # 1. With a remote: fetch the latest base so we branch off the freshest
        #    origin tip, then base the worktree on ``origin/<base>`` (a remote-
        #    tracking ref — ``worktree add`` checks it out DETACHED, never as a
        #    local branch). Local-only: no fetch, resolve the LOCAL ``<base>``
        #    branch to its commit sha so the worktree can check it out DETACHED —
        #    we MUST NOT ``worktree add`` a local branch name that is already
        #    checked out in the repo's live working tree (git refuses it).
        if has_origin:
            code, _out, err = await _run(["git", "fetch", "origin", base_branch], cwd=repo_path)
            if code != 0:
                await _fail(
                    f"git fetch origin {base_branch} failed: {err.strip()[:300]}",
                    error_class="GitFetchFailed",
                )
                return
            worktree_base = f"origin/{base_branch}"
        else:
            code, out, err = await _run(
                ["git", "rev-parse", "--verify", base_branch], cwd=repo_path
            )
            if code != 0:
                await _fail(
                    f"local base branch {base_branch!r} not found: {err.strip()[:300]}",
                    error_class="BaseBranchNotFound",
                )
                return
            worktree_base = out.strip()

        # 2. Fresh worktree DETACHED at the base ref. If the dir somehow exists
        #    from a prior crash, remove it first so add doesn't refuse. ``--detach``
        #    keeps it a detached HEAD so step 3 can create the belt branch without
        #    colliding with a branch already checked out in the live working tree.
        if worktree_dir.exists():
            await _force_remove_worktree(repo_path, worktree_dir)
        code, _out, err = await _run(
            ["git", "worktree", "add", "--detach", str(worktree_dir), worktree_base],
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

        commit_sha = await _head_sha(worktree_dir)

        # 6. LOCAL-ONLY GATE MODE — a repo with NO ``origin`` remote can't be
        #    pushed and has no PR target. Approve = apply + commit on the belt
        #    branch LOCALLY. We detected the missing remote BEFORE the push step
        #    (step 0) so the push / PR path is skipped entirely (never attempted).
        #    The outcome carries the branch + commit sha instead of a pr_url; the
        #    run still lands as executed and ``belt_run_updated`` still fires.
        if not has_origin:
            await _land_local_only(
                store=store,
                action=action,
                worktree_dir=worktree_dir,
                repo_path=repo_path,
                branch=branch,
                commit_sha=commit_sha,
                files_changed=files_changed,
                workspace_id=workspace_id,
                requested_by=requested_by,
                correlation_id=correlation_id,
                causation=causation,
            )
            return

        # 7. Push the branch.
        code, _out, err = await _run(["git", "push", "-u", "origin", branch], cwd=worktree_dir)
        if code != 0:
            await _fail(f"git push failed: {err.strip()[:300]}", error_class="GitPushFailed")
            return

        # 8. Open the PR via the injectable opener.
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

        # 9. Mark executed with the structured outcome.
        await store.mark_executed(
            action.id,
            f"PR opened: {pr_url} (branch '{branch}', {len(files_changed)} file(s) changed)",
        )
        # SC-2 — back-write the PR result onto the blob so the runs read model
        # reads pr_url / branch / files_changed STRUCTURALLY (no free-text
        # parsing). Best-effort: a write failure leaves the run without the
        # structured fields but the free-text outcome above still records it.
        await _persist_run_result(
            store=store,
            action_id=str(action.id),
            branch=branch,
            files_changed=len(files_changed),
            pr_url=pr_url,
        )
        # SC-2 — publish ``belt_run_updated`` (status=landed, stage=done) on the
        # workspace bus so the /belt page reflects the landed PR live. Best-effort.
        await _emit_run_updated(
            workspace_id=workspace_id,
            action_id=str(action.id),
            status="landed",
            stage="done",
            pr_url=pr_url,
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
    code, out, _err = await _run(["git", "diff", "--cached", "--name-only"], cwd=worktree_dir)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


async def _has_origin(cwd: Path) -> bool:
    """True when the repo has an ``origin`` remote (``git remote get-url origin``
    exits 0). A repo with no origin is a LOCAL-ONLY landing — the executor skips
    the push + PR and lands the change on the belt branch locally. Checked once
    up front against the repo (the worktree shares the repo's remotes)."""
    code, _out, _err = await _run(["git", "remote", "get-url", "origin"], cwd=cwd)
    return code == 0


async def _head_sha(worktree_dir: Path) -> str:
    """Resolve the worktree's current HEAD commit sha (``git rev-parse HEAD``).
    Returns the full sha, or ``""`` on any error — the local-only outcome still
    records the branch even if the sha read fails."""
    code, out, _err = await _run(["git", "rev-parse", "HEAD"], cwd=worktree_dir)
    if code != 0:
        return ""
    return out.strip()


async def _land_local_only(
    *,
    store: Any,
    action: Any,
    worktree_dir: Path,
    repo_path: Path,
    branch: str,
    commit_sha: str,
    files_changed: list[str],
    workspace_id: str,
    requested_by: str,
    correlation_id: Any | None,
    causation: Any | None,
) -> None:
    """Land a local-only (no-origin) Belt code change.

    The change is already committed on ``branch`` in the throwaway worktree. A
    local-only repo has no push target and no PR, so we promote the branch into
    the REAL repo (``git branch <branch> <sha>`` run in ``repo_path``) so the
    landed branch survives the worktree teardown, then record the executed
    outcome carrying the branch + commit sha INSTEAD of a pr_url:

      * ``mark_executed`` free-text outcome names the branch + sha (The Tray).
      * ``_persist_run_result`` back-writes ``branch`` + ``commit_sha`` (and NOT
        ``pr_url``) onto the blob so the runs read model surfaces them
        structurally and emits ``pr_url=None`` — the page renders a branch chip.
      * ``belt_run_updated`` fires (status=landed, stage=done) with NO pr_url.
      * the Decision-Graph chain closes once (``action_outcome="landed"``) with
        the branch + sha on the payload (no pr_url).

    Mirrors the with-remote success terminal exactly (one mark_executed, one
    persist, one emit, one chain-close) so the runs read model and the Tray stay
    consistent across both landing shapes.
    """
    n_files = len(files_changed)

    # Promote the worktree branch into the real repo so it outlives the worktree
    # teardown in the finally block. ``git worktree remove`` would otherwise drop
    # the only ref to the commit. Best-effort: a failure here still records the
    # outcome (the commit object survives, reachable by sha) but logs the gap.
    if commit_sha:
        code, _out, err = await _run(["git", "branch", branch, commit_sha], cwd=repo_path)
        if code != 0:
            logger.warning(
                "belt: could not promote local-only branch %s in repo %s: %s",
                branch,
                repo_path,
                err.strip()[:200],
            )

    await store.mark_executed(
        action.id,
        (
            f"Committed locally on branch '{branch}' "
            f"({commit_sha[:12] or 'unknown'}, {n_files} file(s) changed). "
            "No origin remote — not pushed, no PR opened."
        ),
    )
    # Back-write branch + commit_sha (NOT pr_url) so the runs read model reads
    # them structurally and surfaces pr_url=None for the page's branch chip.
    await _persist_run_result(
        store=store,
        action_id=str(action.id),
        branch=branch,
        files_changed=n_files,
        commit_sha=commit_sha,
    )
    # Publish belt_run_updated (status=landed, stage=done) — NO pr_url for a
    # local-only landing. Best-effort.
    await _emit_run_updated(
        workspace_id=workspace_id,
        action_id=str(action.id),
        status="landed",
        stage="done",
    )
    # Close the Decision-Graph chain once on the success path — branch + sha
    # ride the payload (no pr_url) for the explain narrator.
    _emit_chain_close(
        passed=True,
        action_outcome="landed",
        error_class=None,
        reason=None,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        user_id=requested_by,
        causation_id=causation,
        branch=branch,
        commit_sha=commit_sha,
        files_changed=n_files,
    )
    logger.info(
        "belt: applied local-only code_change action %s → branch %s, %d file(s), sha %s",
        action.id,
        branch,
        n_files,
        commit_sha[:12] or "unknown",
    )


async def _force_remove_worktree(repo_path: Path, worktree_dir: Path) -> None:
    """Remove a worktree and prune the registration — best-effort, never
    raises. Falls back to an ``rmtree`` if ``git worktree remove`` refuses."""
    with _suppress():
        await _run(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=repo_path)
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
