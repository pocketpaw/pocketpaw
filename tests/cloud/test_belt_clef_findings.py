# tests/cloud/test_belt_clef_findings.py — Clef on Belt's FINDINGS.
#
# The Clef HTTP boundary is an ``httpx.MockTransport`` (``Judge``: it answers by
# keywords in the finding: BLOCKER blocks, LINT is mechanical, DUP duplicates
# the first candidate). gh runs through the station's and the Review Room's fake
# runners, extended with the open-issue list, issue create and comment.
# QUESTIONS: ``blocking`` + ``mechanical`` always, ``duplicate`` (a choice over
# ``issue_<n>`` + ``none``) only with candidates; the state carries the finding,
# files, screenshots, context and the candidates (never the tester's own issue);
# candidates rank by shared words. DECIDE: each live threshold, and no score
# holds. STOP RULE: ``round_clean`` / ``loop_decision`` (2 clean in a row, else
# escalate at the cap). HOOKS, shadow: the reviewer's fail, the tester's bugs and
# a Review Room issue are recorded on ``clef_findings`` and nothing else changes.
# HOOKS, live: a review round with no holding finding passes and its findings
# become follow-up issues (``audit-rule`` when mechanical) or comments on the
# issue they duplicate; a held round fails and its fixer gets only the holding
# items; Clef down keeps today's path; a tester bug gets ``audit-rule`` /
# ``duplicate``; a Review Room duplicate comments instead of filing. EVAL:
# ``list_verdicts`` rows, ``resolve_outcomes`` from gh, ``agreement``.

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi.testclient import TestClient  # noqa: E402
from pocketpaw_ee.cloud.belt import clef  # noqa: E402
from pocketpaw_ee.cloud.mandates import patrols  # noqa: E402

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from tests.cloud import test_belt_cloud_quality as quality  # noqa: E402
from tests.cloud import test_belt_cloud_station as base  # noqa: E402
from tests.cloud import test_belt_review_room as room  # noqa: E402
from tests.cloud.test_belt_cloud_quality import (  # noqa: E402
    H1,
    H2,
    RUNNING,
    _cc,
    _review,
    _reviewer,
    _status,
    _verdict,
)
from tests.cloud.test_belt_cloud_station import (  # noqa: E402
    MERGED,
    SESSION,
    SLUG,
    WS,
    _briefs,
    _queue,
    _tester,
)

store, dirs, stages, updates = base.store, base.dirs, base.stages, base.updates
FOLLOWED = "Belt review passed: its findings were filed as follow-ups"


