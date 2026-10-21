# tests/cloud/test_belt_architecture_live.py — the ``live`` block of
# ``GET /belt/architecture``.
#
# Pure: the status rule by precedence, the PR check roll-up, a finding's
# ``Files:`` line, every signal joined to its components (none: ``repo``)
# and the block's size at creative-apps' counts. Reader: real git for the merge
# log, fakes for gh and the run list; a gh failure keeps the model fresh and
# serves the last live block with ``stale``.

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.belt import app_blueprint_read as abr
from pocketpaw_ee.cloud.belt import architecture_live as al
from pocketpaw_ee.cloud.belt import architecture_read as ar
from pocketpaw_ee.cloud.belt import cloud_station as cs

from tests.cloud.test_belt_architecture_read import Origin, git

PATHS_MODEL = {
    "scope": "app",
    "model": {
        "systems": [
            {
                "id": "app",
                "containers": [
                    {
                        "id": "web",
                        "components": [
                            {"id": "ui", "paths": ["src/ui/"]},
                            {"id": "api", "paths": ["src/api/"]},
                            {"id": "next", "tags": ["planned"]},
                        ],
                    }
                ],
            }
        ],
        "relationships": [],
    },
}
NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)
EMPTY = {"runs": [], "prs": [], "merges": {"count_7d": 0, "last": None}, "findings": []}
HOURS_AGO_10 = {"count_7d": 1, "last": {"at": "2026-10-21T02:00:00+00:00"}}
HOURS_AGO_25 = {"count_7d": 3, "last": {"at": "2026-10-20T11:00:00+00:00"}}


def run_row(status="running", repo="/r", **kw):
    lead = {"feature": "f", "app": None, "issue": 7, "prs": [], "stalls": 0}
    return {"action_id": "a1", "task": "t", "title": None, "status": status, "stage": "develop",
            "repo": repo, "lead": lead, **kw}  # fmt: skip


def pr(n, files, rollup=(), title="p"):
    return {"number": n, "title": title, "headRefName": f"b{n}", "url": f"u{n}",
            "files": [{"path": f} for f in files], "statusCheckRollup": list(rollup)}  # fmt: skip


