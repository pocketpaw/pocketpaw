# tests/cloud/test_belt_gate.py — Belt & Pulley code-change gate (BS-3).
#
# Created: 2026-06-10 (feat/belt-gate, Belt & Pulley stations thin slice).
#
# Updated: 2026-06-11 (feat/belt-repo-init — local-only gate mode) — added the
# NO-ORIGIN landing path: a propose→approve cycle against a repo with no
# ``origin`` remote applies + commits on ``feat/belt-<id>`` LOCALLY, NEVER pushes
# and NEVER opens a PR (a fake opener proves it's untouched), the branch + commit
# survive in the repo, and the executed outcome carries the branch + commit sha
# (and the blob carries branch + commit_sha, NOT pr_url) so the runs read model
# surfaces a branch chip with pr_url=None.
#
# What this pins — the WHOLE gate, driven through the REAL path with a local
# git fixture (a bare repo as origin + a seeded working clone):
#   * belt_propose_change (the real MCP handler) validates identity + inputs,
#     files an Instinct Action carrying the ``_code_change`` blob, and returns
#     {ok, action_id, tray_hint} — only after the store confirms the Action.
#   * the apply-on-approve executor, on a REAL git repo: fresh worktree off
#     origin/<base>, git apply --3way, branch feat/belt-<id>, Conventional-
#     Commits commit (summary as body, NO AI attribution), push, PR via an
#     INJECTED fake opener, mark_executed with {pr_url, branch, files_changed}.
#     The branch + commit land in the BARE origin; the worktree is removed.
#   * two actions run back-to-back with no cross-contamination (distinct
#     branches, distinct PRs, distinct outcomes).
#   * reject round-trips for a code_change Action (status REJECTED, recorded
#     reason, NO worktree, NO PR).
#   * apply conflict (doctored diff against a mutated base) → action FAILED,
#     worktree cleaned, no branch pushed.
#   * size-cap rejection (over the changed-line / byte budget).
#   * identity-missing error (no workspace/user ContextVars).
#   * repo-outside-allowlist refusal.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The handler reads
# identity through ee.cloud.chat.agent_service ContextVars (set in-test via
# attach_agent_identity) and the store through pocketpaw.stores.get_instinct_store
# (patched to a tmp-file store so nothing touches ~/.pocketpaw/instinct.db).
#
# Updated: 2026-06-26 (fix/cloud-iso-executor-scope) — the ``store`` fixture's
# factory mock is now WORKSPACE-FAITHFUL (resolves explicit workspace_id →
# current_workspace ContextVar → "", seeded store for "w1" only). The old
# arg-swallowing mock hid C1: the approve-path executor's bare/unscoped store
# call (run OUTSIDE _identity, no ContextVar) still reached the seeded store, so
# split-brain isolation passed. The end-to-end test below now approves+executes
# outside _identity, so it only stays green because the executor threads the
# blob's workspace_id.

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

