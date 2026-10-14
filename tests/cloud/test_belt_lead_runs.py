# tests/cloud/test_belt_lead_runs.py — the cloud station's feature LEAD runs,
# plus the tracker robustness every cloud run gets (batch-1 lessons 42 and 45).
#
# A run whose blob carries ``recipe == "feature-lead"`` is a lead: ONE cloud
# session per feature issue that opens many ``Belt-Lead:`` PRs and reports on
# its ``lead/<feature>`` branch. Every subprocess goes through the injected
# runner, so git, the pty launcher, ``claude -p --cloud`` and gh are scripted.
#
# BRIEF: loaded from ``docs/factory/lead.md`` in the launch clone, then the
# feature issue, the packet path, the run id, the status branch and the
# contract's brief rules appended; no playbook file, no packet or an unsafe
# feature name fails the launch before any session starts.
# TRACK: every PR whose body has ``Belt-Lead: <run id>`` is kept on the run
# (open and merged), never reviewed (no ``belt/review``) and never fixed.
# DONE: a merged ``feature-done`` PR, every dev CI run of its merge commit
# green, and a ``Belt-Contact-Sheet:`` comment on the issue since the launch;
# any one missing keeps the run tracking.
# STALL: 30 min with no lead-branch commit, PR update or issue comment: a
# ``claude -p --cloud <session>`` nudge, then a relaunch with the resume brief,
# then ``needs-human`` on the issue and the run released. Activity holds it off.
# TIMEOUT: 8 h from the launch, and no 1 h no-PR rule.
# RESUME: a launched lead is tracked again as a lead, never relaunched.
# LESSON 42: a transient gh failure is retried with backoff; a permanent one
# fails at once; a transient one past the retries ends the tracker.
# LESSON 45: on startup a run whose tracker died on a TRACK error (inside its
# timeout) is tracked again with the error cleared; an errored run is never
# counted in flight.

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import cloud_station  # noqa: E402
from pocketpaw_ee.cloud.belt.cloud_station import CloudTaskDispatcher  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import Budget  # noqa: E402

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from tests.cloud import test_belt_cloud_station as base  # noqa: E402
from tests.cloud.test_belt_cloud_station import (  # noqa: E402
    CLOUD_URL,
    MERGED,
    SESSION,
    SLUG,
    WS,
    FakeRunner,
    _dispatcher,
    _launched,
    _open,
    _queue,
)

store, dirs = base.store, base.dirs

PLAYBOOK = "# Feature lead playbook\nRun the stages in order: Plan, Swarm, Anatomy, Ship.\n"
PACKET = "docs/blueprints/flare/features/text-shapes.md"
FEATURE = "text-shapes"
HEAD_SHA = "f" * 40
MERGE_SHA = "a" * 40
SID = "session_ABC123"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def playbook(dirs) -> Path:
    _, launch = dirs
    (launch / "docs" / "factory").mkdir(parents=True)
    path = launch / "docs" / "factory" / "lead.md"
    path.write_text(PLAYBOOK)
    return path


async def _queue_lead(store, repo: Path, *, task: str | None = None) -> str:
    run_ref = await _queue(store, repo, base="dev")
    text = task if task is not None else f"Text shapes\n\nFeature issue #93. Packet: {PACKET}"
    await cloud_station._patch(store, run_ref, recipe="feature-lead", task=text)
    return run_ref


def _lead_pr(number: int, run_ref: str, *, state="OPEN", done=False, at: float = 0.0) -> dict:
    labels = [{"name": "fast-lane"}] + ([{"name": "feature-done"}] if done else [])
    return {
        "number": number,
        "url": f"https://github.com/{SLUG}/pull/{number}",
        "state": state,
        "body": f"Swarm slice.\n\nBelt-Lead: {run_ref}\nFeature: {FEATURE}\n",
        "labels": labels,
        "headRefName": f"lead/{FEATURE}-{number}",
        "updatedAt": _iso(at) if at else "2020-01-01T00:00:00Z",
        "mergeCommit": {"oid": MERGE_SHA} if state == "MERGED" else None,
    }


GREEN_RUN = {"status": "completed", "conclusion": "success", "event": "push", "headBranch": "dev"}


