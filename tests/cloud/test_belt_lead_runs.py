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
# any one missing keeps the run tracking. A closed feature issue finishes the
# run as done at the next poll and frees its slot, never nudged or relaunched.
# STALL: 30 min with no lead-branch commit, PR update or issue comment: a
# ``claude -p --cloud <session>`` nudge, then a relaunch with the resume brief,
# then ``needs-human`` on the issue and the run released, the reason naming the
# rungs that ran ("after two nudges" when no relaunch did). Activity holds it off.
# WAITING: a quiet lead whose RECORD.md ``Next:`` names CI or the lane while a run
# on the base is live or ended inside the window gets no rung (the viewer-playback
# and photo-edit leads); a silent lead with no such evidence still climbs.
# TIMEOUT: 8 h from the launch (from the last progress while waiting), and no 1 h
# no-PR rule; a timed-out lead is labelled ``needs-human`` and released like the
# ladder's last rung.
# RESUME: a launched lead is tracked again as a lead, never relaunched.
# LESSON 42: a transient gh failure is retried with backoff; a permanent one
# fails at once; a transient one past the retries ends the tracker.
# LESSON 45: on startup a run whose tracker died on a TRACK error (inside its
# timeout) is tracked again with the error cleared; an errored run is never
# counted in flight.
# ROW: a lead run's console row carries ``lead`` (feature, app, PRs, session and
# stall counts, contact sheet) off the tracker's blob; a task run's is null.

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import cloud_station  # noqa: E402
from pocketpaw_ee.cloud.belt.cloud_station import CloudTaskDispatcher  # noqa: E402
from pocketpaw_ee.cloud.belt.service import _run_summary  # noqa: E402
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


def _lead_pr(
    number: int,
    run_ref: str,
    *,
    state="OPEN",
    done=False,
    at: float = 0.0,
    sha: str = MERGE_SHA,
    merged: str = "2026-01-01T00:00:00Z",
) -> dict:
    labels = [{"name": "fast-lane"}] + ([{"name": "feature-done"}] if done else [])
    return {
        "number": number,
        "url": f"https://github.com/{SLUG}/pull/{number}",
        "state": state,
        "body": f"Swarm slice.\n\nBelt-Lead: {run_ref}\nFeature: {FEATURE}\n",
        "labels": labels,
        "headRefName": f"lead/{FEATURE}-{number}",
        "updatedAt": _iso(at) if at else "2020-01-01T00:00:00Z",
        "mergeCommit": {"oid": sha} if state == "MERGED" else None,
        "mergedAt": merged if state == "MERGED" else None,
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
        ci_by_sha: dict[str, list[dict]] | None = None,
        nudge_codes: list[int] | None = None,
        fail: dict[str, int] | None = None,
        base_runs: list[dict] | Callable[[float], list[dict]] | None = None,
    ):
        self.calls: list[tuple[list[str], Path]] = []
        self.prs = prs or [[]]
        self.comments = comments or [[]]
        self.commits = commits or [None]
        self.ci = ci or [[GREEN_RUN]]
        self.issue_body = issue_body
        self.launches_left = launches
        self.wall = time.time
        self.ci_by_sha = ci_by_sha or {}  # dev CI runs per merge commit, over ``ci``
        self.dev_runs: list[dict] = []  # newest dev CI runs, any head (no --commit)
        self.contains: dict[str, str] = {}  # head sha -> compare status vs the merge
        self.nudge_codes = nudge_codes or [0]  # exit code per ``claude -p --cloud``
        # gh reads to fail transiently, by "pr list" / "issue view": calls left.
        self.fail = dict(fail or {})
        self.locked_at_nudge: list[bool] = []
        self.on_pr_list = None  # called on each ``gh pr list``, before it answers
        self.record = ""  # RECORD.md on the lead branch, read with its last commit
        # Base-branch runs the stall check reads: a list, or a function of the wall.
        self.base_runs = base_runs or []

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
            self.locked_at_nudge.append(cloud_station._LAUNCH_LOCK.locked())
            code = self._next(self.nudge_codes)
            return (
                code,
                "" if code else "Sent to cloud session.\n",
                "session not found" * bool(code),
            )
        if "--cloud" in argv:
            n = sum("--cloud" in a and "-p" not in a for a, _ in self.calls)
            return 0, f"Created cloud session: https://claude.ai/code/session_L{n}\r\n", ""
        key = " ".join(argv[1:3])
        if self.fail.get(key):
            self.fail[key] -= 1
            return 1, "", "Post https://api.github.com/graphql: HTTP 502 Bad Gateway"
        if argv[:3] == ["gh", "pr", "list"]:
            if self.on_pr_list is not None:
                await self.on_pr_list()
            return 0, json.dumps(self._next(self.prs)), ""
        if argv[:3] == ["gh", "issue", "view"]:
            data = {"comments": self._next(self.comments), "title": "Text shapes"}
            return 0, json.dumps({**data, "body": self.issue_body}), ""
        if argv[:3] == ["gh", "api", "graphql"]:  # the lead branch's last commit + RECORD.md
            at = self._next(self.commits)
            record = {"object": {"text": self.record}} if self.record else None
            head = None if at is None else {"committedDate": _iso(at), "file": record}
            return 0, json.dumps({"data": {"repository": {"object": head}}}), ""
        if argv[:3] == ["gh", "run", "list"] and "updatedAt" in argv[-1]:
            runs = self.base_runs
            return 0, json.dumps(runs(self.wall()) if callable(runs) else runs), ""
        if argv[:2] == ["gh", "api"] and "/compare/" in argv[2]:
            head = argv[2].split("...")[-1]
            return 0, json.dumps({"status": self.contains.get(head, "diverged")}), ""
        if argv[:3] == ["gh", "run", "list"] and "--commit" not in argv:
            return 0, json.dumps(self.dev_runs), ""
        if argv[:3] == ["gh", "run", "list"]:
            sha = argv[argv.index("--commit") + 1]
            if sha in self.ci_by_sha:
                return 0, json.dumps(self.ci_by_sha[sha]), ""
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
    assert "`Next:` line names CI or the lane" in brief  # a wait holds the ladder off
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
    # Any event counts: the lane dispatches dev CI (workflow_dispatch) with a
    # token that fires no push runs, so a push-only filter never sees green.
    assert "--event" not in ci


