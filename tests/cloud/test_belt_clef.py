# tests/cloud/test_belt_clef.py — Belt's shadow consult of Cloudflare Clef.
#
# The HTTP boundary is an ``httpx.MockTransport`` (``clef._transport``); nothing
# reaches Cloudflare. Pinned: the request shape (URL, bearer, model, typed
# questions), answer parsing for plans and diffs, the diff state's file list +
# named truncation, flag off / missing credentials -> None with no request, one
# retry on a 5xx, a timeout -> None, and that a failing Clef never breaks a
# plan proposal (through the router) while a working one records its verdict on
# the ``_belt_plan`` blob, which ``list_verdicts`` reads back beside the outcome.

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud.belt import clef  # noqa: E402

import pocketpaw.stores as stores  # noqa: E402

# The mandates module's fixtures and helpers: the real router + store + journal.
from tests.cloud.test_belt_headless import CANNED_DIFF, _queue_station_run  # noqa: E402
from tests.cloud.test_belt_mandates import (  # noqa: E402,F401
    USER,
    WS,
    _create_mandate,
    _make_client,
    graph,
    journal,
    mock_llm,
    store,
)

pytestmark = pytest.mark.usefixtures("any_repo_root")


def _answers(qs: dict) -> dict:
    out = {}
    for qid, q in qs.items():
        if q["type"] == "noul":
            out[qid] = {"type": "noul", "noul": 0.25}
        else:
            out[qid] = {
                "type": "choice",
                "choice": "fix",
                "probabilities": {"fix": 0.8, "feature": 0.2},
                "confidence": 0.7,
            }
    return {"success": True, "result": {"model": "clef", "answers": out}}


class Fake:
    """Records requests; replies with ``statuses`` in order, then 200 answers."""

    def __init__(self, statuses: list[int] | None = None, exc: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.statuses = list(statuses or [])
        self.exc = exc

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        if self.statuses:
            return httpx.Response(self.statuses.pop(0), json={"success": False})
        return httpx.Response(200, json=_answers(json.loads(request.content)["questions"]))


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("POCKETPAW_BELT_CLEF", "shadow")
    monkeypatch.setenv("POCKETPAW_CLEF_TOKEN", "tok-test")
    monkeypatch.setenv("POCKETPAW_CLEF_ACCOUNT_ID", "acct-test")

    def use(fake: Fake) -> Fake:
        monkeypatch.setattr(clef, "_transport", httpx.MockTransport(fake))
        return fake

    return use


TASKS = [{"title": "bump lodash", "why": "CVE", "evidence_refs": ["s1"]}]


async def test_plan_request_shape_and_parse(on):
    fake = on(Fake())
    evidence = {"s1": "**Acceptance test**\nlodash >= 4.17.21 in the lockfile"}
    verdict = await clef.score_plan(TASKS, "Goal: deps", "Boundary: no auth", evidence=evidence)
    assert verdict == clef.PlanVerdict(0.25, 0.25, 0.25, 0.25, truncated=False, spec_clear=(0.25,))
    (req,) = fake.requests
    assert req.method == "POST"
    assert str(req.url).endswith("/accounts/acct-test/ai/run/@cf/cloudflare/clef")
    assert req.headers["authorization"] == "Bearer tok-test"
    body = json.loads(req.content)
    assert body["model"] == "clef"
    assert body["state"]["mandate_scope"] == "Goal: deps"
    assert "bump lodash" in body["state"]["plan_tasks"]
    assert "lodash >= 4.17.21" in body["state"]["cited_issues"]
    assert set(body["questions"]) == {
        "in_scope",
        "low_risk",
        "tasks_independent",
        "too_big",
        "spec_clear_0",
    }
    assert "task 1" in body["questions"]["spec_clear_0"]["instructions"]
    assert all(q["type"] == "noul" and q["criteria"] for q in body["questions"].values())


async def test_diff_parse_choice_and_truncation(on):
    fake = on(Fake())
    diff = "diff --git a/x.py b/x.py\n+print(1)\n" + "+pad\n" * 20_000
    verdict = await clef.score_diff(diff, clef.DEFAULT_DIFF_RUBRIC, "- App / Helper: does x")
    assert verdict is not None
    assert verdict.change_kind == "fix" and verdict.change_kind_confidence == 0.7
    assert verdict.tests_assert_behavior == 0.25 and verdict.truncated is True
    body = json.loads(fake.requests[0].content)
    assert body["questions"]["change_kind"]["type"] == "choice"
    assert body["questions"]["change_kind"]["criteria"]["fix"]
    shown = body["state"]["diff"]
    assert shown.startswith("Files changed (1):\n- x.py")
    assert "[truncated: showing the first" in shown
    assert len(shown) <= clef.MAX_STATE_CHARS + 200


async def test_flag_off_returns_none_without_a_request(on, monkeypatch):
    fake = on(Fake())
    monkeypatch.delenv("POCKETPAW_BELT_CLEF")
    assert await clef.score_plan(TASKS, "s", "r") is None
    assert clef.shadow_plan(WS, "a1", TASKS, {}) is None
    assert fake.requests == []


async def test_missing_credentials_return_none(on, monkeypatch):
    fake = on(Fake())
    monkeypatch.delenv("POCKETPAW_CLEF_TOKEN")
    assert await clef.score_diff("diff", "r", "h") is None
    assert fake.requests == []


async def test_one_retry_on_5xx(on):
    fake = on(Fake(statuses=[503]))
    assert await clef.score_plan(TASKS, "s", "r") is not None
    assert len(fake.requests) == 2
    fake = on(Fake(statuses=[502, 500]))
    assert await clef.score_plan(TASKS, "s", "r") is None
    assert len(fake.requests) == 2  # one retry only
    fake = on(Fake(statuses=[400]))
    assert await clef.score_plan(TASKS, "s", "r") is None
    assert len(fake.requests) == 1  # a 4xx is not retried


async def test_timeout_returns_none(on):
    fake = on(Fake(exc=httpx.ReadTimeout("slow")))
    assert await clef.score_diff("diff", "r", "h") is None
    assert len(fake.requests) == 1


async def test_shadow_hook_swallows_failure_and_records_nothing(on, monkeypatch):
    on(Fake(exc=RuntimeError("boom")))
    recorded: list = []

    async def record(*a, **k):
        recorded.append(a)
        return True

    monkeypatch.setattr(clef, "record_verdict", record)
    task = clef.shadow_plan(WS, "a1", TASKS, {"goal": "g"})
    assert task is not None
    await task  # never raises
    assert recorded == []


@pytest.mark.usefixtures("store", "journal", "graph")
async def test_failing_clef_never_breaks_a_plan_proposal(tmp_path: Path, mongo_db, monkeypatch, on):
    fake = on(Fake(exc=httpx.ConnectError("down")))
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)
    client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "lodash CVE", "severity": 4, "source": "support"},
    )
    res = client.post(f"/belt/mandates/{mandate_id}/shift")
    assert res.status_code == 200, res.text
    assert res.json()["shift"]["state"] == "in_gate"
    # And on the test loop, where the background task can be awaited: a Clef
    # outage leaves the proposal intact and unscored.
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "builds got slower", "severity": 3, "source": "support"},
    )
    out = await mandate_service.trigger_shift(WS, USER, mandate_id)
    await asyncio.gather(*list(clef._TASKS))
    assert out["shift"]["state"] == "in_gate"
    action = await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    blob = action.parameters["_belt_plan"]
    assert "clef" not in blob
    assert fake.requests  # it did try


