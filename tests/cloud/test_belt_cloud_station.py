# tests/cloud/test_belt_cloud_station.py — the CLOUD develop station.
#
# ``CloudTaskDispatcher`` launches a Claude Code cloud session for a queued run
# and tracks the session's PR through gh. Every subprocess goes through the
# injected ``Runner``, so these tests script git, the pty launcher and gh with a
# fake: nothing real is launched and no network is touched.
#
# LAND: a merged PR is approved by system:fast-lane and executed (pr_url, merge
# sha, head branch, the PR's diff, the bound repo fetched, a failed fetch never
# fatal); the launch ran from the launch clone detached at origin/<base> with a
# brief naming the issue, the Belt-Run marker, the label, the charter check and
# the seated dev's fenced instructions. A lane refusal attaches the diff at the
# per-diff gate; no session URL or a tracker timeout leaves the run queued with
# ``headless_error``; the launch clone is re-pointed to another repo's origin
# (never a malformed slug), keeps streamed output on a timeout, and fails clearly
# when the CLI does not trust it; Clef shadow-scores the PR's diff.
# BASE: a run with no base launches from the bound repo's checked-out branch
# (main when detached) and stores it for the brief, land and escalation; its own wins.
# FIXERS: a conflicting PR gets a rebase brief, a red one the failed job's last
# 40 log lines, after the grace period; green within grace gets none; the cap
# escalates; a failed fixer launch is recorded and tracking goes on.
# SILENT WORKERS: no PR by the no-PR timeout relaunches ONE worker (its brief
# says the last attempt produced none), then the run is rejected with "no PR
# from N worker(s) in Xm": the mandate's due check is held before and free
# after, and the task's sighting stays open; a worker's comment on the issue
# since the launch releases it at once with that line and labels the issue
# needs-human; the relaunch count and clock survive a resume.
# ADOPTION: the brief says not to open a second PR for the issue; a run with no
# PR of its own adopts an OPEN PR that closes its issue (never a closed one or
# #930 for #93), also at the poll that would release it, and lands on its merge;
# a resumed run keeps its adopted PR even once merged.
# TESTER: with the flag and a seat, a landed run launches ONE tester (engine
# brief: fuzz; app brief: snapshots against docs/design.md); off by default,
# none without a seat, one per run, a failed launch recorded. Its Belt-Test PR
# is nudged when green, gets its own fixer when red (marker kept, run fixes and
# screenshots untouched) and is recorded merged; no PR by the timeout is done;
# tracking it never holds the mandate's due check; a restart resumes it.
# SCREENSHOTS: the head's CI ``snapshots`` artifact via gh api, real PNGs kept
# (sanitized, 40 files / 20 MB), once the PR is known and on land, never fatal.
# RESUME: a launched run is tracked again, never relaunched, and lands (also
# when merged while down); unmarked or failed runs and unreadable workspaces are
# skipped; the timeout counts from ``launched_at``; a fixer is not doubled.
# LANE NUDGE: a labelled PR green and mergeable past the interval gets one
# ``gh workflow run "Fast Lane"``, one per interval per repo; red, conflicting,
# pending, unknown or unlabelled PRs get none; a failed dispatch is harmless.
# ``pty_run.py`` runs for real: it streams output and exits on the folder-trust
# prompt; ``wire_from_env`` / ``wire_cloud`` read the env.

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import cloud_station, headless  # noqa: E402
from pocketpaw_ee.cloud.belt import develop_station as ds  # noqa: E402
from pocketpaw_ee.cloud.belt import service as belt_service  # noqa: E402
from pocketpaw_ee.cloud.belt.cloud_station import (  # noqa: E402
    MARKER,
    TEST_MARKER,
    CloudTaskDispatcher,
)
from pocketpaw_ee.cloud.belt.develop_station import ClaudeCodeDevelop  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import ShiftDoc, SightingDoc  # noqa: E402
from pocketpaw_ee.cloud.mandates.executor import StationTaskDispatcher  # noqa: E402

from pocketpaw.instinct.models import ActionStatus, ActionTrigger  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

WS = "w1"
SLUG = "qbtrix/creative-core"
DIFF = "diff --git a/A.swift b/A.swift\n--- a/A.swift\n+++ b/A.swift\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    st = InstinctStore(tmp_path / "instinct_cloud.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo, launch = tmp_path / "repo", tmp_path / "launch"
    repo.mkdir()
    launch.mkdir()
    monkeypatch.setattr(ClaudeCodeDevelop, "_resolve_repo", staticmethod(lambda r: repo))
    return repo, launch


async def _queue(
    store: InstinctStore,
    repo: Path,
    mandate_id: str = "m1",
    plan_action_id: str = "plan-1",
    base: str = "main",
) -> str:
    import pocketpaw_ee.cloud.mandates.executor as ex

    async def fake_repo(workspace_id: str, mandate_id: str) -> str:
        return str(repo)

    async def fake_base(repo: str) -> str:
        return base  # "" = the bound repo was detached when the run was queued

    orig = ex._repo_for_mandate
    ex._repo_for_mandate = fake_repo  # type: ignore[assignment]
    from pocketpaw_ee.cloud.belt import service as belt_service

    orig_base = belt_service.default_base
    belt_service.default_base = fake_base  # type: ignore[assignment]
    try:
        return await StationTaskDispatcher().dispatch(
            workspace_id=WS,
            mandate_id=mandate_id,
            shift_no=1,
            plan_action_id=plan_action_id,
            index=1,
            task={
                "title": "G41: Text shapes",
                "why": "Issue #93 is an open p1 gap.",
                "expected_outcome": "gaps closed up",
            },
        )
    finally:
        ex._repo_for_mandate = orig  # type: ignore[assignment]
        belt_service.default_base = orig_base  # type: ignore[assignment]


# The source issue's comments by default: one from long before any launch.
OLD_COMMENT = [{"createdAt": "2020-01-02T03:04:05Z", "body": "Spec question from long ago."}]


