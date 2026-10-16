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
# DONE (v2, a wave.json with no ``slices``): a merged ``feature-done`` PR, every
# dev CI run of its merge commit green, and a ``Belt-Contact-Sheet:`` comment on
# the issue since the launch; any one missing keeps the run tracking.
# DONE (v3, ``slices`` in the wave.json at launch): a ``Belt-Slice-Done:`` line
# since the launch for every slice, each naming a successful ci.yml run on the
# base that holds the slice's last PR (``Slice:`` lines), then the sheet; dev
# CI is not read, gaps are kept per slice, and an unsafe slice id, an oversized
# wave.json or one outside the clone never launches. A closed feature issue
# finishes the run as done at the next poll and frees its slot, never nudged or
# relaunched.
# LIVENESS: live (a sign of work in the last 30 min, or a CI wait), exited (the
# session's own end state; no reader exists today, so tests inject one) or
# unverifiable. STALL: an unverifiable quiet lead gets a ``claude -p --cloud
# <session>`` nudge each window and nothing else; the 8 h budget ends it. Only an
# exited one is relaunched with the resume brief, then labelled ``needs-human``
# and released if it exits quiet again. Activity resets the ladder.
# WAITING: a quiet lead whose RECORD.md ``Next:`` names CI or the lane while a run
# on the base is live or ended inside the window gets no rung (the viewer-playback
# and photo-edit leads); a silent lead with no such evidence still climbs.
# TIMEOUT: 8 h from the launch (from the last progress while waiting), and no 1 h
# no-PR rule; a timed-out lead is parked like the ladder's last rung: a sheet closes
# the issue, all slices done asks for it, else ``needs-human``; never a failure.
# STALE BASE: a ``lead/<feature>`` on origin more than 40 commits behind the base
# gets a rebase-first rule in the brief; more than 150 and the launch or relaunch
# is refused: a note and ``needs-human`` on the issue, the run released (never a
# develop failure). No lead branch yet is nothing to measure.
# RESUME: a launched lead is tracked again as a lead, never relaunched; one whose
# feature issue has a newer pending lead is released (not a failure), not tracked.
# LESSON 42: a transient gh failure is retried with backoff; a permanent one
# fails at once; a transient one past the retries ends the tracker.
# LESSON 45: on startup a run whose tracker died on a TRACK error (inside its
# timeout) is tracked again with the error cleared; an errored run is never
# counted in flight.
# ROW: a lead run's console row carries ``lead`` (feature, app, PRs, session and
# stall counts, contact sheet, slices) off the tracker's blob; a task run's is null.

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
from pocketpaw_ee.cloud.mandates import lead_planner  # noqa: E402
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
    slices: str = "",
) -> dict:
    labels = [{"name": "fast-lane"}] + ([{"name": "feature-done"}] if done else [])
    slice_line = f"Slice: {slices}\n" if slices else ""
    return {
        "number": number,
        "url": f"https://github.com/{SLUG}/pull/{number}",
        "state": state,
        "body": f"Swarm slice.\n\nBelt-Lead: {run_ref}\nFeature: {FEATURE}\n{slice_line}",
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
        # compare status: "<sha>...<head>" first, else by head alone
        self.contains: dict[str, str] = {}
        self.runs: dict[str, dict] = {}  # Actions run id -> its API row (a slice's run)
        self.nudge_codes = nudge_codes or [0]  # exit code per ``claude -p --cloud``
        # gh reads to fail transiently, by "pr list" / "issue view": calls left.
        self.fail = dict(fail or {})
        self.locked_at_nudge: list[bool] = []
        self.on_pr_list = None  # called on each ``gh pr list``, before it answers
        self.record = ""  # RECORD.md on the lead branch, read with its last commit
        # Base-branch runs the stall check reads: a list, or a function of the wall.
        self.base_runs = base_runs or []
        # ``git rev-list --count lead/<f>..origin/<base>``, one per launch; and
        # whether ``lead/<f>`` is on origin at all (a first launch: not yet).
        self.behind = [0]
        self.lead_branch = True
        self.labels: list[str] = []  # the feature issue's labels (a request's check)

    @staticmethod
    def _next(seq: list):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        self.calls.append((list(argv), cwd))
        if "get-url" in argv:
            return 0, f"https://github.com/{SLUG}.git\n", ""
        if "rev-parse" in argv and "HEAD" in argv and "--abbrev-ref" not in argv:
            return 0, f"{HEAD_SHA}\n", ""
        if "rev-list" in argv:
            return 0, f"{self._next(self.behind)}\n", ""
        if "fetch" in argv and any(a.startswith("+refs/heads/lead/") for a in argv):
            return (0, "", "") if self.lead_branch else (128, "", "couldn't find remote ref")
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
            data["labels"] = [{"name": n} for n in self.labels]
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
            pair = argv[2].split("/compare/")[-1]
            status = self.contains.get(pair, self.contains.get(pair.split("...")[-1]))
            return 0, json.dumps({"status": status or "diverged"}), ""
        if argv[:2] == ["gh", "api"] and "/actions/runs/" in argv[2]:
            run = self.runs.get(argv[2].rsplit("/", 1)[-1])
            return (0, json.dumps(run), "") if run else (1, "", "gh: Not Found (HTTP 404)")
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


async def _ended(session_url: str) -> str:
    """A session-state read that proves every session ended."""
    return "ended"


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


def _rev_lists(runner: LeadRunner) -> list[list[str]]:
    return [a for a, _ in runner.calls if "rev-list" in a]


@pytest.mark.parametrize("behind", [40, 41])
async def test_a_lead_branch_behind_its_base_is_told_to_rebase_first(store, dirs, playbook, behind):
    """Past ``lead_rebase_behind`` (40) the brief's first rule is a rebase.
    Mutation: ``>`` as ``>=`` hints at 40."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    runner.behind = [behind]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    (brief,) = _launch_briefs(runner)
    (count,) = _rev_lists(runner)
    assert count[-1] == f"refs/remotes/origin/lead/{FEATURE}..origin/dev"
    hint = f"rebase lead/{FEATURE} onto origin/dev: it is {behind} commits behind"
    assert (hint in brief) == (behind > 40)


@pytest.mark.parametrize(("behind", "refused"), [(150, False), (151, True)])
async def test_a_lead_branch_past_the_hard_limit_is_refused_not_failed(
    store, dirs, playbook, behind, refused
):
    """Past ``lead_refuse_behind`` (150) no session starts: the issue gets the
    note and ``needs-human``, and the run is released (rejected), never a
    develop failure, so the breaker does not count it."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    runner.behind = [behind]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    notes = [a[-1] for a, _ in runner.calls if a[:3] == ["gh", "issue", "comment"]]
    if not refused:
        assert _launch_briefs(runner) and not notes
        assert action.status == ActionStatus.EXECUTED
        return
    assert not _launch_briefs(runner)
    note = f"Belt: lead/{FEATURE} is 151 commits behind dev (over 150)"
    assert [n.startswith(note) for n in notes] == [True]
    assert any(a[:3] == ["gh", "issue", "edit"] and "needs-human" in a for a, _ in runner.calls)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason.startswith(note)
    assert "headless_error" not in action.parameters["_code_change"]


def _writes(runner: LeadRunner) -> list[list[str]]:
    return [
        a for a, _ in runner.calls if a[:3] in (["gh", "issue", "comment"], ["gh", "issue", "edit"])
    ]


async def test_each_note_label_and_release_runs_once_under_a_request_id(
    store, dirs, playbook, monkeypatch
):
    """A refusal posts a note, labels the issue and releases the run, each under
    a request id kept on the blob. A retry skips the done ones; one whose answer
    was lost (still ``sent``) is checked first and repeated only if it did not
    land. Mutation: dropping the done check posts the note twice."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    runner.behind = [151]
    disp = _lead_dispatcher(runner, launch)
    await disp.run(run_ref, workspace_id=WS)

    blob = (await store.get_action(run_ref)).parameters["_code_change"]
    reqs = blob["requests"]
    assert sorted(r["kind"] for r in reqs.values()) == ["label", "note", "release"]
    assert {r["state"] for r in reqs.values()} == {"done"}
    (note_id,) = [rid for rid, r in reqs.items() if r["kind"] == "note"]
    (note,) = [a[-1] for a in _writes(runner) if a[2] == "comment"]
    assert f"<!-- belt-request: {note_id} -->" in note
    stale = cloud_station.StaleBaseError(SLUG, "Belt: stale")

    seen = len(runner.calls)
    await disp._refuse(store, run_ref, blob, stale)
    assert not _writes(SimpleNamespace(calls=runner.calls[seen:]))

    # Every answer lost: the note is on the issue and the label too, so nothing repeats.
    blob["requests"] = {k: {**r, "state": "sent"} for k, r in reqs.items()}
    runner.comments, runner.labels = [[{"body": note, "createdAt": _iso(0)}]], ["needs-human"]
    seen, rejects = len(runner.calls), []
    real_reject = store.reject

    async def reject(*args, **kwargs):
        rejects.append(args)
        return await real_reject(*args, **kwargs)

    monkeypatch.setattr(store, "reject", reject)
    await disp._refuse(store, run_ref, blob, stale)
    assert not _writes(SimpleNamespace(calls=runner.calls[seen:])) and not rejects
    assert {r["state"] for r in blob["requests"].values()} == {"done"}

    # Lost, and the check says it never landed: only then is it repeated.
    blob["requests"] = {k: {**r, "state": "sent"} for k, r in reqs.items()}
    runner.comments, runner.labels = [[]], []
    seen = len(runner.calls)
    await disp._refuse(store, run_ref, blob, stale)
    assert [a[2] for a in _writes(SimpleNamespace(calls=runner.calls[seen:]))] == [
        "comment",
        "edit",
    ]


async def test_a_lost_relaunch_is_never_launched_twice(store, dirs, playbook):
    """A relaunch has no read-back: a sent one whose answer was lost counts as
    done, and the next exited rung releases instead of starting a second session."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch, session_end=_ended)
    rid = cloud_station.hashlib.sha1(f"{run_ref}:relaunch:session-1".encode()).hexdigest()[:12]
    lost = {rid: {"kind": "relaunch", "target": "session-1", "state": "sent", "at": 0}}
    await cloud_station._patch(store, run_ref, requests=lost)

    await disp.run(run_ref, workspace_id=WS)

    assert len(_launch_briefs(runner)) == 1
    lead = (await store.get_action(run_ref)).parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["relaunch", "needs-human"]
    assert "answer was lost" in lead["stalls"][0]["error"]


async def test_a_first_launch_has_no_lead_branch_to_measure(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    runner.lead_branch, runner.behind = False, [999]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert not _rev_lists(runner) and len(_launch_briefs(runner)) == 1


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


def _ran_to_the_timeout(action) -> bool:
    """The done rule never landed the lead: it ran to the timeout (which, with
    its sheet up, then closes the issue, #2556)."""
    lead = action.parameters["_code_change"]["lead"]
    return "contact_sheet" not in lead and lead["stalls"][-1]["kind"] == "timeout"


async def test_a_green_dev_run_without_the_merge_does_not_land_the_lead(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref, ci_by_sha={MERGE_SHA: [RED_RUN]})
    other = "e" * 40
    runner.dev_runs = [{**GREEN_RUN, "headSha": other}]
    runner.contains = {other: "diverged"}  # a green head that lacks the merge commit

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert _ran_to_the_timeout(await store.get_action(run_ref))


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

    assert _ran_to_the_timeout(await store.get_action(run_ref))


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


async def test_a_silent_lead_nobody_can_prove_ended_is_only_nudged(store, dirs, playbook):
    """Silence is no proof the session ended (lessons 123, 133): an unverifiable
    lead gets a warning rung (a nudge with the resume text) each quiet window,
    never a relaunch or a release; the 8 h budget settles it."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()  # no commit, no PR, no comment: ever

    disp = _lead_dispatcher(runner, launch)
    await disp.run(run_ref, workspace_id=WS)

    nudges = _nudges(runner)
    assert nudges[0][1:4] == ["-p", "--cloud", "session_L1"]
    assert f"docs/leads/{FEATURE}/RECORD.md" in nudges[0][-1]
    assert "continue from the last stage" in nudges[0][-1]
    assert len(nudges) == 15 and len(_launch_briefs(runner)) == 1  # one per 30 min to 8 h
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason.startswith("feature lead not done after 8h")
    lead = action.parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["nudge"] * 15 + ["timeout"]
    assert lead["liveness"] == "unverifiable"
    gaps = [b["at"] - a["at"] for a, b in zip(lead["stalls"], lead["stalls"][1:], strict=False)]
    assert all(g >= 1800 for g in gaps)  # each rung waits a full stall window


async def test_a_lead_whose_session_ended_is_relaunched_then_flagged(store, dirs, playbook):
    """Proof the session ended (its own end state) is the only thing that
    relaunches a lead, and a relaunched lead whose session ends quiet again is
    labelled needs-human and released. No nudge goes into an ended session."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()

    await _lead_dispatcher(runner, launch, session_end=_ended).run(run_ref, workspace_id=WS)

    assert not _nudges(runner)
    first, again = _launch_briefs(runner)
    assert "continue from the last stage" not in first
    assert "continue from the last stage" in again and PLAYBOOK.strip() in again
    assert _flagged(runner)
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason == "feature lead session ended, silent 30m after a relaunch"
    lead = action.parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["relaunch", "needs-human"]
    assert lead["liveness"] == "exited"
    assert [s["session_url"] for s in lead["sessions"]] == [
        "https://claude.ai/code/session_L1",
        "https://claude.ai/code/session_L2",
    ]
    assert lead["stalls"][1]["at"] - lead["stalls"][0]["at"] >= 1800


async def test_a_relaunch_on_a_stale_lead_branch_is_refused(store, dirs, playbook):
    """The base moved on while the lead was silent: the relaunch is refused like
    a launch, released with the note, and no second session starts."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    runner.behind = [3, 151]

    await _lead_dispatcher(runner, launch, session_end=_ended).run(run_ref, workspace_id=WS)

    assert len(_launch_briefs(runner)) == 1 and len(_rev_lists(runner)) == 2
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason.startswith(f"Belt: lead/{FEATURE} is 151 commits behind dev")
    assert "headless_error" not in action.parameters["_code_change"]


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
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert action.parameters["_code_change"]["lead"]["liveness"] == "live"


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


async def test_a_lead_quiet_before_the_timeout_is_nudged_until_the_timeout(store, dirs, playbook):
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch)
    runner.commits = [runner.wall() + 60 * i for i in range(5 * 60)]  # busy 5 h, then quiet

    await disp.run(run_ref, workspace_id=WS)

    # Quiet at 5 h gets warning rungs; with no proof its session ended, only
    # the 8 h budget releases it.
    assert 5 <= len(_nudges(runner)) <= 6 and len(_launch_briefs(runner)) == 1
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert action.rejected_reason.startswith("feature lead not done after 8h")


def _never_a_failure(action) -> None:
    """A timed-out lead never counts toward the planner's circuit breaker,
    which counts only ``lead_planner.FAILED`` runs."""
    cc = action.parameters["_code_change"]
    assert action.status.value not in lead_planner.FAILED
    assert "headless_error" not in cc and cc["lead"]["stalls"][-1]["kind"] == "timeout"


def _closes(runner: LeadRunner) -> list[list[str]]:
    return [a for a, _ in runner.calls if a[:3] == ["gh", "issue", "close"]]


async def test_a_lead_whose_contact_sheet_is_up_at_the_timeout_closes_its_issue(
    store, dirs, playbook
):
    """#2556: a lead whose sheet is up is finished. It waits on dev CI (red
    here, for other lines' failures) with no nudge or relaunch, and at the
    timeout its issue is closed as completed and the run finished as done,
    never labelled ``needs-human`` (5 done features were parked that way).
    Mutation: dropping the sheet check labels it needs-human."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref, ci_by_sha={MERGE_SHA: [RED_RUN]})

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert not _nudges(runner) and len(_launch_briefs(runner)) == 1
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert "contact sheet" in str(action.outcome)
    (close,) = _closes(runner)
    assert close[3:] == ["93", "-R", SLUG, "--reason", "completed"]
    assert not _flagged(runner)
    _never_a_failure(action)


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
    assert [s["kind"] for s in lead["stalls"]][:2] == ["nudge", "nudge"]
    assert action.rejected_reason.startswith("feature lead not done after 8h")
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
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LEAD_REBASE_BEHIND", "7")
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LEAD_REFUSE_BEHIND", "9")
    seen: list[CloudTaskDispatcher] = []
    monkeypatch.setattr("pocketpaw_ee.cloud.belt.headless.set_production_dispatcher", seen.append)

    assert cloud_station.wire_cloud()

    (disp,) = seen
    assert disp.lead_timeout_seconds == 100 and disp.lead_stall_seconds == 50
    assert disp.lead_git_name == "Pat Lead"
    assert (disp.lead_rebase_behind, disp.lead_refuse_behind) == (7, 9)
    assert CloudTaskDispatcher().lead_timeout_seconds == 8 * 3600
    assert (CloudTaskDispatcher().lead_rebase_behind, CloudTaskDispatcher().lead_refuse_behind) == (
        40,
        150,
    )
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


async def test_resume_releases_an_errored_lead_the_planner_already_replaced(store, dirs, playbook):
    """#2554: an errored lead whose issue the planner gave a newer lead is
    released at startup, never tracked next to it, and never counted as a
    failed attempt against the issue's breaker."""
    repo, launch = dirs
    lead = {"feature": FEATURE, "packet": PACKET, "issue": 93, "branch": f"lead/{FEATURE}"}
    old = await _queue_lead(store, repo)
    await cloud_station._patch(
        store,
        old,
        headless_state=None,
        headless_error="TRACK: gh pr list failed (exit 1)",
        cloud={"session_url": CLOUD_URL, "launched_at": time.time() - 600},
        lead=lead,
    )
    import aiosqlite

    # SQLite stamps created_at to the second: make the old run an hour older.
    async with aiosqlite.connect(store._db_path) as db:
        sql = "UPDATE instinct_actions SET created_at = datetime('now', '-1 hour') WHERE id = ?"
        await db.execute(sql, (old,))
        await db.commit()
    new = await _queue_lead(store, repo)
    await cloud_station._patch(
        store,
        new,
        headless_state="cloud",
        cloud={"session_url": CLOUD_URL, "launched_at": time.time()},
        lead=dict(lead),
    )
    runner = _done_runner(new)
    disp = _lead_dispatcher(runner, launch, workspaces_for=_ws)
    tracked: list[str] = []
    track = disp._track_lead

    async def spy(store_, ref, *a, **k):
        tracked.append(ref)
        if ref == new:
            await track(store_, ref, *a, **k)

    disp._track_lead = spy  # type: ignore[method-assign]

    assert await disp.resume_all() == 1

    assert tracked == [new]
    assert (await store.get_action(new)).status == ActionStatus.EXECUTED
    released = await store.get_action(old)
    assert released.status == ActionStatus.REJECTED
    assert not any(a[:3] == ["gh", "issue", "edit"] for a, _ in runner.calls)
    from pocketpaw_ee.cloud.mandates import lead_planner

    row = _run_summary(released, released.parameters["_code_change"])
    assert mandate_service._task_status(row) not in lead_planner.FAILED


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


async def test_activity_after_a_relaunch_resets_the_ladder(store, dirs, playbook):
    """A relaunched lead that then worked starts the ladder over on its next
    quiet window: a relaunch again, never the release meant for a relaunch
    that never showed a sign of work."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner()
    disp = _lead_dispatcher(runner, launch, session_end=_ended)
    start = runner.wall()
    # Silent to 30 min (a relaunch), one commit at 40 min, then silent for good.
    runner.commits = [None] * 40 + [start + 2400]

    await disp.run(run_ref, workspace_id=WS)

    lead = (await store.get_action(run_ref)).parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["relaunch", "relaunch", "needs-human"]
    assert lead["stalls"][1]["at"] >= start + 2400 + 1800  # a full quiet window after it


async def test_a_failed_nudge_never_leads_to_a_relaunch(store, dirs, playbook):
    """A nudge that exits non-zero ("session not found") is kept as the rung's
    error, but it is no proof the session ended: the next rung nudges again,
    and the lead is never relaunched beside a session that may still be live."""
    repo, launch = dirs
    run_ref = await _queue_lead(store, repo)
    runner = LeadRunner(nudge_codes=[1, 0])

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    stalls = action.parameters["_code_change"]["lead"]["stalls"]
    assert [s["kind"] for s in stalls][:2] == ["nudge", "nudge"]
    assert "session not found" in stalls[0]["error"] and "error" not in stalls[1]
    assert len(_launch_briefs(runner)) == 1  # never relaunched
    assert action.rejected_reason.startswith("feature lead not done after 8h")


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

    await _lead_dispatcher(runner, launch, session_end=_ended).run(run_ref, workspace_id=WS)

    lead = (await store.get_action(run_ref)).parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["relaunch", "needs-human"]
    assert all(s["at"] > 2.0 for s in lead["stalls"])  # none of the old run's
    assert len(_launch_briefs(runner)) == 2


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

    await _lead_dispatcher(runner, launch, session_end=_ended).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    lead = action.parameters["_code_change"]["lead"]
    assert [s["kind"] for s in lead["stalls"]] == ["relaunch", "needs-human"]


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


# -- v3: a feature is done slice by slice -----------------------------------
# A feature whose wave.json lists ``slices`` (creative-apps #1302, #1305) is done when
# every slice has a ``Belt-Slice-Done: <slice> <run url>`` line since the launch
# whose run is a completed, successful ci.yml run on the base whose head contains
# the slice's last merged PR (PRs map to slices by their ``Slice:`` line), and
# the contact sheet is up. Dev CI on the merge is no longer read. A feature with
# no slices keeps the v2 rule above, byte for byte.

WAVE = PACKET.removesuffix(".md") + ".wave.json"
# The feature's wave.json as creative-apps #1305 writes it: v3 lists ``slices``.
PLAIN = {"title": "Text shapes", "wave": 1, "status": "planned", "owned": ["Sources/A.swift"]}
SLICED = {
    **PLAIN,
    "slices": [
        {"id": "contract", "behavior": "protocols and fakes", "tasks": ["T-1"], "journeys": []},
        {"id": "S1", "tasks": ["T-2", "T-3"], "journeys": ["F-J4"], "waits": {"contract": ["T-2"]}},
    ],
}
CONTRACT_SHA, S1_SHA, RUN_HEAD = "1" * 40, "2" * 40, "9" * 40


def _wave(launch: Path, wave: dict | str = SLICED) -> None:
    path = launch / WAVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(wave if isinstance(wave, str) else json.dumps(wave))


def _run_url(run_id: int) -> str:
    return f"https://github.com/{SLUG}/actions/runs/{run_id}"


def _ci_run(run_id: int, head: str = RUN_HEAD, **over) -> dict:
    row = {
        "id": run_id,
        "path": ".github/workflows/ci.yml",
        "head_branch": "dev",
        "head_sha": head,
        "status": "completed",
        "conclusion": "success",
        "html_url": _run_url(run_id),
    }
    return row | over


def _slice_done(slice_id: str, run_id: int, at: float, tail: str = "") -> dict:
    body = f"Slice {slice_id} is done.\nBelt-Slice-Done: {slice_id} {_run_url(run_id)}{tail}"
    return {"createdAt": _iso(at), "body": body}


def _sliced_runner(run_ref: str, *, sheet: bool = True) -> LeadRunner:
    """A v3 lead done at the first poll: contract (#41) and S1 (#42, the
    ``feature-done`` one) merged, a slice-done line for each naming a green
    ci.yml run on dev that contains its PR, and the contact sheet. Dev CI on the
    merges is red, which no longer matters."""
    future = time.time() + 3600
    prs = [
        _lead_pr(
            41,
            run_ref,
            state="MERGED",
            sha=CONTRACT_SHA,
            slices="contract",
            merged="2026-10-16T10:00:00Z",
        ),
        _lead_pr(
            42,
            run_ref,
            state="MERGED",
            sha=S1_SHA,
            slices="S1",
            done=True,
            merged="2026-10-16T11:00:00Z",
        ),
    ]
    said = [_slice_done("contract", 901, future), _slice_done("S1", 902, future)]
    runner = LeadRunner(prs=[prs], comments=[said + ([_sheet(future)] if sheet else [])])
    runner.ci = [[RED_RUN]]
    runner.runs = {"901": _ci_run(901, CONTRACT_SHA), "902": _ci_run(902)}
    runner.contains = {f"{S1_SHA}...{RUN_HEAD}": "ahead"}
    return runner


def _issue_views(runner: LeadRunner) -> int:
    return sum(a[:3] == ["gh", "issue", "view"] for a, _ in runner.calls)


async def test_a_v3_lead_is_done_when_every_slice_is_done_and_the_sheet_is_up(
    store, dirs, playbook
):
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    cc = action.parameters["_code_change"]
    lead = cc["lead"]
    assert lead["slice_ids"] == ["contract", "S1"]
    assert [(s["id"], s["done"], s["run"], s["gaps"], s["pr"]) for s in lead["slices"]] == [
        ("contract", True, _run_url(901), [], 41),
        ("S1", True, _run_url(902), [], 42),
    ]
    assert [p["slices"] for p in lead["prs"]] == [["contract"], ["S1"]]
    assert cc["pr_url"].endswith("/pull/42") and cc["commit_sha"] == S1_SHA
    assert "2 slice(s) done (0 with gaps)" in str(action.outcome)
    # Red dev CI on the merges no longer holds a v3 feature: it is never read.
    assert not any(a[:3] == ["gh", "run", "list"] and "--commit" in a for a, _ in runner.calls)


async def test_a_v3_lead_waits_for_every_slice_and_the_sheet(store, dirs, playbook):
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)
    future = time.time() + 3600
    contract, s1 = _slice_done("contract", 901, future), _slice_done("S1", 902, future)
    runner.comments = [[contract, _sheet(future)], [contract, s1], [contract, s1, _sheet(future)]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert _issue_views(runner) == 3  # no S1 line, then no sheet, then done
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_a_v3_lead_with_every_slice_done_and_no_sheet_at_the_timeout_is_asked_for_it(
    store, dirs, playbook
):
    """#2556: every slice has its ``Belt-Slice-Done:`` line but no sheet is up
    at the timeout. One note asks for the sheet; the issue stays open and is
    never labelled ``needs-human``; the run is released. Mutation: reading one
    slice's line as enough here posts the note for the partial lead below."""
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref, sheet=False)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert "contact sheet" in (action.rejected_reason or "")
    (note,) = [a[-1] for a in _writes(runner) if a[2] == "comment"]
    assert note.splitlines()[0].startswith("Belt: every slice") and "Belt-Contact-Sheet:" in note
    reqs = action.parameters["_code_change"]["requests"]
    (rid,) = [k for k, r in reqs.items() if r["kind"] == "note"]
    assert f"<!-- belt-request: {rid} -->" in note
    assert not _flagged(runner) and not _closes(runner)
    _never_a_failure(action)


async def test_a_v3_lead_with_a_slice_left_at_the_timeout_is_labelled_needs_human(
    store, dirs, playbook
):
    """#2556: a lead that is not done (S1 has no ``Belt-Slice-Done:`` line, no
    sheet) keeps today's timeout: ``needs-human`` and released, no note, no close."""
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref, sheet=False)
    runner.comments = [[_slice_done("contract", 901, time.time() + 3600)]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.REJECTED
    assert (action.rejected_reason or "").startswith("feature lead not done after 8h")
    assert _flagged(runner) and not _closes(runner)
    assert not [a for a in _writes(runner) if a[2] == "comment"]
    _never_a_failure(action)


async def test_a_slice_done_line_from_before_the_launch_does_not_count(store, dirs, playbook):
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)
    future = time.time() + 3600
    old = _slice_done("S1", 902, time.time() - 86400)  # another run's line
    first = [_slice_done("contract", 901, future), old, _sheet(future)]
    runner.comments = [first, [*first, _slice_done("S1", 902, future)]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert _issue_views(runner) == 2
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


@pytest.mark.parametrize(
    "bad",
    [
        {"conclusion": "failure"},
        {"conclusion": "skipped"},  # a skipped run ran no journeys
        {"status": "in_progress", "conclusion": None},
        {"path": ".github/workflows/fast-lane.yml"},  # not ci.yml
        {"head_branch": "lead/text-shapes"},  # not a run of the base
        {"head_sha": "8" * 40},  # its head lacks the slice's last PR
    ],
    ids=["red", "skipped", "running", "other-workflow", "other-branch", "lacks-the-pr"],
)
async def test_a_slice_done_whose_run_does_not_hold_up_does_not_count(store, dirs, playbook, bad):
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)
    future = time.time() + 3600
    runner.runs["903"] = _ci_run(903) | bad
    first = [_slice_done("contract", 901, future), _slice_done("S1", 903, future), _sheet(future)]
    # The lead posts a fresh line naming a good run: the newest line per slice wins.
    runner.comments = [first, [*first, _slice_done("S1", 902, future + 1)]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert _issue_views(runner) == 2
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    s1 = action.parameters["_code_change"]["lead"]["slices"][1]
    assert s1["run"] == _run_url(902)


async def test_a_later_pr_with_no_slice_line_is_no_slice_s_last_pr(store, dirs, playbook):
    # #43 merged last but names no slice: S1's last PR is still #42, which the
    # run holds, so the lead lands at the first poll.
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)
    other = _lead_pr(43, run_ref, state="MERGED", sha="3" * 40, merged="2026-10-16T13:00:00Z")
    runner.prs = [[*runner.prs[0], other]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert _issue_views(runner) == 1
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_a_later_slice_pr_needs_a_newer_slice_done(store, dirs, playbook):
    # Both slices verified at poll 1 (no sheet yet); #44 then merges for both,
    # so the kept verdicts no longer hold until lines naming a run with #44.
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref, sheet=False)
    prs = runner.prs[0]
    fix = _lead_pr(
        44,
        run_ref,
        state="MERGED",
        sha="4" * 40,
        slices="S1, contract",
        merged="2026-10-16T12:00:00Z",
    )
    runner.prs = [prs, [*prs, fix]]
    later = "7" * 40
    runner.runs["904"] = _ci_run(904, later)
    runner.contains[f"{'4' * 40}...{later}"] = "ahead"
    said = [*runner.comments[0], _sheet(time.time() + 3600)]
    future = time.time() + 7200
    newer = [_slice_done("contract", 904, future), _slice_done("S1", 904, future)]
    runner.comments = [runner.comments[0], said, [*said, *newer]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert _issue_views(runner) == 3
    lead = (await store.get_action(run_ref)).parameters["_code_change"]["lead"]
    assert [(s["pr"], s["run"]) for s in lead["slices"]] == [(44, _run_url(904))] * 2


async def test_a_newer_slice_done_naming_a_red_run_undoes_the_slice(store, dirs, playbook):
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref, sheet=False)
    runner.runs["903"] = _ci_run(903, conclusion="failure")
    said, at = runner.comments[0], time.time() + 7200
    red = [*said, _slice_done("S1", 903, at), _sheet(at)]
    runner.comments = [said, red, [*red, _slice_done("S1", 902, at + 1)]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert _issue_views(runner) == 3


async def test_a_slice_done_with_gaps_is_done_and_counted(store, dirs, playbook):
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)
    future = time.time() + 3600
    runner.comments = [
        [
            _slice_done("contract", 901, future),
            _slice_done("S1", 902, future, " gaps: #77 #78"),
            _sheet(future),
        ]
    ]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    slices = action.parameters["_code_change"]["lead"]["slices"]
    assert [s["gaps"] for s in slices] == [[], [77, 78]]
    assert sum(bool(s["done"] and s["gaps"]) for s in slices) == 1
    assert "1 with gaps" in str(action.outcome)


