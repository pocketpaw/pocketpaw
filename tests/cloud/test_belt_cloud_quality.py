# tests/cloud/test_belt_cloud_quality.py — the cloud station's quality loop.
#
# REVIEW: with the reviewer on, a tracked PR (worker's or tester's) whose CI first
# goes green on a head gets ONE reviewer session for that head: ``belt/review``
# goes pending on it, the brief names the diff, issue and rubric docs, the status
# call and "never push"; the verdict the reviewer posts is recorded on
# ``reviews``; a failure runs the fixer with the review comment (kind
# ``review``, the shared cap) and the new head is reviewed again; no verdict by
# the timeout (or no reviewer session) posts success so the line never stalls;
# off by default; a resume never doubles a reviewer.
# Same fakes as tests/cloud/test_belt_cloud_station.py (``FakeRunner`` extended
# with the gh calls this loop adds); nothing real is launched.

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import cloud_station, headless  # noqa: E402

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
REVIEW_NOTE = (
    "Belt-Review: a1b2c3d4e5f6\n- Sources/FlareCore/Text.swift:12 duplicates Geometry.clamp"
)


class Runner(FakeRunner):
    """``FakeRunner`` plus the commit-status POSTs (kept on ``statuses``)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.statuses: list[dict[str, str]] = []

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        if argv[:2] == ["gh", "api"] and "/statuses/" in argv[2]:
            self.calls.append((list(argv), cwd))
            fields = dict(a.split("=", 1) for a in argv[3:] if "=" in a)
            self.statuses.append({"sha": argv[2].rsplit("/", 1)[-1], **fields})
            return 0, "{}", ""
        return await super().__call__(argv, cwd=cwd, timeout=timeout, stdin=stdin, on_line=on_line)


def _review(head: str, state: str | None = None, *, comments=(), **kw) -> dict:
    """An open, green PR on ``head``; ``state`` adds its ``belt/review`` status."""
    pr = _open(head, **kw)
    if state:
        status = {"__typename": "StatusContext", "context": "belt/review", "state": state}
        pr["statusCheckRollup"] = [*pr["statusCheckRollup"], status]
    pr["comments"] = [{"body": c, "createdAt": "2030-01-01T00:00:00Z"} for c in comments]
    return pr


def _reviewer(runner: FakeRunner, launch: Path, **kw):
    # Polls every 60 s; a stuck PR gets its fixer 100 s in; no lane nudge in reach.
    kw = {"poll_seconds": 60, "fix_grace_seconds": 100, "lane_nudge_seconds": 10**9, **kw}
    return _dispatcher(runner, launch, reviewer=True, **kw)


async def test_a_green_pr_gets_one_reviewer_and_its_pass_is_recorded(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    seq = [_review(H1), _review(H1, "PENDING"), _review(H1, "SUCCESS"), MERGED]
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
    assert f"gh api repos/{SLUG}/statuses/{H1}" in brief and "context=belt/review" in brief
    assert "140" in brief and "Belt-Review:" in brief and "file:line" in brief
    assert "never push" in brief.lower()
    # Pending on the head at launch: the lane waits for the verdict.
    assert runner.statuses == [
        {
            "sha": H1,
            "state": "pending",
            "context": "belt/review",
            "description": "Belt review running",
        }
    ]
    action = await store.get_action(run_ref)
    assert action.status == ActionStatus.EXECUTED
    (review,) = action.parameters["_code_change"]["reviews"]
    assert review["sha"] == H1 and review["verdict"] == "pass"
    assert review["session_url"] == CLOUD_URL and review["at"] <= time.time()
    assert "review" in stages


async def test_a_failed_review_runs_a_fixer_then_reviews_the_new_head(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    failed = _review(H1, "FAILURE", comments=["Looks off.", REVIEW_NOTE])
    seq = [_review(H1), failed, failed, failed, _review(H2), _review(H2, "SUCCESS"), MERGED]
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
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert [f["kind"] for f in cc["fixes"]] == ["review"]
    assert [(r["sha"], r["verdict"]) for r in cc["reviews"]] == [(H1, "fail"), (H2, "pass")]
    assert [s["sha"] for s in runner.statuses] == [H1, H2]


async def test_the_review_fixer_shares_the_fix_cap(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    note = ["Belt-Review: x\n- A.swift:1 bad"]
    seq = [_review(H1), *[_review(H1, "FAILURE", comments=note)] * 3]
    seq += [_review(H2), *[_review(H2, "FAILURE", comments=note)] * 4]
    runner = Runner(launch_out=SESSION, pr=seq)
    runner.run_ref = run_ref

    await _reviewer(runner, launch, fix_attempts=1).run(run_ref, workspace_id=WS)

    cc = (await store.get_action(run_ref)).parameters["_code_change"]
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

    assert [s["state"] for s in runner.statuses] == ["pending", "success"]
    assert runner.statuses[1]["description"] == "review timed out"
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert [r["verdict"] for r in cc["reviews"]] == ["timeout"]
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_a_reviewer_that_does_not_start_never_holds_the_line(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = Runner(launch_out=[SESSION, "Error: rate limited"], pr=[_review(H1), MERGED])
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert [s["state"] for s in runner.statuses] == ["pending", "success"]
    (review,) = (await store.get_action(run_ref)).parameters["_code_change"]["reviews"]
    assert review["verdict"] == "error" and "no cloud session started" in review["error"]


async def test_the_reviewer_is_off_by_default(store, dirs):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    runner = Runner(launch_out=SESSION, pr=[_review(H1), MERGED])
    runner.run_ref = run_ref

    await _dispatcher(runner, launch).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1 and runner.statuses == []
    assert "reviews" not in (await store.get_action(run_ref)).parameters["_code_change"]


async def test_no_review_before_ci_reports_or_while_it_is_red(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    empty = {**_review(H1), "statusCheckRollup": []}
    runner = Runner(launch_out=SESSION, pr=[empty, _review(H1, red=True), MERGED])
    runner.run_ref = run_ref

    await _reviewer(runner, launch, fix_grace_seconds=10**9).run(run_ref, workspace_id=WS)

    assert len(_briefs(runner)) == 1 and runner.statuses == []


async def test_a_resume_never_doubles_a_reviewer(store, dirs, stages):
    repo, launch = dirs
    run_ref = await _launched(store, repo, launched_at=time.time())
    pending = {"sha": H1, "verdict": "pending", "session_url": CLOUD_URL, "at": time.time()}
    await cloud_station._patch(store, run_ref, reviews=[pending])
    runner = Runner(launch_out=SESSION, pr=[_review(H1, "PENDING"), _review(H1, "SUCCESS"), MERGED])
    runner.run_ref = run_ref

    async def workspaces() -> list[str]:
        return [WS]

    await _reviewer(runner, launch, workspaces_for=workspaces).resume_all()

    assert not _briefs(runner) and runner.statuses == []
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert [r["verdict"] for r in cc["reviews"]] == ["pass"]


async def test_the_tester_pr_is_reviewed_too(store, dirs, updates):
    repo, launch = dirs
    run_ref = await _queue(store, repo)
    green = _tester_pr(head=H2)
    passed = {**green, "statusCheckRollup": _review(H2, "SUCCESS")["statusCheckRollup"]}
    runner = Runner(
        launch_out=SESSION, pr=MERGED, tester_pr=[green, passed, {**passed, "state": "MERGED"}]
    )
    runner.run_ref = run_ref
    d = _tester(runner, launch, poll_seconds=60, lane_nudge_seconds=10**9)
    d.reviewer = True

    await d.run(run_ref, workspace_id=WS)

    briefs = _briefs(runner)
    assert len(briefs) == 3 and "#20" in briefs[2] and "reviewer" in briefs[2]
    cc = (await store.get_action(run_ref)).parameters["_code_change"]
    assert [r["verdict"] for r in cc["tester"]["reviews"]] == ["pass"]
    assert "reviews" not in cc
    assert ("landed", "review") in updates


def test_wire_cloud_reads_the_reviewer_env(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", None)
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_LAUNCH_DIR", str(tmp_path))
    monkeypatch.delenv("POCKETPAW_FACTORY_CLOUD_REVIEWER", raising=False)
    monkeypatch.delenv("POCKETPAW_FACTORY_CLOUD_REVIEW_TIMEOUT", raising=False)
    assert cloud_station.wire_cloud() is True
    d = headless.resolve_headless_dispatcher()
    assert d.reviewer is False and d.review_timeout_seconds == 1800.0
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_REVIEWER", "1")
    monkeypatch.setenv("POCKETPAW_FACTORY_CLOUD_REVIEW_TIMEOUT", "600")
    assert cloud_station.wire_cloud() is True
    d = headless.resolve_headless_dispatcher()
    assert d.reviewer is True and d.review_timeout_seconds == 600.0