class FakeRunner:
    """Scripted git / launcher / gh. ``pr`` is what ``gh pr view`` returns for
    the run's PR (#9), ``tester_pr`` for the tester's (#20, ``Belt-Test``)."""

    def __init__(
        self,
        *,
        launch_out: str | list[str],
        launch_origin: str = SLUG,
        repo_origin: str = SLUG,
        pr: dict | list[dict] | None = None,
        launch_code: int = 0,
        launch_lines: tuple[str, ...] = (),
        repo_fetch_code: int = 0,
        snapshots: dict[str, bytes] | None = None,
        download_code: int = 0,
        workflow_code: int = 0,
        tester_pr: dict | list[dict] | None = None,
        issue_comments: list[dict] | None = None,
        closing: list[list[dict]] | None = None,
        repo_head: str = "",
    ):
        self.calls: list[tuple[list[str], Path]] = []
        self.repo_head = repo_head  # ``rev-parse --abbrev-ref HEAD`` in the bound repo
        # A list is consumed one entry per launch / per ``gh pr view``, holding the last.
        self.launch_outs = [launch_out] if isinstance(launch_out, str) else list(launch_out)
        self.launch_code = launch_code
        self.launch_lines = launch_lines  # streamed through on_line before returning
        self.launch_origin = launch_origin
        self.repo_origin = repo_origin
        self.repo_fetch_code = repo_fetch_code  # ``git fetch`` in the bound repo
        self.prs = pr if isinstance(pr, list) else [pr]
        self.tester_prs = tester_pr if isinstance(tester_pr, list) else [tester_pr]
        self.issue_comments = OLD_COMMENT if issue_comments is None else issue_comments
        # Rows ``gh pr list`` gives INSTEAD of the run's own PR, one entry per call
        # (holding the last): other PRs, e.g. one that closes the run's issue.
        self.closing = closing
        self.run_ref = ""
        # The PR head's CI runs: 502 carries the ``snapshots`` artifact, 501 does not.
        self.snapshots = snapshots
        self.download_code = download_code
        self.also: list[str] = []  # more run refs, each with its own PR (#10, #11, ...)
        self.workflow_code = workflow_code  # ``gh workflow run`` (the lane nudge)
        self.clock = lambda: 0.0  # the dispatcher's clock, to stamp each dispatch
        self.dispatched: list[float] = []

    @staticmethod
    def _next(seq: list):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        self.calls.append((list(argv), cwd))
        if "get-url" in argv:
            slug = self.repo_origin if cwd.name == "repo" else self.launch_origin
            return 0, f"https://github.com/{slug}.git\n", ""
        if "--cloud" in argv:
            for line in self.launch_lines:
                if on_line is not None:
                    await on_line(line)
            if self.launch_code == -1:  # what run_subprocess returns after the kill
                return -1, "", f"timed out after {timeout:.0f}s"
            return self.launch_code, self._next(self.launch_outs), ""
        if argv[:3] == ["gh", "pr", "list"]:
            prs = []
            if self.closing is not None:
                prs = list(self._next(self.closing))
            elif self.prs[0] is not None:
                body = f"Closes #93\n\n{MARKER} {self.run_ref}\n"
                prs = [{"number": 7, "body": "other"}, {"number": 9, "body": body}]
                prs += [
                    {"number": 10 + i, "body": f"{MARKER} {r}"} for i, r in enumerate(self.also)
                ]
            if self.tester_prs[0] is not None:
                prs.append({"number": 20, "body": f"Tests.\n{TEST_MARKER} {self.run_ref}"})
            return 0, json.dumps(prs), ""
        if argv[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"comments": self.issue_comments}), ""
        if argv[:3] == ["gh", "workflow", "run"]:
            self.dispatched.append(self.clock())
            return self.workflow_code, "", "HTTP 403" if self.workflow_code else ""
        if argv[:3] == ["gh", "pr", "view"]:
            seq = self.tester_prs if argv[3] == "20" else self.prs
            return 0, json.dumps(self._next(seq)), ""
        if argv[:3] == ["gh", "run", "view"]:
            return 0, "".join(f"build\tstep\tL{i:03d}\n" for i in range(1, 51)), ""
        if argv[:3] == ["gh", "pr", "diff"]:
            return 0, DIFF, ""
        if argv[:2] == ["gh", "api"] and argv[2].endswith("/artifacts"):
            names = ["snapshots"] if "/runs/502/" in argv[2] else ["coverage"]
            return 0, json.dumps({"artifacts": [{"name": n, "expired": False} for n in names]}), ""
        if argv[:2] == ["gh", "api"] and "/actions/runs?head_sha=" in argv[2]:
            runs = [{"id": 501}, {"id": 502}] if self.snapshots is not None else []
            return 0, json.dumps({"total_count": len(runs), "workflow_runs": runs}), ""
        if argv[:3] == ["gh", "run", "download"]:
            out = Path(argv[argv.index("-D") + 1])
            for rel, data in (self.snapshots or {}).items():
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                (out / rel).write_bytes(data)
            return self.download_code, "", "no artifact" if self.download_code else ""
        if "fetch" in argv and cwd.name == "repo":
            return self.repo_fetch_code, "", "fatal: unable to access origin"
        if "--abbrev-ref" in argv and cwd.name == "repo":
            return 0, f"{self.repo_head}\n", ""
        return 0, "", ""  # git fetch / checkout


def _dispatcher(runner: FakeRunner, launch: Path, **kw) -> CloudTaskDispatcher:
    async def charter(workspace_id: str, mandate_id: str) -> dict:
        return {"repo": "x", "charter": {"checks": ["make ci"]}}

    now = [0.0]  # a fake clock the fake sleep advances: a loop that never ends fails fast

    async def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] > 30 * 24 * 3600:
            raise RuntimeError("the tracker never stopped polling")

    return CloudTaskDispatcher(
        launch_dir=str(launch),
        runner=runner,
        charter_for=charter,
        sleep=sleep,
        clock=lambda: now[0],
        **kw,
    )


SESSION = "Created cloud session: https://claude.ai/code/session_ABC123\r\n"


async def test_merged_pr_lands_the_run(store, dirs, monkeypatch):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    pr = {
        "state": "MERGED",
        "url": f"https://github.com/{SLUG}/pull/9",
        "headRefName": "claude/g41-text",
        "mergeCommit": {"oid": "abc123"},
        "comments": [],
        "files": [{"path": "Sources/FlareCore/Text.swift"}, {"path": "Tests/T.swift"}],
    }
    runner = FakeRunner(launch_out=SESSION, pr=pr)
    runner.run_ref = run_ref
    scored: list[tuple] = []
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.belt.clef.shadow_diff", lambda *a, **k: scored.append(a)
    )

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    # Clef's shadow scorer got the PR's diff once the PR was known.
    assert scored == [(WS, run_ref, DIFF, str(repo))]
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.approved_by == "system:fast-lane"
    cc = action.parameters["_code_change"]
    assert cc["pr_url"] == pr["url"]
    assert cc["commit_sha"] == "abc123"
    assert cc["branch"] == "claude/g41-text"
    assert cc["files_changed"] == 2
    # The task's #93 is the run's issue (the Review Room's run detail reads it).
    assert cc["issue_url"] == f"https://github.com/{SLUG}/issues/93"
    assert cc["cloud"]["session_url"] == "https://claude.ai/code/session_ABC123"
    assert cc["cloud"]["pr"] == 9
    assert cc["cloud"]["launched_at"] <= time.time()  # the run timeout counts from it
    assert any(a[:4] == ["gh", "pr", "view", "9"] for a, _ in runner.calls)
    assert "headless_state" not in cc
    # The merged PR's diff is on the blob for the UI, reusing Clef's fetch.
    assert cc["diff"] == DIFF
    assert sum(a[:3] == ["gh", "pr", "diff"] for a, _ in runner.calls) == 1
    # The bound repo is fetched so the blueprint reads a fresh origin/main.
    assert any(a[-3:] == ["fetch", "-q", "origin"] and c == repo for a, c in runner.calls)

    # Launched from the launch clone, detached at origin/main, with the brief.
    git = [argv for argv, cwd in runner.calls if cwd == launch and "git" in argv[0]]
    assert any(a[-3:] == ["-q", "--detach", "origin/main"] for a in git)
    (launch_argv, launch_cwd) = next((a, c) for a, c in runner.calls if "--cloud" in a)
    assert launch_cwd == launch
    assert launch_argv[1] == cloud_station.PTY_RUN
    brief = launch_argv[-1]
    assert "Closes #93" in brief
    assert f"{MARKER} {run_ref}" in brief
    assert "`fast-lane`" in brief and "`make ci`" in brief
    assert "Never merge" in brief