class Judge:
    """Clef double for findings, by keywords in the finding's text."""

    def __init__(self, exc: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.exc = exc

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        body = json.loads(request.content)
        text, qs = body["state"]["finding"], body["questions"]
        out: dict = {
            "blocking": {"type": "noul", "noul": 0.9 if "BLOCKER" in text else 0.2},
            "mechanical": {"type": "noul", "noul": 0.9 if "LINT" in text else 0.3},
        }
        if "duplicate" in qs:
            first = next(k for k in qs["duplicate"]["criteria"] if k != "none")
            dup = "DUP" in text
            out["duplicate"] = {
                "type": "choice",
                "choice": first if dup else "none",
                "probabilities": {first: 0.9 if dup else 0.05, "none": 0.1 if dup else 0.95},
                "confidence": 0.9,
            }
        return httpx.Response(200, json={"success": True, "result": {"answers": out}})

    def states(self) -> list[dict]:
        return [json.loads(r.content)["state"] for r in self.requests]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """A fresh open-issue cache, and no diff scoring: its fire-and-forget blob
    write is another test's subject (tests/cloud/test_belt_clef.py)."""
    patrols._OPEN_ISSUES.clear()
    monkeypatch.setattr(clef, "shadow_diff", lambda *a, **k: None)


@pytest.fixture
def judge(monkeypatch):
    """Clef on (shadow), answering through a ``Judge``; ``judge(live=True)``
    turns the findings flag live, ``judge(exc=...)`` makes Clef fail."""
    monkeypatch.setenv("POCKETPAW_BELT_CLEF", "shadow")
    monkeypatch.setenv("POCKETPAW_CLEF_TOKEN", "tok-test")
    monkeypatch.setenv("POCKETPAW_CLEF_ACCOUNT_ID", "acct-test")

    def use(*, live: bool = False, exc: Exception | None = None) -> Judge:
        if live:
            monkeypatch.setenv(clef.FINDINGS_FLAG, "live")
        fake = Judge(exc)
        monkeypatch.setattr(clef, "_transport", httpx.MockTransport(fake))
        return fake

    return use


class Runner(quality.Runner):
    """The quality loop's runner plus the open-issue list, issue create (#101,
    #102, ...) and the label / comment / edit calls, kept on ``calls``."""

    def __init__(self, *, open_issues: list[dict] | None = None, **kw):
        super().__init__(**kw)
        self.open_issues = open_issues or []
        self.filed = 100

    async def __call__(self, argv, *, cwd, timeout, stdin=None, on_line=None):
        if argv[:3] == ["gh", "issue", "list"] and "open" in argv:
            self.calls.append((list(argv), cwd))
            return 0, json.dumps(self.open_issues), ""
        if argv[:3] == ["gh", "issue", "create"]:
            self.calls.append((list(argv), cwd))
            self.filed += 1
            return 0, f"https://github.com/{SLUG}/issues/{self.filed}\n", ""
        return await super().__call__(argv, cwd=cwd, timeout=timeout, stdin=stdin, on_line=on_line)

    def gh(self, *verb: str) -> list[list[str]]:
        return [a for a, _ in self.calls if a[: len(verb) + 1] == ["gh", *verb]]


def _labels(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a in ("--label", "--add-label")]


def V(blocking: float, mechanical: float = 0.0, dup: float = -1.0, of: int = 0):
    return clef.FindingVerdict(blocking, mechanical, dup, of, truncated=False)


# ---------------------------------------------------------------------------
# questions, candidates, decide
# ---------------------------------------------------------------------------


def test_finding_questions_ask_duplicate_only_with_candidates():
    qs = clef.finding_questions()
    assert set(qs) == {"blocking", "mechanical"}
    assert all(q.type == "noul" and set(q.criteria) == {"true", "false"} for q in qs.values())
    assert "acceptance test" in qs["blocking"].instructions
    assert (
        "lint" in qs["mechanical"].instructions and "accessibility" in qs["mechanical"].instructions
    )

    dup = clef.finding_questions([{"number": 12, "title": "Save fails"}, {"number": 4}])[
        "duplicate"
    ]
    assert dup.type == "choice"
    assert set(dup.criteria) == {"issue_12", "issue_4", "none"}
    assert "#12: Save fails" in dup.criteria["issue_12"]


async def test_score_finding_sends_its_state_and_reads_the_duplicate(judge):
    fake = judge()
    finding = clef.Finding(
        "DUP Save does nothing", "room", "r1", files=("A.swift",), screenshots=("12500.png",)
    )
    found = [{"number": 12, "title": "Save does nothing", "body": "tap Save"}]
    verdict = await clef.score_finding(clef.from_env(), finding, "Task: #93", found)
    assert verdict == clef.FindingVerdict(0.2, 0.3, 0.9, 12, truncated=False)
    (state,) = fake.states()
    assert state["finding"] == "DUP Save does nothing" and state["run"] == "r1"
    assert state["files"] == "A.swift" and state["screenshots"] == "12500.png"
    assert state["context"] == "Task: #93" and "Review Room" in state["source"]
    assert "#12 Save does nothing\ntap Save" in state["candidate_issues"]
    body = json.loads(fake.requests[0].content)
    assert set(body["questions"]) == {"blocking", "mechanical", "duplicate"}

    alone = await clef.score_finding(clef.from_env(), finding, "", [])
    assert (alone.duplicate, alone.duplicate_of) == (-1.0, 0)
    assert "duplicate" not in json.loads(fake.requests[1].content)["questions"]


def test_candidates_rank_by_shared_words_and_skip_the_findings_own_issue():
    rows = [
        {"number": 3, "title": "Unrelated", "body": "nothing here"},
        {"number": 5, "title": "Save button spinner", "body": ""},
        {"number": 7, "title": "Save button spinner never stops", "body": ""},
        {"number": 9, "title": "spinner never stops on save button", "body": ""},
    ]
    text = "The save button spinner never stops"
    assert [r["number"] for r in clef.candidate_issues(rows, text)] == [7, 9, 5]
    assert [r["number"] for r in clef.candidate_issues(rows, text, n=1)] == [7]
    assert [r["number"] for r in clef.candidate_issues(rows, text, exclude=7)] == [9, 5]


@pytest.mark.parametrize(
    ("verdict", "holds", "audit", "dup"),
    [
        (None, True, False, 0),
        (V(-1.0), True, False, 0),  # unanswered: no opinion holds
        (V(0.5), True, False, 0),
        (V(0.49), False, False, 0),
        (V(0.1, mechanical=0.71), False, True, 0),
        (V(0.1, mechanical=0.7), False, False, 0),
        (V(0.1, dup=0.81, of=12), False, False, 12),
        (V(0.1, dup=0.8, of=12), False, False, 0),
    ],
)
def test_decide_thresholds(verdict, holds, audit, dup):
    assert clef.decide(verdict) == clef.FindingDecision(holds, audit, dup)


# ---------------------------------------------------------------------------
# the stop rule
# ---------------------------------------------------------------------------


def test_round_clean_means_no_finding_blocks():
    assert clef.round_clean([])
    assert clef.round_clean([V(0.49), V(0.0)])
    assert not clef.round_clean([V(0.1), V(0.5)])
    assert not clef.round_clean([V(-1.0)])  # unanswered is not clean
    assert not clef.round_clean([None])


def test_loop_decision_needs_two_clean_rounds_in_a_row_under_the_cap():
    clean, dirty = [V(0.1)], [V(0.9)]
    assert clef.loop_decision([], cap=4) == "continue"
    assert clef.loop_decision([clean], cap=4) == "continue"  # one clean round is not done
    assert clef.loop_decision([dirty, clean, clean], cap=4) == "done"
    assert clef.loop_decision([clean, dirty, clean], cap=4) == "continue"  # not in a row
    assert clef.loop_decision([dirty, clean, dirty, clean], cap=4) == "escalate"
    assert clef.loop_decision([dirty, dirty, clean, clean], cap=4) == "done"  # done wins at the cap
    assert clef.loop_decision([[]], cap=1, clean_needed=1) == "done"  # no findings is clean


# ---------------------------------------------------------------------------
# the reviewer's verdict
# ---------------------------------------------------------------------------

BLOCKING_ITEM = "- BLOCKER Sources/A.swift:3 crashes on empty input"
POLISH_ITEM = "- Sources/C.swift:4 rename the helper for clarity"


def _failed_then_fixed(note: str) -> list[dict]:
    """Reviewed H1 fails with ``note``, a fixer pushes H2, H2 passes, merged."""
    said = _review(H1, "PENDING", comments=[note])
    failed = _review(H1, "FAILURE", comments=[note])
    passed = _review(H2, "PENDING", comments=[note, _verdict("pass", H2)])
    return [_review(H1), said, failed, failed, failed, _review(H2), passed, MERGED]


async def test_shadow_records_a_failed_reviews_findings_and_changes_nothing(
    store, dirs, stages, judge
):
    repo, launch = dirs
    fake = judge()
    run_ref = await _queue(store, repo)
    note = _verdict("fail", H1, BLOCKING_ITEM, POLISH_ITEM)
    runner = Runner(launch_out=SESSION, pr=_failed_then_fixed(note))
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert [(s["sha"], s["state"]) for s in runner.statuses] == [
        (H1, "pending"),
        (H1, "failure"),
        (H2, "pending"),
        (H2, "success"),
    ]
    fix = _briefs(runner)[2]
    assert "crashes on empty input" in fix and "rename the helper" in fix
    assert not runner.gh("issue", "create") and not runner.gh("issue", "comment")
    assert sorted(s["files"] for s in fake.states()) == ["Sources/A.swift", "Sources/C.swift"]
    cc = await _cc(store, run_ref)
    assert [(f["source"], f["actions"], f["issue"]) for f in cc["clef_findings"]] == [
        ("review", [], None),
        ("review", [], None),
    ]
    assert [f["verdict"]["blocking"] for f in cc["clef_findings"]] == [0.9, 0.2]
    assert "crashes" not in json.dumps(cc["clef_findings"])  # numbers only, never the text
    assert cc["reviews"][0]["verdict"] == "fail"


async def test_live_round_with_nothing_blocking_passes_and_files_follow_ups(
    store, dirs, stages, judge
):
    repo, launch = dirs
    judge(live=True)
    run_ref = await _queue(store, repo)
    note = _verdict(
        "fail",
        H1,
        "- LINT Sources/A.swift:3 missing doc comment",
        "- DUP Sources/B.swift:9 Save spinner never stops",
    )
    spinner = {"number": 120, "title": "Save spinner never stops", "body": "Sources/B.swift"}
    # The run's own issue (#93, which its PR closes) ties and ranks first, but is
    # never a candidate.
    own = {"number": 93, "title": "Save spinner never stops", "body": "Sources/B.swift:9 DUP"}
    seq = [_review(H1), _review(H1, "PENDING", comments=[note]), MERGED]
    runner = Runner(launch_out=SESSION, pr=seq, open_issues=[own, spinner])
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert runner.statuses == [RUNNING, _status("success", FOLLOWED)]
    assert len(_briefs(runner)) == 2  # the worker and the reviewer; no fixer
    (create,) = runner.gh("issue", "create")
    assert _labels(create) == ["review", "gap", "audit-rule"]
    assert create[create.index("--title") + 1].startswith("Follow-up: LINT Sources/A.swift:3")
    assert "did not hold the PR" in create[create.index("--body") + 1]
    (comment,) = runner.gh("issue", "comment")
    assert comment[3] == "120" and "Save spinner never stops" in comment[-1]
    cc = await _cc(store, run_ref)
    assert [(f["actions"], f["issue"]) for f in cc["clef_findings"]] == [
        (["follow_up", "audit_rule"], f"{SLUG}#101"),
        (["duplicate"], f"{SLUG}#120"),
    ]
    assert cc["reviews"][0]["verdict"] == "pass" and cc["reviews"][0]["judged"] == "followup"
    assert (await store.get_action(run_ref)).status == ActionStatus.EXECUTED


async def test_live_held_round_fails_and_its_fixer_gets_only_the_holding_items(
    store, dirs, stages, judge
):
    repo, launch = dirs
    judge(live=True)
    run_ref = await _queue(store, repo)
    note = _verdict("fail", H1, BLOCKING_ITEM, POLISH_ITEM)
    runner = Runner(launch_out=SESSION, pr=_failed_then_fixed(note))
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert [(s["sha"], s["state"]) for s in runner.statuses][:2] == [
        (H1, "pending"),
        (H1, "failure"),
    ]
    fix = _briefs(runner)[2]
    assert "crashes on empty input" in fix and "rename the helper" not in fix
    assert f"not for this PR: {SLUG}#101" in fix
    (create,) = runner.gh("issue", "create")
    assert "rename the helper" in create[create.index("--title") + 1]
    cc = await _cc(store, run_ref)
    assert [f["actions"] for f in cc["clef_findings"]] == [["held"], ["follow_up"]]


async def test_live_with_clef_down_keeps_todays_review_path(store, dirs, stages, judge):
    repo, launch = dirs
    judge(live=True, exc=httpx.ConnectError("down"))
    run_ref = await _queue(store, repo)
    note = _verdict("fail", H1, POLISH_ITEM)
    runner = Runner(launch_out=SESSION, pr=_failed_then_fixed(note))
    runner.run_ref = run_ref

    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    assert [s["state"] for s in runner.statuses][:2] == ["pending", "failure"]
    assert "rename the helper" in _briefs(runner)[2]
    assert not runner.gh("issue", "create")
    assert "clef_findings" not in await _cc(store, run_ref)


# ---------------------------------------------------------------------------
# the tester's bugs
# ---------------------------------------------------------------------------

BUGS = [
    {
        "number": 61,
        "title": "LINT Text with NaN size crashes",
        "body": "DUP Repro in Sources/T.swift:4, see nan-size.png",
        "createdAt": "2999-01-01T00:00:00Z",
        "labels": [{"name": "bug"}],
    },
    {"number": 60, "title": "Old", "createdAt": "2020-01-01T00:00:00Z", "labels": []},
]
NAN = {"number": 12, "title": "NaN size crashes Text", "body": ""}
OWN = {"number": 61, "title": "LINT Text with NaN size crashes", "body": ""}


@pytest.mark.parametrize("live", [False, True])
async def test_tester_bugs_are_judged_and_live_labels_them(store, dirs, updates, judge, live):
    repo, launch = dirs
    fake = judge(live=live)
    run_ref = await _queue(store, repo)
    runner = Runner(launch_out=SESSION, pr=MERGED, bugs=BUGS, open_issues=[OWN, NAN])
    runner.run_ref = run_ref

    await _tester(runner, launch, poll_seconds=600).run(run_ref, workspace_id=WS)

    (state,) = fake.states()
    assert state["files"] == "Sources/T.swift" and state["screenshots"] == "nan-size.png"
    assert "#12 NaN" in state["candidate_issues"] and "#61" not in state["candidate_issues"]
    (entry,) = (await _cc(store, run_ref))["clef_findings"]
    assert entry["source"] == "tester" and entry["issue"] == f"{SLUG}#61"
    edits = runner.gh("issue", "edit")
    if not live:
        assert entry["actions"] == [] and not edits and not runner.gh("issue", "comment")
        return
    assert entry["actions"] == ["audit_rule", "duplicate"]
    assert [(e[3], _labels(e)) for e in edits] == [("61", ["audit-rule"]), ("61", ["duplicate"])]
    (comment,) = runner.gh("issue", "comment")
    assert comment[3] == "12" and "#61" in comment[-1]


# ---------------------------------------------------------------------------
# the Review Room's needs-changes issue
# ---------------------------------------------------------------------------


class RoomRunner(room.FakeRunner):
    def __init__(self, open_issues: list[dict], **kw):
        super().__init__(**kw)
        self.open_issues = open_issues

    async def __call__(self, argv, *, cwd, timeout, stdin=None):
        if "get-url" in argv:
            return 0, f"https://github.com/{room.SLUG}.git\n", ""
        if argv[:3] == ["gh", "issue", "list"]:
            return 0, json.dumps(self.open_issues), ""
        return await super().__call__(argv, cwd=cwd, timeout=timeout, stdin=stdin)


@pytest.fixture
def room_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POCKETPAW_FACTORY_REVIEW_DIR", str(tmp_path / "reviews"))
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "https://paw.example")