_PATH = os.environ.get("PATH", "/usr/bin:/bin")

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.belt as belt  # noqa: E402
from pocketpaw_ee.cloud.belt import executor as belt_executor  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers + fixtures
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run git from an arg list (no shell), assert success, return stdout."""
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "Belt Test",
            "GIT_AUTHOR_EMAIL": "belt@test.local",
            "GIT_COMMITTER_NAME": "Belt Test",
            "GIT_COMMITTER_EMAIL": "belt@test.local",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": _PATH,
            "HOME": str(cwd),
        },
    )
    return res.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A bare repo as origin + a seeded working clone.

    Returns the WORKING CLONE path (the thing a proposal's ``repo`` points at).
    The clone has ``origin`` pointing at the bare repo, a committed ``app.py`` on
    ``main``, and ``main`` pushed to origin — so the executor can fetch
    origin/main and branch off it.
    """
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(bare))

    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    # Local identity on the clone so commits succeed regardless of global config.
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")

    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    # Ensure the default branch is named 'main' regardless of git's init default.
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-u", "origin", "main")
    return work


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A git repo with NO ``origin`` remote — the local-only landing fixture.

    A committed ``app.py`` on ``main``, identity configured locally so commits
    succeed, but no remote at all. The executor must apply + commit on the belt
    branch WITHOUT pushing or opening a PR.
    """
    work = tmp_path / "local-work"
    work.mkdir()
    _git(work, "init")
    _git(work, "config", "user.name", "Belt Test")
    _git(work, "config", "user.email", "belt@test.local")
    (work / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "main")
    # Sanity: there is no origin remote.
    remotes = _git(work, "remote")
    assert remotes.strip() == ""
    return work


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, wired in everywhere the gate
    reads it (the MCP handler + the executor both lazy-import
    ``pocketpaw.stores.get_instinct_store``).

    WORKSPACE-FAITHFUL mock (fix/cloud-iso-executor-scope): the factory resolves
    the workspace exactly like the real one — explicit ``workspace_id`` arg →
    ``current_workspace`` ContextVar → "" — and returns the seeded store ONLY
    for ``w1`` (the blob/identity workspace). Any other workspace gets a fresh,
    empty store on a sibling tmp file. The old ``lambda *a, **k: st`` ignored
    the workspace entirely, so the executor's BARE (pre-fix, no-ContextVar)
    approve-path call still hit the seeded store and the isolation bug (C1) went
    green. Now the MCP handler (bare call INSIDE _identity → ContextVar=w1)
    still resolves to ``st``, but the executor (run OUTSIDE _identity) only does
    so once it threads the blob's workspace — which is exactly the fix."""
    from pocketpaw.stores import current_workspace

    st = InstinctStore(tmp_path / "instinct_belt_test.db")
    others: dict[str, InstinctStore] = {}

    def _factory(*_a, workspace_id: str | None = None, **_k) -> InstinctStore:
        ws = str((workspace_id if workspace_id is not None else current_workspace.get()) or "")
        if ws == "w1":
            return st
        return others.setdefault(ws, InstinctStore(tmp_path / f"instinct_other_{ws or 'none'}.db"))

    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", _factory)
    return st


@pytest.fixture
def allowlist(repo: Path, monkeypatch) -> None:
    """Point belt_repo_allowlist at the repo's parent so the real repo resolves
    inside the boundary. Patches get_settings to carry the field."""
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(repo.parent)]

        def __getattr__(self, name):  # delegate everything else to real settings
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())


class _identity:
    """Context manager that sets the workspace/user/session ContextVars the
    handler reads, then resets them."""

    def __init__(self, *, workspace="w1", user="u1", session="sess-1"):
        self._ws, self._user, self._sess = workspace, user, session
        self._tokens = None

    def __enter__(self):
        self._tokens = attach_agent_identity(
            workspace_id=self._ws, user_id=self._user, session_mongo_id=self._sess
        )
        return self

    def __exit__(self, *exc):
        detach_agent_identity(self._tokens)
        return False


class FakePrOpener:
    """An injectable PrOpener that records its call args and returns a fixed
    URL — never touches GitHub."""

    def __init__(self, url="https://github.com/acme/repo/pull/1"):
        self.url = url
        self.calls: list[dict] = []

    async def open_pr(self, *, repo_path, branch, base_branch, title, body) -> str:
        self.calls.append(
            {
                "repo_path": Path(repo_path),
                "branch": branch,
                "base_branch": base_branch,
                "title": title,
                "body": body,
            }
        )
        return self.url


async def _result_body(res: dict) -> dict:
    """Parse the JSON body out of a success MCP response."""
    assert res.get("is_error") is not True, res
    return json.loads(res["content"][0]["text"])


def _good_diff() -> str:
    """A diff that applies cleanly to the seeded app.py — changes the return
    value on line 2."""
    return (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'hi'\n"
        "+    return 'hello world'\n"
    )


# ---------------------------------------------------------------------------
# tool-id / provider contract pins
# ---------------------------------------------------------------------------


def test_tool_id_contract_pin() -> None:
    """The server + tool id are the exact strings the sibling PR hardcodes."""
    assert belt.SERVER_NAME == "pocketpaw_belt"
    assert belt.PROPOSE_CHANGE_TOOL_ID == "mcp__pocketpaw_belt__belt_propose_change"
    assert belt.BELT_TOOL_IDS == ("mcp__pocketpaw_belt__belt_propose_change",)
    assert belt.CODE_CHANGE_KIND == "code_change"


# ---------------------------------------------------------------------------
# the REAL end-to-end path: propose → approve → apply → PR
# ---------------------------------------------------------------------------


