# tests/cloud/test_belt_plan_policy.py — the plan gate's auto policy.
#
# Two layers. The pure ``plan_policy.decide`` is pinned against the seven plan
# decisions in the supervisor's gate log from the first unattended pilot (the
# calibration fixtures below), plus each escalation rule and the env overrides.
# The wiring runs the real shift trigger, the real Instinct store and the real
# plan executor with ``POCKETPAW_BELT_PLAN_GATE=auto`` and a mocked Clef: an
# approved plan dispatches through the same resolve path the console uses, with
# ``system:belt-policy`` as the actor and the policy's reason on the blob; an
# escalated plan stays pending; with the flag off the gate is unchanged.
# SPEC CLARITY: Clef's per-task ``spec_clear_<i>`` (the cited issue states a
# concrete, testable acceptance criterion) under the threshold escalates the plan
# as ``spec_unclear`` naming those tasks, and the auto gate comments on and labels
# each one's GitHub source issue ``needs-spec`` (once). The gate log predates the
# question: its rows carry no spec score ("no opinion") and decide as before.

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud.belt import clef  # noqa: E402
from pocketpaw_ee.cloud.mandates import patrols, plan_policy  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402

import pocketpaw.stores as stores  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402

# The mandates module's fixtures and helpers: the real router + store + journal.
from tests.cloud.test_belt_mandates import (  # noqa: E402,F401
    USER,
    WS,
    RecorderDispatcher,
    _chain,
    _create_mandate,
    _make_client,
    dispatcher,
    graph,
    journal,
    mock_llm,
    store,
)

pytestmark = pytest.mark.usefixtures("any_repo_root")


def _v(scope, risk, indep, big, truncated=False, spec=()):
    return clef.PlanVerdict(
        in_scope=scope,
        low_risk=risk,
        tasks_independent=indep,
        too_big=big,
        truncated=truncated,
        spec_clear=tuple(spec),
    )


def _tasks(n: int, ref: str = "s1") -> list[dict]:
    return [{"title": f"task {i}", "evidence_refs": [ref]} for i in range(n)]


# The supervisor's gate log (belt-pilot.status.md, "Gate log" table): every
# plan decision, Clef's numbers, what the supervisor did, and what the policy
# must do. ``changed`` = the supervisor did not approve the plan as proposed.
GATE_LOG = [
    # (row, scope, risk, indep, big, tasks, supervisor, changed, policy_approves)
    ("plan s1", 0.93, 0.91, 0.59, 0.48, 4, "approve 4/4", False, True),
    ("plan s2", 0.92, 0.92, 0.58, 0.42, 4, "approve 4/4", False, True),
    ("plan s3", 0.88, 0.83, 0.52, 0.46, 3, "approve Q3+Q7, defer Q8", True, True),
    ("plan engines s1", 0.92, 0.95, 0.76, 0.35, 2, "approve S2+S6", False, True),
    ("plan quality s4", 0.90, 0.79, 0.27, 0.71, 2, "approve Q7b, split Q8", True, False),
    ("plan engines s2", 0.91, 0.93, 0.50, 0.32, 3, "approve P1+P4+P7", False, True),
    ("plan quality s5", 0.93, 0.90, 0.74, 0.11, 2, "approve Q8a+Q8b", False, True),
]


@pytest.mark.parametrize("row", GATE_LOG, ids=[r[0] for r in GATE_LOG])
def test_policy_against_the_supervisors_gate_log(row):
    """Six of seven rows reproduce the supervisor's call. ``plan s3`` is the
    known miss: the supervisor deferred Q8 for a FILE OVERLAP with Q3, a signal
    Clef does not score, and its independence (.52) sits above ``engines s2``
    (.50), which the supervisor approved in full, so no independence threshold
    separates them. The policy approves s3 as proposed."""
    name, scope, risk, indep, big, n, _supervisor, changed, approves = row
    decision = plan_policy.decide(_tasks(n), {"s1"}, 4, _v(scope, risk, indep, big))
    assert decision.approve is approves, decision.reason
    if name == "plan s3":
        assert changed and decision.approve  # the documented disagreement
    else:
        assert decision.approve is not changed