class LeadRunner:
    """Scripted git / launcher / ``claude -p --cloud`` / gh for a lead run. Each
    scripted list is consumed one entry per poll, holding its last. ``wall`` is
    the dispatcher's wall clock, so "now" activity can be stamped."""

    def __init__(
        self,
        *,
        prs: list[list[dict]] | None = None,
        comments: list[list[dict]] | None = None,
        commits: list[float | None] | None = None,
        ci: list[list[dict]] | None = None,
        issue_body: str = "",
        launches: int = 99,
    ):
        self.calls: list[tuple[list[str], Path]] = []
        self.prs = prs or [[]]
        self.comments = comments or [[]]
        self.commits = commits or [None]
        self.ci = ci or [[GREEN_RUN]]
        self.issue_body = issue_body
        self.launches_left = launches
        self.wall = time.time

    @staticmethod
    def _next(seq: list):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        self.calls.append((list(argv), cwd))
        if "get-url" in argv:
            return 0, f"https://github.com/{SLUG}.git\n", ""
        if "rev-parse" in argv and "HEAD" in argv and "--abbrev-ref" not in argv:
            return 0, f"{HEAD_SHA}\n", ""
        if "-p" in argv and "--cloud" in argv:
            return 0, "Sent.\n", ""
        if "--cloud" in argv:
            n = sum("--cloud" in a and "-p" not in a for a, _ in self.calls)
            return 0, f"Created cloud session: https://claude.ai/code/session_L{n}\r\n", ""
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(self._next(self.prs)), ""
        if argv[:3] == ["gh", "issue", "view"]:
            data = {"comments": self._next(self.comments), "title": "Text shapes"}
            return 0, json.dumps({**data, "body": self.issue_body}), ""
        if argv[:2] == ["gh", "api"] and "/commits/lead/" in argv[2]:
            at = self._next(self.commits)
            if at is None:
                return 1, "", "gh: Not Found (HTTP 404)"
            return 0, json.dumps({"commit": {"committer": {"date": _iso(at)}}}), ""
        if argv[:3] == ["gh", "run", "list"]:
            return 0, json.dumps(self._next(self.ci)), ""
        return 0, "", ""  # git fetch / checkout, gh label / issue edit


def _lead_dispatcher(runner: LeadRunner, launch: Path, **kw) -> CloudTaskDispatcher:
    async def charter(workspace_id: str, mandate_id: str) -> dict:
        return {"repo": "x", "charter": {"checks": ["scripts/ci"]}}

    start = time.time()
    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] > 30 * 24 * 3600:
            raise RuntimeError("the tracker never stopped polling")

    runner.wall = lambda: start + now[0]
    return CloudTaskDispatcher(
        launch_dir=str(launch),
        runner=runner,
        charter_for=charter,
        sleep=sleep,
        clock=lambda: now[0],
        wall=runner.wall,
        poll_seconds=60.0,
        **kw,
    )


def _launch_briefs(runner: LeadRunner) -> list[str]:
    return [a[-1] for a, _ in runner.calls if "--cloud" in a and "-p" not in a]


def _nudges(runner: LeadRunner) -> list[list[str]]:
    return [a for a, _ in runner.calls if "--cloud" in a and "-p" in a]


def _sheet(at: float) -> dict:
    return {"createdAt": _iso(at), "body": f"Belt-Contact-Sheet: all green\n{CLOUD_URL}"}


def _done_runner(run_ref: str, **kw) -> LeadRunner:
    """A lead whose feature is done at the first poll: a merged slice, a merged
    ``feature-done`` PR, green dev CI and the contact sheet."""
    future = time.time() + 3600  # after any launch the test makes
    prs = [
        _lead_pr(31, run_ref, state="MERGED"),
        _lead_pr(32, run_ref, state="MERGED", done=True),
        {"number": 7, "state": "OPEN", "body": "someone else's", "labels": []},
    ]
    return LeadRunner(prs=[prs], comments=[[_sheet(future)]], **kw)


# -- brief ------------------------------------------------------------------