async def test_done_and_slice_done_comments_become_typed_records(store, dirs, playbook):
    """Each slice-done line and the contact sheet land on ``lead.records``:
    outcome, summary, files, report url and the comment id they came from."""
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)
    future = time.time() + 3600
    s1 = _slice_done("S1", 902, future, " gaps: #77")
    spec = f"https://github.com/{SLUG}/blob/dev/docs/shapes.md"  # a link, not a file
    s1["body"] = f"Shapes render.\nTouched `Sources/Flare/Shape.swift`, see {spec}\n" + s1["body"]
    sheet = {**_sheet(future), "id": "IC_sheet"}
    runner.comments = [
        [{**_slice_done("contract", 901, future), "id": "IC_c"}, {**s1, "id": "IC_s1"}, sheet]
    ]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    records = (await store.get_action(run_ref)).parameters["_code_change"]["lead"]["records"]
    by = {r.get("slice", r["kind"]): r for r in records}
    assert set(by) == {"contract", "S1", "done"}
    assert by["S1"] == {
        "kind": "slice",
        "slice": "S1",
        "outcome": "done with gaps",
        "report_url": _run_url(902),
        "summary": (
            f"Shapes render. Touched `Sources/Flare/Shape.swift`, see {spec} Slice S1 is done."
        ),
        "files": ["Sources/Flare/Shape.swift"],
        "comment_id": "IC_s1",
    }
    assert by["contract"]["outcome"] == "done" and by["contract"]["comment_id"] == "IC_c"
    assert by["done"]["report_url"] == CLOUD_URL and by["done"]["comment_id"] == "IC_sheet"
    assert by["done"]["files"] == []