# Spec clarity rows, two tasks each, on the gate log's cleanest numbers (quality
# s5). Synthetic until the shadow eval records real ``spec_clear`` scores: the
# shapes are an issue with an "Acceptance test" section (high), one whose body
# is a wish ("make it better", low), the threshold itself, and an unanswered one.
SPEC_LOG = [
    # (row, spec_clear per task, approves, flagged task indices)
    ("both issues carry an acceptance test", (0.91, 0.88), True, ()),
    ("task 2's issue is a wish", (0.90, 0.31), False, (1,)),
    ("neither issue is testable", (0.42, 0.18), False, (0, 1)),
    ("on the threshold", (0.60, 0.60), True, ()),
    ("an unanswered spec is no opinion", (-1.0, 0.82), True, ()),
]


@pytest.mark.parametrize("row", SPEC_LOG, ids=[r[0] for r in SPEC_LOG])
def test_spec_clarity(row):
    _name, spec, approves, flagged = row
    d = plan_policy.decide(_tasks(2), {"s1"}, 4, _v(0.93, 0.90, 0.74, 0.11, spec=spec))
    assert d.approve is approves, d.reason
    assert d.tasks == flagged
    if not approves:
        assert d.rule == "spec_unclear"
        assert all(f"task {i + 1}" in d.reason for i in flagged)


def test_gate_log_rows_decide_the_same_with_clear_specs():
    """Calibration: the threshold flips none of the supervisor's calls when every
    task's issue is clear (they all carried an Acceptance test section)."""
    for name, scope, risk, indep, big, n, *_rest, approves in GATE_LOG:
        d = plan_policy.decide(_tasks(n), {"s1"}, 4, _v(scope, risk, indep, big, spec=[0.9] * n))
        assert d.approve is approves, (name, d.reason)


def test_spec_threshold_from_env(monkeypatch):
    assert plan_policy.Thresholds().min_spec_clear == 0.60
    monkeypatch.setenv("POCKETPAW_BELT_POLICY_MIN_SPEC_CLEAR", "0.95")
    t = plan_policy.thresholds_from_env()
    d = plan_policy.decide(_tasks(2), {"s1"}, 3, _v(0.9, 0.9, 0.7, 0.1, spec=(0.91, 0.96)), t)
    assert d.rule == "spec_unclear" and d.tasks == (0,)


def test_s4_escalates_on_independence_first():
    d = plan_policy.decide(_tasks(2), {"s1"}, 4, _v(0.90, 0.79, 0.27, 0.71))
    assert d.rule == "tasks_not_independent"
    assert "0.27" in d.reason


@pytest.mark.parametrize(
    "verdict,rule",
    [
        (None, "clef_unavailable"),
        (_v(0.9, 0.9, -1.0, 0.1), "clef_unavailable"),
        (_v(0.9, 0.9, 0.6, 0.1, truncated=True), "clef_truncated"),
        (_v(0.9, 0.9, 0.39, 0.1), "tasks_not_independent"),
        (_v(0.9, 0.9, 0.6, 0.66), "task_too_big"),
        (_v(0.79, 0.9, 0.6, 0.1), "out_of_scope"),
        (_v(0.9, 0.74, 0.6, 0.1), "risky"),
    ],
)
def test_each_escalation_rule(verdict, rule):
    d = plan_policy.decide(_tasks(2), {"s1"}, 3, verdict)
    assert not d.approve and d.rule == rule


def test_boundaries_are_inclusive():
    d = plan_policy.decide(_tasks(1), {"s1"}, 3, _v(0.80, 0.75, 0.40, 0.65))
    assert d.approve, d.reason


def test_a_task_without_open_evidence_escalates_the_whole_plan():
    tasks = _tasks(2) + [{"title": "invented work", "evidence_refs": ["gone"]}]
    d = plan_policy.decide(tasks, {"s1"}, 3, _v(0.95, 0.95, 0.9, 0.1))
    assert not d.approve and d.rule == "no_open_evidence"
    assert "task 3" in d.reason


def test_over_budget_escalates():
    d = plan_policy.decide(_tasks(4), {"s1"}, 3, _v(0.95, 0.95, 0.9, 0.1))
    assert not d.approve and d.rule == "over_budget"