async def test_lead_brief_is_the_playbook_plus_the_run_and_the_rules(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    (brief,) = _launch_briefs(runner)
    assert brief.startswith(PLAYBOOK.strip())  # the playbook is the file, not Python
    assert f"https://github.com/{SLUG}/issues/93" in brief
    assert PACKET in brief
    assert f"Belt-Lead: {run_ref}" in brief and f"Feature: {FEATURE}" in brief
    assert f"lead/{FEATURE}" in brief and f"docs/leads/{FEATURE}/RECORD.md" in brief
    assert "timeline.tsv" in brief
    assert HEAD_SHA in brief  # the base SHA step one verifies
    assert "Fast-Check: pass" in brief and "Head: <full sha>" in brief
    assert "`feature-done`" in brief and "Belt-Contact-Sheet:" in brief
    assert "2 clean rounds or 4 rounds total" in brief
    assert "first-use clarity" in brief
    assert "chat message" in brief and "supervisor" in brief
    assert "No attribution line anywhere" in brief
    assert "Belt-Run:" not in brief  # a lead's PRs carry Belt-Lead, never Belt-Run
    # Launched like every cloud run: under the pty, from the clone at origin/dev.
    launch_argv = next(a for a, _ in runner.calls if "--cloud" in a)
    assert launch_argv[1] == cloud_station.PTY_RUN


async def test_lead_git_identity_comes_from_the_dispatcher(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)

    await _lead_dispatcher(
        runner, launch, lead_git_name="Pat Lead", lead_git_email="pat@example.com"
    ).run(run_ref, workspace_id=WS)

    (brief,) = _launch_briefs(runner)
    assert 'user.name "Pat Lead"' in brief and 'user.email "pat@example.com"' in brief


async def test_lead_packet_is_read_from_the_issue_when_the_task_omits_it(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo, task="Text shapes\n\nFeature issue #93.")
    runner = _done_runner(run_ref, issue_body=f"Packet: [{PACKET}]({PACKET})\nDepends on: none")

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    (brief,) = _launch_briefs(runner)
    assert PACKET in brief
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_lead_without_a_playbook_never_launches(store, dirs):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert "docs/factory/lead.md" in cc["headless_error"]
    assert cc["station_pending"] is True
    assert not _launch_briefs(runner)


@pytest.mark.parametrize(
    "task",
    [
        "Text shapes\n\nNo issue named here.",
        "Text shapes\n\nFeature issue #93.",  # and the issue body names no packet
        "Text shapes #93\n\ndocs/blueprints/flare/features/-x.md",  # an unsafe feature name
    ],
)
async def test_lead_without_an_issue_or_a_packet_never_launches(store, dirs, playbook, task):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo, task=task)
    runner = _done_runner(run_ref)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["headless_error"].startswith("LAUNCH:")
    assert not _launch_briefs(runner)


# -- track + done -----------------------------------------------------------