async def test_a_done_slice_run_is_not_read_again(store, dirs, playbook):
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref, sheet=False)
    said = runner.comments[0]
    runner.comments = [said] * 5 + [[*said, _sheet(time.time() + 3600)]]

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert _issue_views(runner) == 6
    reads = [a for a, _ in runner.calls if a[:2] == ["gh", "api"] and "/actions/runs/" in a[2]]
    assert len(reads) == 2  # one per slice, then the verdict is kept


@pytest.mark.parametrize("wave", [None, PLAIN, "{not json"], ids=["none", "no-slices", "bad"])
async def test_a_feature_without_slices_keeps_the_v2_rule(store, dirs, playbook, wave):
    # The live v2 leads: slice-done lines and Slice: lines change nothing; the
    # feature-done PR's merge on green dev CI and the sheet still decide.
    repo, launch = dirs
    if wave is not None:
        _wave(launch, wave)
    run_ref = await _queue_lead(store, repo)
    runner = _done_runner(run_ref)
    future = time.time() + 3600
    runner.comments = [[_slice_done("S1", 902, future), _sheet(future)]]
    runner.runs = {"902": _ci_run(902, MERGE_SHA)}
    runner.prs = [[{**p, "body": f"{p['body']}Slice: S1\n"} for p in runner.prs[0]]]
    runner.ci = [[RED_RUN], [GREEN_RUN]]  # dev CI on the feature-done merge still decides

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    lead = action.parameters["_code_change"]["lead"]
    assert lead["slice_ids"] == [] and "slices" not in lead
    assert sum(a[:3] == ["gh", "run", "list"] and "--commit" in a for a, _ in runner.calls) == 2
    assert not any("/actions/runs/" in " ".join(a) for a, _ in runner.calls)
    assert "slice" not in str(action.outcome)
    (brief,) = _launch_briefs(runner)
    assert V2_DONE in brief and V2_LOOP in brief and "Belt-Slice-Done" not in brief