async def test_propose_then_approve_applies_and_opens_pr(repo, store, allowlist):
    """Drive the whole gate: propose via the real handler, execute via the real
    executor with a fake PR opener, assert the branch + commit land in the bare
    origin, the opener gets the right args, and mark_executed carries the PR
    url."""
    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": _good_diff(),
                "summary": "Return a friendlier greeting from hello().",
                "task": "Make hello() return a greeting.",
                "orient_ref": "loom: app.py is the entrypoint",
            }
        )
    body = await _result_body(res)
    assert body["ok"] is True
    action_id = body["action_id"]
    assert "tray_hint" in body

    action = await store.get_action(action_id)
    assert action is not None
    assert action.status == ActionStatus.PENDING
    blob = action.parameters["_code_change"]
    assert blob["kind"] == "code_change"
    assert blob["base_branch"] == "main"
    assert blob["workspace_id"] == "w1"
    assert blob["requested_by"] == "u1"
    # The diff is stored verbatim — it's data.
    assert "hello world" in blob["diff"]

    # Approve via the real store, then run the real executor with a fake opener.
    approved = await store.approve(action_id, approver="u1")
    opener = FakePrOpener()
    await belt_executor.execute_approved_change(approved, pr_opener=opener)

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED, final.outcome
    outcome = final.outcome if isinstance(final.outcome, str) else str(final.outcome)
    assert "github.com/acme/repo/pull/1" in outcome

    # The fake opener was called with the belt branch + base.
    assert len(opener.calls) == 1
    call = opener.calls[0]
    assert call["base_branch"] == "main"
    assert call["branch"].startswith("feat/belt-")
    # Conventional Commits title, NO AI attribution.
    assert call["title"].startswith("feat(belt):")
    assert "claude" not in (call["title"] + call["body"]).lower()
    assert "co-authored-by" not in (call["title"] + call["body"]).lower()

    # The branch + commit actually landed in the BARE origin.
    bare = repo.parent / "origin.git"
    branches = _git(bare, "branch", "--list", call["branch"])
    assert call["branch"] in branches
    log = _git(bare, "log", call["branch"], "--oneline", "-1")
    assert "feat(belt)" in log
    # The applied content is on that branch in origin.
    blob_on_branch = _git(bare, "show", f"{call['branch']}:app.py")
    assert "hello world" in blob_on_branch

    # The worktree was cleaned up — nothing left under tmp/belt-actions for it.
    leftover = (
        list((repo / ".git" / "worktrees").glob("*"))
        if (repo / ".git" / "worktrees").exists()
        else []
    )
    # worktree registration is pruned
    assert not any("act-" in p.name for p in leftover)


async def test_two_actions_no_cross_contamination(repo, store, allowlist):
    """Two proposals applied back-to-back land on DISTINCT branches with
    DISTINCT PRs and DISTINCT outcomes — no shared worktree state leaks."""

    async def _propose(summary: str, new_val: str) -> str:
        diff = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def hello():\n"
            "-    return 'hi'\n"
            f"+    return '{new_val}'\n"
        )
        with _identity():
            res = await belt._propose_change_handler(
                {
                    "repo": str(repo),
                    "base_branch": "main",
                    "diff": diff,
                    "summary": summary,
                    "task": "change greeting",
                }
            )
        return (await _result_body(res))["action_id"]

    id_a = await _propose("First greeting change.", "first")
    id_b = await _propose("Second greeting change.", "second")

    opener_a = FakePrOpener(url="https://github.com/acme/repo/pull/10")
    opener_b = FakePrOpener(url="https://github.com/acme/repo/pull/11")

    await belt_executor.execute_approved_change(
        await store.approve(id_a, approver="u1"), pr_opener=opener_a
    )
    await belt_executor.execute_approved_change(
        await store.approve(id_b, approver="u1"), pr_opener=opener_b
    )

    fa = await store.get_action(id_a)
    fb = await store.get_action(id_b)
    assert fa.status == ActionStatus.EXECUTED
    assert fb.status == ActionStatus.EXECUTED
    branch_a = opener_a.calls[0]["branch"]
    branch_b = opener_b.calls[0]["branch"]
    assert branch_a != branch_b

    bare = repo.parent / "origin.git"
    assert "first" in _git(bare, "show", f"{branch_a}:app.py")
    assert "second" in _git(bare, "show", f"{branch_b}:app.py")
    # The second branch must NOT carry the first's change (clean base each time).
    assert "first" not in _git(bare, "show", f"{branch_b}:app.py")


