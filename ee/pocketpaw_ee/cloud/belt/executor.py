# ee/pocketpaw_ee/cloud/belt/executor.py — apply-on-approve for Belt code changes.
#
# A ``code_change`` Instinct Action carries a unified diff under
# ``parameters._code_change`` (schema 2). After a human approves it, the instinct
# router calls ``execute_approved_change``, which:
#   1. refuses a malformed or stale-schema blob and a QUEUED station run
#      (``station_pending``: no diff yet), and re-resolves the repo inside the
#      allowlist (defense in depth);
#   2. picks the TARGET branch: a mandate's run lands on the mandate's LINE,
#      ``belt/line/<mandate id>`` (``line_branch``: the id must be a Mongo
#      ObjectId, never user text); a run with no mandate gets ``feat/belt-<id>``;
#   3. adds a throwaway worktree DETACHED at the line tip (``line_tip``: the
#      local line, or ``origin/<line>`` when the pushed one is ahead), else at
#      ``origin/<base>`` (fetched) or the local ``<base>`` commit;
#   4. applies the diff from a temp file (``git apply --3way``) and commits it:
#      ``feat: <task title>`` with a ``title``, else ``feat(belt): <summary>``;
#      the body is the why plus the develop report. No AI attribution;
#   5. moves the target ref by compare-and-swap (``move_ref``: ``update-ref``
#      with the expected old sha, "" = create), so two landings can't both win
#      and nothing needs the branch checked out;
#   6. with an ``origin``: pushes the target and opens its PR (``GhCliPrOpener``
#      reuses the open PR of a branch, so a line keeps one PR); local-only: no
#      push, no PR;
#   7. back-writes ``branch`` / ``commit_sha`` / ``pr_url`` / ``files_changed``
#      onto the blob, fires ``belt_run_updated`` (naming the branch) and closes
#      the Decision-Graph chain once.
# Every failure goes through ``_fail`` (mark_failed + one chain close + a
# ``failed`` run event). The worktree is always removed. A per-run branch is
# deleted when the run did not land (a retry starts clean); a line is never
# deleted, and once its ref moved the run IS landed: a push or PR failure after
# that is noted on the outcome, never a failed run (the Foreman would re-plan
# work the line already holds). ``line_merged`` answers the Foreman's "is it in
# the base yet".
#
# RE-DEVELOP (``_moved``): a headless run whose diff no longer fits where it
# lands (``git apply --check`` and ``--3way`` both fail on the moved base or
# line, or the line moved between read and swap) is sent back to the develop
# station once (``_requeue_for_redevelop``), then waits at the per-diff gate for
# a fresh approval. A second time fails with "base moved twice"; with no develop
# loop wired it fails with that reason.
#
# Security: argv-only subprocesses (never a shell); the diff is data in a temp
# file, never on a command line or in a log; destructive git ops stay inside
# the throwaway worktree; refs move only by compare-and-swap.

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
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
        # A branch that already has an open PR into the base (a mandate line
        # after its first landing) keeps it: the push above updated it.
        code, out, _err = await _run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--base",
                base_branch,
                "--state",
                "open",
                "--json",
                "url",
                "--jq",
                ".[0].url // empty",
            ],
            cwd=repo_path,
        )
        if code == 0 and out.strip().startswith("http"):
            return out.strip()
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


# A mandate's runs land on its LINE branch. The name comes from the mandate id
# alone, and only a Mongo ObjectId (24 lowercase hex) makes one: anything else
# (a hand-proposed change has no mandate) lands on its own ``feat/belt-<id>``.
_LINE_ID = re.compile(r"[0-9a-f]{24}")
_SHA = re.compile(r"[0-9a-f]{7,64}")

# ``git(*args) -> (code, stdout, stderr)`` bound to one repo. The executor's
# runs with the full env (push and fetch need the owner's credentials); the
# develop station passes its hardened, env-scrubbed runner.
GitFn = Callable[..., Awaitable[tuple[int, str, str]]]


class LineError(RuntimeError):
    """A line that can't be built on: its local and pushed tips diverged."""


def line_branch(mandate_id: str) -> str | None:
    """``belt/line/<mandate id>``, or ``None`` when the id is not a mandate's."""
    return f"belt/line/{mandate_id}" if _LINE_ID.fullmatch(mandate_id or "") else None


def _git_in(repo: Path) -> GitFn:
    async def git(*args: str) -> tuple[int, str, str]:
        return await _run(["git", *args], cwd=repo)

    return git


