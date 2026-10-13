# tests/cloud/test_belt_cloud_quality.py — the cloud station's quality loop.
#
# REVIEW: with the reviewer on, a tracked PR (worker's, adopted or tester's) whose
# head has no verdict and whose CI is green (the head commit's own check runs when
# the PR rollup has none) gets ONE reviewer session, under the concurrent cap:
# ``belt/review`` goes pending, the brief asks for ONE comment ``Belt-Review:
# pass|fail`` + ``Head: <sha>`` and never a status or a push; Belt reads that
# comment (after the launch, for this head) and posts the status itself; a fail
# runs the fixer with the comment (kind ``review``, the shared cap) and the new
# head is reviewed again; no verdict by the timeout, no reviewer session, the run's
# own deadline, or a stale pending status nobody owns posts success ("review timed
# out"), counted on ``review_timeouts``; a resume never doubles a reviewer and
# settles one an ended tracker left pending; off by default.
# ORIENT: every worker and fixer brief carries an Orientation block built from the
# launch clone's docs/c4/model.json (at origin/<base>): the components the
# issue's "Files likely involved" or its title words match, their paths and
# public helpers (one ``git grep``), the reuse rule verbatim, capped at 2,500.
# LESSONS: a merged PR a ``ci``/``review`` fixer got through, and each bug a
# tester files on the line, become one deduplicated procedural lesson in the
# mandate's soul (real soul-protocol on a tmp ``.soul``), tagged repo and line;
# a worker brief recalls the top ones, fenced and capped at 1,200, and every
# worker/fixer brief says to check current docs with the repo's docs MCP tools.
# Same fakes as tests/cloud/test_belt_cloud_station.py (``FakeRunner`` extended
# with the gh calls this loop adds); nothing real is launched.

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import cloud_station, headless, orient  # noqa: E402
from pocketpaw_ee.cloud.mandates import soul_link  # noqa: E402

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from tests.cloud import test_belt_cloud_station as base  # noqa: E402
from tests.cloud.test_belt_cloud_station import (  # noqa: E402
    CLOUD_URL,
    MERGED,
    SESSION,
    SLUG,
    WS,
    FakeRunner,
    _briefs,
    _dispatcher,
    _launched,
    _open,
    _queue,
    _tester,
    _tester_pr,
)

# The cloud station suite's fixtures, shared as-is.
store, dirs, stages, updates = base.store, base.dirs, base.stages, base.updates

H1, H2 = "a1b2c3d4e5f6", "f6e5d4c3b2a1"


def _verdict(word: str, head: str, *notes: str) -> str:
    """A reviewer's verdict comment: ``Belt-Review: <word>``, ``Head: <head>``, notes."""
    return "\n".join([f"Belt-Review: {word}", f"Head: {head}", *notes])


REVIEW_NOTE = _verdict("fail", H1, "- Sources/FlareCore/Text.swift:12 duplicates Geometry.clamp")
PASSED = "Belt review passed"
TIMED_OUT = "review timed out"


class Runner(FakeRunner):
    """``FakeRunner`` plus the commit-status POSTs (kept on ``statuses``), the
    head commit's own check runs and statuses, the source issue's title and
    body, and ``git grep``'s output."""

    def __init__(
        self,
        *,
        issue: dict | None = None,
        grep: str = "",
        log: str = "",
        commits: list[dict] | None = None,
        bugs: list[dict] | None = None,
        head_runs: list[dict] | None = None,
        **kw,
    ):
        super().__init__(**kw)
        self.statuses: list[dict[str, str]] = []
        self.head_runs = head_runs or []  # ``gh api .../commits/<sha>/check-runs``
        self.issue = issue or {}
        self.grep = grep
        self.log = log  # ``gh run view --log-failed``
        self.commits = commits or []  # ``gh pr view --json commits``
        self.bugs = bugs or []  # ``gh issue list --label bug``

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        if argv[:2] == ["gh", "api"] and "/statuses/" in argv[2]:
            self.calls.append((list(argv), cwd))
            fields = dict(a.split("=", 1) for a in argv[3:] if "=" in a)
            self.statuses.append({"sha": argv[2].rsplit("/", 1)[-1], **fields})
            return 0, "{}", ""
        if argv[:2] == ["gh", "api"] and "/check-runs" in argv[2]:
            self.calls.append((list(argv), cwd))
            return 0, json.dumps({"total_count": 9, "check_runs": self.head_runs}), ""
        if argv[:2] == ["gh", "api"] and argv[2].endswith("/status"):
            self.calls.append((list(argv), cwd))
            return 0, json.dumps({"state": "pending", "statuses": []}), ""
        if argv[:3] == ["gh", "issue", "view"] and "title,body" in argv:
            self.calls.append((list(argv), cwd))
            return 0, json.dumps(self.issue), ""
        if "grep" in argv:
            self.calls.append((list(argv), cwd))
            return 0, self.grep, ""
        if argv[:3] == ["gh", "run", "view"] and self.log:
            self.calls.append((list(argv), cwd))
            return 0, self.log, ""
        if argv[:3] == ["gh", "pr", "view"] and argv[-1] == "commits":
            self.calls.append((list(argv), cwd))
            return 0, json.dumps({"commits": self.commits}), ""
        if argv[:3] == ["gh", "issue", "list"]:
            self.calls.append((list(argv), cwd))
            return 0, json.dumps(self.bugs), ""
        return await super().__call__(argv, cwd=cwd, timeout=timeout, stdin=stdin, on_line=on_line)


