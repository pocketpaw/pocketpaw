# test_websandbox_git.py — service-level tests for the Web Cursor git write path
# (WC-7/P4a, feat/code-mode): status / stage / commit / push.
#
# All Daytona interaction goes through a FAKE injected via the DI seam (``client=``
# on each git op) — no test touches real Daytona. The registry AND the code
# connections run on real Beanie over mongomock-motor (the ``mongo_db`` fixture) so
# the tenant-filtered guards and the commit-identity resolution are exercised for
# real.
#
# Covers:
#   * status parses the branch header (ahead/behind) + staged / unstaged /
#     untracked / renamed entries.
#   * stage / unstage build the right git add / git reset commands, then return a
#     fresh status.
#   * commit uses the resolved identity, captures the new SHA, and refuses an empty
#     stage cleanly (committed:false, not a crash).
#   * commit identity resolves from the caller's GitHub connection, falling back to
#     the PocketPaw identity when there's none.
#   * push maps exit 0 -> pushed:true and a non-zero exit -> pushed:false + detail
#     (never a 500).
#   * the injection crux: a path and a commit message full of shell metacharacters
#     are shlex-quoted to a single argv token.
#   * tenancy: a not-owned row is a NotFound; an unprovisioned row is a clean 409.
from __future__ import annotations

import shlex
from dataclasses import dataclass, field

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codeconnect import service as codeconnect_service
from pocketpaw_ee.cloud.websandbox import git as git_svc
from pocketpaw_ee.cloud.websandbox import service as sandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR
from pocketpaw_ee.cloud.websandbox.githubapp import GitHubAppError

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeExec:
    exit_code: int = 0
    result: str = ""


@dataclass
class _FakeGitDaytona:
    """Routes git commands to canned exec responses and records every call.

    Drop-in for the DaytonaClient DI seam — only ``execute_command`` is used.
    """

    status_stdout: str = "## paw/edit-abc...origin/paw/edit-abc\n"
    status_exit: int = 0
    diff_cached_exit: int = 1  # 1 = staged changes present -> commit allowed
    commit_exit: int = 0
    head_sha: str = "abc1234"
    push_exit: int = 0
    push_output: str = ""
    calls: list[dict] = field(default_factory=list)

    async def execute_command(self, sandbox_id, command, cwd=None, timeout=None):  # noqa: ANN001
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        c = command
        if "status --porcelain" in c:
            return _FakeExec(self.status_exit, self.status_stdout)
        if "diff --cached --quiet" in c:
            return _FakeExec(self.diff_cached_exit, "")
        if "rev-parse HEAD" in c:
            return _FakeExec(0, self.head_sha + "\n")
        if " commit " in c:
            return _FakeExec(self.commit_exit, "")
        if " push " in c:
            return _FakeExec(self.push_exit, self.push_output)
        # git add / git reset
        return _FakeExec(0, "")


async def _ready_row(
    workspace="w1", user="u1", sandbox_id="dtn-1", branch="paw/edit-abc"
):  # noqa: ANN001
    view = await sandbox_service.create_sandbox(
        workspace, user, {"repo": "https://github.com/acme/api.git",
                          "status": "ready", "sandbox_id": sandbox_id}
    )
    if branch:
        await sandbox_service.update_status(
            workspace,
            user,
            view.id,
            {"status": "ready", "sandbox_id": sandbox_id, "branch": branch},
        )
    return view.id


def _commands(fake: _FakeGitDaytona) -> list[str]:
    return [c["command"] for c in fake.calls]


# ---------------------------------------------------------------------------
# status.
# ---------------------------------------------------------------------------