async def test_lead_is_not_done_until_dev_ci_on_the_merge_is_green(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    pending = {**GREEN_RUN, "status": "in_progress", "conclusion": ""}
    red = {**GREEN_RUN, "conclusion": "failure"}
    runner.ci = [[], [pending, GREEN_RUN], [red], [GREEN_RUN, GREEN_RUN]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert sum(a[:3] == ["gh", "run", "list"] and "--commit" in a for a, _ in runner.calls) == 4
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_a_later_green_dev_run_that_contains_the_merge_lands_the_lead(store, dirs, playbook):
    # The lane often merges more PRs before it dispatches dev CI, and a red run
    # on the merge commit is superseded by a later green one that includes it.
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref, ci_by_sha={MERGE_SHA: [{**RED_RUN, "name": "CI"}]})
    later = "d" * 40
    runner.dev_runs = [
        {**GREEN_RUN, "name": "CI", "headSha": later},
        {**RED_RUN, "name": "CI", "headSha": MERGE_SHA},
    ]
    runner.contains = {later: "ahead"}

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_a_green_dev_run_without_the_merge_does_not_land_the_lead(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref, ci_by_sha={MERGE_SHA: [RED_RUN]})
    other = "e" * 40
    runner.dev_runs = [{**GREEN_RUN, "headSha": other}]
    runner.contains = {other: "diverged"}  # a green head that lacks the merge commit

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert (await store.get_action(run_ref)).status != ActionStatus.EXECUTED


async def test_a_later_green_run_of_another_workflow_does_not_land_the_lead(store, dirs, playbook):
    # Only the workflow that judged the merge commit counts on a later head: a
    # green lane or audit run on dev says nothing about CI.
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    red_ci = {**RED_RUN, "name": "CI"}
    runner = _done_runner(run_ref, ci_by_sha={MERGE_SHA: [red_ci]})
    later = "f" * 40
    runner.dev_runs = [{**GREEN_RUN, "name": "Fast Lane", "headSha": later}]
    runner.contains = {later: "ahead"}

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert (await store.get_action(run_ref)).status != ActionStatus.EXECUTED


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
    assert action.rejected_reason == "feature lead silent 30m after a nudge and a relaunch"
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
    # Past 8 h a lead still committing keeps going; it is released once it has
    # been past the timeout AND silent for a whole stall window (commits stop at
    # 9 h, so 9 h 30 m). A paused plan or a long Anatomy round must not release
    # a live lead (factory lesson 100).
    assert 9 * 60 + 30 <= polls <= 9 * 60 + 32
    # Out of time it settles like the ladder's last rung, never as a develop
    # failure: a failed run stops counting in flight while its session may still
    # commit, and the next shift would launch a second lead on the feature.
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason == (
        "feature lead not done after 8h (session https://claude.ai/code/session_L1)"
    )
    assert any(a[:3] == ["gh", "issue", "edit"] and "needs-human" in a for a, _ in runner.calls)
    cc = action.parameters["_code_change"]
    assert "headless_error" not in cc and cc.get("headless_state") is None
    assert cc["lead"]["stalls"][-1]["kind"] == "timeout"
    # A lead with no PR change for hours still writes its row once per stall
    # window, so the line's headroom (it drops rows unwritten for 3 h) counts it.
    assert runner.wall() - cc["lead"]["heartbeat_at"] <= 1800


async def test_a_lead_still_working_is_released_only_at_twice_the_timeout(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch)
    runner.commits = [runner.wall() + 60 * i for i in range(20 * 60)]  # busy for 20 h

    await disp.run(run_ref, workspace_id=WS)

    polls = sum(a[:3] == ["gh", "pr", "list"] for a, _ in runner.calls)
    assert 16 * 60 <= polls <= 16 * 60 + 2  # the hard cap: twice the 8 h timeout
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason == (
        "feature lead not done after 16h (session https://claude.ai/code/session_L1)"
    )


async def test_a_lead_quiet_before_the_timeout_gets_the_ladder_not_the_timeout(
    store, dirs, playbook
):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch)
    runner.commits = [runner.wall() + 60 * i for i in range(5 * 60)]  # busy 5 h, then quiet

    await disp.run(run_ref, workspace_id=WS)

    # Quiet at 5 h is a stall, not a timeout: the 8 h clock has not run out, so
    # the ladder (nudge, relaunch, then needs-human) settles it.
    assert _nudges(runner)
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert "not done after" not in (action.rejected_reason or "")