def test_approve_reason_names_the_rule_and_clefs_numbers():
    d = plan_policy.decide(_tasks(2), {"s1"}, 3, _v(0.93, 0.90, 0.74, 0.11))
    assert d.approve and d.rule == "auto_approve"
    for part in ("in_scope 0.93", "low_risk 0.90", "tasks_independent 0.74", "too_big 0.11"):
        assert part in d.reason


def test_thresholds_from_env(monkeypatch):
    assert plan_policy.thresholds_from_env() == plan_policy.Thresholds()
    monkeypatch.setenv("POCKETPAW_BELT_POLICY_MIN_INDEPENDENT", "0.6")
    monkeypatch.setenv("POCKETPAW_BELT_POLICY_MAX_TOO_BIG", "nope")
    t = plan_policy.thresholds_from_env()
    assert t.min_independent == 0.6 and t.max_too_big == 0.65
    d = plan_policy.decide(_tasks(1), {"s1"}, 3, _v(0.9, 0.9, 0.59, 0.1), t)
    assert d.rule == "tasks_not_independent"


def test_flag_defaults_to_manual(monkeypatch):
    monkeypatch.delenv("POCKETPAW_BELT_PLAN_GATE", raising=False)
    assert not plan_policy.enabled()
    monkeypatch.setenv("POCKETPAW_BELT_PLAN_GATE", "auto")
    assert plan_policy.enabled()


# ---------------------------------------------------------------------------
# wiring — the real trigger, store and executor; Clef over a mock transport
# ---------------------------------------------------------------------------


def _clef(monkeypatch, scores: dict | None = None, exc: Exception | None = None) -> list:
    """Clef on, answering each noul question from ``scores``."""
    monkeypatch.setenv("POCKETPAW_BELT_CLEF", "shadow")
    monkeypatch.setenv("POCKETPAW_CLEF_TOKEN", "tok-test")
    monkeypatch.setenv("POCKETPAW_CLEF_ACCOUNT_ID", "acct-test")
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if exc is not None:
            raise exc
        qs = json.loads(request.content)["questions"]
        # Unlisted questions (each task's spec_clear_<i>) answer a clear 0.9.
        answers = {q: {"type": "noul", "noul": (scores or {}).get(q, 0.9)} for q in qs}
        return httpx.Response(200, json={"success": True, "result": {"answers": answers}})

    monkeypatch.setattr(clef, "_transport", httpx.MockTransport(handler))
    return seen


GOOD = {"in_scope": 0.93, "low_risk": 0.90, "tasks_independent": 0.74, "too_big": 0.11}


def _mandate_with_sightings(client, tmp_path, n: int = 2) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)
    for i in range(n):
        client.post(
            f"/belt/mandates/{mandate_id}/feedback",
            json={"text": f"signal {i}", "severity": 3, "source": "support"},
        )
    return mandate_id


async def _blob(plan_id: str) -> dict:
    return (await stores.get_instinct_store().get_action(plan_id)).parameters["_belt_plan"]


@pytest.mark.usefixtures("store", "graph")
async def test_auto_gate_approves_through_the_resolve_path(
    tmp_path,
    mongo_db,
    monkeypatch,
    journal,  # noqa: F811 — the imported fixture
    dispatcher,  # noqa: F811
    recording_bus,
):
    monkeypatch.setenv("POCKETPAW_BELT_PLAN_GATE", "auto")
    _clef(monkeypatch, GOOD)
    client = _make_client(monkeypatch)
    mandate_id = _mandate_with_sightings(client, tmp_path)

    out = await mandate_service.trigger_shift(WS, USER, mandate_id)

    assert out["shift"]["state"] == "done"
    plan_id = out["shift"]["plan_action_id"]
    action = await stores.get_instinct_store().get_action(plan_id)
    assert action.status == ActionStatus.EXECUTED, action.outcome
    assert len(RecorderDispatcher.instances[0].calls) == 2

    blob = action.parameters["_belt_plan"]
    assert blob["clef"]["verdict"]["tasks_independent"] == 0.74
    assert blob["policy"]["decision"] == "approved"
    assert blob["policy"]["approver"] == "system:belt-policy"
    assert "tasks_independent 0.74" in blob["policy"]["reason"]

    chain = _chain(journal, UUID(blob["correlation_id"]))
    assert [e.action for e in chain] == ["agent.proposed", "human.corrected", "decision.completed"]
    assert chain[1].actor.id == "user:system:belt-policy"
    assert chain[2].payload["action_outcome"] == "dispatched"

    (shift,) = (await mandate_service.get_mandate(WS, USER, mandate_id))["recent_shifts"]
    assert shift["approved_by"] == "system:belt-policy"
    assert shift["policy"]["decision"] == "approved"
    assert "tasks_independent 0.74" in shift["policy"]["reason"]

    (resolved,) = [e.data for e in recording_bus.events if e.type == "belt_plan_resolved"]
    assert resolved["resolved_by"] == "system:belt-policy"
    assert (resolved["plan_action_id"], resolved["approved"], resolved["rejected"]) == (
        plan_id,
        2,
        0,
    )