V2_DONE = (
    "- Done: the last PR also carries the label `feature-done`. Once it has merged and the CI "
    "run it triggered on dev is green, comment on #93 with a line starting "
    "`Belt-Contact-Sheet:` and this run's session link.\n\n## Brief rules\n"
)
V2_LOOP = (
    "\n5. The Anatomy loop stops after 2 clean rounds or 4 rounds total; once a round is clean, "
    "add no new critics.\n6. One critic lens is first-use clarity"
)


async def test_a_lead_launched_before_slices_keeps_the_v2_rule(store, dirs, playbook):
    # A blob from before this change has no ``slice_ids``: a re-tracked running
    # lead keeps the rule it was briefed with, whatever its wave.json says now.
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    lead = {"feature": FEATURE, "packet": PACKET, "issue": 93, "branch": f"lead/{FEATURE}"}
    await cloud_station._patch(
        store,
        run_ref,
        headless_state="cloud",
        cloud={"session_url": CLOUD_URL, "launched_at": time.time()},
        lead=lead,
    )
    runner = _done_runner(run_ref)

    dispatcher = _lead_dispatcher(runner, launch, workspaces_for=_ws)
    assert await dispatcher.resume_all() == 1

    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    assert any(a[:3] == ["gh", "run", "list"] and "--commit" in a for a, _ in runner.calls)