async def test_a_lead_that_posted_its_contact_sheet_waits_for_ci_without_the_ladder(
    store, dirs, playbook
):
    # A lead whose sheet is up is finished: it waits on dev CI (red here, for
    # other lines' failures), so a quiet branch is not a stall. No nudge, no
    # relaunch of a session with nothing left to do; the timeout settles it.
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref, ci_by_sha={MERGE_SHA: [RED_RUN]})

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert not _nudges(runner) and len(_launch_briefs(runner)) == 1
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert (action.rejected_reason or "").startswith("feature lead not done after 8h")


# -- waiting, not stalled ------------------------------------------------------


def _record(next_step: str) -> str:
    return f"# {FEATURE} lead record\n\nIssue: #93\nStage: 4 round 1\nNext: {next_step}\n"


def _commits(start: float, polls: int, busy: Callable[[int], bool]) -> list[float | None]:
    """The lead branch's last commit time at each poll (one per minute): a commit
    on every ``busy`` minute, none on the others."""
    out: list[float | None] = []
    last = None
    for minute in range(polls):
        last = start + 60 * minute if busy(minute) else last
        out.append(last)
    return out


def _base_run(updated: float, *, status: str = "completed", conclusion: str = "failure") -> dict:
    return {"status": status, "conclusion": conclusion, "updatedAt": _iso(updated)}


def _flagged(runner: LeadRunner) -> bool:
    return any(a[:3] == ["gh", "issue", "edit"] and "needs-human" in a for a, _ in runner.calls)


