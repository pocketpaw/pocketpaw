# tests/cloud/test_belt_lead_planner.py — the feature-lead shift plan.
#
# A charter with ``run_kind: "feature-lead"`` marks a lead mandate. Its shift
# skips the foreman: ``lead_planner`` plans ONE ``feature-lead`` run per free
# feature issue (not in flight, not blocked by an open dependency, not closed),
# p1 before p2 before p3 before unlabelled, then oldest first, up to the shift's
# budget (the line's headroom). Each task's title is the issue title, its why a
# one-line ``#<n>`` pointer (what the cloud station reads the lead's issue
# from), its recipe ``feature-lead`` and its evidence the sighting. The plan is
# approved with no human and no Clef, as ``system:lead-planner``, through the
# auto gate's approve path, with the gate flag off or on. A task mandate (the
# default) still goes to the foreman and the manual plan gate.
# Real store (mongomock), real trigger and executor, recorder dispatcher; gh is
# a fake executor.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud.belt import cloud_station  # noqa: E402
from pocketpaw_ee.cloud.mandates import lead_planner, patrols  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import LEAD_RUN_KIND, SightingDoc  # noqa: E402
from pocketpaw_ee.cloud.mandates.dto import CharterRequest  # noqa: E402
from pydantic import ValidationError as PydanticValidationError  # noqa: E402

import pocketpaw.stores as stores  # noqa: E402
from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from tests.cloud.test_belt_github_issues_patrol import (  # noqa: E402
    SLUG,
    FakeGh,
    _fresh_gh_caches,  # noqa: F401 — autouse: the gh caches are per test
    _github_mandate,
    _issue,
)
from tests.cloud.test_belt_mandates import (  # noqa: E402,F401
    USER,
    WS,
    RecorderDispatcher,
    _chain,
    _seed_run,
    dispatcher,
    foreman_calls,
    graph,
    journal,
    mock_llm,
    store,
)

pytestmark = pytest.mark.usefixtures("any_repo_root")

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# lead_planner.plan — pure
# ---------------------------------------------------------------------------


def _s(n: int, *labels: str, age_h: float = 1.0, source: str | None = None) -> dict:
    return {
        "id": f"s{n}",
        "evidence": {
            "source": source if source is not None else f"github:{SLUG}#{n}",
            "number": n,
            "title": f"Feature {n}",
            "labels": ["feature-lead", *labels],
            "url": f"https://github.com/{SLUG}/issues/{n}",
        },
        "ts": NOW - timedelta(hours=age_h),
    }


def _numbers(plan) -> list[str]:
    return [t.evidence_refs[0] for t in plan.tasks]


def test_one_task_per_issue_p1_first_then_oldest():
    sightings = [
        _s(1, "p2", age_h=5),
        _s(2, "p1", age_h=3),
        _s(3, age_h=9),  # no priority label: after p3, however old
        _s(4, "p3", age_h=6),
        _s(5, "P1", age_h=4),  # labels match case-insensitively
        _s(6, "p2", "p1", age_h=1),  # the highest label wins
    ]
    plan = lead_planner.plan(7, sightings, 10)
    assert plan.shift_no == 7 and not plan.no_action
    assert _numbers(plan) == ["s5", "s2", "s6", "s1", "s4", "s3"]


def test_each_task_is_a_feature_lead_on_its_issue():
    (task,) = lead_planner.plan(1, [_s(42, "p1")], 3).tasks
    assert task.title == "Feature 42"
    assert task.why == f"Feature issue #42: https://github.com/{SLUG}/issues/42"
    assert "\n" not in task.why
    assert task.recipe == LEAD_RUN_KIND == "feature-lead"
    assert task.evidence_refs == ["s42"]
    assert cloud_station._ISSUE.search(f"{task.title}\n\n{task.why}").group(1) == "42"


def test_the_budget_caps_the_plan():
    sightings = [_s(n, "p2", age_h=n) for n in range(1, 6)]
    assert _numbers(lead_planner.plan(1, sightings, 2)) == ["s5", "s4"]
    assert lead_planner.plan(1, sightings, 0).no_action


def test_no_free_issue_stands_down():
    plan = lead_planner.plan(1, [], 3)
    assert plan.no_action and plan.tasks == []
    assert plan.no_action_reason == "no free feature issue"