async def test_a_v3_brief_describes_slices_and_never_waiting(store, dirs, playbook):
    repo, launch = dirs
    _wave(launch)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    (brief,) = _launch_briefs(runner)
    assert brief.startswith(PLAYBOOK.strip())
    assert "Slices: contract, S1" in brief
    assert "`Slice: <slice>`" in brief
    assert "Belt-Slice-Done: <slice> <run url>" in brief and "gaps: #<n>" in brief
    assert "Belt-Contact-Sheet:" in brief
    assert "never wait" in brief.lower() and "`finding`" in brief
    assert "2 fix rounds" in brief
    assert "2 clean rounds or 4 rounds total" not in brief  # the v2 loop
    assert "the CI run it triggered" not in brief  # the v2 done rule


@pytest.mark.parametrize("where", ["over-the-cap", "outside-the-clone"])
async def test_a_wave_json_over_the_cap_or_outside_the_clone_never_launches(
    store, dirs, playbook, tmp_path, where
):
    repo, launch = dirs
    if where == "over-the-cap":
        _wave(launch, json.dumps(SLICED) + " " * cloud_station._WAVE_BYTES)
    else:
        outside = tmp_path / "outside.wave.json"
        outside.write_text(json.dumps(SLICED))
        (launch / WAVE).parent.mkdir(parents=True, exist_ok=True)
        (launch / WAVE).symlink_to(outside)
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["headless_error"].startswith(f"LAUNCH: {WAVE}")
    assert not _launch_briefs(runner)


async def test_a_wave_json_with_an_unsafe_slice_id_never_launches(store, dirs, playbook):
    repo, launch = dirs
    _wave(launch, {**SLICED, "slices": [*SLICED["slices"], {"id": "S2; rm"}]})
    run_ref = await _queue_lead(store, repo)
    runner = _sliced_runner(run_ref)

    await _lead_dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert cc["headless_error"].startswith("LAUNCH:") and "slice" in cc["headless_error"]
    assert not _launch_briefs(runner)


def test_lead_run_row_carries_its_slices() -> None:
    slices = [
        {"id": "contract", "done": True, "run": _run_url(901), "gaps": [], "pr": 41},
        {"id": "S1", "done": False, "run": None, "gaps": [], "pr": None},
    ]
    blob = {"recipe": "feature-lead", "task": "Lead #93", "lead": {"packet": PACKET}}
    blob["lead"]["slices"] = slices
    lead = _run_summary(_row_action(), blob)["lead"]
    assert lead["slices"] == slices
    assert lead["app"] == "flare" and lead["prs"] == []  # the old fields stay
    queued = _run_summary(_row_action(), {"recipe": "feature-lead", "task": "Lead #7"})["lead"]
    assert queued["slices"] == []
