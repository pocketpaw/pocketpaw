# git.py — Web Cursor git write path: status / stage / commit / push (WC-7/P4a).
# Created 2026-07-16 (feat/code-mode).
#
# Lets a workspace stage, commit, and push its work. Git runs IN the VM via
# ``execute_command`` in the pinned project root (``WEBSANDBOX_WORKDIR``). The
# push token is NEVER in the VM: the provisioner already repointed ``origin`` at
# the codegit broker (``codegit_wire.wire_push_remote``), so an in-VM ``git push``
# authenticates server-side. Thin service-layer orchestration ABOVE
# ``websandbox/service.py`` — every op resolves + fail-closed-authorizes the row
# exactly like ``preview.py`` / ``edit.py`` BEFORE any VM touch.
#
# SCOPE: status/stage/commit/push, plus open-a-PR (P4b). ``open_pr`` is the only op
# that does NOT touch the VM — it opens a GitHub pull request server-side via the
# GitHub App (the token never enters the VM), for the ``paw/edit-*`` branch the
# push already put on the remote.
#
# SECURITY CRUX: every client-supplied value that lands in a shell string — each
# staged ``path``, the commit ``message``, and the commit identity name/email — is
# ``shlex.quote``d into a single argv token. A raw client value is NEVER f-strung
# into a command, so a path or message full of shell metacharacters can't break out
# of its token.
#
# COMMIT IDENTITY: resolved from the caller's GitHub connection
# (``codeconnect_service.list_connections`` → the first view carrying an
# ``account_login``): name = login, email = ``<login>@users.noreply.github.com`` so
# commits are attributed to the real user the UI already shows. Fallback when there
# is no connection/login: ``PocketPaw`` / ``noreply@pocketpaw.dev``.
#
# DI seam: ``client: DaytonaClient | None = None`` (default ``get_daytona_client()``)
# so tests inject a fake and never hit real Daytona.
from __future__ import annotations

import logging
import re
import shlex

from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError, ConflictError
from pocketpaw_ee.cloud.codeconnect import service as codeconnect_service
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.websandbox import broker as websandbox_broker
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR
from pocketpaw_ee.cloud.websandbox.domain import WebSandboxView
from pocketpaw_ee.cloud.websandbox.dto import (
    CommitRequest,
    CreatePrRequest,
    GitCommitResponse,
    GitFileEntry,
    GitPrResponse,
    GitPushResponse,
    GitStatusResponse,
    StageRequest,
)
from pocketpaw_ee.cloud.websandbox.githubapp import GitHubAppError, get_github_app_client

logger = logging.getLogger(__name__)

# Bounded exec timeouts (seconds). A push reaches the network (the broker proxy),
# so it gets the longest budget; the local ops are quick.
_GIT_TIMEOUT_SECONDS = 30
_PUSH_TIMEOUT_SECONDS = 120

# Fallback commit identity when the caller has no GitHub connection/login yet.
_FALLBACK_NAME = "PocketPaw"
_FALLBACK_EMAIL = "noreply@pocketpaw.dev"

# Cap the push ``detail`` so a verbose git error can't bloat the response.
_MAX_PUSH_DETAIL_CHARS = 500


def _require_client(client: DaytonaClient | None) -> DaytonaClient:
    """Resolve the Daytona client, raising a clean CloudError when unconfigured
    (mirrors ``preview._require_client`` — a None client is a 503, not a crash)."""
    resolved = client if client is not None else get_daytona_client()
    if resolved is None:
        raise CloudError(
            503,
            "websandbox.daytona_unavailable",
            "The sandbox runtime is not configured",
        )
    return resolved


async def _resolve_ready(
    workspace_id: str,
    user_id: str,
    row_id: str,
    client: DaytonaClient | None,
) -> tuple[WebSandboxView, DaytonaClient]:
    """Owner-scoped resolve + fail-closed authorize + client, shared by every op.

    ``get_sandbox`` raises ``NotFound`` for a row the caller doesn't own; a row
    with no bound Daytona id is a clean 409; ``authorize_sandbox`` is the
    fail-closed oracle run BEFORE any VM touch.
    """
    daytona = _require_client(client)
    row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
    if not row.sandbox_id:
        raise ConflictError("websandbox.not_ready", "Sandbox is not provisioned yet")
    await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)
    return row, daytona


async def _exec(daytona: DaytonaClient, sandbox_id: str, cmd: str, *, timeout: int):
    """Run a git command in the pinned project root."""
    return await daytona.execute_command(sandbox_id, cmd, cwd=WEBSANDBOX_WORKDIR, timeout=timeout)