def check(conclusion="SUCCESS", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": "ci", "status": status, "conclusion": conclusion}


def ctx(state, context="local-ci/quality"):
    return {"__typename": "StatusContext", "context": context, "state": state}


def finding(n, body, created="2026-10-21T07:00:00Z"):
    return {"number": n, "title": f"f{n}", "body": body, "createdAt": created, "url": f"i{n}"}


@pytest.mark.parametrize(
    ("row", "planned", "want"),
    [
        ({"prs": [{"checks": "red"}], "runs": [{}]}, False, "failed"),
        ({"runs": [{}], "merges": HOURS_AGO_10}, False, "changing"),
        ({"merges": HOURS_AGO_10}, True, "changed"),
        ({"merges": HOURS_AGO_25}, False, "landed"),  # counted in 7 d, not changed in 24 h
        ({"prs": [{"checks": "green"}]}, True, "planned"),
        ({"prs": [{"checks": "pending"}], "findings": [{}]}, False, "landed"),
    ],
)
def test_status_takes_the_first_rule_that_holds(row, planned, want):
    assert al.component_status({**EMPTY, **row}, planned, NOW) == want


@pytest.mark.parametrize(
    ("rollup", "want"),
    [
        ([], (None, None)),
        ([check(), check("NEUTRAL"), check("SKIPPED")], ("green", None)),
        ([check("", "IN_PROGRESS"), check("FAILURE")], ("red", None)),
        ([check("", "IN_PROGRESS"), ctx("SUCCESS")], ("pending", "local-ci/quality: success")),
        ([check(), ctx("PENDING")], ("pending", "local-ci/quality: pending")),
        ([check(), ctx("ERROR", "other")], ("red", None)),
        ([check(), ctx("SUCCESS")], ("green", "local-ci/quality: success")),
    ],
)
def test_pr_checks_roll_up_to_green_red_or_pending_and_name_the_gate(rollup, want):
    assert al.checks(rollup) == want


def test_a_finding_maps_by_its_files_line():
    body = "What: x\n\nFiles: src/ui/a.swift, ./src/api/b.py ../escape.py\nTest: y"
    assert al.finding_files(body) == ["src/ui/a.swift", "src/api/b.py"]
    assert al.finding_files("no files named") == []


def test_every_signal_joins_its_components_and_one_that_joins_none_lands_on_repo():
    log = (
        "s2\tfix(ui): newest\t2026-10-21T10:00:00+00:00\n\nsrc/ui/a.ts\nsrc/api/b.py\n"
        "s1\tchore: docs\t2026-10-20T10:00:00+00:00\n\nREADME.md\nsrc/ui/c.ts\n"
    )
    live = al.derive(
        PATHS_MODEL,
        runs=[(run_row(task="x" * 999), ["src/ui/a.ts", "Makefile"])],
        prs=[pr(1, ["src/api/b.py", "src/ui/a.ts"], [check("FAILURE")]), pr(2, ["README.md"])],
        log=log,
        findings=[finding(9, "Files: src/api/b.py"), finding(8, "loose")],
        dev_ci={"sha": "s2", "state": "green", "url": "c"},
        now=NOW,
    )
    ui, api, nxt = (live["components"][k] for k in ("ui", "api", "next"))
    repo = live["repo"]
    assert (live["stale"], live["dev_ci"]["state"]) == (False, "green")
    assert [r["action_id"] for r in ui["runs"]] == ["a1"] and len(ui["runs"][0]["task"]) <= 160
    assert ui["runs"][0]["lead"] == {"feature": "f", "app": None, "issue": 7}
    assert [p["number"] for p in ui["prs"]] == [p["number"] for p in api["prs"]] == [1]
    assert ui["prs"][0] == {"number": 1, "title": "p", "branch": "b1", "checks": "red",
                            "gate": None, "url": "u1"}  # fmt: skip
    assert ui["merges"] == {"count_7d": 2, "last": {"sha": "s2", "title": "fix(ui): newest",
                                                    "at": "2026-10-21T10:00:00+00:00"}}  # fmt: skip
    assert api["merges"]["count_7d"] == 1
    assert api["findings"] == [{"number": 9, "title": "f9", "age_h": 5.0, "url": "i9"}]
    assert (ui["status"], api["status"], nxt["status"]) == ("failed", "failed", "planned")
    assert repo["runs"] == [] and [p["number"] for p in repo["prs"]] == [2]  # a1 joined ui
    assert repo["merges"] == {"count_7d": 0, "last": None}  # s1 touched ui too
    assert [f["number"] for f in repo["findings"]] == [8] and repo["status"] == "landed"


def test_a_run_with_no_touched_files_joins_the_components_its_belt_lead_prs_touch():
    mine = {**pr(3, ["src/api/b.py", "README.md"]), "body": "Feature: f\n Belt-Lead: a1 \n"}
    other = {**pr(4, ["src/ui/a.ts"]), "body": "Belt-Lead: a2\nsee Belt-Lead: a1 later"}
    runs = [(run_row(), []), (run_row(action_id="a9"), [])]  # a9 has no PR yet
    live = al.derive(PATHS_MODEL, runs=runs, prs=[mine, other], log="", findings=[],
                     dev_ci=None, now=NOW)  # fmt: skip
    ui, api = live["components"]["ui"], live["components"]["api"]
    assert [r["action_id"] for r in api["runs"]] == ["a1"] and api["status"] == "changing"
    assert ui["runs"] == []  # a2's PR, and a1 named mid-line, do not join a1
    assert [r["action_id"] for r in live["repo"]["runs"]] == ["a9"]
    assert al.LEAD_MARKER == cs.LEAD_MARKER


def test_the_block_stays_small_at_creative_apps_counts():
    findings = [finding(n, "x" * 700 + "\nFiles: src/ui/a.ts") for n in range(150)]
    prs = [pr(n, [f"src/api/{i}.py" for i in range(30)], [check()] * 5) for n in range(100)]
    live = al.derive(PATHS_MODEL, runs=[], prs=prs, log="", findings=findings, dev_ci=None, now=NOW)
    assert len(json.dumps(live)) < 60_000


# -- reader: real git for the merge log, fakes for gh and the runs ---------


def fake_gh(prs=(), findings=(), fail=False, started=None):
    """``fail``: True for every read, or the nouns (``pr``, ``issue``, ``run``)
    that fail. With ``started``, each read waits until all three have begun."""

    async def gh(argv):
        if argv[0] == "git":  # the slug read
            return 0, "https://github.com/o/r.git\n", ""
        if started is not None:
            started.append(argv[1])
            while len(started) < 3:  # never passes when the reads run one by one
                await asyncio.sleep(0)
        if fail is True or argv[1] in (fail or ()):
            return 1, "", "API rate limit exceeded"
        rows = {"pr": prs, "issue": findings, "run": [{"headSha": "h", "conclusion": "success"}]}
        fields = argv[-1].split(",")  # gh returns only the --json fields asked for
        return 0, json.dumps([{k: r[k] for k in fields if k in r} for r in rows[argv[1]]]), ""

    return gh


@pytest.fixture
def origin(monkeypatch, tmp_path):
    for name in ("_CACHE", "_LOCKS"):
        monkeypatch.setattr(ar, name, {})
    o = Origin(tmp_path)
    monkeypatch.setattr(ar, "_re_resolve_repo", lambda r: (o.clone.resolve(), None))
    return o


def commit(o: Origin, path: str, *, days_ago: int = 0) -> None:
    f = o.work / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(path + str(days_ago))
    git(o.work, "add", path)
    when = f"{int(datetime.now(UTC).timestamp()) - days_ago * 86400} +0000"
    env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", path],
                   cwd=o.work, env=env, check=True, capture_output=True)  # fmt: skip
    git(o.work, "push", "-q", "origin", o.branch)