async def test_a_lead_waiting_on_red_dev_ci_is_not_stalled(store, dirs, playbook):
    """#1035 (viewer-playback): its RECORD's Next was "read the dev CI run
    dispatched by #1211 as round 2" while dev CI stayed red for hours on other
    features' breaks. It took nudges, then the 8 h timeout labelled it
    needs-human 31 min after its last commit. A lead waiting on CI it cannot
    shorten is not stalled: no rung while it waits, before or past 8 h."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch)
    start = runner.wall()
    runner.record = _record("read the dev CI run dispatched by #1211 as round 2")
    # Busy, waiting on dev CI from 3 h to 5 h, busy, waiting again from 8 h 5 m.
    polls = 11 * 60
    waits = (range(3 * 60, 5 * 60), range(8 * 60 + 5, polls))
    runner.commits = _commits(start, polls, lambda m: not any(m in w for w in waits))
    # Red dev CI all day: a run finishes every 10 min.
    runner.base_runs = lambda now: [_base_run(now - (now - start) % 600)]
    done = _done_runner(run_ref)  # at 11 h the round is clean and the feature done
    runner.prs = [[]] * polls + done.prs
    runner.comments = [[]] * polls + done.comments

    await disp.run(run_ref, workspace_id=WS)

    assert not _nudges(runner) and len(_launch_briefs(runner)) == 1
    assert not _flagged(runner)
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert not action.parameters["_code_change"]["lead"]["stalls"]


async def test_a_lead_waiting_for_the_lane_and_ci_on_its_fixes_is_not_stalled(
    store, dirs, playbook
):
    """#1075 (photo-edit): its five fix PRs merged, then its RECORD's Next was
    "... collect PRs, wait for the lane, then round 2 on the CI run that contains
    them" while that dev CI run was still in progress. Past 8 h, 31 min after
    its last commit, the timeout labelled it needs-human."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch)
    start = runner.wall()
    runner.record = _record(
        "fix batches r1-b1..b5 running (workflow wf_1); collect PRs, wait for the lane, "
        "then round 2 on the CI run that contains them"
    )
    runner.commits = _commits(start, 8 * 60 + 5, lambda m: True)  # quiet from 8 h 5 m
    fixes = [_lead_pr(40 + i, run_ref, state="MERGED", at=start + 8 * 3600) for i in range(5)]
    # The dev CI run with the fixes started at 7 h 55 m and runs past 9 h 30 m.
    runner.base_runs = [_base_run(start + 7 * 3600 + 55 * 60, status="in_progress")]
    done = _done_runner(run_ref)
    runner.prs = [[]] * (8 * 60) + [fixes] * 90 + [fixes + done.prs[0]]
    runner.comments = [[]] * (8 * 60 + 90) + done.comments

    await disp.run(run_ref, workspace_id=WS)

    assert not _nudges(runner) and not _flagged(runner)
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


@pytest.mark.parametrize(
    ("next_step", "base_runs"),
    [
        # Next names CI, but nothing ran on the base inside the stall window.
        ("read the dev CI run as round 2", [_base_run(time.time() - 3600)]),
        # The base is busy, but Next names neither CI nor the lane.
        ("start the wave 3 builders", [_base_run(time.time(), status="in_progress")]),
    ],
)
async def test_a_silent_lead_with_no_wait_in_evidence_still_climbs_the_ladder(
    store, dirs, playbook, next_step, base_runs
):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner(base_runs=base_runs)
    runner.commits = [runner.wall()]  # its RECORD at the launch, then silence
    runner.record = _record(next_step)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    lead = action.parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["nudge", "relaunch", "needs-human"]
    assert action.rejected_reason == "feature lead silent 30m after a nudge and a relaunch"
    assert _flagged(runner)