def _review(
    head: str, state: str | None = None, *, comments=(), started: str | None = None, **kw
) -> dict:
    """An open, green PR on ``head``; ``state`` adds its ``belt/review`` status,
    posted at ``started``."""
    pr = _open(head, **kw)
    if state:
        status = {"__typename": "StatusContext", "context": "belt/review", "state": state}
        if started:
            status["startedAt"] = started
        pr["statusCheckRollup"] = [*pr["statusCheckRollup"], status]
    pr["comments"] = [{"body": c, "createdAt": "2030-01-01T00:00:00Z"} for c in comments]
    return pr


def _reviewer(runner: FakeRunner, launch: Path, **kw):
    # Polls every 60 s; a stuck PR gets its fixer 100 s in; no lane nudge in reach.
    kw = {"poll_seconds": 60, "fix_grace_seconds": 100, "lane_nudge_seconds": 10**9, **kw}
    return _dispatcher(runner, launch, **{"reviewer": True, **kw})


def _status(state: str, description: str, sha: str = H1) -> dict:
    return {"sha": sha, "state": state, "context": "belt/review", "description": description}


RUNNING = _status("pending", "Belt review running")


async def _cc(store, run_ref: str) -> dict:
    return (await store.get_action(run_ref)).parameters["_code_change"]