def test_a_sighting_without_an_issue_is_not_lead_work():
    """A lead is briefed from its issue: a feedback sighting has none."""
    sightings = [_s(1, "p1", source="support"), _s(2, "p3")]
    assert _numbers(lead_planner.plan(1, sightings, 3)) == ["s2"]


def test_a_hand_filed_issue_falls_back_to_its_number():
    s = _s(9)
    s["evidence"] = {"source": f"github:{SLUG}#9"}
    (task,) = lead_planner.plan(1, [s], 3).tasks
    assert (task.title, task.why) == ("Feature issue #9", "Feature issue #9")


def test_the_run_kind_is_the_cloud_stations_lead_recipe():
    assert LEAD_RUN_KIND == cloud_station.LEAD_RECIPE


def test_the_charter_request_carries_the_run_kind():
    assert CharterRequest(goal="g").run_kind == "task"
    assert CharterRequest(goal="g", run_kind="feature-lead").run_kind == "feature-lead"
    with pytest.raises(PydanticValidationError):
        CharterRequest(goal="g", run_kind="lead")


# ---------------------------------------------------------------------------
# trigger_shift — the real trigger, store and executor
# ---------------------------------------------------------------------------


def _lead_gh() -> FakeGh:
    """#16 and #11 p1 (#16 older), #10 p2, #15 p3, #17 unlabelled (oldest);
    #12 p1 but blocked by the open #412; #14 p1 but an open PR closes it; #13
    hand-filed and closed."""
    blocked = _issue(12, "feature-lead", "p1")
    blocked["body"] = "**Behavior**\nexport\n\nDepends on #412"
    issues = [
        _issue(10, "feature-lead", "p2", title="Text shapes"),
        _issue(11, "feature-lead", "p1", title="Brush engine"),
        blocked,
        _issue(14, "feature-lead", "p1"),
        _issue(15, "feature-lead", "p3"),
        _issue(16, "feature-lead", "p1", title="Layer panel"),
        _issue(17, "feature-lead"),
    ]
    return FakeGh(
        issues,
        states={13: "CLOSED", 412: "OPEN"},
        prs=[{"number": 160, "body": "Fixes #14"}],
    )


AGES_H = {10: 5, 11: 3, 12: 8, 13: 7, 14: 9, 15: 6, 16: 4, 17: 10}


async def _lead_mandate(tmp_path, monkeypatch, gh: FakeGh, **charter) -> tuple[str, dict]:
    """A lead line over ``gh``'s issues, patrolled once, its sightings aged by
    ``AGES_H``. Returns the mandate id and ``{issue number: sighting id}``."""
    monkeypatch.setattr(patrols, "_default_gh", gh)
    charter = {
        "cadence": "manual",
        "issue_labels": ["feature-lead"],
        "budget": {"max_tasks_per_shift": 4, "max_in_flight": 4},
        **charter,
    }
    mid = await _github_mandate(tmp_path, monkeypatch, gh, **charter)
    await mandate_service.file_feedback(
        WS, USER, mid, {"text": "GitHub issue #13", "severity": 3, "source": f"github:{SLUG}#13"}
    )
    await mandate_service.run_patrols(WS, USER, mid)
    ids = {}
    for d in await SightingDoc.find(SightingDoc.mandate_id == mid).to_list():
        n = int(str(d.evidence["source"]).rsplit("#", 1)[1])
        d.ts = NOW - timedelta(hours=AGES_H[n])
        await d.save()
        ids[n] = str(d.id)
    return mid, ids