# ---------------------------------------------------------------------------
# status.
# ---------------------------------------------------------------------------


_BRANCH_TRACKING_RE = re.compile(r"\[(?P<body>[^\]]*)\]")


def _parse_branch_header(header: str) -> tuple[str | None, int, int]:
    """Parse a ``## ...`` porcelain branch header into (branch, ahead, behind).

    Handles ``main...origin/main [ahead 2, behind 1]``, a bare ``main`` (no
    upstream → 0/0), the fresh-repo ``No commits yet on main``, and detached
    ``HEAD (no branch)`` → branch None.
    """
    ahead = behind = 0
    match = _BRANCH_TRACKING_RE.search(header)
    if match:
        for token in match.group("body").split(","):
            token = token.strip()
            try:
                if token.startswith("ahead "):
                    ahead = int(token[len("ahead ") :])
                elif token.startswith("behind "):
                    behind = int(token[len("behind ") :])
            except ValueError:  # a malformed count is simply left at 0
                continue
        header = header[: match.start()].strip()
    header = header.strip()
    if header.startswith("No commits yet on "):
        return header[len("No commits yet on ") :].strip() or None, ahead, behind
    if header.startswith("HEAD (no branch)"):
        return None, ahead, behind  # detached HEAD
    branch = header.split("...", 1)[0].strip()
    return (branch or None), ahead, behind


def _parse_status(stdout: str) -> GitStatusResponse:
    """Parse ``git status --porcelain=v1 -b`` into a GitStatusResponse.

    Each non-header line is ``XY<space>path`` — X is the index (staged) column and
    Y the worktree (unstaged) column. ``staged`` is True when the index column is a
    real change (not a space and not the ``?`` untracked marker). A rename line
    (``R  old -> new``) reports the NEW path. Malformed short lines are skipped.
    """
    branch: str | None = None
    ahead = behind = 0
    files: list[GitFileEntry] = []
    for raw in stdout.splitlines():
        if raw.startswith("## "):
            branch, ahead, behind = _parse_branch_header(raw[3:])
            continue
        if len(raw) < 4:  # need at least "XY p"
            continue
        index = raw[0]
        worktree = raw[1]
        path = raw[3:]
        if index in ("R", "C") and " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(
            GitFileEntry(
                path=path,
                index=index,
                worktree=worktree,
                staged=index not in (" ", "?"),
            )
        )
    return GitStatusResponse(branch=branch, ahead=ahead, behind=behind, files=files)


async def _read_status(daytona: DaytonaClient, sandbox_id: str) -> GitStatusResponse:
    """Run + parse ``git status``. A non-zero exit is a clean CloudError."""
    resp = await _exec(
        daytona, sandbox_id, "git status --porcelain=v1 -b", timeout=_GIT_TIMEOUT_SECONDS
    )
    if int(getattr(resp, "exit_code", 0) or 0) != 0:
        raise CloudError(502, "websandbox.git_failed", "Failed to read git status")
    return _parse_status(getattr(resp, "result", "") or "")


async def git_status(
    workspace_id: str,
    user_id: str,
    row_id: str,
    *,
    client: DaytonaClient | None = None,
) -> GitStatusResponse:
    """Return the working-tree status of a ready sandbox's repo."""
    row, daytona = await _resolve_ready(workspace_id, user_id, row_id, client)
    return await _read_status(daytona, row.sandbox_id)


# ---------------------------------------------------------------------------
# stage / unstage.
# ---------------------------------------------------------------------------


async def stage(
    workspace_id: str,
    user_id: str,
    row_id: str,
    body: StageRequest | dict,
    *,
    client: DaytonaClient | None = None,
) -> GitStatusResponse:
    """Stage (``git add``) or unstage (``git reset HEAD``) each path, then return a
    fresh status. Every path is ``shlex.quote``d into one argv token."""
    body = StageRequest.model_validate(body)
    row, daytona = await _resolve_ready(workspace_id, user_id, row_id, client)
    for path in body.paths:
        quoted = shlex.quote(path)
        cmd = f"git reset -q HEAD -- {quoted}" if body.unstage else f"git add -- {quoted}"
        resp = await _exec(daytona, row.sandbox_id, cmd, timeout=_GIT_TIMEOUT_SECONDS)
        if int(getattr(resp, "exit_code", 0) or 0) != 0:
            raise CloudError(
                502,
                "websandbox.git_failed",
                f"Failed to {'unstage' if body.unstage else 'stage'} {path}",
            )
    return await _read_status(daytona, row.sandbox_id)


