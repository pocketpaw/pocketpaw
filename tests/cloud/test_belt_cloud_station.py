# tests/cloud/test_belt_cloud_station.py — the CLOUD develop station.
#
# ``CloudTaskDispatcher`` launches a Claude Code cloud session for a queued run
# and tracks the session's PR through gh. Every subprocess goes through the
# injected ``Runner``, so these tests script git, the pty launcher and gh with a
# fake: nothing real is launched and no network is touched.
#
# Asserted: a merged PR lands the run (approved by system:fast-lane, executed,
# pr_url + merge sha + head branch on the blob) and the launch ran from the
# launch clone detached at origin/<base> with a brief that names the issue, the
# Belt-Run marker, the label and the charter check; a lane refusal attaches the
# PR's diff and leaves the run PENDING at the per-diff gate; a launch with no
# session URL and a tracker timeout each leave the run queued with
# ``headless_error``; a launch clone on another repo is re-pointed (``set-url``
# before the fetch, for the worker and the fixer), one on the same repo is not,
# and a malformed bound-repo slug is refused before any git runs in the clone;
# the seated dev's instructions ride the brief, fenced; the PR's diff goes to
# Clef's shadow scorer once the PR is known; a launch that times out keeps
# the CLI output it streamed; a launch clone the CLI does not trust fails with
# a clear error. A stuck PR gets a fixer session after the grace period: a
# conflicting one a rebase brief, a red one a brief carrying the failed job's
# last 40 log lines; a PR that goes green within grace gets none; the attempts
# cap escalates like a refusal; a fixer that fails to launch is recorded on the
# blob's ``fixes`` and tracking goes on. A landed run carries the PR's diff
# (Clef's copy when the head has not moved, else fetched again) and fetches the
# bound repo, whose failure never fails the land. ``pty_run.py`` itself runs for real:
# it streams the child's output as it lands and exits on the folder-trust
# prompt instead of waiting; ``wire_from_env`` wires the cloud dispatcher only
# with a launch clone.

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import cloud_station, headless  # noqa: E402
from pocketpaw_ee.cloud.belt import develop_station as ds  # noqa: E402
from pocketpaw_ee.cloud.belt.cloud_station import MARKER, CloudTaskDispatcher  # noqa: E402
from pocketpaw_ee.cloud.belt.develop_station import ClaudeCodeDevelop  # noqa: E402
from pocketpaw_ee.cloud.mandates.executor import StationTaskDispatcher  # noqa: E402

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
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


async def _queue(store: InstinctStore, repo: Path) -> str:
    import pocketpaw_ee.cloud.mandates.executor as ex

    async def fake_repo(workspace_id: str, mandate_id: str) -> str:
        return str(repo)

    async def fake_base(repo: str) -> str:
        return "main"

    orig = ex._repo_for_mandate
    ex._repo_for_mandate = fake_repo  # type: ignore[assignment]
    from pocketpaw_ee.cloud.belt import service as belt_service

    orig_base = belt_service.default_base
    belt_service.default_base = fake_base  # type: ignore[assignment]
    try:
        return await StationTaskDispatcher().dispatch(
            workspace_id=WS,
            mandate_id="m1",
            shift_no=1,
            plan_action_id="plan-1",
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


class FakeRunner:
    """Scripted git / launcher / gh. ``pr`` is what ``gh pr view`` returns."""

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
    ):
        self.calls: list[tuple[list[str], Path]] = []
        # A list is consumed one entry per launch / per ``gh pr view``, holding the last.
        self.launch_outs = [launch_out] if isinstance(launch_out, str) else list(launch_out)
        self.launch_code = launch_code
        self.launch_lines = launch_lines  # streamed through on_line before returning
        self.launch_origin = launch_origin
        self.repo_origin = repo_origin
        self.repo_fetch_code = repo_fetch_code  # ``git fetch`` in the bound repo
        self.prs = pr if isinstance(pr, list) else [pr]
        self.run_ref = ""

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
            if self.prs[0] is None:
                return 0, "[]", ""
            body = f"Closes #93\n\n{MARKER} {self.run_ref}\n"
            return 0, json.dumps([{"number": 7, "body": "other"}, {"number": 9, "body": body}]), ""
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps(self._next(self.prs)), ""
        if argv[:3] == ["gh", "run", "view"]:
            return 0, "".join(f"build\tstep\tL{i:03d}\n" for i in range(1, 51)), ""
        if argv[:3] == ["gh", "pr", "diff"]:
            return 0, DIFF, ""
        if "fetch" in argv and cwd.name == "repo":
            return self.repo_fetch_code, "", "fatal: unable to access origin"
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
    assert cc["cloud"] == {"session_url": "https://claude.ai/code/session_ABC123", "pr": 9}
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