async def commit_of(git: GitFn, ref: str) -> str:
    """The commit ``ref`` names, or "" when there is none."""
    code, out, _err = await git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if code == 0 else ""


async def is_ancestor(git: GitFn, older: str, newer: str) -> bool:
    code, _out, _err = await git("merge-base", "--is-ancestor", older, newer)
    return code == 0


async def line_tip(git: GitFn, line: str, *, has_origin: bool) -> tuple[str, str]:
    """``(tip, local)``: the commit a run on ``line`` builds on, and the local
    ref's sha a landing swaps from ("" = no line yet). With an origin the pushed
    line counts too, refetched (a tracking ref left by a deleted remote branch
    never does): the newer of the two when one contains the other. Diverged
    tips raise ``LineError``; a human merges them."""
    local = await commit_of(git, f"refs/heads/{line}")
    remote = ""
    if has_origin:
        code, _out, _err = await git(
            "fetch", "origin", f"+refs/heads/{line}:refs/remotes/origin/{line}"
        )
        if code == 0:
            remote = await commit_of(git, f"refs/remotes/origin/{line}")
    if not remote or remote == local:
        return local, local
    if not local or await is_ancestor(git, local, remote):
        return remote, local
    if await is_ancestor(git, remote, local):
        return local, local
    raise LineError(f"{line} and origin/{line} have diverged; merge them by hand, then re-run")


async def move_ref(git: GitFn, branch: str, new: str, old: str) -> bool:
    """Compare-and-swap ``refs/heads/<branch>`` from ``old`` to ``new`` (``old``
    "" = create only). False = the ref was not where expected; nothing moved."""
    code, _out, _err = await git("update-ref", f"refs/heads/{branch}", new, old)
    return code == 0