# ---------------------------------------------------------------------------
# commit.
# ---------------------------------------------------------------------------


async def _resolve_identity(workspace_id: str, user_id: str) -> tuple[str, str]:
    """Resolve the commit identity from the caller's GitHub connection.

    The first connection carrying an ``account_login`` wins: name = login,
    email = ``<login>@users.noreply.github.com`` (GitHub's no-reply form). No
    connection/login → the PocketPaw fallback identity. Connection-lookup failures
    fall back too — a commit must never fail because identity resolution hiccuped.
    """
    try:
        conns = await codeconnect_service.list_connections(workspace_id, user_id)
    except Exception:  # noqa: BLE001 — identity is best-effort; fall back rather than fail the commit
        logger.debug("git: identity lookup failed; using fallback", exc_info=True)
        conns = []
    for conn in conns:
        login = getattr(conn, "account_login", None)
        if login:
            return login, f"{login}@users.noreply.github.com"
    return _FALLBACK_NAME, _FALLBACK_EMAIL


async def commit(
    workspace_id: str,
    user_id: str,
    row_id: str,
    body: CommitRequest | dict,
    *,
    client: DaytonaClient | None = None,
) -> GitCommitResponse:
    """Commit the staged changes as the resolved identity; capture the new SHA.

    Nothing staged (``git diff --cached --quiet`` exits 0) is a clean
    ``committed:false`` result, not a crash. The message and identity are
    ``shlex.quote``d into single argv tokens.
    """
    body = CommitRequest.model_validate(body)
    row, daytona = await _resolve_ready(workspace_id, user_id, row_id, client)

    # Nothing staged? --cached --quiet exits 0 when the index matches HEAD.
    probe = await _exec(
        daytona, row.sandbox_id, "git diff --cached --quiet", timeout=_GIT_TIMEOUT_SECONDS
    )
    if int(getattr(probe, "exit_code", 0) or 0) == 0:
        return GitCommitResponse(sha="", committed=False)

    name, email = await _resolve_identity(workspace_id, user_id)
    commit_cmd = (
        f"git -c user.name={shlex.quote(name)} -c user.email={shlex.quote(email)} "
        f"commit -m {shlex.quote(body.message)}"
    )
    resp = await _exec(daytona, row.sandbox_id, commit_cmd, timeout=_GIT_TIMEOUT_SECONDS)
    if int(getattr(resp, "exit_code", 0) or 0) != 0:
        raise CloudError(502, "websandbox.git_commit_failed", "The commit failed")

    head = await _exec(daytona, row.sandbox_id, "git rev-parse HEAD", timeout=_GIT_TIMEOUT_SECONDS)
    sha = (getattr(head, "result", "") or "").strip()
    return GitCommitResponse(sha=sha, committed=True)


# ---------------------------------------------------------------------------
# push.
# ---------------------------------------------------------------------------