async def test_a_wait_is_bounded_by_the_timeout_from_the_last_progress(store, dirs, playbook):
    """A lead whose RECORD names CI on a base that never goes quiet, with no
    progress after 1 h, is never nudged; the timeout counts from its last
    commit and releases it at 9 h, labelled needs-human."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    in_progress = [_base_run(time.time(), status="in_progress")]
    runner = LeadRunner(base_runs=in_progress)
    disp = _lead_dispatcher(runner, launch)
    runner.commits = _commits(runner.wall(), 60, lambda m: True)  # busy 1 h, then silent
    runner.record = _record("wait for the lane, then round 2 on the CI run")

    await disp.run(run_ref, workspace_id=WS)

    assert not _nudges(runner) and _flagged(runner)
    polls = sum(a[:3] == ["gh", "pr", "list"] for a, _ in runner.calls)
    assert 9 * 60 - 2 <= polls <= 9 * 60 + 2  # 8 h after the last commit, not the launch
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason.startswith("feature lead not done after 8h")


async def test_an_outage_while_a_lead_waits_past_8h_does_not_release_it(store, dirs, playbook):
    """A GitHub outage skips polls; a lead that was waiting when it began keeps
    its timeout counted from its last progress, not from the launch."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner(base_runs=[_base_run(time.time(), status="in_progress")])
    disp = _lead_dispatcher(runner, launch)
    start = runner.wall()
    runner.commits = _commits(start, 8 * 60 + 5, lambda m: True)
    runner.record = _record("read the dev CI run as round 2")
    done = _done_runner(run_ref)
    runner.prs = [[]] * (9 * 60) + done.prs
    runner.comments = [[]] * (9 * 60) + done.comments

    async def outage() -> None:  # two polls' worth of failed reads at 8 h 40 m
        if runner.wall() >= start + 8 * 3600 + 40 * 60 and runner.on_pr_list:
            runner.fail["pr list"] = 2 * (disp.gh_retries + 1)
            runner.on_pr_list = None

    runner.on_pr_list = outage

    await disp.run(run_ref, workspace_id=WS)

    assert sum(" ".join(a[1:3]) == "pr list" for a, _ in runner.calls) > 9 * 60
    assert not _nudges(runner) and not _flagged(runner)
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


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


# -- review fixes: newest done PR, settled runs, the ladder, outages ----------

RED_RUN = {**GREEN_RUN, "conclusion": "failure"}


async def test_the_newest_feature_done_pr_decides_done(store, dirs, playbook):
    """A first ``feature-done`` PR that broke dev CI is superseded by a later
    one: the lead lands on the newest merged ``feature-done`` PR, never stuck
    on the first."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    first = _lead_pr(
        32, run_ref, state="MERGED", done=True, sha="b" * 40, merged="2026-10-14T08:00:00Z"
    )
    fixed = _lead_pr(
        35, run_ref, state="MERGED", done=True, sha="c" * 40, merged="2026-10-14T09:00:00Z"
    )
    runner.prs = [[first], [first, fixed]]
    runner.ci_by_sha = {"b" * 40: [RED_RUN], "c" * 40: [GREEN_RUN]}

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    assert cc["pr_url"].endswith("/pull/35") and cc["commit_sha"] == "c" * 40


async def test_newest_done_pr_is_by_merge_time_not_number(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    late = _lead_pr(
        32, run_ref, state="MERGED", done=True, sha="c" * 40, merged="2026-10-14T09:00:00Z"
    )
    early = _lead_pr(
        35, run_ref, state="MERGED", done=True, sha="b" * 40, merged="2026-10-14T08:00:00Z"
    )
    runner.prs = [[late, early]]
    runner.ci_by_sha = {"b" * 40: [RED_RUN], "c" * 40: [GREEN_RUN]}

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["pr_url"].endswith("/pull/32")


@pytest.mark.parametrize("settle", ["reject", "approve"])
async def test_a_settled_lead_is_no_longer_tracked(store, dirs, playbook, settle):
    """A human who rejects or approves the run mid-flight ends its tracker at
    the next poll: no nudge, no relaunch, no needs-human."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()  # silent: the ladder would act if tracking went on
    polls = [0]

    async def on_pr_list() -> None:
        polls[0] += 1
        if polls[0] == 2:
            if settle == "reject":
                await store.reject(run_ref, reason="not now", rejector="u1")
            else:
                await store.auto_approve(run_ref, approver="u1", verdict="ok", reasoning="by hand")

    runner.on_pr_list = on_pr_list

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert polls[0] == 2
    assert not _nudges(runner) and len(_launch_briefs(runner)) == 1
    assert not any(a[:3] == ["gh", "issue", "edit"] for a, _ in runner.calls)
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert not cc["lead"].get("stalls") and "headless_error" not in cc