@pytest.mark.usefixtures("store", "graph")
@pytest.mark.parametrize("gate", [None, "auto"])
async def test_a_lead_shift_plans_one_run_per_free_issue_and_approves_itself(
    tmp_path,
    mongo_db,
    monkeypatch,
    journal,  # noqa: F811 — the imported fixture
    dispatcher,  # noqa: F811
    foreman_calls,  # noqa: F811
    gate,
):
    if gate:
        monkeypatch.setenv("POCKETPAW_BELT_PLAN_GATE", gate)
    else:
        monkeypatch.delenv("POCKETPAW_BELT_PLAN_GATE", raising=False)
    mid, ids = await _lead_mandate(tmp_path, monkeypatch, _lead_gh(), run_kind="feature-lead")

    out = await mandate_service.trigger_shift(WS, USER, mid)

    assert foreman_calls == [], "a lead shift never calls the foreman"
    assert out["shift"]["state"] == "done" and out["shift"]["task_count"] == 4
    action = await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    assert action.status == ActionStatus.EXECUTED, action.outcome
    assert action.approved_by == "system:lead-planner"
    blob = action.parameters["_belt_plan"]
    tasks = blob["plan"]["tasks"]
    # p1 (#16 older than #11), p2, p3; #17 unlabelled waits for headroom;
    # #12 blocked, #13 closed and #14 in flight are never planned.
    assert [t["evidence_refs"] for t in tasks] == [[ids[n]] for n in (16, 11, 10, 15)]
    assert [t["title"] for t in tasks[:3]] == ["Layer panel", "Brush engine", "Text shapes"]
    assert all(t["recipe"] == "feature-lead" for t in tasks)
    assert tasks[0]["why"] == f"Feature issue #16: https://github.com/{SLUG}/issues/16"
    assert blob["policy"]["decision"] == "approved"
    assert blob["policy"]["rule"] == "lead_plan"
    assert blob["policy"]["approver"] == "system:lead-planner"
    assert "clef" not in blob

    calls = RecorderDispatcher.instances[0].calls
    assert [c["task"]["recipe"] for c in calls] == ["feature-lead"] * 4
    chain = _chain(journal, UUID(blob["correlation_id"]))
    assert chain[1].actor.id == "user:system:lead-planner"

    detail = await mandate_service.get_mandate(WS, USER, mid)
    assert detail["charter"]["run_kind"] == "feature-lead"
    (shift,) = detail["recent_shifts"]
    assert shift["approved_by"] == "system:lead-planner"
    assert shift["policy"]["decision"] == "approved"


@pytest.mark.usefixtures("graph", "journal")
async def test_a_lead_shift_plans_within_the_lines_headroom(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
    dispatcher,  # noqa: F811
):
    """One run already at its gate on a line capped at 3 in flight: the shift's
    budget is 2, so the two p1 issues and nothing else."""
    mid, ids = await _lead_mandate(
        tmp_path,
        monkeypatch,
        _lead_gh(),
        run_kind="feature-lead",
        budget={"max_tasks_per_shift": 4, "max_in_flight": 3},
    )
    await _seed_run(store, mid, "an earlier lead")

    out = await mandate_service.trigger_shift(WS, USER, mid)

    blob = (await store.get_action(out["shift"]["plan_action_id"])).parameters["_belt_plan"]
    assert [t["evidence_refs"] for t in blob["plan"]["tasks"]] == [[ids[16]], [ids[11]]]


@pytest.mark.usefixtures("store", "graph", "journal")
async def test_a_lead_line_with_no_free_issue_stands_down(
    tmp_path,
    mongo_db,
    monkeypatch,
    dispatcher,  # noqa: F811
    foreman_calls,  # noqa: F811
):
    gh = FakeGh([_issue(14, "feature-lead", "p1")], prs=[{"number": 160, "body": "Fixes #14"}])
    monkeypatch.setattr(patrols, "_default_gh", gh)
    mid = await _github_mandate(
        tmp_path, monkeypatch, gh, issue_labels=["feature-lead"], run_kind="feature-lead"
    )

    out = await mandate_service.trigger_shift(WS, USER, mid)

    assert out["shift"]["state"] == "stood_down"
    assert out["shift"]["no_action_reason"] == "no free feature issue"
    assert foreman_calls == [] and RecorderDispatcher.instances == []


@pytest.mark.usefixtures("store", "graph", "journal")
async def test_a_task_mandate_still_goes_to_the_foreman_and_the_manual_gate(
    tmp_path,
    mongo_db,
    monkeypatch,
    dispatcher,  # noqa: F811
    foreman_calls,  # noqa: F811
):
    monkeypatch.delenv("POCKETPAW_BELT_PLAN_GATE", raising=False)
    mid, _ids = await _lead_mandate(tmp_path, monkeypatch, _lead_gh())

    out = await mandate_service.trigger_shift(WS, USER, mid)

    assert len(foreman_calls) == 1
    assert out["shift"]["state"] == "in_gate"
    action = await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    assert action.status == ActionStatus.PENDING and action.approved_by is None
    blob = action.parameters["_belt_plan"]
    assert "policy" not in blob
    assert all(t.get("recipe") is None for t in blob["plan"]["tasks"])
    assert RecorderDispatcher.instances == []
    detail = await mandate_service.get_mandate(WS, USER, mid)
    assert detail["charter"]["run_kind"] == "task"