async def test_status_parses_branch_ahead_behind_and_files() -> None:
    stdout = (
        "## paw/edit-abc...origin/paw/edit-abc [ahead 2, behind 1]\n"
        "M  staged_only.py\n"
        " M unstaged_only.py\n"
        "MM both.py\n"
        "?? untracked.py\n"
        "R  old_name.py -> new_name.py\n"
    )
    fake = _FakeGitDaytona(status_stdout=stdout)
    row_id = await _ready_row()

    resp = await git_svc.git_status("w1", "u1", row_id, client=fake)

    assert resp.branch == "paw/edit-abc"
    assert resp.ahead == 2
    assert resp.behind == 1
    # Ran in the jailed project root.
    assert fake.calls[0]["cwd"] == WEBSANDBOX_WORKDIR
    by_path = {f.path: f for f in resp.files}
    assert by_path["staged_only.py"].index == "M" and by_path["staged_only.py"].staged is True
    assert by_path["unstaged_only.py"].index == " " and by_path["unstaged_only.py"].staged is False
    assert by_path["both.py"].index == "M" and by_path["both.py"].worktree == "M"
    assert by_path["untracked.py"].index == "?" and by_path["untracked.py"].staged is False
    # A rename reports the NEW path.
    assert "new_name.py" in by_path and by_path["new_name.py"].staged is True
    assert "old_name.py" not in by_path


async def test_status_no_upstream_has_zero_ahead_behind() -> None:
    fake = _FakeGitDaytona(status_stdout="## paw/edit-abc\n M app.py\n")
    row_id = await _ready_row()
    resp = await git_svc.git_status("w1", "u1", row_id, client=fake)
    assert resp.branch == "paw/edit-abc"
    assert resp.ahead == 0 and resp.behind == 0


# ---------------------------------------------------------------------------
# stage / unstage.
# ---------------------------------------------------------------------------


async def test_stage_adds_each_path_and_returns_status() -> None:
    fake = _FakeGitDaytona()
    row_id = await _ready_row()

    resp = await git_svc.stage("w1", "u1", row_id, {"paths": ["a.py", "src/b.py"]}, client=fake)

    cmds = _commands(fake)
    assert "git add -- a.py" in cmds
    assert "git add -- src/b.py" in cmds
    # A fresh status was read after staging.
    assert any("status --porcelain" in c for c in cmds)
    assert resp.branch == "paw/edit-abc"


async def test_unstage_resets_each_path() -> None:
    fake = _FakeGitDaytona()
    row_id = await _ready_row()

    await git_svc.stage("w1", "u1", row_id, {"paths": ["a.py"], "unstage": True}, client=fake)

    assert "git reset -q HEAD -- a.py" in _commands(fake)


# ---------------------------------------------------------------------------
# commit.
# ---------------------------------------------------------------------------


async def test_commit_uses_identity_and_captures_sha() -> None:
    # Seed a GitHub connection so the commit is attributed to the real login.
    await codeconnect_service.save_connection("w1", "u1", "inst-1", account_login="octocat")
    fake = _FakeGitDaytona(head_sha="deadbeef")
    row_id = await _ready_row()

    resp = await git_svc.commit("w1", "u1", row_id, {"message": "fix the thing"}, client=fake)

    assert resp.committed is True
    assert resp.sha == "deadbeef"
    commit_cmd = next(c for c in _commands(fake) if " commit " in c)
    assert "user.name=octocat" in commit_cmd
    assert "user.email=octocat@users.noreply.github.com" in commit_cmd


async def test_commit_identity_falls_back_to_pocketpaw() -> None:
    fake = _FakeGitDaytona()
    row_id = await _ready_row()

    await git_svc.commit("w1", "u1", row_id, {"message": "no connection here"}, client=fake)

    commit_cmd = next(c for c in _commands(fake) if " commit " in c)
    assert "user.name=PocketPaw" in commit_cmd
    assert "user.email=noreply@pocketpaw.dev" in commit_cmd


async def test_commit_refuses_empty_stage_cleanly() -> None:
    # diff --cached --quiet exit 0 == nothing staged.
    fake = _FakeGitDaytona(diff_cached_exit=0)
    row_id = await _ready_row()

    resp = await git_svc.commit("w1", "u1", row_id, {"message": "nothing here"}, client=fake)

    assert resp.committed is False
    assert resp.sha == ""
    # Never ran the actual commit.
    assert not any(" commit " in c for c in _commands(fake))