@pytest.mark.usefixtures("store", "journal", "graph")
@pytest.mark.parametrize(
    "scores,exc,rule",
    [
        (None, httpx.ConnectError("down"), "clef_unavailable"),
        ({**GOOD, "tasks_independent": 0.27, "too_big": 0.71}, None, "tasks_not_independent"),
    ],
)
async def test_auto_gate_escalates_and_leaves_the_plan_pending(
    tmp_path,
    mongo_db,
    monkeypatch,
    dispatcher,  # noqa: F811 — the imported fixture
    scores,
    exc,
    rule,
):
    monkeypatch.setenv("POCKETPAW_BELT_PLAN_GATE", "auto")
    _clef(monkeypatch, scores, exc)
    client = _make_client(monkeypatch)
    mandate_id = _mandate_with_sightings(client, tmp_path)

    out = await mandate_service.trigger_shift(WS, USER, mandate_id)

    assert out["shift"]["state"] == "in_gate"
    action = await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    assert action.status == ActionStatus.PENDING
    policy = action.parameters["_belt_plan"]["policy"]
    assert policy["decision"] == "escalated" and policy["rule"] == rule
    assert RecorderDispatcher.instances == []


@pytest.mark.usefixtures("store", "journal", "graph")
async def test_gate_off_is_unchanged(
    tmp_path,
    mongo_db,
    monkeypatch,
    dispatcher,  # noqa: F811 — the imported fixture
):
    """No flag: the plan waits for a human, Clef stays fire-and-forget, and no
    policy record is written, even with a plan Clef would pass."""
    monkeypatch.delenv("POCKETPAW_BELT_PLAN_GATE", raising=False)
    _clef(monkeypatch, GOOD)
    client = _make_client(monkeypatch)
    mandate_id = _mandate_with_sightings(client, tmp_path)

    out = await mandate_service.trigger_shift(WS, USER, mandate_id)
    import asyncio

    await asyncio.gather(*list(clef._TASKS))
    assert out["shift"]["state"] == "in_gate"
    blob = await _blob(out["shift"]["plan_action_id"])
    assert "policy" not in blob and "clef" in blob


async def test_shared_resolve_is_what_the_route_runs(monkeypatch):
    """The route delegates to ``service.resolve_plan``; there is one resolve."""
    import importlib

    mandates_router_mod = importlib.import_module("pocketpaw_ee.cloud.mandates.router")
    seen = []

    async def fake(workspace_id, user_id, mandate_id, body, *, approver):
        seen.append((workspace_id, user_id, mandate_id, body, approver))
        return {"shift": {}}

    monkeypatch.setattr(mandate_service, "resolve_plan", fake)
    user = SimpleNamespace(id="u9")
    await mandates_router_mod.resolve_plan("m1", {"x": 1}, user, WS, "u9")
    assert seen == [(WS, "u9", "m1", {"x": 1}, user)]