async def test_lane_refusal_escalates_to_the_diff_gate(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    pr = {
        "state": "OPEN",
        "url": f"https://github.com/{SLUG}/pull/9",
        "comments": [{"body": "Fast lane: not eligible. 512 lines > 400."}],
        "files": [],
    }
    runner = FakeRunner(launch_out=SESSION, pr=pr)
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.PENDING
    cc = action.parameters["_code_change"]
    assert cc["diff"] == DIFF
    assert cc["station_pending"] is False
    assert "512 lines > 400" in cc["summary"]
    assert cc["pr_url"] == pr["url"]


async def test_no_session_url_leaves_the_run_queued(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out="Error: --cloud requires an interactive terminal.")

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["station_pending"] is True
    assert "no cloud session started" in cc["headless_error"]
    assert not any(a[0] == "gh" for a, _ in runner.calls)


def _launch_git(runner: FakeRunner, launch: Path) -> list[list[str]]:
    return [a for a, c in runner.calls if c == launch and "git" in a[0]]


async def test_launch_clone_on_another_origin_is_repointed_then_fetched(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, launch_origin="someone/else", pr=None)
    runner.run_ref = run_ref

    await _dispatcher(runner, launch, timeout_seconds=0).run(run_ref, workspace_id=WS)

    git = _launch_git(runner, launch)
    url = f"https://github.com/{SLUG}.git"
    set_url = git.index([*cloud_station._GIT, "remote", "set-url", "origin", url])
    fetch = next(i for i, a in enumerate(git) if a[-4:] == ["fetch", "-q", "origin", "main"])
    assert set_url < fetch
    assert any("--cloud" in a for a, _ in runner.calls)
    assert any(a[:3] == ["gh", "pr", "list"] and SLUG in a for a, _ in runner.calls)


async def test_launch_clone_on_the_same_origin_is_not_repointed(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=None)
    runner.run_ref = run_ref

    await _dispatcher(runner, launch, timeout_seconds=0).run(run_ref, workspace_id=WS)

    assert not any("set-url" in a for a in _launch_git(runner, launch))
    assert any("--cloud" in a for a, _ in runner.calls)


async def test_malformed_bound_repo_slug_is_refused(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, launch_origin="someone/else", repo_origin="../evil")

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert "unsafe GitHub slug" in cc["headless_error"]
    assert not any("set-url" in a or "fetch" in a for a in _launch_git(runner, launch))
    assert not any("--cloud" in a for a, _ in runner.calls)


@pytest.mark.parametrize(("head", "base"), [("dev", "dev"), ("HEAD", "main")])
async def test_run_with_no_base_takes_the_bound_repos_branch(store, dirs, head, base):
    """A run queued with no base (its bound repo was detached then) launches from
    the bound repo's checked-out branch, the develop station's rule, and keeps
    it: the brief, the run record and the land all name it. ``main`` only when
    the bound repo is detached at launch too. Breaks if the launch hard-defaults
    to main or the resolved base is not stored on the run."""
    repo, launch = dirs
    run_ref = await _queue(store, repo, base="")
    pr = {
        "state": "MERGED",
        "url": f"https://github.com/{SLUG}/pull/9",
        "headRefName": "claude/g41-text",
        "mergeCommit": {"oid": "abc123"},
        "comments": [],
        "files": [],
    }
    runner = FakeRunner(launch_out=SESSION, pr=pr, repo_head=head)
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    git = _launch_git(runner, launch)
    assert any(a[-4:] == ["fetch", "-q", "origin", base] for a in git)
    assert any(a[-3:] == ["-q", "--detach", f"origin/{base}"] for a in git)
    brief = next(a for a, _ in runner.calls if "--cloud" in a)[-1]
    assert f"Open ONE pull request to {base}." in brief
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.parameters["_code_change"]["base_branch"] == base
    assert f"into {base} through the fast lane" in str(action.outcome)


async def test_escalated_run_with_no_base_carries_the_bound_repos_branch(store, dirs):
    """A lane refusal escalates the diff on the base the launch resolved, so the
    approved diff's line PR opens into dev, not main."""
    repo, launch = dirs
    run_ref = await _queue(store, repo, base="")
    pr = {
        "state": "OPEN",
        "url": f"https://github.com/{SLUG}/pull/9",
        "comments": [{"body": "Fast lane: not eligible. 512 lines > 400."}],
        "files": [],
    }
    runner = FakeRunner(launch_out=SESSION, pr=pr, repo_head="dev")
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["diff"] == DIFF
    assert cc["base_branch"] == "dev"


async def test_run_with_a_base_keeps_it_over_the_bound_repos_branch(store, dirs):
    """An explicit base wins: the bound repo's branch is never read."""
    repo, launch = dirs
    run_ref = await _queue(store, repo, base="release/2")
    runner = FakeRunner(launch_out=SESSION, pr=None, repo_head="dev")
    runner.run_ref = run_ref

    await _dispatcher(runner, launch, timeout_seconds=0).run(run_ref, workspace_id=WS)

    git = _launch_git(runner, launch)
    assert any(a[-3:] == ["-q", "--detach", "origin/release/2"] for a in git)
    assert not any("--abbrev-ref" in a for a, c in runner.calls if c == repo)
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["base_branch"] == "release/2"


async def test_tracker_timeout_without_a_pr(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=None)

    await _dispatcher(runner, launch, timeout_seconds=0).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.PENDING
    assert "no PR" in action.parameters["_code_change"]["headless_error"]


def test_wire_from_env_cloud_needs_a_launch_clone(tmp_path, monkeypatch):
    from pocketpaw_ee.cloud.belt.develop_station import wire_from_env
    from pocketpaw_ee.cloud.shared import db as cloud_db

    monkeypatch.setattr(cloud_db, "is_multi_tenant_cloud", lambda: False)
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "headless")
    monkeypatch.setenv("POCKETPAW_FACTORY_DEVELOP", "cloud")
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", None)
    monkeypatch.setattr(headless, "_PRODUCTION_DEVELOP_FN", None)
    monkeypatch.delenv("POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR", raising=False)
    assert wire_from_env() is False
    assert headless.resolve_headless_dispatcher() is None

    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR", str(tmp_path))
    assert wire_from_env() is True
    wired = headless.resolve_headless_dispatcher()
    assert isinstance(wired, CloudTaskDispatcher)
    assert wired.background is True and wired.launch_dir == str(tmp_path)


async def test_seated_dev_instructions_ride_the_brief(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out="no session")
    d = _dispatcher(runner, launch)

    async def worker_for(workspace_id: str, agent_id: str, seated_by: str) -> dict:
        return {"name": "dev", "model": "", "instructions": "Read docs/review-rubric.md first."}

    d._blobs.worker_for = worker_for
    action = await store.get_action(run_ref)
    params = dict(action.parameters)
    params["_code_change"] = {**params["_code_change"], "worker": {"agent_id": "a1"}}
    await store.update_parameters(run_ref, params)

    await d.run(run_ref, workspace_id=WS)

    brief = next(a for a, _ in runner.calls if "--cloud" in a)[-1]
    assert "<untrusted>\nRead docs/review-rubric.md first.\n</untrusted>" in brief


