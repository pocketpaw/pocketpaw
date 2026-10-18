# tests/cloud/test_belt_lead_read.py — Belt's batched lead read and rate-limit wait.
#
# Every feature-lead tracker on a repo shares ONE aliased GraphQL query per half
# poll (``CloudTaskDispatcher._lead_read``) instead of a PR list, an issue view
# and a branch read per feature: 11 leads at a 60 s poll spent ~4,400 of the
# token's 5,000 GraphQL points an hour that way (workspace doc
# docs/design/drafts/2026-10-18-github-api-limits.md). A rate-limited gh read
# sleeps to the reset its response headers give instead of retrying into an
# empty bucket. The fake below answers like GitHub does: a missing issue is a
# null alias plus a NOT_FOUND error, and gh exits 1 even though data came back.

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.belt import cloud_station  # noqa: E402
from pocketpaw_ee.cloud.belt.cloud_station import CloudTaskDispatcher  # noqa: E402

SLUG = "qbtrix/creative-apps"
RUN = "run-1"
START = 1_800_000_000.0


def _headers(remaining: int, reset: float) -> str:
    """``gh api -i``'s status line and rate-limit headers, as gh prints them."""
    return (
        "HTTP/2.0 200 OK\nX-Ratelimit-Limit: 5000\r\n"
        f"X-Ratelimit-Remaining: {remaining}\r\nX-Ratelimit-Reset: {int(reset)}\r\n"
        "X-Ratelimit-Resource: graphql\r\n\r\n"
    )


def _comment(body: str) -> dict:
    return {"id": f"IC_{body}", "url": "u", "body": body, "createdAt": "2026-10-18T06:00:00Z"}