async def push(
    workspace_id: str,
    user_id: str,
    row_id: str,
    *,
    client: DaytonaClient | None = None,
) -> GitPushResponse:
    """Push the sandbox's feature branch to ``origin`` (the broker proxy).

    Pushes ``row.branch`` — the ``paw/edit-*`` the provisioner bound. Exit 0 →
    ``pushed:true``. A push failure NEVER raises: it comes back as
    ``pushed:false`` with a human ``detail`` (e.g. the broker origin isn't wired
    because ``POCKETPAW_PUBLIC_BASE_URL`` is local — see ``codegit/wire``). stderr
    is folded into stdout (``2>&1``) so the reason is captured for ``detail``.
    """
    row, daytona = await _resolve_ready(workspace_id, user_id, row_id, client)
    branch = row.branch or ""
    if not branch:
        return GitPushResponse(pushed=False, branch="", detail="No branch is checked out to push")

    cmd = f"git push -u origin {shlex.quote(branch)} 2>&1"
    try:
        resp = await _exec(daytona, row.sandbox_id, cmd, timeout=_PUSH_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — a push must never 500; surface it as pushed:false
        logger.warning("git push exec failed for row=%s", row_id, exc_info=True)
        return GitPushResponse(
            pushed=False, branch=branch, detail="Push isn't available in this environment"
        )

    if int(getattr(resp, "exit_code", 0) or 0) == 0:
        return GitPushResponse(pushed=True, branch=branch, detail=None)

    output = (getattr(resp, "result", "") or "").strip()
    detail = (
        output[:_MAX_PUSH_DETAIL_CHARS] if output else "Push isn't available in this environment"
    )
    return GitPushResponse(pushed=False, branch=branch, detail=detail)


# ---------------------------------------------------------------------------
# open pull request (P4b).
# ---------------------------------------------------------------------------


def _repo_full_name(repo_raw: str) -> str | None:
    """Normalize the row's ``repo`` to ``owner/name`` (URL or bare ``owner/repo``).

    Reuses the broker's URL parser for an https git URL; if the row instead stored a
    bare ``owner/repo`` (no scheme), accepts that too. Returns ``None`` for anything
    that isn't a recognizable two-segment repo.
    """
    full = websandbox_broker.repo_full_name(repo_raw)
    if full:
        return full
    cleaned = (repo_raw or "").strip().removesuffix(".git").strip("/")
    if "://" in cleaned:
        return None
    parts = [p for p in cleaned.split("/") if p]
    return "/".join(parts[:2]) if len(parts) == 2 else None


async def _resolve_pr_installation(
    workspace_id: str,
    user_id: str,
    repo_full: str,
    github_client,  # noqa: ANN001 — a GitHubAppClient-shaped object (DI seam)
) -> tuple[str | None, str | None]:
    """Find the first GitHub connection whose installation can reach ``repo_full``.

    Broker-style routing: iterate the caller's connections and probe each with
    ``get_default_branch`` — which mints a repo-scoped token GitHub declines when the
    installation can't see the repo, so a success both proves reachability AND yields
    the PR base branch in one call. Returns ``(installation_id, base)`` for the first
    reachable connection, or ``(None, None)`` when none can reach it.
    """
    conns = await codeconnect_service.list_connections(workspace_id, user_id)
    for conn in conns:
        if conn.provider != "github":
            continue
        try:
            base = await github_client.get_default_branch(conn.installation_id, repo_full)
        except GitHubAppError:
            continue  # this installation can't reach the repo — try the next
        return conn.installation_id, base
    return None, None


async def open_pr(
    workspace_id: str,
    user_id: str,
    row_id: str,
    body: CreatePrRequest | dict,
    *,
    client: DaytonaClient | None = None,
    github_client=None,  # noqa: ANN001 — a GitHubAppClient-shaped object (DI seam)
) -> GitPrResponse:
    """Open a GitHub pull request for the sandbox's pushed feature branch.

    Resolves + authorizes the row (same guard as the other git ops), then works
    entirely against GitHub server-side — the VM is never touched. The head is the
    row's ``paw/edit-*`` branch (already pushed via ``push``); the base is the repo's
    default branch. The installation is resolved broker-style from the caller's
    connections. This does NOT auto-push: if the head isn't on the remote yet,
    GitHub's 422 message ("make sure the branch is pushed") is surfaced as-is.
    """
    body = CreatePrRequest.model_validate(body)
    row, _daytona = await _resolve_ready(workspace_id, user_id, row_id, client)

    head = row.branch or ""
    if not head:
        raise ConflictError(
            "websandbox.no_branch", "This sandbox has no feature branch to open a pull request from"
        )

    repo_full = _repo_full_name(row.repo)
    if repo_full is None:
        raise BadRequest(
            "websandbox.pr_repo_unrecognized",
            "This workspace's repository isn't a recognizable GitHub repository",
        )

    gh = github_client if github_client is not None else get_github_app_client()
    if gh is None:
        raise CloudError(
            503, "websandbox.github_not_configured", "GitHub is not configured for pull requests"
        )

    installation_id, base = await _resolve_pr_installation(workspace_id, user_id, repo_full, gh)
    if installation_id is None:
        # No connection whose installation can reach this repo — the user must
        # connect GitHub and grant this repository to the Paw app.
        raise BadRequest(
            "websandbox.pr_no_connection",
            "Connect GitHub and grant this repository access before opening a pull request",
        )

    result = await gh.create_pull_request(
        installation_id,
        repo_full,
        head=head,
        base=base,
        title=body.title,
        body=body.body,
    )
    url = result.get("url")
    number = result.get("number")
    if not url or number is None:
        raise CloudError(502, "websandbox.pr_failed", "GitHub returned an incomplete pull request")
    return GitPrResponse(url=url, number=int(number))


__all__ = ["commit", "git_status", "open_pr", "push", "stage"]