# ---------------------------------------------------------------------------
# reject path
# ---------------------------------------------------------------------------


async def test_reject_round_trips_for_code_change(repo, store, allowlist):
    """A code_change Action rejects cleanly through the real store: status
    REJECTED, reason recorded, no PR, no executor run."""
    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": _good_diff(),
                "summary": "A change to reject.",
                "task": "change greeting",
            }
        )
    action_id = (await _result_body(res))["action_id"]

    rejected = await store.reject(action_id, reason="not now", rejector="u1")
    assert rejected is not None
    assert rejected.status == ActionStatus.REJECTED
    assert rejected.rejected_reason == "not now"

    # No branch was pushed to origin (the executor never ran).
    bare = repo.parent / "origin.git"
    branches = _git(bare, "branch", "--list", "feat/belt-*")
    assert branches.strip() == ""


# ---------------------------------------------------------------------------
# apply-conflict path
# ---------------------------------------------------------------------------


async def test_apply_conflict_marks_failed_and_cleans_up(repo, store, allowlist):
    """A diff that cannot apply (it edits a line the base no longer has) →
    the Action is marked FAILED, no branch is pushed, the worktree is cleaned."""
    # A diff that targets content NOT present in the seeded app.py — git apply
    # --3way can't reconcile it, so it fails.
    bad_diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def hello():\n"
        "-    return 'NONEXISTENT LINE THAT IS NOT IN THE FILE'\n"
        "+    return 'whatever'\n"
        " trailing context that does not match\n"
    )
    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": bad_diff,
                "summary": "A conflicting change.",
                "task": "change greeting",
            }
        )
    action_id = (await _result_body(res))["action_id"]

    opener = FakePrOpener()
    await belt_executor.execute_approved_change(
        await store.approve(action_id, approver="u1"), pr_opener=opener
    )

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.FAILED
    assert opener.calls == []  # no PR opened
    # Nothing pushed to origin.
    bare = repo.parent / "origin.git"
    assert _git(bare, "branch", "--list", "feat/belt-*").strip() == ""
    # The worktree dir is gone.

    leftover = Path(tempfile.gettempdir()) / "belt-actions"
    if leftover.exists():
        assert not any(p.is_dir() for p in leftover.iterdir())


# ---------------------------------------------------------------------------
# input validation: size cap, identity, allowlist
# ---------------------------------------------------------------------------


async def test_size_cap_rejection(repo, store, allowlist):
    """A diff over the changed-line budget is refused with a split-the-task
    error — and no Action is filed."""
    # Build a diff with > MAX_CHANGED_LINES added lines.
    added = "\n".join(f"+line {i}" for i in range(belt.MAX_CHANGED_LINES + 5))
    big_diff = "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,9999 @@\n" + added + "\n"
    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": big_diff,
                "summary": "Way too big.",
                "task": "change greeting",
            }
        )
    assert res.get("is_error") is True
    assert "split the task" in res["content"][0]["text"].lower()
    # No action filed.
    assert await store.list_actions() == []


async def test_identity_missing_errors(repo, store, allowlist):
    """Called without workspace/user ContextVars → an explicit error, no
    Action."""
    res = await belt._propose_change_handler(
        {
            "repo": str(repo),
            "base_branch": "main",
            "diff": _good_diff(),
            "summary": "No identity.",
            "task": "change greeting",
        }
    )
    assert res.get("is_error") is True
    assert "workspace and user context" in res["content"][0]["text"]
    assert await store.list_actions() == []


async def test_repo_outside_allowlist_refused(repo, store, monkeypatch, tmp_path):
    """A repo path outside the allowlist roots is refused — even if it's a real
    git repo on disk."""
    # Allowlist points somewhere ELSE, not the repo's parent.
    from pocketpaw.config import get_settings

    real = get_settings()
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()

    class _S:
        belt_repo_allowlist = [str(other_root)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())

    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": _good_diff(),
                "summary": "Out of bounds.",
                "task": "change greeting",
            }
        )
    assert res.get("is_error") is True
    assert "outside the allowed roots" in res["content"][0]["text"]
    assert await store.list_actions() == []