async def test_launch_timeout_keeps_the_streamed_output(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(
        launch_out="",
        launch_code=-1,
        launch_lines=("Connecting to Claude Code cloud...", "\x1b[2mWaiting for login\x1b[0m"),
    )

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    err = (await store.get_action(run_ref)).parameters["_code_change"]["headless_error"]
    assert "no cloud session started (exit -1)" in err
    assert "Waiting for login" in err and "Connecting to Claude Code cloud" in err
    assert "\x1b" not in err  # escape codes stripped from the tail


async def test_untrusted_launch_clone_fails_with_a_clear_error(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    prompt = (
        "Quick safety check: Is this a project you created or one you trust?\r\n"
        "\x1b[1m> 1. Yes,\x1b[1CI\x1b[1Ctrust\x1b[1Cthis\x1b[1Cfolder\x1b[0m"
    )
    runner = FakeRunner(launch_out=prompt, launch_code=3)

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    err = (await store.get_action(run_ref)).parameters["_code_change"]["headless_error"]
    assert err.startswith(f"LAUNCH: the claude CLI does not trust {launch.resolve()}")
    assert "accept the trust prompt" in err
    assert not any(a[0] == "gh" for a, _ in runner.calls)


# -- fixers: a stuck PR (conflicting, or red CI) gets a fixer session ---------

PR_URL = f"https://github.com/{SLUG}/pull/9"
RED_CHECK = {
    "__typename": "CheckRun",
    "name": "build (macos-14)",
    "workflowName": "CI",
    "status": "COMPLETED",
    "conclusion": "FAILURE",
    "detailsUrl": f"https://github.com/{SLUG}/actions/runs/555/job/777",
}
GREEN_CHECK = {**RED_CHECK, "name": "build (ubuntu)", "conclusion": "SUCCESS"}


def _open(head: str = "h1", *, mergeable: str = "MERGEABLE", red: bool = False) -> dict:
    return {
        "state": "OPEN",
        "url": PR_URL,
        "headRefName": "claude/g41-text",
        "headRefOid": head,
        "mergeable": mergeable,
        "statusCheckRollup": [GREEN_CHECK, RED_CHECK] if red else [GREEN_CHECK],
        "comments": [],
        "files": [],
    }


MERGED = {**_open(), "state": "MERGED", "mergeCommit": {"oid": "abc123"}}


@pytest.fixture
def stages(monkeypatch) -> list[str]:
    seen: list[str] = []

    async def emit(**kw) -> None:
        seen.append(kw["stage"])

    monkeypatch.setattr(cloud_station, "_emit_run_updated", emit)
    return seen


def _fixer(runner: FakeRunner, launch: Path) -> CloudTaskDispatcher:
    # Polls at t=0, 60, 120: a PR stuck since t=0 passes the 100 s grace at t=120.
    return _dispatcher(runner, launch, poll_seconds=60, fix_grace_seconds=100)


def _briefs(runner: FakeRunner) -> list[str]:
    return [a[-1] for a, _ in runner.calls if "--cloud" in a]


async def test_conflicting_pr_gets_one_rebase_fixer_then_merges(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    stuck = _open(mergeable="CONFLICTING")
    # The clone stays on another repo (as if another run used it in between).
    runner = FakeRunner(
        launch_out=SESSION, launch_origin="someone/else", pr=[stuck, stuck, stuck, stuck, MERGED]
    )
    runner.run_ref = run_ref

    await _fixer(runner, launch).run(run_ref, workspace_id=WS)

    briefs = _briefs(runner)
    assert len(briefs) == 2  # the worker, then exactly one fixer
    # The fixer re-points the clone too, so its session opens on the right repo.
    assert sum("set-url" in a for a in _launch_git(runner, launch)) == 2
    fix = briefs[1]
    assert "#9" in fix and "`claude/g41-text`" in fix
    assert "rebase" in fix.lower() and "origin/main" in fix
    assert "both" in fix.lower() and "--force-with-lease" in fix
    assert "Do not open a new pull request" in fix and "Never merge" in fix
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    (entry,) = action.parameters["_code_change"]["fixes"]
    assert entry["kind"] == "rebase"
    assert entry["session_url"] == "https://claude.ai/code/session_ABC123"
    assert "fix" in stages


async def test_red_pr_fixer_brief_carries_the_failed_log_tail(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    red = _open(red=True)
    runner = FakeRunner(launch_out=SESSION, pr=[red, red, red, red, MERGED])
    runner.run_ref = run_ref

    await _fixer(runner, launch).run(run_ref, workspace_id=WS)

    fix = _briefs(runner)[1]
    assert "build (macos-14)" in fix and "#9" in fix
    assert "L050" in fix and "L011" in fix and "L010" not in fix  # the last 40 lines
    assert "<untrusted>" in fix  # CI output is fenced as data
    assert "SAME branch" in fix and "Do not open a new pull request" in fix
    view = next(a for a, _ in runner.calls if a[:3] == ["gh", "run", "view"])
    assert "555" in view and "--log-failed" in view
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert [f["kind"] for f in cc["fixes"]] == ["ci"]


async def test_fixer_attempts_cap_escalates(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    # Each fixer pushes a new head that is still red.
    reds = [_open(h, red=True) for h in ("h1", "h1", "h1", "h2", "h2", "h2", "h3")]
    runner = FakeRunner(launch_out=SESSION, pr=reds)
    runner.run_ref = run_ref

    await _fixer(runner, launch).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 3  # the worker + the default cap of 2 fixers
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.PENDING
    cc = action.parameters["_code_change"]
    assert cc["diff"] == DIFF and cc["station_pending"] is False
    assert len(cc["fixes"]) == 2
    assert "2 fixer" in cc["summary"]
    assert "headless_error" not in cc


async def test_pr_green_within_grace_launches_no_fixer(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    red = _open(red=True)
    runner = FakeRunner(launch_out=SESSION, pr=[red, red, MERGED])
    runner.run_ref = run_ref

    await _fixer(runner, launch).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert "fixes" not in action.parameters["_code_change"]
    assert "fix" not in stages


async def test_fixer_launch_failure_is_recorded_and_tracking_goes_on(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    stuck = _open(mergeable="CONFLICTING")
    runner = FakeRunner(
        launch_out=[SESSION, "Error: rate limited"], pr=[stuck, stuck, stuck, stuck, MERGED]
    )
    runner.run_ref = run_ref

    await _fixer(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    (entry,) = cc["fixes"]
    assert entry["kind"] == "rebase" and "session_url" not in entry
    assert "no cloud session started" in entry["error"]
    assert "headless_error" not in cc


async def test_landing_refetches_the_diff_when_the_head_moved(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=[_open("h1"), {**MERGED, "headRefOid": "h2"}])
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.parameters["_code_change"]["diff"] == DIFF
    # Clef's diff was of h1; the landed diff is fetched again for h2.
    assert sum(a[:3] == ["gh", "pr", "diff"] for a, _ in runner.calls) == 2


async def test_bound_repo_fetch_failure_never_fails_the_land(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED, repo_fetch_code=128)
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    assert cc["diff"] == DIFF and "headless_error" not in cc
    assert any(a[-3:] == ["fetch", "-q", "origin"] and c == repo for a, c in runner.calls)


# -- the tester: one cloud session per landed run tries to break what it built --


@pytest.fixture
def updates(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    async def emit(**kw) -> None:
        seen.append((kw["status"], kw["stage"]))

    monkeypatch.setattr(cloud_station, "_emit_run_updated", emit)
    return seen


TESTER_SEAT = {"agent_id": "t1", "name": "Tess", "setup": "", "seated_by": "u1"}


def _tester(
    runner: FakeRunner, launch: Path, *, seat: dict | None = TESTER_SEAT, mandate="m1", **kw
):
    d = _dispatcher(runner, launch, tester=True, **kw)

    async def tester_for(workspace_id: str, mandate_id: str) -> dict | None:
        assert (workspace_id, mandate_id) == (WS, mandate)
        return seat

    async def worker_for(workspace_id: str, agent_id: str, seated_by: str) -> dict:
        assert agent_id == "t1"
        return {"name": "Tess", "model": "", "instructions": "Be mean to edge cases."}

    d.tester_for = tester_for
    d._blobs.worker_for = worker_for
    return d


async def test_a_landed_run_launches_one_tester(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    await _tester(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    briefs = _briefs(runner)
    assert len(briefs) == 2  # the worker, then the tester
    brief = briefs[1]
    assert "tester" in brief and "break" in brief
    assert PR_URL in brief and "#93" in brief  # the merged PR and the source issue
    assert f"{cloud_station.TEST_MARKER} {run_ref}" in brief
    assert "`bug`" in brief and "`gap`" in brief and "`fast-lane`" in brief
    assert "Do not fix" in brief and "Never merge" in brief
    assert "fuzz" in brief  # engine code: no Sources/*App in the repo
    assert "<untrusted>\nBe mean to edge cases.\n</untrusted>" in brief
    tester = action.parameters["_code_change"]["tester"]
    assert tester["session_url"] == "https://claude.ai/code/session_ABC123"
    assert isinstance(tester["at"], float)
    assert ("landed", "test") in updates


async def test_the_tester_looks_at_snapshots_on_an_app_repo(store, dirs, updates):
    repo, launch = dirs
    (launch / "Sources" / "FlareApp").mkdir(parents=True)
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    await _tester(runner, launch).run(run_ref, workspace_id=WS)

    brief = _briefs(runner)[1]
    assert "snapshots" in brief and "gh run download" in brief
    assert "docs/design.md" in brief and "dark" in brief and "empty state" in brief


async def test_the_tester_is_off_by_default(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    d = _tester(runner, launch)  # a tester seat is there; the env flag is not
    d.tester = False
    await d.run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1
    assert "tester" not in (await store.get_action(run_ref)).parameters["_code_change"]
    assert ("landed", "test") not in updates


async def test_no_tester_seat_launches_no_tester(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    await _tester(runner, launch, seat=None).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1
    assert "tester" not in (await store.get_action(run_ref)).parameters["_code_change"]


async def test_a_run_gets_one_tester_at_most(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    action = await store.get_action(run_ref)
    params = dict(action.parameters)
    params["_code_change"] = {**params["_code_change"], "tester": {"session_url": "x", "at": 1.0}}
    await store.update_parameters(run_ref, params)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    await _tester(runner, launch).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1
    assert (await store.get_action(run_ref)).parameters["_code_change"]["tester"]["at"] == 1.0


async def test_a_failed_tester_launch_is_recorded_and_the_land_stands(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=[SESSION, "Error: rate limited"], pr=MERGED)
    runner.run_ref = run_ref

    await _tester(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    assert "no cloud session started" in cc["tester"]["error"]
    assert "session_url" not in cc["tester"] and "headless_error" not in cc


def test_wire_cloud_reads_the_tester_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", None)
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR", str(tmp_path))
    monkeypatch.delenv("POCKETPAW_FACTORY_CLOUD_TESTER", raising=False)
    assert cloud_station.wire_cloud() is True
    assert headless.resolve_headless_dispatcher().tester is False
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_TESTER", "1")
    assert cloud_station.wire_cloud() is True
    assert headless.resolve_headless_dispatcher().tester is True


# -- screenshots: the PR head's ``snapshots`` artifact, kept on the run ---------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
HEAD = "abc1234def5678"
LANDED = {**_open(HEAD), "state": "MERGED", "mergeCommit": {"oid": "abc123"}}


@pytest.fixture
def shots(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "shots"
    monkeypatch.setenv("POCKETPAW_FACTORY_SCREENSHOT_DIR", str(root))
    return root


async def test_a_landed_run_keeps_its_snapshot_pngs(store, dirs, shots):
    import hashlib

    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(
        launch_out=SESSION,
        pr=LANDED,
        snapshots={
            "flare-empty-light.png": PNG + b"L",
            "dark/flare empty (dark).png": PNG + b"D",
            "notes.txt": b"not an image",
            "fake.png": b"<html>not a png</html>",
            ".hidden.png": PNG,
        },
    )
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    kept = action.parameters["_code_change"]["screenshots"]
    assert [k["name"] for k in kept] == ["flare-empty-light.png", "flare_empty__dark_.png"]
    assert kept[0] == {
        "name": "flare-empty-light.png",
        "bytes": len(PNG) + 1,
        "sha": hashlib.sha256(PNG + b"L").hexdigest(),
    }
    assert sorted(p.name for p in (shots / run_ref).iterdir()) == [k["name"] for k in kept]
    assert (shots / run_ref / "flare_empty__dark_.png").read_bytes() == PNG + b"D"
    api = [a[2] for a, _ in runner.calls if a[:2] == ["gh", "api"]]
    assert api[0] == f"repos/{SLUG}/actions/runs?head_sha={HEAD}"
    (download,) = [a for a, _ in runner.calls if a[:3] == ["gh", "run", "download"]]
    assert download[3:8] == ["502", "-R", SLUG, "-n", "snapshots"]
    assert not Path(download[-1]).exists()  # the temp download dir is gone


async def test_screenshots_are_capped_by_count_and_bytes(store, dirs, shots, monkeypatch):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    many = {f"s{i:02d}.png": PNG for i in range(45)}
    runner = FakeRunner(launch_out=SESSION, pr=LANDED, snapshots=many)
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)
    kept = (await store.get_action(run_ref)).parameters["_code_change"]["screenshots"]
    assert len(kept) == 40 and len(list((shots / run_ref).iterdir())) == 40

    run_ref = await _queue(store, repo)
    runner.run_ref = run_ref
    monkeypatch.setattr(cloud_station, "_SHOT_BYTES", len(PNG) * 3)
    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)
    kept = (await store.get_action(run_ref)).parameters["_code_change"]["screenshots"]
    assert len(kept) == 3


async def test_screenshots_are_fetched_once_the_pr_is_known(store, dirs, shots):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=_open(HEAD), snapshots={"a.png": PNG})
    runner.run_ref = run_ref

    await _dispatcher(runner, launch, timeout_seconds=0).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert [k["name"] for k in cc["screenshots"]] == ["a.png"]
    assert "no PR" not in cc["headless_error"]  # it timed out on the open PR


async def test_no_snapshots_artifact_and_a_failed_download_never_fail_the_land(store, dirs, shots):
    repo, launch = dirs
    for runner in (
        FakeRunner(launch_out=SESSION, pr=LANDED),
        FakeRunner(launch_out=SESSION, pr=LANDED, snapshots={"a.png": PNG}, download_code=1),
    ):
        run_ref = await _queue(store, repo)
        runner.run_ref = run_ref
        await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)
        action = await store.get_action(run_ref)
        assert action.status == ActionStatus.EXECUTED
        assert "screenshots" not in action.parameters["_code_change"]


async def test_the_app_tester_brief_names_the_snapshots_run(store, dirs, shots, updates):
    repo, launch = dirs
    (launch / "Sources" / "FlareApp").mkdir(parents=True)
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=LANDED, snapshots={"a.png": PNG})
    runner.run_ref = run_ref

    await _tester(runner, launch).run(run_ref, workspace_id=WS)

    assert f"gh run download 502 -R {SLUG} -n snapshots" in _briefs(runner)[1]


def _pty_child(script: str) -> list[str]:
    return [sys.executable, cloud_station.PTY_RUN, sys.executable, "-c", script]


async def test_pty_run_streams_output_before_a_timeout(tmp_path):
    seen: list[str] = []

    async def on_line(line: str) -> None:
        seen.append(line)

    child = "import time; print('cloud: starting', flush=True); time.sleep(60)"
    code, _, err = await ds.run_subprocess(
        _pty_child(child), cwd=tmp_path, timeout=3, on_line=on_line
    )

    assert code == -1 and "timed out" in err
    assert any("cloud: starting" in line for line in seen)


async def test_pty_run_fails_fast_at_the_folder_trust_prompt(tmp_path):
    # The prompt waits for input with no trailing newline, its words split by
    # cursor moves, as the CLI's TUI renders it.
    child = (
        "import sys, time\n"
        "sys.stdout.write('Quick safety check: Is this a project you created or one you "
        "trust?\\r\\n\\x1b[1m> 1. Yes,\\x1b[1CI trust\\x1b[1Cthis folder\\x1b[0m')\n"
        "sys.stdout.flush(); time.sleep(60)"
    )
    started = time.monotonic()
    code, out, _ = await ds.run_subprocess(_pty_child(child), cwd=tmp_path, timeout=30)

    assert code not in (0, -1)  # pty_run bailed on its own, not via the timeout
    assert time.monotonic() - started < 20
    assert "Quick safety check" in out


# -- resume after a restart -------------------------------------------------

CLOUD_URL = "https://claude.ai/code/session_ABC123"


async def _launched(store: InstinctStore, repo: Path, **cloud) -> str:
    """A queued run as ``run()`` leaves it once launched, its tracker gone."""
    run_ref = await _queue(store, repo)
    await cloud_station._patch(
        store, run_ref, headless_state="cloud", cloud={"session_url": CLOUD_URL, **cloud}
    )
    return run_ref


def _resumer(runner: FakeRunner, launch: Path, **kw) -> CloudTaskDispatcher:
    async def workspaces() -> list[str]:
        return [WS]

    return _dispatcher(runner, launch, workspaces_for=workspaces, **kw)


async def test_resume_tracks_a_launched_run_and_lands_it(store, dirs):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time())
    runner = FakeRunner(launch_out=SESSION, pr=[_open("h1"), MERGED])
    runner.run_ref = run_ref

    assert await _resumer(runner, launch).resume_all() == 1

    assert not _briefs(runner)  # the worker exists: nothing is launched
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    assert cc["pr_url"] == PR_URL and cc["cloud"]["pr"] == 9
    assert "headless_state" not in cc


async def test_resume_lands_a_pr_that_merged_while_the_backend_was_down(store, dirs):
    repo, launch = dirs
    run_ref = await _launched(store, repo)  # no launched_at: an older run
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    assert await _resumer(runner, launch).resume_all() == 1

    views = [a for a, _ in runner.calls if a[:3] == ["gh", "pr", "view"]]
    assert len(views) == 1  # the first poll saw MERGED and landed it
    assert not _briefs(runner)
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.approved_by == "system:fast-lane"


async def test_resume_skips_runs_without_the_cloud_mark(store, dirs):
    repo, launch = dirs
    await _queue(store, repo)  # queued, never launched
    failed = await _queue(store, repo)  # a tracker that gave up keeps only its session
    await cloud_station._patch(
        store, failed, cloud={"session_url": CLOUD_URL}, headless_error="TRACK: no PR"
    )
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)

    assert await _resumer(runner, launch).resume_all() == 0

    assert runner.calls == []
    assert (await store.get_action(failed)).status == ActionStatus.PENDING


async def test_resume_survives_a_store_error(store, dirs, monkeypatch):
    repo, launch = dirs
    run_ref = await _launched(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    def get_store(*, workspace_id=None):
        if workspace_id == "broken":
            raise OSError("disk I/O error")
        return store

    async def workspaces() -> list[str]:
        return ["broken", WS]

    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", get_store)
    assert await _dispatcher(runner, launch, workspaces_for=workspaces).resume_all() == 1
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED

    async def no_workspaces() -> list[str]:
        raise RuntimeError("mongo is down")

    assert await _dispatcher(runner, launch, workspaces_for=no_workspaces).resume_all() == 0


async def test_resume_counts_the_timeout_from_the_launch(store, dirs):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time() - 5 * 3600)
    runner = FakeRunner(launch_out=SESSION, pr=None)
    runner.run_ref = run_ref

    await _resumer(runner, launch).resume_all()  # the 4 h timeout already ran out

    assert sum(a[:3] == ["gh", "pr", "list"] for a, _ in runner.calls) == 1
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert "no PR" in cc["headless_error"] and cc["station_pending"] is True


async def test_resume_does_not_double_a_fixer_on_the_same_head(store, dirs, stages):
    repo, launch = dirs
    fix = {"kind": "ci", "at": time.time(), "head": "h1", "session_url": CLOUD_URL}
    run_ref = await _launched(store, repo, launched_at=time.time())
    await cloud_station._patch(store, run_ref, fixes=[fix])
    red = _open("h1", red=True)  # the fixer is still working on h1
    runner = FakeRunner(launch_out=SESSION, pr=[red] * 6 + [MERGED])
    runner.run_ref = run_ref

    await _resumer(runner, launch, poll_seconds=60, fix_grace_seconds=100).resume_all()

    assert not _briefs(runner)
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.parameters["_code_change"]["fixes"] == [fix]


# -- lane nudge: a green PR the lane left open gets its workflow dispatched ----

LANE = [{"name": "fast-lane"}]
NUDGE = ["gh", "workflow", "run", "Fast Lane", "-R", SLUG]


@pytest.fixture(autouse=True)
def _fresh_lane_gate(monkeypatch) -> None:
    monkeypatch.setattr(cloud_station, "_LANE_NUDGED", {})


def _ready(**kw) -> dict:
    return {**_open(**kw), "labels": LANE}


def _nudger(runner: FakeRunner, launch: Path, **kw) -> CloudTaskDispatcher:
    # Polls every 60 s, the default 300 s nudge interval, and no fixer in reach.
    d = _dispatcher(runner, launch, poll_seconds=60, fix_grace_seconds=10**9, **kw)
    runner.clock = d.clock
    return d


async def test_a_green_pr_left_open_gets_one_lane_nudge(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    # Green at t=0..300, merged at 360: due at 300, not before.
    runner = FakeRunner(launch_out=SESSION, pr=[_ready()] * 6 + [MERGED])
    runner.run_ref = run_ref

    await _nudger(runner, launch).run(run_ref, workspace_id=WS)

    assert runner.dispatched == [300.0]
    assert [a for a, _ in runner.calls if a[:3] == ["gh", "workflow", "run"]] == [NUDGE]
    view = next(a for a, _ in runner.calls if a[:3] == ["gh", "pr", "view"])
    assert "labels" in view[view.index("--json") + 1].split(",")
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    (at,) = action.parameters["_code_change"]["lane_nudges"]
    assert at <= time.time()


async def test_two_prs_on_one_repo_share_one_nudge_per_interval(store, dirs):
    repo, launch = dirs
    first, second = await _queue(store, repo), await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=[_ready()] * 14 + [MERGED])
    runner.run_ref, runner.also = first, [second]
    d = _nudger(runner, launch)
    inner = d.sleep

    async def sleep(seconds: float) -> None:  # yield, so the two trackers interleave
        await inner(seconds)
        await asyncio.sleep(0)

    d.sleep = sleep

    await asyncio.gather(d.run(first, workspace_id=WS), d.run(second, workspace_id=WS))

    times = runner.dispatched
    assert times and all(b - a >= 300 for a, b in zip(times, times[1:]))
    blobs = [(await store.get_action(r)).parameters["_code_change"] for r in (first, second)]
    assert sum(len(b.get("lane_nudges") or []) for b in blobs) == len(times)
    assert all(b["cloud"]["pr"] in {9, 10} for b in blobs)


@pytest.mark.parametrize(
    "pr",
    [
        _ready(red=True),
        _ready(mergeable="CONFLICTING"),
        _ready(mergeable="UNKNOWN"),
        {**_ready(), "statusCheckRollup": [{**GREEN_CHECK, "status": "IN_PROGRESS"}]},
        _open(),  # green, but not labelled for the lane
    ],
    ids=["red", "conflicting", "unknown", "pending", "unlabelled"],
)
async def test_a_pr_the_lane_should_not_merge_gets_no_nudge(store, dirs, pr):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=[pr] * 12 + [MERGED])
    runner.run_ref = run_ref

    await _nudger(runner, launch).run(run_ref, workspace_id=WS)

    assert runner.dispatched == []
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert "lane_nudges" not in cc


async def test_a_failed_lane_nudge_never_stops_the_tracker(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=[_ready()] * 6 + [MERGED], workflow_code=1)
    runner.run_ref = run_ref

    await _nudger(runner, launch).run(run_ref, workspace_id=WS)

    assert runner.dispatched == [300.0]
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    assert "lane_nudges" not in cc and "headless_error" not in cc


def test_wire_cloud_reads_the_lane_nudge_env(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", None)
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR", str(tmp_path))
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LANE_NUDGE", "120")
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LANE_WORKFLOW", "Lane")
    assert cloud_station.wire_cloud() is True
    d = headless.resolve_headless_dispatcher()
    assert (d.lane_nudge_seconds, d.lane_workflow) == (120.0, "Lane")


# -- silent workers: no PR in time -> one fresh worker, then the run is released --


async def _mandate_world(store: InstinctStore, repo: Path) -> tuple[str, str, str]:
    """A continuous mandate with two open sightings and a done shift whose plan's
    task 1 cites the first, plus that task's queued run: (mandate, run, cited).
    Its in-flight cap is 1, so the run in flight is what holds the line."""
    created = await mandate_service.create_mandate(
        WS,
        "u1",
        {
            "name": "m",
            "surface": {"repo_id": str(repo)},
            "charter": {"goal": "g", "cadence": "continuous", "budget": {"max_in_flight": 1}},
        },
    )
    mid = created["mandate"]["id"]
    for text in ("text shapes are missing", "the export button is grey"):
        await mandate_service.file_feedback(
            WS, "u1", mid, {"text": text, "severity": 3, "source": "support"}
        )
    cited = str((await SightingDoc.find(SightingDoc.mandate_id == mid).to_list())[0].id)
    plan = await store.propose(
        WS,
        "Shift 1 plan",
        "",
        "",
        ActionTrigger(type="agent", source="belt:mandate", reason="test"),
        parameters={
            "_belt_plan": {
                "shift_no": 1,
                "plan": {"tasks": [{"title": "G41", "evidence_refs": [cited]}]},
            }
        },
    )
    shift = ShiftDoc(workspace=WS, mandate_id=mid, no=1, state="done", plan_action_id=plan.id)
    await shift.insert()
    await shift.set({ShiftDoc.createdAt: datetime.now(UTC) - timedelta(minutes=10)})
    return mid, await _queue(store, repo, mandate_id=mid, plan_action_id=plan.id), cited


async def _due(mandate_id: str) -> bool:
    """The continuous scheduler's verdict: may the mandate's next shift fire?"""
    due = await mandate_service.list_cadence_due(datetime.now(UTC))
    return mandate_id in {r["mandate_id"] for r in due}


def _sampling(d: CloudTaskDispatcher, mandate_id: str, *, every: bool) -> list[bool]:
    """Record the mandate's due verdict at each poll's sleep (or the first only)."""
    seen: list[bool] = []
    inner = d.sleep

    async def sleep(seconds: float) -> None:
        if every or not seen:
            seen.append(await _due(mandate_id))
        await inner(seconds)

    d.sleep = sleep
    return seen


@pytest.mark.usefixtures("any_repo_root")
async def test_a_silent_worker_is_relaunched_once_then_released(store, dirs, mongo_db, updates):
    repo, launch = dirs
    mid, run_ref, cited = await _mandate_world(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=None)
    runner.run_ref = run_ref
    d = _dispatcher(runner, launch, poll_seconds=600)
    held = _sampling(d, mid, every=False)

    await d.run(run_ref, workspace_id=WS)

    briefs = _briefs(runner)
    assert len(briefs) == 2  # the worker, then ONE fresh worker an hour later
    assert "produced no pull request" not in briefs[0]
    assert "produced no pull request" in briefs[1] and f"{MARKER} {run_ref}" in briefs[1]
    # The issue was read before the relaunch; its only comment predates the launch.
    view = ["gh", "issue", "view", "93", "-R", SLUG, "--json", "comments"]
    assert view in [a for a, _ in runner.calls]
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason == "no PR from 2 worker(s) in 120m"
    cc = action.parameters["_code_change"]
    (again,) = cc["relaunches"]
    assert again["session_url"] == CLOUD_URL and again["at"] <= time.time()
    assert "headless_state" not in cc and "headless_error" not in cc
    assert ("running", "cloud") in updates and ("rejected", "done") in updates
    # The line moved: held while the worker was out, due once released, and the
    # task's sighting still open (only a landed task resolves one).
    assert held == [False]
    assert await _due(mid)
    backlog = await mandate_service._backlog(WS, mid, (await belt_service.list_runs(WS))["runs"])
    assert cited in {o["id"] for o in backlog["open"]}


async def test_a_worker_that_says_impossible_is_released_without_a_relaunch(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    soon = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    said = {"createdAt": soon, "body": "\nImpossible as written: no glyph API.\n\nDetails."}
    runner = FakeRunner(launch_out=SESSION, pr=None, issue_comments=[*OLD_COMMENT, said])
    runner.run_ref = run_ref

    await _dispatcher(runner, launch, poll_seconds=600).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1  # taken at its word: no relaunch
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason == "Impossible as written: no glyph API."
    calls = [a for a, _ in runner.calls]
    assert ["gh", "issue", "edit", "93", "-R", SLUG, "--add-label", "needs-human"] in calls
    assert "relaunches" not in action.parameters["_code_change"]
    assert ("rejected", "done") in updates


async def test_the_relaunch_count_survives_a_resume(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time() - 2 * 3600)
    again = {"session_url": "https://claude.ai/code/session_AGAIN", "at": time.time() - 1800}
    await cloud_station._patch(store, run_ref, relaunches=[again])
    runner = FakeRunner(launch_out=SESSION, pr=None)
    runner.run_ref = run_ref

    assert await _resumer(runner, launch).resume_all() == 1

    assert not _briefs(runner)  # the one relaunch was spent before the restart
    # The no-PR clock counts from that relaunch: 30 more minutes of 60 s polls.
    assert sum(a[:3] == ["gh", "pr", "list"] for a, _ in runner.calls) == 31
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason.startswith("no PR from 2 worker(s) in ")
    assert action.parameters["_code_change"]["relaunches"] == [again]


def test_wire_cloud_reads_the_silent_worker_env(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", None)
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR", str(tmp_path))
    monkeypatch.delenv("POCKETPAW_FACTORY_CLOUD_NO_PR_TIMEOUT", raising=False)
    monkeypatch.delenv("POCKETPAW_FACTORY_CLOUD_RELAUNCHES", raising=False)
    assert cloud_station.wire_cloud() is True
    d = headless.resolve_headless_dispatcher()
    assert (d.no_pr_seconds, d.relaunch_attempts) == (3600.0, 1)
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_NO_PR_TIMEOUT", "900")
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_RELAUNCHES", "2")
    assert cloud_station.wire_cloud() is True
    d = headless.resolve_headless_dispatcher()
    assert (d.no_pr_seconds, d.relaunch_attempts) == (900.0, 2)


# -- adoption: one PR per issue, whichever run opened it -------------------------

# An open PR from an earlier run that closes the task's issue (#93).
CLOSING = {"number": 160, "body": "Text shapes.\n\nfixes #93\n\nBelt-Run: an-older-run"}
MERGED_160 = {**MERGED, "url": f"https://github.com/{SLUG}/pull/160"}


async def test_a_run_adopts_an_open_pr_that_closes_its_issue(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    rows = [
        {"number": 150, "body": "Closes #930", "state": "OPEN"},
        {"number": 151, "body": "Closes #93", "state": "CLOSED"},
        {**CLOSING, "state": "OPEN"},
    ]
    runner = FakeRunner(launch_out=SESSION, pr=MERGED_160, closing=[rows])
    runner.run_ref = run_ref

    await _dispatcher(runner, launch, poll_seconds=600).run(run_ref, workspace_id=WS)

    (brief,) = _briefs(runner)  # adopted at the first poll: no relaunch
    assert f"If an open pull request on {SLUG} already closes #93, don't open another" in brief
    views = [a[3] for a, _ in runner.calls if a[:3] == ["gh", "pr", "view"]]
    assert views == ["160"]
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    assert cc["adopted_pr"] == 160 and cc["cloud"]["pr"] == 160
    assert cc["pr_url"] == MERGED_160["url"]
    assert ("landed", "done") in updates


async def test_a_silent_run_adopts_a_closing_pr_instead_of_being_released(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    # No PR for two hours (one relaunch at the hour); the closing PR shows up at
    # the poll that would release the run.
    closing = [[]] * 12 + [[{**CLOSING, "state": "OPEN"}]]
    runner = FakeRunner(launch_out=SESSION, pr=MERGED_160, closing=closing)
    runner.run_ref = run_ref

    await _dispatcher(runner, launch, poll_seconds=600).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 2
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED, action.rejected_reason
    assert action.parameters["_code_change"]["adopted_pr"] == 160
    assert ("rejected", "done") not in updates


async def test_resume_keeps_an_adopted_pr_that_merged_while_down(store, dirs):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time())
    await cloud_station._patch(store, run_ref, adopted_pr=160)
    # Merged, so no longer an OPEN PR that closes #93.
    runner = FakeRunner(
        launch_out=SESSION, pr=MERGED_160, closing=[[{**CLOSING, "state": "MERGED"}]]
    )
    runner.run_ref = run_ref

    assert await _resumer(runner, launch).resume_all() == 1

    assert not _briefs(runner)
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.parameters["_code_change"]["pr_url"] == MERGED_160["url"]


# -- the tester's PR: tracked like the worker's, never holding the line ---------

TESTER_PR = f"https://github.com/{SLUG}/pull/20"


def _tester_pr(**kw) -> dict:
    return {**_ready(**kw), "url": TESTER_PR, "headRefName": "claude/fuzz-text"}


async def test_the_tester_pr_is_nudged_fixed_and_recorded_merged(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    green, red = _tester_pr(head="abcdef1"), _tester_pr(head="t2", red=True)
    runner = FakeRunner(
        launch_out=SESSION,
        pr=MERGED,
        tester_pr=[green] * 6 + [red] * 3 + [{**green, "state": "MERGED"}],
    )
    runner.run_ref = run_ref
    d = _tester(runner, launch, poll_seconds=60, fix_grace_seconds=100)
    runner.clock = d.clock

    await d.run(run_ref, workspace_id=WS)

    assert runner.dispatched == [300.0]  # green and quiet for the nudge interval
    briefs = _briefs(runner)
    assert len(briefs) == 3  # the worker, the tester, one fixer on the tester's PR
    fix = briefs[2]
    assert "#20" in fix and "`claude/fuzz-text`" in fix and "build (macos-14)" in fix
    assert f"{TEST_MARKER} {run_ref}" in fix and f"{MARKER} {run_ref}" not in fix
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    tester = cc["tester"]
    assert tester["pr_url"] == TESTER_PR and tester["state"] == "merged"
    (entry,) = tester["fixes"]
    assert entry["kind"] == "ci" and entry["head"] == "t2"
    assert "fixes" not in cc  # its own cap: the run's fixers are untouched
    assert ("running", "fix") not in updates  # a landed run never reads as running
    # A tester PR's screenshots would replace the run's: none are fetched for it.
    assert not any("head_sha=abcdef1" in " ".join(a) for a, _ in runner.calls)


async def test_a_tester_with_no_pr_is_done_after_the_no_pr_timeout(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    await _tester(runner, launch, poll_seconds=600).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 2  # a tester is never relaunched
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.parameters["_code_change"]["tester"]["state"] == "none"
    assert not any(a[:3] == ["gh", "issue", "view"] for a, _ in runner.calls)


@pytest.mark.usefixtures("any_repo_root")
async def test_tester_tracking_never_holds_the_mandate_due_check(store, dirs, mongo_db, updates):
    repo, launch = dirs
    mid, run_ref, _ = await _mandate_world(store, repo)
    open_pr = _tester_pr()
    runner = FakeRunner(
        launch_out=SESSION,
        pr=[_open("h1"), MERGED],
        tester_pr=[open_pr] * 3 + [{**open_pr, "state": "MERGED"}],
    )
    runner.run_ref = run_ref
    d = _tester(runner, launch, mandate=mid)
    due = _sampling(d, mid, every=True)

    await d.run(run_ref, workspace_id=WS)

    # The worker's PR open holds the line; the tester's PR open does not.
    assert due == [False, True, True, True]
    tester = (await store.get_action(run_ref)).parameters["_code_change"]["tester"]
    assert tester["state"] == "merged"


async def test_resume_tracks_a_landed_runs_open_tester_pr(store, dirs, updates):
    repo, launch = dirs
    refs = []
    for tester in (
        {"session_url": CLOUD_URL, "at": time.time() - 600},  # unknown: no PR seen yet
        {"session_url": CLOUD_URL, "at": time.time() - 600, "state": "merged"},
        {"session_url": CLOUD_URL, "at": time.time() - 5 * 3600, "state": "open"},  # too old
    ):
        run_ref = await _queue(store, repo)
        await store.approve(run_ref)
        await store.mark_executed(run_ref, "landed")
        await cloud_station._patch(store, run_ref, station_pending=False, tester=tester)
        refs.append(run_ref)
    runner = FakeRunner(launch_out=SESSION, pr=None, tester_pr={**_tester_pr(), "state": "MERGED"})
    runner.run_ref = refs[0]
    d = _resumer(runner, launch)

    assert await d.resume_all() == 0  # the tester off: its PRs are left alone
    d.tester = True
    assert await d.resume_all() == 1

    assert not _briefs(runner)
    tester = (await store.get_action(refs[0])).parameters["_code_change"]["tester"]
    assert tester["state"] == "merged" and tester["pr_url"] == TESTER_PR
    assert (await store.get_action(refs[0])).status == ActionStatus.EXECUTED
