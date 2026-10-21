# tests/cloud/test_belt_architecture_live.py — the ``live`` block of
# ``GET /belt/architecture``.
#
# Pure: the status rule by precedence, the PR check roll-up, a finding's
# ``Files:`` line, every signal joined to its components (none: ``repo``)
# and the block's size at creative-apps' counts.

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.belt import architecture_live as al

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


def test_the_block_stays_small_at_creative_apps_counts():
    findings = [finding(n, "x" * 700 + "\nFiles: src/ui/a.ts") for n in range(150)]
    prs = [pr(n, [f"src/api/{i}.py" for i in range(30)], [check()] * 5) for n in range(100)]
    live = al.derive(PATHS_MODEL, runs=[], prs=prs, log="", findings=findings, dev_ci=None, now=NOW)
    assert len(json.dumps(live)) < 60_000
