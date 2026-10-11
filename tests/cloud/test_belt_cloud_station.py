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
# session URL, a launch clone on another origin, and a tracker timeout each leave
# the run queued with ``headless_error`` (and the origin mismatch never launches);
# the seated dev's instructions ride the brief, fenced; the PR's diff goes to
# Clef's shadow scorer once the PR is known;
# ``wire_from_env`` wires the cloud dispatcher only with a launch clone.

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import cloud_station, headless  # noqa: E402
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

    def __init__(self, *, launch_out: str, launch_origin: str = SLUG, pr: dict | None = None):
        self.calls: list[tuple[list[str], Path]] = []
        self.launch_out = launch_out
        self.launch_origin = launch_origin
        self.pr = pr
        self.run_ref = ""

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        self.calls.append((list(argv), cwd))
        if "get-url" in argv:
            slug = SLUG if cwd.name == "repo" else self.launch_origin
            return 0, f"https://github.com/{slug}.git\n", ""
        if "--cloud" in argv:
            return 0, self.launch_out, ""
        if argv[:3] == ["gh", "pr", "list"]:
            if self.pr is None:
                return 0, "[]", ""
            body = f"Closes #93\n\n{MARKER} {self.run_ref}\n"
            return 0, json.dumps([{"number": 7, "body": "other"}, {"number": 9, "body": body}]), ""
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps(self.pr), ""
        if argv[:3] == ["gh", "pr", "diff"]:
            return 0, DIFF, ""
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


async def test_launch_clone_on_another_origin_never_launches(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FakeRunner(launch_out=SESSION, launch_origin="someone/else")

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert "origin is not" in cc["headless_error"]
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