class GitHub:
    """``gh api graphql``: ``i<k>`` aliases from ``issues`` (a missing number is
    null + NOT_FOUND, exit 1), ``b<k>`` from ``heads`` by ref, ``prs`` for the PR
    list. The first ``limited`` calls answer RATE_LIMITED with an empty bucket
    resetting at ``reset``; ``remaining`` goes on every other answer's headers.
    ``error``: one more error of that type on the query's answer."""

    def __init__(self, issues=None, heads=None, prs=None, *, limited=0, reset=START + 3600):
        self.issues: dict[int, dict] = issues or {}
        self.heads: dict[str, dict] = heads or {}
        self.prs: list[dict] = prs or []
        self.limited, self.reset, self.remaining = limited, reset, 4000
        self.error = ""
        self.calls: list[list[str]] = []

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        self.calls.append(list(argv))
        query = next((a for a in argv if a.startswith("query=")), "")
        if self.limited:
            self.limited -= 1
            body = {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
            head = _headers(0, self.reset) if "-i" in argv else ""
            return 1, head + json.dumps(body), "gh: API rate limit already exceeded for user ID 1."
        if "rateLimit" in query:
            return 0, _headers(self.remaining, self.reset) + "{}", ""
        if argv[:3] == ["gh", "issue", "view"]:
            return 0, json.dumps({"comments": [], "state": "OPEN"}), ""
        repo: dict = {"prs": {"nodes": self.prs}}
        errors = [{"type": self.error, "message": "timeout"}] if self.error else []
        for alias, n in re.findall(r"(i\d+):issue\(number:(\d+)\)", query):
            issue = self.issues.get(int(n))
            if issue is None:
                errors.append({"type": "NOT_FOUND", "path": ["repository", alias]})
            repo[alias] = issue and {
                "state": issue["state"],
                "comments": {"totalCount": len(issue["comments"]), "nodes": issue["comments"]},
            }
        for alias, ref in re.findall(r'(b\d+):object\(expression:"([^"]+)"\)', query):
            repo[alias] = self.heads.get(ref)
        body = {"data": {"repository": repo}} | ({"errors": errors} if errors else {})
        out = _headers(self.remaining, self.reset) + json.dumps(body)
        return (1, out, "gh: Could not resolve to an Issue") if errors else (0, out, "")


def _dispatcher(gh: GitHub) -> tuple[CloudTaskDispatcher, list[float]]:
    now, slept = [START], []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    disp = CloudTaskDispatcher(
        runner=gh, sleep=sleep, clock=lambda: now[0], wall=lambda: now[0], poll_seconds=60.0
    )
    return disp, slept


def _feat(issue: int, feature: str) -> dict:
    return {"issue": issue, "feature": feature, "branch": f"lead/{feature}"}


def _head(next_line: str | None) -> dict:
    record = None if next_line is None else {"object": {"text": f"Next: {next_line}\n"}}
    return {"committedDate": "2026-10-18T06:30:00Z", "file": record}


def _batches(gh: GitHub) -> list[str]:
    return [a[-5] for a in gh.calls if a[:4] == ["gh", "api", "graphql", "-i"]]


A = [_feat(11, "canvas")]
B = [_feat(12, "brush"), _feat(13, "layers")]


async def test_one_query_serves_every_lead_on_the_repo_and_maps_back_to_each():
    lead_pr = {
        "number": 40,
        "url": "pr40",
        "state": "MERGED",
        "body": f"Belt-Lead: {RUN}\nFeature: brush\nSlice: S1",
        "labels": {"nodes": [{"name": "feature-done"}]},
        "updatedAt": "2026-10-18T06:00:00Z",
        "mergedAt": "2026-10-18T06:00:00Z",
        "mergeCommit": {"oid": "a" * 40},
        "headRefName": "lead/brush",
    }
    gh = GitHub(
        issues={
            11: {"state": "OPEN", "comments": [_comment("canvas-said")]},
            12: {"state": "CLOSED", "comments": [_comment("brush-said")]},
            13: {"state": "OPEN", "comments": []},
        },
        heads={"lead/canvas": _head("wait for CI"), "lead/brush": _head("S2")},
        prs=[lead_pr, {**lead_pr, "number": 41, "body": "someone else's"}],
    )
    disp, slept = _dispatcher(gh)
    await disp._lead_read(SLUG, A, Path("."))
    warm = await disp._lead_read(SLUG, B, Path("."))  # B is new: it fetches for both
    assert len(_batches(gh)) == 2 and warm.issues[12]["state"] == "CLOSED"
    disp.clock = disp.wall = lambda: START + 60.0  # the next poll
    gh.calls.clear()

    a = await disp._lead_read(SLUG, A, Path("."))
    b = await disp._lead_read(SLUG, B, Path("."))

    # One query for both leads' three issues and three branches, and the PR list.
    (query,) = _batches(gh)
    assert len(gh.calls) == 1 and a is b
    assert sorted(map(int, re.findall(r"issue\(number:(\d+)\)", query))) == [11, 12, 13]
    assert sorted(re.findall(r'expression:"([^"]+)"', query)) == [
        "lead/brush",
        "lead/canvas",
        "lead/layers",
    ]
    assert "docs/leads/canvas/RECORD.md" in query and "pullRequests(first:100" in query
    # Each lead gets its own issue (gh's ``--json comments,state`` shape) and branch.
    assert a.issues[11] == {"state": "OPEN", "comments": [_comment("canvas-said")]}
    assert b.issues[12]["state"] == "CLOSED"
    assert b.issues[12]["comments"][0]["body"] == "brush-said"
    assert cloud_station._branch_at(a, A[0])[1] == "wait for CI"
    assert cloud_station._branch_at(b, B[0])[0] == cloud_station._epoch("2026-10-18T06:30:00Z")
    prs, _ = cloud_station._lead_prs(b.prs, RUN)
    assert [(p["number"], p["feature"], p["done"], p["slices"]) for p in prs] == [
        (40, "brush", True, ["S1"])
    ]
    # Inside the same half poll nobody asks GitHub again.
    disp.clock = disp.wall = lambda: START + 80.0
    await disp._lead_read(SLUG, B, Path("."))
    assert len(gh.calls) == 1
    assert slept == []  # 4,000 points left: nothing waits for the reset


async def test_a_missing_issue_or_branch_or_record_reads_empty_not_as_an_error():
    gh = GitHub(
        issues={11: {"state": "OPEN", "comments": [_comment("here")]}},  # 12, 13: gone
        heads={"lead/canvas": _head(None)},  # a branch with no RECORD.md; brush: none
    )
    disp, _ = _dispatcher(gh)

    read = await disp._lead_read(SLUG, [*A, *B], Path("."))

    assert read.issues[11]["comments"][0]["body"] == "here"
    assert read.issues[12] == {} and read.issues[13] == {}
    assert cloud_station._branch_at(read, B[0]) == (0.0, "")
    at, step = cloud_station._branch_at(read, A[0])
    assert at > 0 and step == ""
    assert len(gh.calls) == 1  # gh's exit 1 on NOT_FOUND was an answer, not a retry


async def test_any_other_graphql_error_fails_the_read_so_the_poll_is_skipped():
    gh = GitHub(issues={11: {"state": "OPEN", "comments": []}})
    gh.error = "SERVICE_UNAVAILABLE"
    disp, _ = _dispatcher(gh)

    with pytest.raises(cloud_station.CloudStationError):
        await disp._lead_read(SLUG, A, Path("."))


async def test_a_rate_limited_read_waits_for_the_reset_in_its_headers():
    gh = GitHub(issues={11: {"state": "OPEN", "comments": []}}, limited=1, reset=START + 600)
    disp, slept = _dispatcher(gh)

    read = await disp._lead_read(SLUG, A, Path("."))

    assert slept == [601.0]  # to the reset (+1 s), never the 2 s / 4 s backoff
    assert len(_batches(gh)) == 2 and read.issues[11]["state"] == "OPEN"
    assert not any("rate_limit" in a for call in gh.calls for a in call)


async def test_a_rate_limit_without_headers_probes_them_and_waits():
    """``gh issue view`` prints no headers: one ``gh api -i graphql`` probe reads
    the reset off its headers (never ``gh api rate_limit``)."""
    gh = GitHub(limited=1, reset=START + 900)
    gh.remaining = 0  # the probe finds the bucket empty
    disp, slept = _dispatcher(gh)
    argv = ["gh", "issue", "view", "11", "-R", SLUG, "--json", "comments"]

    await disp._gh(argv, Path("."), "TRACK")

    assert slept == [901.0]
    probe = gh.calls[1]
    assert probe[:4] == ["gh", "api", "graphql", "-i"] and "rateLimit" in probe[-1]
    assert gh.calls[2] == argv


async def test_an_answer_that_empties_the_bucket_holds_every_next_read_to_the_reset():
    gh = GitHub(issues={11: {"state": "OPEN", "comments": []}}, reset=START + 300)
    gh.remaining = 0
    disp, slept = _dispatcher(gh)

    await disp._lead_read(SLUG, A, Path("."))
    assert slept == []
    gh.remaining = 4999
    disp.clock = lambda: START + 60.0  # stale: the next poll fetches again
    await disp._lead_read(SLUG, A, Path("."))

    assert slept == [301.0] and len(_batches(gh)) == 2