# ---------------------------------------------------------------------------
# push.
# ---------------------------------------------------------------------------


async def test_push_success() -> None:
    fake = _FakeGitDaytona(push_exit=0)
    row_id = await _ready_row(branch="paw/edit-xyz")

    resp = await git_svc.push("w1", "u1", row_id, client=fake)

    assert resp.pushed is True
    assert resp.branch == "paw/edit-xyz"
    push_cmd = next(c for c in _commands(fake) if " push " in c)
    assert "git push -u origin paw/edit-xyz" in push_cmd


async def test_push_failure_returns_detail_not_500() -> None:
    fake = _FakeGitDaytona(
        push_exit=128,
        push_output="fatal: 'origin' does not appear to be a git repository\n",
    )
    row_id = await _ready_row()

    resp = await git_svc.push("w1", "u1", row_id, client=fake)

    assert resp.pushed is False
    assert resp.branch == "paw/edit-abc"
    assert resp.detail  # a human-readable reason, never a raised 500


# ---------------------------------------------------------------------------
# injection safety (the crux).
# ---------------------------------------------------------------------------


async def test_stage_quotes_shell_metacharacters_in_path() -> None:
    evil = 'a.py; rm -rf / #`whoami`'
    fake = _FakeGitDaytona()
    row_id = await _ready_row()

    await git_svc.stage("w1", "u1", row_id, {"paths": [evil]}, client=fake)

    add_cmd = next(c for c in _commands(fake) if c.startswith("git add "))
    assert shlex.quote(evil) in add_cmd
    assert evil in shlex.split(add_cmd)


async def test_commit_quotes_shell_metacharacters_in_message() -> None:
    evil = 'oops"; rm -rf / #$(id) `whoami`'
    fake = _FakeGitDaytona()
    row_id = await _ready_row()

    await git_svc.commit("w1", "u1", row_id, {"message": evil}, client=fake)

    commit_cmd = next(c for c in _commands(fake) if " commit " in c)
    assert shlex.quote(evil) in commit_cmd
    assert evil in shlex.split(commit_cmd)


# ---------------------------------------------------------------------------
# tenancy.
# ---------------------------------------------------------------------------


async def test_status_denies_not_owned_row() -> None:
    fake = _FakeGitDaytona()
    row_id = await _ready_row("w1", "u1")

    with pytest.raises(CloudError) as exc:
        await git_svc.git_status("w2", "u1", row_id, client=fake)
    assert exc.value.status_code == 404
    assert fake.calls == []


async def test_status_not_ready_when_unprovisioned() -> None:
    row = await sandbox_service.create_sandbox(
        "w1", "u1", {"repo": "https://github.com/acme/api.git", "status": "pending"}
    )
    fake = _FakeGitDaytona()

    with pytest.raises(CloudError) as exc:
        await git_svc.git_status("w1", "u1", row.id, client=fake)
    assert exc.value.code == "websandbox.not_ready"
    assert fake.calls == []


# ---------------------------------------------------------------------------
# open pull request (WC-7/P4b).
# ---------------------------------------------------------------------------


@dataclass
class _FakeGitHub:
    """Drop-in for the GitHubAppClient DI seam — only the two methods open_pr uses."""

    default_branch: str = "main"
    pr_url: str = "https://github.com/acme/api/pull/7"
    pr_number: int = 7
    reachable: bool = True
    raise_pr_422: bool = False
    calls: list = field(default_factory=list)

    async def get_default_branch(self, installation_id, repo, *, now=None):  # noqa: ANN001
        self.calls.append(("default_branch", installation_id, repo))
        if not self.reachable:
            raise GitHubAppError("websandbox.repo_unreachable", "not found", status=404)
        return self.default_branch

    async def create_pull_request(
        self, installation_id, repo, *, head, base, title, body="", now=None
    ):  # noqa: ANN001
        self.calls.append(("pr", installation_id, repo, head, base, title, body))
        if self.raise_pr_422:
            raise GitHubAppError(
                "websandbox.pr_invalid", "No commits between main and paw/edit-abc", status=422
            )
        return {"url": self.pr_url, "number": self.pr_number}