@pytest.mark.usefixtures("store", "journal", "graph")
async def test_shadow_plan_records_verdict_and_lists_it(tmp_path: Path, mongo_db, monkeypatch, on):
    on(Fake())
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)
    client.post(
        f"/belt/mandates/{mandate_id}/feedback",
        json={"text": "lodash CVE", "severity": 4, "source": "support"},
    )
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    out = await mandate_service.trigger_shift(WS, USER, mandate_id)
    await asyncio.gather(*list(clef._TASKS))
    plan_id = out["shift"]["plan_action_id"]
    rec = (await stores.get_instinct_store().get_action(plan_id)).parameters["_belt_plan"]["clef"]
    assert rec["kind"] == "plan" and rec["verdict"]["in_scope"] == 0.25
    rows = await clef.list_verdicts(WS)
    assert [(r["action_id"], r["kind"], r["outcome"]) for r in rows] == [
        (plan_id, "plan", "pending")
    ]


@pytest.mark.usefixtures("store")
async def test_headless_attach_records_a_diff_verdict(on):
    from pocketpaw_ee.cloud.belt.headless import DevelopResult, HeadlessDevelopRunner

    fake = on(Fake())
    st = stores.get_instinct_store()
    action_id = await _queue_station_run(st)

    async def develop(_req):
        return DevelopResult(diff=CANNED_DIFF, base_branch="main", summary="adds hello.txt")

    await HeadlessDevelopRunner(develop_fn=develop).run(action_id)
    await asyncio.gather(*list(clef._TASKS))
    body = json.loads(fake.requests[0].content)
    assert "- hello.txt" in body["state"]["diff"]
    rec = (await st.get_action(action_id)).parameters["_code_change"]["clef"]
    assert rec["kind"] == "diff" and rec["verdict"]["change_kind"] == "fix"
    rows = await clef.list_verdicts(WS)
    assert [(r["action_id"], r["kind"], r["outcome"]) for r in rows] == [
        (action_id, "diff", "pending")
    ]