async def test_a_pending_run_reads_changing_and_a_3_day_old_merge_counts_but_lands(origin):
    origin.push(PATHS_MODEL)
    commit(origin, "src/old/x.py", days_ago=30)
    commit(origin, "src/api/x.py", days_ago=3)
    clone = origin.cloned()

    async def runs(ws):
        assert ws == "ws1"
        here = str(clone)
        return [run_row(repo=here), run_row("landed", here, action_id="a2"), run_row(repo="/x")]

    async def touched(ws, action_id):
        assert action_id == "a1"  # settled and foreign runs are never read
        return ["src/ui/a.ts"]

    body = await ar.read_architecture(
        "ws1", "repo", now=lambda: 0.0, gh=fake_gh(), runs=runs, touched=touched
    )
    comps = body["live"]["components"]
    assert (comps["ui"]["status"], comps["api"]["status"]) == ("changing", "landed")
    assert comps["api"]["merges"]["count_7d"] == 1  # 3 days counts; 30 days is out
    assert body["live"]["dev_ci"] == {"sha": "h", "state": "green", "url": None}
    assert body["slug"] == "o/r"


async def test_the_pr_read_carries_the_body_that_joins_a_run_to_its_prs(origin):
    origin.push(PATHS_MODEL)
    clone = origin.cloned()

    async def runs(ws):
        return [run_row(repo=str(clone))]

    async def touched(ws, action_id):
        return []

    lead_pr = {**pr(3, ["src/api/b.py"]), "body": "Belt-Lead: a1"}
    body = await ar.read_architecture(
        "ws1", "repo", now=lambda: 0.0, gh=fake_gh(prs=[lead_pr]), runs=runs, touched=touched
    )
    live = body["live"]
    assert [r["action_id"] for r in live["components"]["api"]["runs"]] == ["a1"]
    assert live["repo"]["runs"] == []


async def test_a_gh_failure_keeps_the_model_fresh_and_serves_the_last_live_block_stale(origin):
    origin.push(PATHS_MODEL)
    origin.cloned()

    async def runs(ws):
        return []

    async def read(t, gh):
        return await ar.read_architecture("ws1", "repo", now=lambda: t, gh=gh, runs=runs)

    first = await read(0.0, fake_gh(fail=True))
    assert first["stale"] is False and first["live"] is None and first["model"]["scope"] == "app"
    ok = await read(100.0, fake_gh(prs=[pr(1, ["src/ui/a.ts"])]))
    sha = origin.push({**PATHS_MODEL, "scope": "moved"})
    body = await read(200.0, fake_gh(fail=True))
    assert (body["stale"], body["sha"]) == (False, sha)
    assert body["live"] == {**ok["live"], "stale": True}


async def test_the_gh_reads_run_together_and_one_failing_still_serves_stale(origin):
    origin.push(PATHS_MODEL)
    origin.cloned()
    started: list[str] = []

    async def runs(ws):
        return []

    async def read(t, gh):
        call = ar.read_architecture("ws1", "repo", now=lambda: t, gh=gh, runs=runs)
        return await asyncio.wait_for(call, 10)

    ok = await read(0.0, fake_gh(prs=[pr(1, ["src/ui/a.ts"])], started=started))
    assert sorted(started) == ["issue", "pr", "run"] and ok["live"]["stale"] is False
    started.clear()
    body = await read(100.0, fake_gh(fail={"pr"}, started=started))
    assert sorted(started) == ["issue", "pr", "run"]  # the other two still ran
    assert body["live"] == {**ok["live"], "stale": True}


async def test_the_lead_runs_reader_is_the_default(origin, monkeypatch):
    origin.push(PATHS_MODEL)
    origin.cloned()

    async def runs(ws):
        return []

    monkeypatch.setattr(abr, "_lead_runs", runs)
    body = await ar.read_architecture("ws1", "repo", now=lambda: 0.0)
    assert body["live"]["repo"]["status"] == "changed"  # the model commit landed today