async def test_empty_diff_refused(repo, store, allowlist):
    """An empty / whitespace diff is refused before anything is stored."""
    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(repo),
                "base_branch": "main",
                "diff": "   \n  ",
                "summary": "Empty.",
                "task": "change greeting",
            }
        )
    assert res.get("is_error") is True
    assert "non-empty unified `diff`" in res["content"][0]["text"]
    assert await store.list_actions() == []


# ---------------------------------------------------------------------------
# LOCAL-ONLY gate mode — a repo with NO origin: apply + commit locally, NO push,
# NO PR; the outcome carries branch + commit sha instead of pr_url.
# ---------------------------------------------------------------------------


@pytest.fixture
def local_allowlist(local_repo: Path, monkeypatch) -> None:
    """Allowlist the no-remote repo's parent so the real repo resolves inside."""
    from pocketpaw.config import get_settings

    real = get_settings()

    class _S:
        belt_repo_allowlist = [str(local_repo.parent)]

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: _S())


async def test_local_only_landing_commits_locally_no_push_no_pr(local_repo, store, local_allowlist):
    """propose→approve against a NO-ORIGIN repo: the change applies + commits on
    feat/belt-<id> LOCALLY, the PR opener is NEVER called (no push, no PR), the
    branch + commit survive in the repo, and the outcome / blob carry branch +
    commit_sha with NO pr_url."""
    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(local_repo),
                "base_branch": "main",
                "diff": _good_diff(),
                "summary": "Friendlier greeting, landed locally.",
                "task": "Make hello() friendlier.",
            }
        )
    action_id = (await _result_body(res))["action_id"]

    approved = await store.approve(action_id, approver="u1")
    opener = FakePrOpener()
    await belt_executor.execute_approved_change(approved, pr_opener=opener)

    # The PR opener was NEVER called — no remote, so no push and no PR.
    assert opener.calls == []

    final = await store.get_action(action_id)
    assert final.status == ActionStatus.EXECUTED, final.outcome

    # The structured blob carries branch + commit_sha, NOT pr_url.
    blob = final.parameters["_code_change"]
    assert blob["branch"].startswith("feat/belt-")
    assert blob.get("commit_sha"), "local-only landing must record the commit sha"
    assert "pr_url" not in blob or blob["pr_url"] is None
    assert blob["files_changed"] == 1

    branch = blob["branch"]
    commit_sha = blob["commit_sha"]

    # The free-text outcome names the branch + sha, and mentions no PR.
    outcome = final.outcome if isinstance(final.outcome, str) else str(final.outcome)
    assert branch in outcome
    assert commit_sha[:12] in outcome
    assert "no pr" in outcome.lower() or "no origin" in outcome.lower()

    # The branch + commit actually landed in the LOCAL repo (promoted off the
    # throwaway worktree). The change is on that branch.
    branches = _git(local_repo, "branch", "--list", branch)
    assert branch in branches
    content = _git(local_repo, "show", f"{branch}:app.py")
    assert "hello world" in content
    # The branch points at the recorded commit sha.
    head = _git(local_repo, "rev-parse", branch).strip()
    assert head == commit_sha

    # Still no origin remote was added.
    assert _git(local_repo, "remote").strip() == ""


async def test_local_only_runs_read_model_branch_chip_no_pr(
    local_repo, store, local_allowlist, monkeypatch
):
    """After a local-only landing, the runs read model returns the run with the
    branch (+ commit_sha) and pr_url=None — so the page renders a branch chip."""
    with _identity():
        res = await belt._propose_change_handler(
            {
                "repo": str(local_repo),
                "base_branch": "main",
                "diff": _good_diff(),
                "summary": "Local-only run for the read model.",
                "task": "tweak greeting",
            }
        )
    action_id = (await _result_body(res))["action_id"]
    approved = await store.approve(action_id, approver="u1")
    await belt_executor.execute_approved_change(approved, pr_opener=FakePrOpener())

    from pocketpaw_ee.cloud.belt import service as belt_service

    detail = await belt_service.get_run("w1", action_id)
    assert detail["status"] == "landed"
    assert detail["stage"] == "done"
    assert detail["branch"].startswith("feat/belt-")
    assert detail["commit_sha"]
    assert detail["pr_url"] is None  # branch chip, not a PR link

    listed = await belt_service.list_runs("w1")
    row = next(r for r in listed["runs"] if r["action_id"] == action_id)
    assert row["branch"].startswith("feat/belt-")
    assert row["pr_url"] is None
    assert row["commit_sha"]