async def line_merged(repo: str, base_branch: str, commit_sha: str) -> bool:
    """Whether a commit landed on a line is in the base yet: in ``origin/<base>``
    (as last fetched) or the local ``<base>``. A squash or rebase merge leaves
    the commit outside the base, so it reads False ("awaiting merge")."""
    repo_path, _err = _re_resolve_repo(repo)
    if repo_path is None or not base_branch or not _SHA.fullmatch(commit_sha or ""):
        return False
    git = _git_in(repo_path)
    for ref in (f"refs/remotes/origin/{base_branch}", f"refs/heads/{base_branch}"):
        if await commit_of(git, ref) and await is_ancestor(git, commit_sha, ref):
            return True
    return False


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
    branch: str | None = None,
) -> None:
    """Publish ``belt_run_updated`` for an executor lifecycle terminal (a
    landing names its ``branch``: the line, for a mandate run).

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
            branch=branch,
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
    ``mark_executed`` outcome. Store-API write — the same pattern belt.py's
    ``persist_chain_ids`` uses for the propose-time chain ids. Best-effort: a
    write failure leaves the run without the structured fields (the read model
    falls back to None) but never breaks the approve response.

    Every landing sets ``branch`` + ``commit_sha``. WITH-REMOTE also sets
    ``pr_url`` (absent when the PR open failed after the line moved); LOCAL-ONLY
    (no ``origin``) never does, so the read model emits ``pr_url=None`` and the
    page renders a branch chip instead of a PR link.
    """

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

        await store.update_parameters(action_id, params)
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

    opener: PrOpener = pr_opener or GhCliPrOpener()

    params = getattr(action, "parameters", None) or {}
    blob = params.get(_CODE_CHANGE_PARAM_KEY)
    if not isinstance(blob, dict):
        # Not a Belt code-change Action at all — no chain was ever opened for
        # it, so there is nothing to close. We can't resolve the store's
        # workspace either (the blob carries it), so bail before opening one.
        logger.warning("approved action %s carries no _code_change blob", action.id)
        return

    # BS-4 — read the chain correlation_id off the schema-2 blob up front so
    # EVERY terminal path below can close the chain it opened. Defensive: a
    # malformed / missing id falls through to None and the chain-close helper
    # no-ops (the Slice 4 abandon-sweeper closes any chain left open).
    correlation_id = _blob_correlation_id(blob)
    workspace_id = str(blob.get("workspace_id") or "")
    # ISO: this runs on the HTTP approve path (no ``current_workspace``
    # ContextVar), so the store MUST be scoped to the blob's workspace or it
    # split-brains onto the shared file (flag unset) / raises (flag set).
    store = get_instinct_store(workspace_id=workspace_id or None)
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
    title = str(blob.get("title") or "")

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

    # A mandate's run lands on its line; any other run on its own branch.
    line = line_branch(str(blob.get("mandate_id") or ""))
    target = line or f"feat/belt-{_short_id(str(action.id))}"
    # What a stale diff no longer applies on (for the re-develop reasons).
    where = line or base_branch
    git = _git_in(repo_path)

    # One throwaway worktree dir per action id, under a tmp/belt-actions root.
    # NEVER the repo's live checkout. Cleaned up in the finally block below.
    tmp_root = Path(tempfile.gettempdir()) / "belt-actions"
    tmp_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = tmp_root / f"act-{_short_id(str(action.id))}"
    diff_file: Path | None = None
    worktree_created = False
    branch_created = False
    landed = False
    redevelop_with: Any = None

    async def _moved(detail: str) -> None:
        """The diff no longer fits where it lands (the base or the line moved
        under a headless run): send it back to re-develop against the new tip,
        once. A second time, or with no develop loop wired, fails with why."""
        nonlocal redevelop_with
        if int(blob.get("redevelop") or 0) >= 1:
            await _fail(
                f"base moved twice: the re-developed diff no longer applies on "
                f"{where} either. Re-run the shift. {detail}",
                error_class="BaseMovedTwice",
            )
            return
        redeveloper = _headless_redeveloper()
        if redeveloper is None:
            await _fail(
                f"diff no longer applies on the moved {where} and the headless "
                f"develop station is not wired to re-develop it. Re-run the shift. {detail}",
                error_class="ApplyConflict",
            )
            return
        requeue_err = await _requeue_for_redevelop(store, str(action.id))
        if requeue_err:
            await _fail(requeue_err, error_class="RedevelopFailed")
            return
        await _emit_run_updated(
            workspace_id=workspace_id,
            action_id=str(action.id),
            status="queued",
            stage="station",
        )
        redevelop_with = redeveloper  # handed off after the cleanup below
        logger.info("belt: action %s no longer fits the moved %s; re-developing", action.id, where)

    try:
        # 0. LOCAL-ONLY DETECTION — a repo with no ``origin`` lands locally: no
        #    fetch, no push, no PR (step 6).
        has_origin = await _has_origin(repo_path)

        # 1. The start: with a remote, the freshly fetched ``origin/<base>`` (a
        #    remote-tracking ref, checked out DETACHED); local-only, the LOCAL
        #    ``<base>`` commit (never the branch name: it may be checked out in
        #    the live working tree). A line that exists replaces either.
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
        expected = ""  # the target's sha the swap expects ("" = create it)
        if line:
            try:
                tip, expected = await line_tip(git, line, has_origin=has_origin)
            except LineError as exc:
                await _fail(str(exc), error_class="LineDiverged")
                return
            worktree_base = tip or worktree_base

        # 2. Fresh worktree DETACHED at the start. A dir left by a prior crash
        #    is removed first so add doesn't refuse.
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

        # 3. Write the diff to a temp FILE and apply it — the diff is DATA, it
        #    never touches a command line beyond the file path argument.
        fd_path = worktree_dir / ".belt-change.diff"
        fd_path.write_text(diff, encoding="utf-8")
        diff_file = fd_path
        # ``--check`` on the still-clean tree says whether the patch fits this
        # start as written (``--3way`` leaves conflict markers when it fails).
        check_code, _out, _err = await _run(
            ["git", "apply", "--check", "--whitespace=nowarn", str(fd_path)], cwd=worktree_dir
        )
        code, _out, err = await _run(
            ["git", "apply", "--3way", "--whitespace=nowarn", str(fd_path)], cwd=worktree_dir
        )
        # Remove the diff file before committing so it never lands in the PR.
        with _suppress():
            fd_path.unlink()
            diff_file = None
        if code != 0 and check_code != 0 and blob.get("headless"):
            await _moved(f"git apply: {err.strip()[:300]}")
            return
        if code != 0:
            await _fail(
                "diff did not apply cleanly (conflict or stale base) — "
                f"re-propose against the current {where}. git apply: {err.strip()[:300]}",
                error_class="ApplyConflict",
            )
            return

        # 4. Stage everything the diff touched, capture the changed-file list,
        #    then commit (detached). Conventional Commits; NO AI attribution.
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

        commit_title = _commit_title(task, summary, title=title)
        commit_body = _commit_body(task, summary, title=title)
        code, _out, err = await _run(
            ["git", "commit", "-m", commit_title, "-m", commit_body], cwd=worktree_dir
        )
        if code != 0:
            await _fail(f"git commit failed: {err.strip()[:300]}", error_class="GitCommitFailed")
            return

        commit_sha = await _head_sha(worktree_dir)

        # 5. Move the target ref by compare-and-swap. A line that moved since it
        #    was read (another landing, a base sync) is the moved-line case.
        if not commit_sha or not await move_ref(git, target, commit_sha, expected):
            if line and blob.get("headless"):
                await _moved(f"{line} moved while this run landed")
                return
            await _fail(
                f"branch '{target}' moved or already exists; it was left alone. Re-run.",
                error_class="RefMoved",
            )
            return
        if line:
            landed = True  # the commit is on the line from here on
        else:
            branch_created = True

        # 6. LOCAL-ONLY — no ``origin``: the change stays on the target branch
        #    in the repo; the outcome carries the branch + commit sha.
        if not has_origin:
            landed = True
            await _land_local_only(
                store=store,
                action=action,
                worktree_dir=worktree_dir,
                repo_path=repo_path,
                branch=target,
                commit_sha=commit_sha,
                files_changed=files_changed,
                workspace_id=workspace_id,
                requested_by=requested_by,
                correlation_id=correlation_id,
                causation=causation,
            )
            return

        # 7. Push the target and open (or reuse) its PR. A per-run branch that
        #    fails here fails the run; a line keeps the landing with a note.
        pr_url: str | None = None
        note = ""
        try:
            code, _out, err = await _run(["git", "push", "-u", "origin", target], cwd=worktree_dir)
        except RuntimeError as exc:  # a timed-out push
            code, err = 1, str(exc)
        if code != 0:
            if not line:
                await _fail(f"git push failed: {err.strip()[:300]}", error_class="GitPushFailed")
                return
            note = f"push failed: {err.strip()[:300]}"
        else:
            try:
                pr_url = await opener.open_pr(
                    repo_path=worktree_dir,
                    branch=target,
                    base_branch=base_branch,
                    title=commit_title,
                    body=commit_body,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("belt: PR open failed for action %s", action.id, exc_info=True)
                if not line:
                    # Pushed but no PR: a human opens it by hand. Not a success.
                    await _fail(
                        f"branch '{target}' pushed but PR open failed: {exc}. "
                        "Open the PR manually or re-propose.",
                        error_class="PrOpenFailed",
                    )
                    return
                note = f"pushed, but the PR open failed: {exc}"

        # 8. Mark executed with the structured outcome.
        landed = True
        if pr_url:
            outcome = (
                f"PR opened: {pr_url} (branch '{target}', {len(files_changed)} file(s) changed)"
            )
        else:
            outcome = (
                f"Landed on '{target}' ({commit_sha[:12]}, {len(files_changed)} file(s) "
                f"changed); {note}. The next landing pushes the line again."
            )
        await store.mark_executed(action.id, outcome)
        # Back-write the landing onto the blob so the runs read model reads
        # pr_url / branch / commit_sha / files_changed STRUCTURALLY.
        await _persist_run_result(
            store=store,
            action_id=str(action.id),
            branch=target,
            files_changed=len(files_changed),
            pr_url=pr_url,
            commit_sha=commit_sha,
        )
        await _emit_run_updated(
            workspace_id=workspace_id,
            action_id=str(action.id),
            status="landed",
            stage="done",
            pr_url=pr_url,
            branch=target,
        )
        # The ONLY terminal on the happy path (every failure above closed via
        # ``_fail`` and returned), so exactly one ``decision.completed`` per run.
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
            branch=target,
            commit_sha=commit_sha,
            files_changed=len(files_changed),
        )
        logger.info(
            "belt: applied code_change action %s → branch %s, %d file(s), PR %s",
            action.id,
            target,
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
        # A per-run branch that did not land must not stay behind (a retry of
        # the same action reuses the name). A line is never deleted.
        if branch_created and not landed:
            with _suppress():
                await _run(["git", "branch", "-D", target], cwd=repo_path)
        # A re-queued run develops only after this worktree is gone (the
        # develop station adds its own worktree in the same repo).
        if redevelop_with is not None:
            try:
                await redevelop_with.develop(str(action.id), workspace_id=workspace_id)
            except Exception:  # noqa: BLE001 — the run stays queued, visible in the digest
                logger.warning("belt: re-develop hand-off failed for %s", action.id, exc_info=True)


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

    The change is already committed on ``branch``, whose ref the caller moved in
    the real repo (``move_ref``), so it survives the worktree teardown. A
    local-only repo has no push target and no PR, so this records the executed
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
    # Publish belt_run_updated (status=landed, stage=done, the branch) — NO
    # pr_url for a local-only landing. Best-effort.
    await _emit_run_updated(
        workspace_id=workspace_id,
        action_id=str(action.id),
        status="landed",
        stage="done",
        branch=branch,
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


def _headless_redeveloper() -> Any | None:
    """The production headless dispatcher (it has ``develop(run_ref)``), or
    ``None`` when no develop loop is wired in this process."""
    from pocketpaw_ee.cloud.belt.headless import resolve_headless_dispatcher

    return resolve_headless_dispatcher()


async def _requeue_for_redevelop(store: Any, action_id: str) -> str | None:
    """Turn an APPROVED headless run whose diff no longer applies back into a
    queued station run: the blob drops its diff (``station_pending``, counted
    in ``redevelop``), THEN the status goes back to pending, so no pending row
    ever carries the stale diff. The re-developed diff waits at the per-diff
    gate for a fresh human approval. Returns an error, or ``None``."""
    from pocketpaw.instinct.models import ActionStatus

    action = await store.get_action(action_id)
    params = dict(getattr(action, "parameters", None) or {})
    blob = dict(params.get(_CODE_CHANGE_PARAM_KEY) or {})
    blob.update(
        station_pending=True,
        diff="",
        redevelop=int(blob.get("redevelop") or 0) + 1,
        # The last attempt's check/review report no longer describes anything.
        summary=str(blob.get("expected_outcome") or blob.get("title") or blob.get("summary") or ""),
    )
    for key in ("headless_error", "files_changed"):
        blob.pop(key, None)
    params[_CODE_CHANGE_PARAM_KEY] = blob
    await store.update_parameters(action_id, params)
    # The store has no public "back to the gate" verb; ``_update_status`` is
    # the canonical status write (CLAUDE.md, canonical primitives), and
    # ``require_status`` makes the flip atomic against a concurrent decision.
    reopened = await store._update_status(
        action_id,
        ActionStatus.PENDING,
        event="action_redevelop",
        actor="system",
        extra_desc=" — the diff no longer applies on the moved base; re-developing",
        require_status=ActionStatus.APPROVED,
    )
    if reopened is None:
        return "could not send the run back to the develop station (no longer approved)"
    return None


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


# A subject that already names its Conventional-Commits type (``fix: x``,
# ``feat(ui)!: y``) is kept as written.
_CONVENTIONAL_SUBJECT = re.compile(r"^[a-z]+(\([^)\n]*\))?!?: \S")
_SUBJECT_MAX = 72


def _commit_title(task: str, summary: str, *, title: str = "") -> str:
    """The commit subject (and PR title), at most ~72 chars, no AI attribution.

    A mandate task carries a short ``title``: ``feat: <title>``. Its summary is
    a multi-line develop report, never a subject. A hand-proposed change has no
    title, and its summary is the agent's one-line description:
    ``feat(belt): <first sentence of the summary, else the task>``."""
    first_line = title.strip().splitlines()[0].strip() if title.strip() else ""
    if first_line:
        subject = first_line if _CONVENTIONAL_SUBJECT.match(first_line) else f"feat: {first_line}"
        if len(subject) > _SUBJECT_MAX:
            subject = subject[:_SUBJECT_MAX].rsplit(" ", 1)[0].rstrip(" .,;:")
        return subject
    source = (summary or task or "apply code change").strip()
    # First sentence / first line only.
    first = source.replace("\n", " ").split(". ", 1)[0].strip().rstrip(".")
    subject = first[:60].strip() or "apply code change"
    return f"feat(belt): {subject}"


def _commit_body(task: str, summary: str, *, title: str = "") -> str:
    """The commit (and PR) body. With a ``title`` the task text is
    ``<title>\n\n<why>``: the body is the why, then the summary (the develop
    station's check/review report). Without one, the summary, else the task."""
    if not title.strip():
        return summary or task
    why = task.strip()
    if why.startswith(title.strip()):
        why = why[len(title.strip()) :].strip()
    return "\n\n".join(part for part in (why, summary.strip()) if part) or title.strip()


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