SAVE = {"number": 7, "title": "Save button does nothing", "body": ""}


@pytest.mark.usefixtures("room_env")
@pytest.mark.parametrize(
    ("live", "text", "issue", "actions", "extra"),
    [
        (False, "DUP Save button does nothing", 42, [], []),  # shadow: filed as ever
        (True, "DUP Save button does nothing", 7, ["duplicate"], None),
        (True, "LINT Save label contrast too low", 42, ["audit_rule"], ["audit-rule"]),
    ],
)
async def test_room_issue_is_judged_and_live_dedups_or_labels(
    store, judge, live, text, issue, actions, extra
):
    fake = judge(live=live)
    run = await room._landed_run(store)
    runner = RoomRunner([SAVE])
    with TestClient(room._app(runner)) as client:
        res = room._post(client, run, notes=[{"t_ms": 12500, "text": text}])

    assert res.status_code == 201, res.text
    assert res.json()["issue_url"] == f"https://github.com/{room.SLUG}/issues/{issue}"
    (state,) = fake.states()
    assert state["screenshots"] == "12500.png" and "[00:12]" in state["finding"]
    creates = [c for c in runner.calls if c[:3] == ["gh", "issue", "create"]]
    comments = [c for c in runner.calls if c[:3] == ["gh", "issue", "comment"]]
    if extra is None:  # the duplicate: a comment on #7 instead of a new issue
        assert not creates and [c[3] for c in comments] == ["7"]
        assert "DUP Save button does nothing" in runner.stdins["gh issue comment 7"]
    else:
        (create,) = creates
        assert _labels(create) == ["review", "gap", "bug", "line:flare", *extra]
        assert not comments
    action = await store.get_action(run)
    (entry,) = action.parameters["_code_change"]["clef_findings"]
    assert entry["source"] == "room" and entry["actions"] == actions
    assert entry["issue"] == f"{room.SLUG}#{issue}"