async def test_a_green_pr_gets_one_reviewer_and_belt_posts_its_pass(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    passed = _review(H1, "PENDING", comments=[_verdict("pass", H1)])
    seq = [_review(H1), _review(H1, "PENDING"), passed, MERGED]
    runner = Runner(launch_out=SESSION, pr=seq)
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    briefs = _briefs(runner)
    assert len(briefs) == 2  # the worker, then one reviewer
    brief = briefs[1]
    assert "reviewer" in brief and "#9" in brief and H1 in brief
    assert f"gh pr diff 9 -R {SLUG}" in brief and "#93" in brief
    for doc in ("docs/review-rubric.md", "docs/engineering.md", "docs/design.md"):
        assert doc in brief
    # The reviewer reports through ONE comment, never a status.
    assert f"gh pr comment 9 -R {SLUG}" in brief
    assert "`Belt-Review: pass`" in brief and "`Belt-Review: fail`" in brief
    assert f"`Head: {H1}`" in brief and "file:line" in brief
    assert "statuses" not in brief and "gh api" not in brief
    assert "never post a commit status" in brief.lower() and "never push" in brief.lower()
    # Belt posts both statuses with its own gh: pending at launch, then the verdict.
    assert runner.statuses == [RUNNING, _status("success", PASSED)]
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    (review,) = action.parameters["_code_change"]["reviews"]
    assert review["sha"] == H1 and review["verdict"] == "pass"
    assert review["session_url"] == CLOUD_URL and review["at"] <= time.time()
    assert "review" in stages


async def test_a_failed_review_runs_a_fixer_then_reviews_the_new_head(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    said = _review(H1, "PENDING", comments=["Looks off.", REVIEW_NOTE])
    failed = _review(H1, "FAILURE", comments=["Looks off.", REVIEW_NOTE])
    passed = _review(H2, "PENDING", comments=[REVIEW_NOTE, _verdict("pass", H2)])
    seq = [_review(H1), said, failed, failed, failed, _review(H2), passed, MERGED]
    runner = Runner(launch_out=SESSION, pr=seq)
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    briefs = _briefs(runner)
    assert len(briefs) == 4  # worker, reviewer, fixer, reviewer of the new head
    fix = briefs[2]
    assert "review" in fix.lower() and "`claude/g41-text`" in fix
    assert "<untrusted>" in fix and "Sources/FlareCore/Text.swift:12" in fix
    assert "Looks off." not in fix  # only the marked review comment
    assert "SAME branch" in fix and "Never merge" in fix
    assert H2 in briefs[3]
    cc = await _cc(store, run_ref)
    assert [f["kind"] for f in cc["fixes"]] == ["review"]
    assert [(r["sha"], r["verdict"]) for r in cc["reviews"]] == [(H1, "fail"), (H2, "pass")]
    assert [(s["sha"], s["state"]) for s in runner.statuses] == [
        (H1, "pending"),
        (H1, "failure"),  # Belt posts the fail it read off the comment
        (H2, "pending"),
        (H2, "success"),
    ]


async def test_a_verdict_counts_only_for_this_head_after_the_launch(store, dirs, stages):
    """Ignored: a comment from before the launch, one naming another head, and
    one whose first line is not exactly the verdict; so the review times out."""
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    pr = _review(
        H1, "PENDING", comments=[_verdict("pass", H2), f"Belt-Review: pass, mostly\nHead: {H1}"]
    )
    pr["comments"].insert(0, {"body": _verdict("pass", H1), "createdAt": "2020-01-01T00:00:00Z"})
    runner = Runner(launch_out=SESSION, pr=[_review(H1), pr, pr, pr, MERGED])
    runner.run_ref = run_ref

    await _reviewer(runner, launch, review_timeout_seconds=150).run(run_ref, workspace_id=WS)

    assert runner.statuses == [RUNNING, _status("success", TIMED_OUT)]
    cc = await _cc(store, run_ref)
    assert [r["verdict"] for r in cc["reviews"]] == ["timeout"]


async def test_the_review_fixer_shares_the_fix_cap(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    seq = [_review(H1), *[_review(H1, "FAILURE", comments=[REVIEW_NOTE])] * 3]
    seq += [_review(H2), *[_review(H2, "FAILURE", comments=[_verdict("fail", H2, "- A:1")])] * 4]
    runner = Runner(launch_out=SESSION, pr=seq)
    runner.run_ref = run_ref

    await _reviewer(runner, launch, fix_attempts=1).run(run_ref, workspace_id=WS)

    cc = await _cc(store, run_ref)
    assert [f["kind"] for f in cc["fixes"]] == ["review"]
    assert "1 fixer" in cc["summary"] and "review" in cc["summary"]
    assert (await store.get_action(run_ref)).status == ActionStatus.PENDING


async def test_a_silent_reviewer_times_out_to_success(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    seq = [_review(H1), *[_review(H1, "PENDING")] * 3, MERGED]
    runner = Runner(launch_out=SESSION, pr=seq)
    runner.run_ref = run_ref

    await _reviewer(runner, launch, review_timeout_seconds=150).run(run_ref, workspace_id=WS)

    assert runner.statuses == [RUNNING, _status("success", TIMED_OUT)]
    cc = await _cc(store, run_ref)
    assert [r["verdict"] for r in cc["reviews"]] == ["timeout"]
    assert cc["review_timeouts"] == 1  # counted on the blob
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_the_runs_own_deadline_settles_a_pending_review(store, dirs, stages):
    """#163: the reviewer launched minutes before the run's TRACK deadline; the
    tracker ended with ``belt/review`` pending and nothing ever replaced it."""
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = Runner(launch_out=SESSION, pr=[_review(H1), _review(H1, "PENDING")])
    runner.run_ref = run_ref

    await _reviewer(runner, launch, timeout_seconds=150).run(run_ref, workspace_id=WS)

    assert runner.statuses == [RUNNING, _status("success", TIMED_OUT)]
    cc = await _cc(store, run_ref)
    assert cc["headless_error"].startswith("TRACK: PR #9 unresolved")
    assert [r["verdict"] for r in cc["reviews"]] == ["timeout"]


async def test_a_stale_pending_status_nobody_owns_is_replaced(store, dirs, stages):
    """A pending ``belt/review`` older than the timeout, with no review entry here
    (an ended tracker's): Belt replaces it with success and launches no reviewer."""
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    stale = _review(H1, "PENDING", started="2020-01-01T00:00:00Z")
    runner = Runner(launch_out=SESSION, pr=[stale, MERGED])
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1 and runner.statuses == [_status("success", TIMED_OUT)]
    cc = await _cc(store, run_ref)
    assert [(r["sha"], r["verdict"]) for r in cc["reviews"]] == [(H1, "timeout")]
    assert cc["review_timeouts"] == 1


async def test_a_live_pending_status_is_adopted_not_doubled(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    now = datetime.now(UTC).isoformat()
    live = _review(H1, "PENDING", started=now)
    passed = _review(H1, "PENDING", started=now, comments=[_verdict("pass", H1)])
    runner = Runner(launch_out=SESSION, pr=[live, passed, MERGED])
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1  # no second reviewer for the head
    assert runner.statuses == [_status("success", PASSED)]
    assert [r["verdict"] for r in (await _cc(store, run_ref))["reviews"]] == ["pass"]


async def test_a_reviewer_that_does_not_start_never_holds_the_line(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = Runner(launch_out=[SESSION, "Error: rate limited"], pr=[_review(H1), MERGED])
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert [s["state"] for s in runner.statuses] == ["pending", "success"]
    (review,) = (await _cc(store, run_ref))["reviews"]
    assert review["verdict"] == "error" and "no cloud session started" in review["error"]


async def test_the_reviewer_is_off_by_default(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = Runner(launch_out=SESSION, pr=[_review(H1), MERGED])
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1 and runner.statuses == []
    assert "reviews" not in await _cc(store, run_ref)


@pytest.mark.parametrize(
    "head_runs",
    [[], [{"name": "Linux", "status": "in_progress", "conclusion": None}]],
    ids=["no CI on the head yet", "CI still running on the head"],
)
async def test_no_review_before_ci_reports_or_while_it_is_red(store, dirs, stages, head_runs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    empty = {**_review(H1), "statusCheckRollup": []}
    seq = [empty, empty, _review(H1, red=True), MERGED]
    runner = Runner(launch_out=SESSION, pr=seq, head_runs=head_runs)
    runner.run_ref = run_ref

    await _reviewer(runner, launch, fix_grace_seconds=10**9).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1 and runner.statuses == []


async def test_a_green_pr_with_an_empty_rollup_is_reviewed(store, dirs, stages):
    """#174: CI the lane dispatched on a refreshed branch has no PR association,
    and the PR-event run is parked ``action_required``, so the PR rollup is empty
    while the head commit's own check runs are green, as the lane reads them."""
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    bare = {**_review(H1), "statusCheckRollup": []}
    runs = [
        {
            "name": "Linux",
            "status": "completed",
            "conclusion": "failure",
            "started_at": "2026-10-12T15:00:00Z",
        },
        {
            "name": "Linux",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-12T15:03:20Z",
        },
        {
            "name": "macOS",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-12T15:03:28Z",
        },
        {
            "name": "CI",
            "status": "completed",
            "conclusion": "action_required",
            "started_at": "2026-10-12T15:03:09Z",
        },
    ]
    passed = {
        **bare,
        "comments": [{"body": _verdict("pass", H1), "createdAt": "2030-01-01T00:00:00Z"}],
    }
    runner = Runner(launch_out=SESSION, pr=[bare, passed, MERGED], head_runs=runs)
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 2  # the worker, then the reviewer
    assert runner.statuses == [RUNNING, _status("success", PASSED)]
    api = [a[2] for a, _ in runner.calls if a[:2] == ["gh", "api"]]
    assert f"repos/{SLUG}/commits/{H1}/check-runs?per_page=100" in api


async def test_reviewers_wait_for_a_free_slot(store, dirs, stages):
    """At most ``review_cap`` reviewers in flight: a green head waits while the
    slots are held, and launches once one frees (here, a review past its due)."""
    repo, launch = dirs
    held = await _queue(store, repo)
    runner = Runner(launch_out=SESSION, pr=[_review(H1)] * 3 + [MERGED])
    runner.run_ref = held
    d = _reviewer(runner, launch)
    assert d.review_cap == 2
    d._reviewing.update({"a" * 12: 10.0**9, "b" * 12: 10.0**9})

    await d.run(held, workspace_id=WS)

    assert len(_briefs(runner)) == 1 and runner.statuses == []

    freed = await _queue(store, repo, plan_action_id="plan-2")
    passed = _review(H1, "PENDING", comments=[_verdict("pass", H1)])
    runner = Runner(launch_out=SESSION, pr=[_review(H1)] * 3 + [passed, MERGED])
    runner.run_ref = freed
    d = _reviewer(runner, launch)
    d._reviewing.update({"a" * 12: 10.0**9, "b" * 12: 90.0})  # due at t=90

    await d.run(freed, workspace_id=WS)

    assert len(_briefs(runner)) == 2 and runner.statuses == [RUNNING, _status("success", PASSED)]
    assert H1 not in d._reviewing  # its slot is free again once the verdict is in


async def test_a_resume_never_doubles_a_reviewer(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time())
    pending = {"sha": H1, "verdict": "pending", "session_url": CLOUD_URL, "at": time.time()}
    await cloud_station._patch(store, run_ref, reviews=[pending])
    passed = _review(H1, "PENDING", comments=[_verdict("pass", H1)])
    runner = Runner(launch_out=SESSION, pr=[_review(H1, "PENDING"), passed, MERGED])
    runner.run_ref = run_ref

    async def workspaces() -> list[str]:
        return [WS]

    await _reviewer(runner, launch, workspaces_for=workspaces).resume_all()

    assert not _briefs(runner) and runner.statuses == [_status("success", PASSED)]
    assert [r["verdict"] for r in (await _cc(store, run_ref))["reviews"]] == ["pass"]


async def test_a_resume_settles_a_review_an_ended_tracker_left_pending(store, dirs, stages):
    """#163's run: its tracker hit the TRACK deadline with the review pending, so
    it is not resumable. The resume settles that review from one look at the PR,
    and ignores the old-format ``Belt-Review: <sha>`` comment on it."""
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    pending = {"sha": H1, "verdict": "pending", "session_url": CLOUD_URL, "at": time.time() - 9000}
    await cloud_station._patch(
        store,
        run_ref,
        cloud={"session_url": CLOUD_URL, "launched_at": time.time() - 20000, "pr": 9},
        reviews=[pending],
        headless_error="TRACK: PR #9 unresolved after 14400s",
    )
    old = _review(
        H1, "PENDING", started="2026-10-12T15:44:42Z", comments=[f"Belt-Review: {H1}\n- A:1"]
    )
    runner = Runner(launch_out=SESSION, pr=[old])
    runner.run_ref = run_ref

    async def workspaces() -> list[str]:
        return [WS]

    assert await _reviewer(runner, launch, workspaces_for=workspaces).resume_all() == 0

    assert not _briefs(runner) and runner.statuses == [_status("success", TIMED_OUT)]
    cc = await _cc(store, run_ref)
    assert [r["verdict"] for r in cc["reviews"]] == ["timeout"] and cc["review_timeouts"] == 1
    assert cc["headless_error"].startswith("TRACK:")  # still a failed run, not re-tracked


async def test_the_tester_pr_is_reviewed_too(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    green = _tester_pr(head=H2)
    said = {
        **green,
        "comments": [{"body": _verdict("pass", H2), "createdAt": "2030-01-01T00:00:00Z"}],
    }
    runner = Runner(
        launch_out=SESSION, pr=MERGED, tester_pr=[green, said, {**said, "state": "MERGED"}]
    )
    runner.run_ref = run_ref
    d = _tester(runner, launch, poll_seconds=60, lane_nudge_seconds=10**9)
    d.reviewer = True

    await d.run(run_ref, workspace_id=WS)

    briefs = _briefs(runner)
    assert len(briefs) == 3 and "#20" in briefs[2] and "reviewer" in briefs[2]
    cc = await _cc(store, run_ref)
    assert [r["verdict"] for r in cc["tester"]["reviews"]] == ["pass"]
    assert "reviews" not in cc
    assert ("landed", "review") in updates


def test_wire_cloud_reads_the_reviewer_env(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", None)
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR", str(tmp_path))
    for var in ("REVIEWER", "REVIEW_TIMEOUT", "REVIEW_CAP"):
        monkeypatch.delenv(f"POCKETPAW_FACTORY_CLOUD_{var}", raising=False)
    assert cloud_station.wire_cloud() is True
    d = headless.resolve_headless_dispatcher()
    assert (d.reviewer, d.review_timeout_seconds, d.review_cap) == (False, 1800.0, 2)
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_REVIEWER", "1")
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_REVIEW_TIMEOUT", "600")
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_REVIEW_CAP", "5")
    assert cloud_station.wire_cloud() is True
    d = headless.resolve_headless_dispatcher()
    assert (d.reviewer, d.review_timeout_seconds, d.review_cap) == (True, 600.0, 5)


# -- orientation: the C4 components a task touches, and what they already offer --

MODEL = {
    "scope": "creative-core",
    "model": {
        "systems": [
            {
                "id": "creative-core",
                "containers": [
                    {
                        "id": "flarecore",
                        "name": "FlareCore library",
                        "components": [
                            {
                                "id": "document-commands",
                                "name": "Document and Command Engine",
                                "description": "The document and its undoable commands. More.",
                                "paths": [
                                    "Sources/FlareCore/Document.swift",
                                    "Sources/FlareCore/CommandRegistry.swift",
                                ],
                            },
                            {
                                "id": "timeline",
                                "name": "Timeline",
                                "description": "Keyframes and easing.",
                                "paths": ["Sources/FlareCore/Timeline/"],
                            },
                            {
                                "id": "geometry",
                                "name": "Geometry",
                                "description": "Points and paths.",
                                "paths": ["Sources/FlareCore/Geometry.swift"],
                            },
                        ],
                    }
                ],
            },
            {"id": "grdb", "containers": []},
        ]
    },
}
ISSUE = {
    "title": "G41: Text shapes on the timeline",
    "body": "**Behavior**\nText.\n\n**Files likely involved**\n"
    "`Sources/FlareCore/Document.swift`, new `Sources/FlareCore/TextCommands.swift`, "
    "`../escape.swift`.\n\n**Size:** M. Mentions `Sources/FlareCore/Geometry.swift` later.",
}
GREP = (
    "Sources/FlareCore/Document.swift:10:public struct Document {\n"
    "Sources/FlareCore/Document.swift:40:    public func layer(id: UUID) -> Layer?\n"
    "Sources/FlareCore/Timeline/Easing.swift:5:public enum Easing {\n"
)
REUSE = (
    "Reuse these before writing anything new. If what you need exists, call it. If you add "
    "a new component or file, add it to docs/c4/model.json in the same PR."
)


def _clone(root: Path, model: dict | None = MODEL) -> Path:
    if model is not None:
        (root / "docs" / "c4").mkdir(parents=True, exist_ok=True)
        (root / "docs" / "c4" / "model.json").write_text(json.dumps(model))
    return root


async def test_reuse_block_names_the_matched_components_and_their_helpers(tmp_path):
    seen: list[list[str]] = []

    async def run(argv, *, cwd, timeout):
        seen.append(list(argv))
        assert cwd == tmp_path
        return 0, GREP, ""

    block = await orient.reuse_block(run, _clone(tmp_path), ISSUE["title"], ISSUE["body"])

    assert block.startswith("ORIENTATION")
    # Document.swift (a listed file) and "timeline" (a title word) match; Geometry
    # sits outside the Files section and its name is not in the title.
    assert "Document and Command Engine" in block and "Timeline" in block
    assert "Geometry" not in block
    assert "Sources/FlareCore/Document.swift" in block and "Sources/FlareCore/Timeline/" in block
    assert "struct Document" in block and "func layer" in block and "enum Easing" in block
    assert block.endswith(REUSE)
    (argv,) = seen  # one grep, over the matched components' paths only
    assert "grep" in argv and "--" in argv
    specs = argv[argv.index("--") + 1 :]
    assert specs == [
        ":(glob)Sources/FlareCore/Document.swift",
        ":(glob)Sources/FlareCore/CommandRegistry.swift",
        ":(glob)Sources/FlareCore/Timeline/**",
    ]


async def test_reuse_block_is_capped_and_degrades_quietly(tmp_path):
    async def run(argv, *, cwd, timeout):
        return 0, "".join(f"Sources/C{i}.swift:1:public func helper{i}()\n" for i in range(400)), ""

    comps = [
        {
            "id": f"c{i}",
            "name": f"Timeline part {i}",
            "description": "x " * 80,
            "paths": [f"Sources/C{i}.swift"],
        }
        for i in range(200)
    ]
    big = {
        "scope": "s",
        "model": {"systems": [{"id": "s", "containers": [{"name": "C", "components": comps}]}]},
    }
    block = await orient.reuse_block(run, _clone(tmp_path / "a", big), "timeline", "")
    assert len(block) <= 2500 and block.endswith(REUSE) and "more not shown" in block

    async def broken(argv, *, cwd, timeout):
        raise OSError("no git")

    block = await orient.reuse_block(broken, _clone(tmp_path / "b"), ISSUE["title"], ISSUE["body"])
    assert "Document and Command Engine" in block and "public:" not in block  # grep lost only
    assert await orient.reuse_block(broken, _clone(tmp_path / "c", None), "timeline", "") == ""
    nothing = await orient.reuse_block(broken, _clone(tmp_path / "d"), "Unrelated", "")
    assert "no component" in nothing and nothing.endswith(REUSE)


async def test_worker_and_fixer_briefs_carry_the_orientation(store, dirs, stages):
    repo, launch = dirs
    _clone(launch)
    run_ref = await _queue(store, repo)
    stuck = _open(mergeable="CONFLICTING")
    runner = Runner(
        launch_out=SESSION, pr=[stuck, stuck, stuck, stuck, MERGED], issue=ISSUE, grep=GREP
    )
    runner.run_ref = run_ref

    await _reviewer(runner, launch, reviewer=False).run(run_ref, workspace_id=WS)

    worker, fixer = _briefs(runner)
    for brief in (worker, fixer):
        assert "ORIENTATION" in brief and "struct Document" in brief and REUSE in brief
    view = next(
        a for a, _ in runner.calls if a[:3] == ["gh", "issue", "view"] and "title,body" in a
    )
    assert view[3:6] == ["93", "-R", SLUG]


# -- lessons: what the line learned, in the mandate's soul -------------------

soul_protocol = pytest.importorskip("soul_protocol")
DOCS_LINE = (
    "Before using any platform or library API (SwiftUI, AppKit, macOS 26 APIs, GRDB, Swift "
    "stdlib), check current docs with the repo's docs MCP tools (see .mcp.json) instead of "
    "relying on memory."
)
CI_LOG = (
    "build\tCompile\t2030-01-01T00:00:00Z Compiling FlareCore\n"
    "build\tCompile\t2030-01-01T00:00:01Z Commands.swift:88: error: cannot open existential "
    "'any Command' in Self.merged\n"
    "build\tCompile\t2030-01-01T00:00:02Z ** BUILD FAILED **\n"
)


@pytest.fixture
async def soul_file(tmp_path: Path) -> Path:
    path = tmp_path / "afterglow.soul"
    soul = await soul_protocol.Soul.birth("afterglow")
    await soul.export(path, include_keys=True)
    return path


def _lined(d, soul_file: Path, labels: list[str] | None = None):
    """``d`` whose mandate is the Flare line, bound to ``soul_file``."""

    async def charter(workspace_id: str, mandate_id: str) -> dict:
        charter = {"checks": ["make ci"], "issue_labels": labels or []}
        return {"repo": "x", "name": "Flare", "soul_path": str(soul_file), "charter": charter}

    d.charter_for = charter
    return d


async def _lessons(path: Path) -> list[str]:
    return await soul_link.recall_lessons(str(path), "G41 Text shapes creative-core", limit=10)


async def test_note_lesson_dedups_and_recall_reads_only_lessons(soul_file):
    lesson = "Lesson [qbtrix/creative-core · Flare]: ci failure on 'G41': error. Fixed by: X."
    assert await soul_link.remember_shift(str(soul_file), "Mandate shift 3: G41 done") is True
    assert await soul_link.note_lesson(str(soul_file), lesson) is True
    assert await soul_link.note_lesson(str(soul_file), lesson) is False  # a near-duplicate
    assert await _lessons(soul_file) == [lesson]  # the shift summary is not a lesson
    assert await soul_link.note_lesson(None, lesson) is False
    assert await soul_link.recall_lessons(None, "G41") == []
    assert soul_file.is_file()  # still an archive


async def test_a_merged_ci_fix_becomes_a_lesson(store, dirs, stages, soul_file):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    red = _open("h1", red=True)
    commits = [
        {"oid": "h1", "messageHeadline": "Add text shapes"},
        {"oid": "h2", "messageHeadline": "Open the existential before merging commands"},
    ]
    runner = Runner(
        launch_out=SESSION, pr=[red, red, red, red, MERGED], log=CI_LOG, commits=commits
    )
    runner.run_ref = run_ref

    await _lined(_reviewer(runner, launch, reviewer=False), soul_file).run(run_ref, workspace_id=WS)

    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED
    (lesson,) = await _lessons(soul_file)
    assert lesson.startswith(f"Lesson [{SLUG} · Flare]:")
    assert "G41: Text shapes" in lesson and "build (macos-14)" in lesson
    assert "cannot open existential" in lesson and "BUILD FAILED" not in lesson
    assert "Fixed by: Open the existential before merging commands" in lesson
    assert len(lesson) <= 400


async def test_a_merged_review_fix_becomes_a_lesson_and_a_rebase_does_not(
    store, dirs, stages, soul_file
):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    failed = _review(H1, "FAILURE", comments=[REVIEW_NOTE])
    commits = [{"oid": H1, "messageHeadline": "x"}, {"oid": H2, "messageHeadline": "Reuse clamp"}]
    seq = [_review(H1), failed, failed, failed, _review(H2), _review(H2, "SUCCESS"), MERGED]
    runner = Runner(launch_out=SESSION, pr=seq, commits=commits)
    runner.run_ref = run_ref

    await _lined(_reviewer(runner, launch), soul_file).run(run_ref, workspace_id=WS)

    (lesson,) = await _lessons(soul_file)
    assert "review" in lesson and "Text.swift:12 duplicates Geometry.clamp" in lesson
    assert "Fixed by: Reuse clamp" in lesson

    other = await _queue(store, repo)
    stuck = _open(mergeable="CONFLICTING")
    runner = Runner(launch_out=SESSION, pr=[stuck] * 4 + [MERGED], commits=commits)
    runner.run_ref = other
    await _lined(_reviewer(runner, launch, reviewer=False), soul_file).run(other, workspace_id=WS)
    assert len(await _lessons(soul_file)) == 1


async def test_tester_bugs_on_the_line_become_lessons(store, dirs, updates, soul_file):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    later = "2999-01-01T00:00:00Z"
    bugs = [
        {
            "number": 61,
            "title": "Text with NaN size crashes",
            "createdAt": later,
            "labels": [{"name": "bug"}, {"name": "flare"}],
        },
        {
            "number": 60,
            "title": "Old bug",
            "createdAt": "2020-01-01T00:00:00Z",
            "labels": [{"name": "bug"}, {"name": "flare"}],
        },
        {
            "number": 62,
            "title": "Another line's bug",
            "createdAt": later,
            "labels": [{"name": "bug"}, {"name": "photo"}],
        },
    ]
    runner = Runner(launch_out=SESSION, pr=MERGED, bugs=bugs)
    runner.run_ref = run_ref

    d = _lined(_tester(runner, launch, poll_seconds=600), soul_file, labels=["flare"])
    await d.run(run_ref, workspace_id=WS)

    (lesson,) = await _lessons(soul_file)
    assert lesson.startswith(f"Lesson [{SLUG} · Flare]:") and "tester" in lesson
    assert "Text with NaN size crashes (#61)" in lesson


async def test_a_worker_brief_carries_lessons_and_the_docs_line(store, dirs, stages, soul_file):
    repo, launch = dirs
    # Distinct words per lesson (dedup tokens are alphabetic only), ~250 chars each.
    for w in ("amber", "birch", "cedar", "delta", "ember", "fjord", "garnet", "harbor"):
        filler = " ".join(f"{w}{x}" for x in ("one", "two", "three", "four", "five", "six") * 3)
        assert await soul_link.note_lesson(
            str(soul_file), f"Lesson [{SLUG} · Flare]: G41 Text shapes {filler}"
        )
    run_ref = await _queue(store, repo)
    stuck = _open(mergeable="CONFLICTING")
    runner = Runner(launch_out=SESSION, pr=[stuck] * 4 + [MERGED])
    runner.run_ref = run_ref

    await _lined(_reviewer(runner, launch, reviewer=False), soul_file).run(run_ref, workspace_id=WS)

    worker, fixer = _briefs(runner)
    at = worker.index("Lessons from this line")
    block = worker[at : worker.index("</untrusted>", at) + len("</untrusted>")]
    assert "<untrusted>" in block and f"Lesson [{SLUG} · Flare]" in block
    assert block.count("- Lesson") <= 5 and len(block) <= 1200
    assert DOCS_LINE in worker and DOCS_LINE in fixer
    assert "Lessons from this line" not in fixer


async def test_no_soul_means_no_lessons_block(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = Runner(launch_out=SESSION, pr=MERGED)
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    (worker,) = _briefs(runner)
    assert "Lessons from this line" not in worker and DOCS_LINE in worker