async def test_activity_after_a_nudge_resets_the_ladder(store, dirs, playbook):
    """A lead the nudge revived starts the ladder over on its next quiet
    window: a nudge again, never a relaunch of a session that just worked."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch)
    start = runner.wall()
    # Silent to 30 min (a nudge), one commit at 40 min, then silent for good.
    runner.commits = [None] * 40 + [start + 2400]

    await disp.run(run_ref, workspace_id=WS)

    lead = (await store.get_action(run_ref)).parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["nudge", "nudge", "relaunch", "needs-human"]
    assert lead["stalls"][1]["at"] >= start + 2400 + 1800  # a full quiet window after it


async def test_a_failed_nudge_never_leads_to_a_relaunch(store, dirs, playbook):
    """A nudge that exits non-zero is recorded and counts as a rung, but it
    proves nothing about the session: the next rung nudges again instead of
    relaunching beside a session that may still be live."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner(nudge_codes=[1, 0])

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    stalls = action.parameters["_code_change"]["lead"]["stalls"]
    assert [s["kind"] for s in stalls] == ["nudge", "nudge", "needs-human"]
    assert "session not found" in stalls[0]["error"] and "error" not in stalls[1]
    assert len(_launch_briefs(runner)) == 1  # never relaunched
    assert action.status == ActionStatus.REJECTED
    # The reason names the rungs that ran: no relaunch on this path.
    assert action.rejected_reason == "feature lead silent 30m after two nudges"


async def test_the_nudge_holds_the_launch_lock(store, dirs, playbook):
    """The nudge runs in the shared launch clone: never beside a launch."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert runner.locked_at_nudge and all(runner.locked_at_nudge)


async def test_a_fresh_run_starts_the_ladder_over(store, dirs, playbook):
    """A human retry of a lead that was flagged starts from a nudge, not from
    the old run's ladder (which would flag it again at the first stall)."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    old = [{"kind": "nudge", "at": 1.0}, {"kind": "relaunch", "at": 2.0}]
    await cloud_station._patch(store, run_ref, lead={"feature": FEATURE, "stalls": old})
    runner = LeadRunner()

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    lead = (await store.get_action(run_ref)).parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["nudge", "relaunch", "needs-human"]
    assert all(s["at"] > 2.0 for s in lead["stalls"])  # none of the old run's
    assert len(_nudges(runner)) == 1 and len(_launch_briefs(runner)) == 2