@pytest.mark.usefixtures("store", "journal", "graph")
async def test_a_slow_clef_escalates_but_still_records_its_score(
    tmp_path,
    mongo_db,
    monkeypatch,
    dispatcher,  # noqa: F811 — the imported fixture
):
    import asyncio

    monkeypatch.setenv("POCKETPAW_BELT_PLAN_GATE", "auto")
    monkeypatch.setenv("POCKETPAW_BELT_PLAN_GATE_CLEF_TIMEOUT", "0.05")
    _clef(monkeypatch, GOOD)
    fast = clef._transport

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return await fast.handle_async_request(request)

    monkeypatch.setattr(clef, "_transport", httpx.MockTransport(slow))
    client = _make_client(monkeypatch)
    mandate_id = _mandate_with_sightings(client, tmp_path)

    out = await mandate_service.trigger_shift(WS, USER, mandate_id)
    assert out["shift"]["state"] == "in_gate"
    plan_id = out["shift"]["plan_action_id"]
    assert (await _blob(plan_id))["policy"]["rule"] == "clef_unavailable"

    await asyncio.gather(*list(clef._TASKS))
    assert (await _blob(plan_id))["clef"]["verdict"]["in_scope"] == 0.93


@pytest.mark.usefixtures("store", "journal", "graph")
async def test_auto_gate_flags_the_unclear_issue_needs_spec(
    tmp_path,
    mongo_db,
    monkeypatch,
    dispatcher,  # noqa: F811 — the imported fixture
):
    monkeypatch.setenv("POCKETPAW_BELT_PLAN_GATE", "auto")
    seen = _clef(monkeypatch, {**GOOD, "spec_clear_1": 0.3})
    labelled: set[str] = {"8"}  # issue #8 was flagged on an earlier shift
    calls: list[list[str]] = []

    async def gh(argv: list[str]) -> tuple[int, str, str]:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "view"]:
            labels = [{"name": "needs-spec"}] if argv[3] in labelled else [{"name": "gap"}]
            return 0, json.dumps({"labels": labels}), ""
        return 0, "", ""

    monkeypatch.setattr(patrols, "_default_gh", gh)
    client = _make_client(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    mandate_id = _create_mandate(client, repo)
    for n, text in ((5, "**Acceptance test**\nUndo returns the document."), (6, "make it nicer")):
        client.post(
            f"/belt/mandates/{mandate_id}/feedback",
            json={
                "text": text,
                "severity": 9 - n,
                "source": f"github:qbtrix/creative-core#{n}",
            },
        )

    out = await mandate_service.trigger_shift(WS, USER, mandate_id)

    assert out["shift"]["state"] == "in_gate"
    action = await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    assert action.status == ActionStatus.PENDING
    policy = action.parameters["_belt_plan"]["policy"]
    assert policy["rule"] == "spec_unclear" and policy["tasks"] == [1]
    assert policy["thresholds"]["min_spec_clear"] == 0.6
    # Clef saw each task's cited issue text, and was asked about each task.
    body = json.loads(seen[0].content)
    assert "make it nicer" in body["state"]["cited_issues"]
    assert {"spec_clear_0", "spec_clear_1"} <= set(body["questions"])
    # Only task 2's issue (#6) is commented on and labelled, once.
    comments = [a for a in calls if a[:3] == ["gh", "issue", "comment"]]
    assert [c[3:6] for c in comments] == [["6", "-R", "qbtrix/creative-core"]]
    reason = comments[0][comments[0].index("--body") + 1]
    assert "\n" not in reason and "acceptance" in reason and re.search(r"0\.30", reason)
    edits = [a for a in calls if a[:3] == ["gh", "issue", "edit"]]
    assert edits == [
        ["gh", "issue", "edit", "6", "-R", "qbtrix/creative-core", "--add-label", "needs-spec"]
    ]
    assert RecorderDispatcher.instances == []


async def test_flag_needs_spec_skips_labelled_and_foreign_sources():
    calls: list[list[str]] = []

    async def gh(argv):
        calls.append(argv)
        return 0, json.dumps({"labels": [{"name": "needs-spec"}]}), ""

    assert await patrols.flag_needs_spec("github:o/r#8", "why", gh=gh) is False
    assert [a[:3] for a in calls] == [["gh", "issue", "view"]]  # labelled: no comment
    assert await patrols.flag_needs_spec("support", "why", gh=gh) is False
    assert await patrols.flag_needs_spec("github:-o/r#8", "why", gh=gh) is False
    assert len(calls) == 1