async def test_open_pr_returns_url_and_number() -> None:
    await codeconnect_service.save_connection("w1", "u1", "inst-1", account_login="octocat")
    row_id = await _ready_row(branch="paw/edit-abc")
    gh = _FakeGitHub(
        default_branch="main", pr_url="https://github.com/acme/api/pull/9", pr_number=9
    )

    resp = await git_svc.open_pr(
        "w1", "u1", row_id, {"title": "Ship it", "body": "please"},
        client=_FakeGitDaytona(), github_client=gh,
    )

    assert resp.url == "https://github.com/acme/api/pull/9"
    assert resp.number == 9
    # The PR opened against the repo's default branch with the row's feature branch.
    pr_call = next(c for c in gh.calls if c[0] == "pr")
    _, inst, repo, head, base, title, body = pr_call
    assert inst == "inst-1" and repo == "acme/api"
    assert head == "paw/edit-abc" and base == "main"
    assert title == "Ship it" and body == "please"


async def test_open_pr_surfaces_github_422() -> None:
    await codeconnect_service.save_connection("w1", "u1", "inst-1", account_login="octocat")
    row_id = await _ready_row(branch="paw/edit-abc")
    gh = _FakeGitHub(raise_pr_422=True)

    with pytest.raises(CloudError) as exc:
        await git_svc.open_pr(
            "w1", "u1", row_id, {"title": "t"}, client=_FakeGitDaytona(), github_client=gh
        )
    assert exc.value.status_code == 422


async def test_open_pr_no_reachable_connection_is_400() -> None:
    # A connection exists but its installation can't reach the repo.
    await codeconnect_service.save_connection("w1", "u1", "inst-1", account_login="octocat")
    row_id = await _ready_row()
    gh = _FakeGitHub(reachable=False)

    with pytest.raises(CloudError) as exc:
        await git_svc.open_pr(
            "w1", "u1", row_id, {"title": "t"}, client=_FakeGitDaytona(), github_client=gh
        )
    assert exc.value.status_code == 400
    # Never attempted the PR itself.
    assert not any(c[0] == "pr" for c in gh.calls)


async def test_open_pr_no_connection_at_all_is_400() -> None:
    row_id = await _ready_row()  # no code connection seeded
    gh = _FakeGitHub()

    with pytest.raises(CloudError) as exc:
        await git_svc.open_pr(
            "w1", "u1", row_id, {"title": "t"}, client=_FakeGitDaytona(), github_client=gh
        )
    assert exc.value.status_code == 400


async def test_open_pr_no_branch_is_clean_error() -> None:
    await codeconnect_service.save_connection("w1", "u1", "inst-1", account_login="octocat")
    row_id = await _ready_row(branch="")  # ready but no feature branch bound
    gh = _FakeGitHub()

    with pytest.raises(CloudError) as exc:
        await git_svc.open_pr(
            "w1", "u1", row_id, {"title": "t"}, client=_FakeGitDaytona(), github_client=gh
        )
    assert exc.value.code == "websandbox.no_branch"


async def test_open_pr_denies_not_owned_row() -> None:
    await codeconnect_service.save_connection("w2", "u1", "inst-1", account_login="octocat")
    row_id = await _ready_row("w1", "u1")
    gh = _FakeGitHub()

    with pytest.raises(CloudError) as exc:
        await git_svc.open_pr(
            "w2", "u1", row_id, {"title": "t"}, client=_FakeGitDaytona(), github_client=gh
        )
    assert exc.value.status_code == 404


async def test_open_pr_not_ready_when_unprovisioned() -> None:
    row = await sandbox_service.create_sandbox(
        "w1", "u1", {"repo": "https://github.com/acme/api.git", "status": "pending"}
    )
    gh = _FakeGitHub()

    with pytest.raises(CloudError) as exc:
        await git_svc.open_pr(
            "w1", "u1", row.id, {"title": "t"}, client=_FakeGitDaytona(), github_client=gh
        )
    assert exc.value.code == "websandbox.not_ready"