@pytest.mark.parametrize("read", ["pr list", "issue view"])
async def test_a_github_outage_skips_polls_instead_of_ending_the_lead(store, dirs, playbook, read):
    """A gh read that fails past its retries skips that poll; the tracker goes
    on and lands the lead once GitHub answers again."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref, fail={read: 10})  # 2+ polls' worth of retries

    disp = _lead_dispatcher(runner, launch)
    await disp.run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert "headless_error" not in action.parameters["_code_change"]
    calls = sum(" ".join(a[1:3]) == read for a, _ in runner.calls)
    assert calls > disp.gh_retries + 1  # it came back for another poll


async def test_an_outage_past_the_lead_timeout_still_ends_it(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner(fail={"pr list": 10**6})

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    # Released and labelled like any timed-out lead, never left as a failure.
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason.startswith("feature lead not done after 8h")
    assert any(a[:3] == ["gh", "issue", "edit"] and "needs-human" in a for a, _ in runner.calls)
    assert "headless_error" not in action.parameters["_code_change"]


# -- the console row ------------------------------------------------------------

_ROW_PR = {
    "number": 1122,
    "url": f"https://github.com/{SLUG}/pull/1122",
    "state": "MERGED",
    "branch": f"lead/{FEATURE}-1122",
    "done": False,
    "merge_sha": MERGE_SHA,
    "merged_at": "2026-10-15T10:00:00Z",
}
_ROW_SHEET = "Belt-Contact-Sheet: https://example.com/sheet.png"


def _row_action() -> SimpleNamespace:
    return SimpleNamespace(id="act-1", status=SimpleNamespace(value="pending"))


def test_lead_run_row_carries_lead() -> None:
    blob = {
        "recipe": "feature-lead",
        "task": "Lead #93",
        "lead": {
            "feature": FEATURE,
            "packet": PACKET,
            "issue": 93,
            "branch": f"lead/{FEATURE}",
            "prs": [_ROW_PR],
            "sessions": [{"session_url": f"https://claude.ai/code/{SID}", "at": 1.0}],
            "stalls": [],
            "heartbeat_at": 2.0,
            "contact_sheet": _ROW_SHEET,
        },
    }
    lead = _run_summary(_row_action(), blob)["lead"]
    assert lead["feature"] == FEATURE
    assert lead["app"] == "flare"
    assert lead["issue"] == 93
    assert lead["branch"] == f"lead/{FEATURE}"
    assert lead["prs"] == [_ROW_PR]
    assert lead["sessions"] == 1
    assert lead["stalls"] == 0
    assert lead["contact_sheet"] == _ROW_SHEET


def test_queued_lead_row_is_a_lead_before_launch() -> None:
    # Planned with the recipe, not launched yet: no ``lead`` dict on the blob.
    lead = _run_summary(_row_action(), {"recipe": "feature-lead", "task": "Lead #7"})["lead"]
    assert lead is not None
    assert lead["feature"] is None
    assert lead["prs"] == []
    assert lead["contact_sheet"] is None


def test_task_run_row_has_null_lead() -> None:
    assert _run_summary(_row_action(), {"task": "fix the bug"})["lead"] is None


# -- a closed feature issue ends the run ---------------------------------------
# Live 2026-10-15: creative-apps #798 closed by hand and #1037 closed by its own
# PR both stayed "running" for hours, holding a slot while dev CI was red. A
# closed issue finishes the run as done at the next poll: no nudge, no relaunch.


class IssueStateRunner(LeadRunner):
    """``LeadRunner`` whose ``gh issue view`` answers ``state`` (one per poll
    from ``states``, holding the last) only when the call asks for it."""

    def __init__(self, *, states: list[str], **kw):
        super().__init__(**kw)
        self.states = states

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        code, out, err = await super().__call__(
            argv, cwd=cwd, timeout=timeout, stdin=stdin, on_line=on_line
        )
        if argv[:3] == ["gh", "issue", "view"] and code == 0 and "--json" in argv:
            fields = argv[argv.index("--json") + 1].split(",")
            if "comments" in fields:
                data = json.loads(out)
                state = self._next(self.states)
                if "state" in fields:
                    data["state"] = state
                out = json.dumps(data)
        return code, out, err


def _in_flight(action) -> int:
    row = _run_summary(action, action.parameters["_code_change"])
    budget = Budget(max_tasks_per_shift=3, max_in_flight=3)
    return mandate_service._headroom(budget, [row], datetime.now(UTC))["in_flight"]


async def test_a_closed_feature_issue_finishes_the_run_and_frees_the_slot(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    # A merged slice, no feature-done PR, no contact sheet: the done rule waits.
    runner = IssueStateRunner(states=["CLOSED"], prs=[[_lead_pr(31, run_ref, state="MERGED")]])

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert "feature issue #93 was closed" in str(action.outcome)
    cc = action.parameters["_code_change"]
    assert cc["station_pending"] is False and "headless_state" not in cc
    assert _in_flight(action) == 0
    assert not _nudges(runner) and len(_launch_briefs(runner)) == 1
    assert not any(a[:3] == ["gh", "issue", "edit"] for a, _ in runner.calls)


async def test_an_open_feature_issue_keeps_the_stall_ladder(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = IssueStateRunner(states=["OPEN"])  # silent, issue open: today's ladder

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    lead = action.parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["nudge", "relaunch", "needs-human"]


async def test_an_open_feature_issue_with_the_done_rule_met_still_lands(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    done = _done_runner(run_ref)
    runner = IssueStateRunner(states=["OPEN"], prs=done.prs, comments=done.comments)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert "was closed" not in str(action.outcome)


async def test_a_closed_feature_issue_is_never_nudged_or_relaunched(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    # Silent from launch; the issue closes at the poll the first stall rung
    # would fire on (30 min in), so the rung must never run.
    runner = IssueStateRunner(states=["OPEN"] * 30 + ["CLOSED"])

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert not _nudges(runner) and len(_launch_briefs(runner)) == 1
    assert action.parameters["_code_change"]["lead"]["stalls"] == []
    assert not any(a[:3] == ["gh", "issue", "edit"] for a, _ in runner.calls)


async def test_a_lead_closed_after_a_nudge_is_not_relaunched(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    # Nudged at 30 min, then the issue closes before the relaunch rung at 60.
    runner = IssueStateRunner(states=["OPEN"] * 45 + ["CLOSED"])

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert len(_nudges(runner)) == 1 and len(_launch_briefs(runner)) == 1