# ---------------------------------------------------------------------------
# the eval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "reason", "outcome"),
    [
        ("OPEN", "", "open"),
        ("OPEN", "REOPENED", "open"),
        ("CLOSED", "COMPLETED", "fixed"),
        ("CLOSED", "NOT_PLANNED", "wont_fix"),
        ("CLOSED", "DUPLICATE", "duplicate"),
        ("CLOSED", "", "closed"),
    ],
)
def test_issue_outcome(state, reason, outcome):
    assert clef.issue_outcome(state, reason) == outcome


async def test_eval_rows_carry_findings_beside_what_happened_to_their_issue(
    store, dirs, stages, judge
):
    repo, launch = dirs
    judge(live=True)
    run_ref = await _queue(store, repo)
    note = _verdict("fail", H1, BLOCKING_ITEM, POLISH_ITEM)
    runner = Runner(launch_out=SESSION, pr=_failed_then_fixed(note))
    runner.run_ref = run_ref
    await _reviewer(runner, launch).run(run_ref, workspace_id=WS)

    rows = [r for r in await clef.list_verdicts(WS) if r["kind"] == "finding"]
    assert [(r["source"], r["actions"], r["issue"], r["outcome"]) for r in rows] == [
        ("review", ["held"], None, "landed"),  # no issue: the run's own outcome
        ("review", ["follow_up"], f"{SLUG}#101", ""),  # its issue, still to read
    ]

    seen: list[list[str]] = []

    async def gh(argv):
        seen.append(argv)
        return 0, json.dumps([{"number": 101, "state": "CLOSED", "stateReason": "NOT_PLANNED"}]), ""

    rows = await clef.resolve_outcomes(rows, gh)
    assert [r["outcome"] for r in rows] == ["landed", "wont_fix"]
    assert len(seen) == 1 and seen[0][:5] == ["gh", "issue", "list", "-R", SLUG]
    assert "stateReason" in seen[0][-1]
    # Clef said .2 blocking and the human closed it as won't-fix: agreed.
    assert clef.agreement(rows) == {"blocking": (1, 1), "duplicate": (0, 0)}


def test_agreement_counts_each_decision_kind_on_closed_issues():
    def row(outcome, **v):
        return {"kind": "finding", "outcome": outcome, "verdict": v}

    rows = [
        row("fixed", blocking=0.9, duplicate=0.1),  # both agree
        row("wont_fix", blocking=0.9, duplicate=-1.0),  # blocking disagrees; dup not asked
        row("duplicate", blocking=0.2, duplicate=0.95),  # dup agrees; blocking not judged
        row("open", blocking=0.9, duplicate=0.95),  # open: not judged
        {"kind": "diff", "outcome": "fixed", "verdict": {"blocking": 0.9}},
    ]
    assert clef.agreement(rows) == {"blocking": (1, 2), "duplicate": (2, 2)}