async def test_done_lead_lands_with_every_pr_and_the_contact_sheet(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.approved_by == "system:fast-lane"
    cc = action.parameters["_code_change"]
    lead = cc["lead"]
    assert [p["number"] for p in lead["prs"]] == [31, 32]  # never someone else's #7
    assert lead["feature"] == FEATURE and lead["packet"] == PACKET and lead["issue"] == 93
    assert lead["contact_sheet"].startswith("Belt-Contact-Sheet:")
    assert lead["sessions"][0]["session_url"] == "https://claude.ai/code/session_L1"
    assert cc["pr_url"] == f"https://github.com/{SLUG}/pull/32"
    assert cc["commit_sha"] == MERGE_SHA
    assert "headless_state" not in cc and "tester" not in cc
    # The dev CI it read is the done PR's merge commit, on dev.
    ci = next(a for a, _ in runner.calls if a[:3] == ["gh", "run", "list"])
    assert MERGE_SHA in ci and "dev" in ci


async def test_lead_is_not_done_until_dev_ci_on_the_merge_is_green(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    pending = {**GREEN_RUN, "status": "in_progress", "conclusion": ""}
    red = {**GREEN_RUN, "conclusion": "failure"}
    runner.ci = [[], [pending, GREEN_RUN], [red], [GREEN_RUN, GREEN_RUN]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert sum(a[:3] == ["gh", "run", "list"] for a, _ in runner.calls) == 4
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_lead_is_not_done_without_a_new_contact_sheet(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    old = _sheet(time.time() - 86400)  # before the launch: another run's sheet
    said = {"createdAt": _iso(time.time() + 3600), "body": "Still on Anatomy round 2."}
    runner.comments = [[old], [old, said], [old, said, _sheet(time.time() + 3600)]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    views = [a for a, _ in runner.calls if a[:3] == ["gh", "issue", "view"]]
    assert len(views) == 3
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_lead_is_not_done_until_a_feature_done_pr_merges(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    slice_ = _lead_pr(31, run_ref, state="MERGED")
    runner.prs = [
        [slice_],
        [slice_, _lead_pr(32, run_ref, done=True)],  # labelled, still open
        [slice_, _lead_pr(32, run_ref, state="MERGED", done=True)],
    ]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert sum(a[:3] == ["gh", "pr", "list"] for a, _ in runner.calls) == 3
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["pr_url"].endswith("/pull/32")


async def test_lead_prs_get_no_review_and_no_fixer(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    runner.prs = [[_lead_pr(31, run_ref)]] * 3 + runner.prs

    await _lead_dispatcher(runner, launch, reviewer=True).run(run_ref, workspace_id=WS)

    assert not any("statuses" in " ".join(a) for a, _ in runner.calls)
    assert not any(a[:3] == ["gh", "pr", "view"] for a, _ in runner.calls)
    assert len(_launch_briefs(runner)) == 1
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


# -- stall ------------------------------------------------------------------


async def test_silent_lead_is_nudged_then_relaunched_then_flagged(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()  # no commit, no PR, no comment: ever

    disp = _lead_dispatcher(runner, launch)
    await disp.run(run_ref, workspace_id=WS)

    # Stall 1 at 30 min: one nudge into the live session, with the resume text.
    (nudge,) = _nudges(runner)
    assert nudge[1:4] == ["-p", "--cloud", "session_L1"]
    assert f"docs/leads/{FEATURE}/RECORD.md" in nudge[-1]
    assert "continue from the last stage" in nudge[-1]
    # Stall 2: a relaunch whose brief says to resume from RECORD.md.
    first, again = _launch_briefs(runner)
    assert "continue from the last stage" not in first
    assert "continue from the last stage" in again and PLAYBOOK.strip() in again
    # Stall 3: needs-human on the issue and the run released.
    assert any(a[:3] == ["gh", "issue", "edit"] and "needs-human" in a for a, _ in runner.calls)
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    lead = action.parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["nudge", "relaunch", "needs-human"]
    assert [s["session_url"] for s in lead["sessions"]] == [
        "https://claude.ai/code/session_L1",
        "https://claude.ai/code/session_L2",
    ]
    gaps = [b["at"] - a["at"] for a, b in zip(lead["stalls"], lead["stalls"][1:], strict=False)]
    assert all(g >= 1800 for g in gaps)  # each step waits a full stall window


async def test_lead_activity_holds_off_the_stall(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch)
    # The lead pushes its RECORD.md every poll for 2 h, then the feature is done.
    polls = 120
    runner.commits = [runner.wall() + 60 * i for i in range(polls)]
    done = _done_runner(run_ref)
    runner.prs = [[]] * polls + done.prs
    runner.comments = [[]] * polls + done.comments

    await disp.run(run_ref, workspace_id=WS)

    assert not _nudges(runner) and len(_launch_briefs(runner)) == 1
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_lead_has_8h_and_no_no_pr_rule(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch)
    runner.commits = [runner.wall() + 60 * i for i in range(9 * 60)]  # busy, never a PR

    await disp.run(run_ref, workspace_id=WS)

    assert len(_launch_briefs(runner)) == 1 and not _nudges(runner)  # no 1 h relaunch
    polls = sum(a[:3] == ["gh", "pr", "list"] for a, _ in runner.calls)
    assert 8 * 60 <= polls <= 8 * 60 + 2
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["headless_error"].startswith("TRACK:") and "28800" in cc["headless_error"]
    assert cc["station_pending"] is True
    # A lead with no PR change for hours still writes its row once per stall
    # window, so the line's headroom (it drops rows unwritten for 3 h) counts it.
    assert runner.wall() - cc["lead"]["heartbeat_at"] <= 1800


async def test_lead_timeouts_read_the_env(monkeypatch, tmp_path):
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR", str(tmp_path))
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LEAD_TIMEOUT", "100")
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LEAD_STALL", "50")
    monkeypatch.setenv("POCKETPAW_FACTORY_LEAD_GIT_NAME", "Pat Lead")
    seen: list[CloudTaskDispatcher] = []
    monkeypatch.setattr("pocketpaw_ee.cloud.belt.headless.set_production_dispatcher", seen.append)

    assert cloud_station.wire_cloud()

    (disp,) = seen
    assert disp.lead_timeout_seconds == 100 and disp.lead_stall_seconds == 50
    assert disp.lead_git_name == "Pat Lead"
    assert CloudTaskDispatcher().lead_timeout_seconds == 8 * 3600
    assert CloudTaskDispatcher().lead_stall_seconds == 1800


# -- resume -----------------------------------------------------------------


async def test_resume_tracks_a_launched_lead_as_a_lead(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    disp = _lead_dispatcher(runner, launch, workspaces_for=_ws)
    lead = {
        "feature": FEATURE,
        "packet": PACKET,
        "issue": 93,
        "branch": f"lead/{FEATURE}",
        "sessions": [{"session_url": CLOUD_URL, "at": time.time()}],
    }
    await cloud_station._patch(
        store,
        run_ref,
        headless_state="cloud",
        cloud={"session_url": CLOUD_URL, "launched_at": time.time()},
        lead=lead,
    )

    assert await disp.resume_all() == 1

    assert not _launch_briefs(runner)
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def _ws() -> list[str]:
    return [WS]


# -- lesson 42: transient gh failures ---------------------------------------


class FlakyRunner(FakeRunner):
    """``FakeRunner`` whose ``gh pr view`` fails with ``errors`` first."""

    def __init__(self, errors: list[str], **kw):
        super().__init__(**kw)
        self.errors = list(errors)

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        if argv[:3] == ["gh", "pr", "view"] and self.errors:
            self.calls.append((list(argv), cwd))
            return 1, "", self.errors.pop(0)
        return await super().__call__(argv, cwd=cwd, timeout=timeout, on_line=on_line)


TRANSIENT = "Post https://api.github.com/graphql: read tcp: connection reset by peer"


async def test_transient_gh_failures_are_retried_with_backoff(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FlakyRunner([TRANSIENT, "HTTP 502: Bad Gateway"], launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref
    slept: list[float] = []
    disp = _dispatcher(runner, launch)
    inner = disp.sleep

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await inner(seconds)

    disp.sleep = sleep

    await disp.run(run_ref, workspace_id=WS)

    assert sum(a[:3] == ["gh", "pr", "view"] for a, _ in runner.calls) == 3
    assert slept[:2] == [disp.gh_backoff_seconds, 2 * disp.gh_backoff_seconds]
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_a_permanent_gh_failure_is_not_retried(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FlakyRunner(
        ["GraphQL: Could not resolve to a PullRequest"], launch_out=SESSION, pr=MERGED
    )
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert sum(a[:3] == ["gh", "pr", "view"] for a, _ in runner.calls) == 1
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["headless_error"].startswith("TRACK: gh pr view failed")


async def test_transient_gh_failures_past_the_retries_end_the_tracker(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = FlakyRunner([TRANSIENT] * 10, launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref
    disp = _dispatcher(runner, launch)

    await disp.run(run_ref, workspace_id=WS)

    views = sum(a[:3] == ["gh", "pr", "view"] for a, _ in runner.calls)
    assert views == disp.gh_retries + 1
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["headless_error"].startswith("TRACK:") and "headless_state" not in cc


# -- lesson 45: re-track TRACK errors, never count errored runs -------------


async def test_resume_retracks_a_run_whose_tracker_died_on_a_track_error(store, dirs):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time() - 600)
    await cloud_station._patch(
        store, run_ref, headless_state=None, headless_error="TRACK: gh pr view failed (exit 1)"
    )
    runner = FakeRunner(launch_out=SESSION, pr=[_open("h1"), MERGED])
    runner.run_ref = run_ref

    assert await _dispatcher(runner, launch, workspaces_for=_ws).resume_all() == 1

    assert not any("--cloud" in a for a, _ in runner.calls)  # tracked, never relaunched
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert "headless_error" not in action.parameters["_code_change"]


async def test_resume_clears_the_error_while_it_tracks_again(store, dirs):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time() - 600)
    await cloud_station._patch(
        store, run_ref, headless_state=None, headless_error="TRACK: gh pr list failed (exit 1)"
    )
    seen: list[dict] = []
    runner = FakeRunner(launch_out=SESSION, pr=_open("h1"))
    runner.run_ref = run_ref
    disp = _dispatcher(runner, launch, workspaces_for=_ws, timeout_seconds=900)
    track = disp._track

    async def spy(store_, ref, blob, *a, **k):
        seen.append(dict((await store_.get_action(ref)).parameters["_code_change"]))
        return await track(store_, ref, blob, *a, **k)

    disp._track = spy  # type: ignore[method-assign]

    await disp.resume_all()

    assert seen and seen[0]["headless_state"] == "cloud" and "headless_error" not in seen[0]


@pytest.mark.parametrize(
    ("error", "age"),
    [
        ("TRACK: gh pr view failed (exit 1)", 5 * 3600),  # past the 4 h run timeout
        ("TRACK: no PR after 14400s (session x)", 5 * 3600),  # the timeout itself
        ("LAUNCH: no cloud session started (exit 1)", 600),  # never got that far
    ],
)
async def test_resume_leaves_other_errored_runs_alone(store, dirs, error, age):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time() - age)
    await cloud_station._patch(store, run_ref, headless_state=None, headless_error=error)
    runner = FakeRunner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    assert await _dispatcher(runner, launch, workspaces_for=_ws).resume_all() == 0

    assert runner.calls == []


def test_an_errored_run_is_never_counted_in_flight():
    from datetime import datetime as dt

    now = dt.now(UTC)
    stamp = now.isoformat()
    budget = Budget(max_tasks_per_shift=3, max_in_flight=3)
    runs = [
        {"status": "running", "headless_state": "cloud", "updated_at": stamp},
        {"status": "running", "headless_error": "TRACK: x", "updated_at": stamp},
        {"status": "queued", "headless_error": "TRACK: x", "updated_at": stamp},
    ]

    assert mandate_service._headroom(budget, runs, now)["in_flight"] == 1
    assert mandate_service._task_status(runs[1]) == "develop failed"


async def test_a_relaunched_run_clears_its_old_error(store, dirs):
    """A queued run a human sends back after a failure is in flight again: the
    old error must not hide it from the line's in-flight count."""
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    await cloud_station._patch(store, run_ref, headless_error="LAUNCH: no cloud session")
    runner = FakeRunner(launch_out=SESSION, pr=_open("h1"))
    runner.run_ref = run_ref
    seen: list[dict] = []
    disp = _dispatcher(runner, launch, timeout_seconds=0)
    track = disp._track

    async def spy(store_, ref, blob, *a, **k):
        seen.append(dict((await store_.get_action(ref)).parameters["_code_change"]))
        return await track(store_, ref, blob, *a, **k)

    disp._track = spy  # type: ignore[method-assign]

    await disp.run(run_ref, workspace_id=WS)

    assert seen and "headless_error" not in seen[0]


async def test_a_run_handed_to_a_headless_develop_again_drops_its_old_error(store, dirs):
    """The headless station's re-develop of a failed run is in flight again too."""
    from pocketpaw_ee.cloud.belt.headless import HeadlessDevelopRunner

    repo, _ = dirs
    run_ref = await _queue(store, repo)
    await cloud_station._patch(store, run_ref, headless_error="CHECK: red")

    await HeadlessDevelopRunner(develop_fn=None).mark_queued(run_ref)  # type: ignore[arg-type]

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["headless_state"] == "queued" and "headless_error" not in cc
