# tests/cloud/test_belt_lead_planner.py — the feature-lead shift plan.
#
# A charter with ``run_kind: "feature-lead"`` marks a lead mandate. Its shift
# skips the foreman: ``lead_planner`` plans ONE ``feature-lead`` run per line of
# free feature issues (not in flight, not blocked by an open dependency, not
# closed), p0 before p1 before p2 before p3 before unlabelled, then oldest first
# (a charter with ``beta_only`` plans no p3), grouped
# by ``line:<app>`` up to ``POCKETPAW_BELT_LINE_LEAD_MAX`` (3; 1 = one issue a
# lead; no line label = alone), up to the shift's budget in runs (the line's
# headroom). Each task's title is the first issue's title, its why a one-line
# ``#<n>`` pointer naming every issue (what the cloud station reads the lead's
# issues from), its recipe ``feature-lead`` and its evidence every sighting, so
# each issue counts in flight and each failure counts per issue. The plan is
# approved with no human and no Clef, as ``system:lead-planner``, through the
# auto gate's approve path, with the gate flag off or on. A task mandate (the
# default) still goes to the foreman and the manual plan gate.
# NEVER TWICE, UNTIL RE-ARMED: an issue labelled ``needs-human`` (live, off a
# fresh ``--label`` list per charter label, so an old issue past the open list's
# limit counts too: the station's releases label it, then reject the run) is not
# planned, nor is one whose lead run is still in flight. A released lead's issue
# is planned again once a human takes the label off, and held again when that
# lead is released too. BREAKER: a failed lead is tried again until 3 failures
# across all the issue's sightings (a re-filed issue keeps its count); then the
# issue is labelled needs-human with a one-line reason, and a human removing the
# label re-arms it for 3 more. A title naming another issue (``#12``)
# is prefixed with its own number, so the lead is briefed on the right issue.
# CLOUD ONLY: a lead mandate is refused (422) at create and at trigger unless
# the cloud develop station is the wired dispatcher; the local station has no
# command for the ``feature-lead`` recipe.
# Real store (mongomock), real trigger and executor, recorder dispatcher; gh is
# a fake executor.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud._core.errors import ValidationError  # noqa: E402
from pocketpaw_ee.cloud.belt import cloud_station, headless  # noqa: E402
from pocketpaw_ee.cloud.mandates import lead_planner, patrols  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import LEAD_RUN_KIND, ShiftDoc, SightingDoc  # noqa: E402
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


class LeadRecorder(RecorderDispatcher):
    """The recorder standing in for the wired cloud station."""

    runs_leads = True


@pytest.fixture
def cloud_station_wired(monkeypatch, dispatcher):  # noqa: F811
    """Approved tasks reach the cloud station (a recorder that runs leads)."""
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "headless")
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", LeadRecorder())


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


def test_a_p0_issue_is_planned_before_every_p1():
    """A ``p0`` issue jumps the queue however new, and leads its line's group.
    Mutation: p0 unranked plans the old p1s first."""
    sightings = [
        _s(1, "p1", "line:ui", age_h=9),
        _s(2, "p1", age_h=8),
        _s(3, "p0", "line:ui", age_h=1),
    ]
    assert _groups(lead_planner.plan(1, sightings, 1)) == [["s3", "s1"]]
    assert _groups(lead_planner.plan(1, sightings, 5)) == [["s3", "s1"], ["s2"]]


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


def test_an_issue_labelled_needs_human_is_not_planned_again():
    """The live labels (``labels``) win over the ones filed with the sighting."""
    held = _s(1, "p1", age_h=5)
    held["labels"] = ["feature-lead", "p1", "Needs-Human"]
    cleared = _s(2, "p1", age_h=4)
    cleared["evidence"]["labels"].append("needs-human")
    cleared["labels"] = ["feature-lead", "p1"]  # a human took the label off
    stale = _s(3, "p2", "needs-human", age_h=3)  # no live row: the filed labels
    assert _numbers(lead_planner.plan(1, [held, cleared, stale], 5)) == ["s2"]


def _failed(n: int, runs: int, *, breaker_at: int = 0, live: tuple[str, ...] = ()) -> dict:
    """Issue ``n`` whose leads failed ``runs`` times (``failed`` or ``develop
    failed``, across all its sightings), its breaker last tripped at ``breaker_at``."""
    s = _s(n, "p1", age_h=5)
    s["failed"] = [f"run-{n}-{i}" for i in range(runs)]
    s["breaker_at"] = breaker_at
    s["labels"] = ["feature-lead", "p1", *live]
    return s


def test_a_failed_lead_is_tried_again_until_its_third_failure():
    """Mutation: ``>= BREAKER`` as ``> BREAKER`` plans a third try; as ``>= 2``
    holds after two."""
    assert _numbers(lead_planner.plan(2, [_failed(1, 1), _failed(2, 2)], 5)) == ["s1", "s2"]
    assert lead_planner.plan(4, [_failed(1, 3)], 5).no_action
    assert lead_planner.plan(4, [_failed(1, 3, live=("needs-human",))], 5).no_action


def test_a_tripped_breaker_names_the_issue_with_a_one_line_reason():
    (trip,) = lead_planner.trips([_failed(1, 3), _failed(2, 2), _s(3, "p1")])
    sighting, reason = trip
    assert sighting["id"] == "s1"
    assert reason.startswith("Belt: 3 feature lead attempts failed")
    assert "run-1-0" in reason and "\n" not in reason
    # Already labelled: nothing to trip, the label holds it.
    assert lead_planner.trips([_failed(1, 3, live=("needs-human",))]) == []
    # A sighting with no issue has nobody to label.
    foreign = _failed(4, 3)
    foreign["evidence"]["source"] = "support"
    assert lead_planner.trips([foreign]) == []


def test_a_human_re_arms_a_tripped_breaker_for_three_more_tries():
    """The trip records how many failures it saw; taking ``needs-human`` off
    re-arms the issue, and three more failures trip it again."""
    assert _numbers(lead_planner.plan(5, [_failed(1, 3, breaker_at=3)], 5)) == ["s1"]
    assert _numbers(lead_planner.plan(5, [_failed(1, 5, breaker_at=3)], 5)) == ["s1"]
    assert lead_planner.plan(5, [_failed(1, 6, breaker_at=3)], 5).no_action
    assert lead_planner.trips([_failed(1, 6, breaker_at=3)])


def test_the_same_failed_run_counts_once():
    s = _failed(1, 0)
    s["failed"] = ["run-a", "run-a", "run-a"]
    assert _numbers(lead_planner.plan(2, [s], 5)) == ["s1"]


def _released(n: int, *statuses: str, live: tuple[str, ...]) -> dict:
    """Issue ``n`` whose citing lead tasks ended in ``statuses`` (shift 1, 2, ...),
    its live labels ``live``."""
    s = _s(n, "p1", "needs-human")
    s["tasks"] = [
        {"shift_no": i, "title": f"Feature {n}", "status": st}
        for i, st in enumerate(statuses, start=1)
    ]
    s["labels"] = ["feature-lead", "p1", *live]
    return s


def test_a_released_lead_is_held_while_needs_human_is_on():
    assert lead_planner.plan(2, [_released(1, "rejected", live=("needs-human",))], 5).no_action


def test_a_released_lead_a_human_re_armed_is_planned_again():
    """The station labelled it and rejected the run; a human took the label off."""
    assert _numbers(lead_planner.plan(2, [_released(1, "rejected", live=())], 5)) == ["s1"]


def test_a_re_armed_lead_released_again_is_held_again():
    """No loop: the second release labels the issue again; only a second
    removal frees it. Released runs never count toward the breaker."""
    again = _released(1, "rejected", "rejected", live=("needs-human",))
    assert lead_planner.plan(3, [again], 5).no_action
    cleared = _released(2, "rejected", "rejected", "rejected", live=())
    assert _numbers(lead_planner.plan(3, [cleared], 5)) == ["s2"]


def test_a_title_naming_another_issue_leads_with_its_own_number():
    s = _s(40, "p1")
    s["evidence"]["title"] = "Follow-up to #12: brushes"
    (task,) = lead_planner.plan(1, [s], 3).tasks
    assert task.title == "#40 Follow-up to #12: brushes"
    assert cloud_station._ISSUE.search(f"{task.title}\n\n{task.why}").group(1) == "40"
    plain = lead_planner.plan(1, [_s(41)], 3).tasks[0]
    assert plain.title == "Feature 41"  # no other issue named: the title as filed


# ---------------------------------------------------------------------------
# line leads: one run per line, up to N features
# ---------------------------------------------------------------------------


def _groups(plan) -> list[list[str]]:
    return [t.evidence_refs for t in plan.tasks]


def test_issues_on_one_line_share_a_lead_in_todays_order():
    """Free issues of a ``line:<app>`` label go to one lead, up to 3 (the
    default), in today's order (p1 first, then oldest); the next ones of the
    line get the next lead. An issue with no line label leads alone. Mutation:
    grouping ignored plans one run per issue."""
    sightings = [
        _s(1, "p2", "line:flare", age_h=5),
        _s(2, "p1", "line:flare", age_h=3),
        _s(3, "p1", "Line:Halo", age_h=2),  # the label matches case-insensitively
        _s(4, "p3", "line:flare", age_h=6),
        _s(5, "p1", "line:flare", age_h=4),
        _s(6, "p2", age_h=9),  # no line: alone
        _s(7, "p3", "line:halo", age_h=1),
    ]
    plan = lead_planner.plan(1, sightings, 10)
    assert _groups(plan) == [["s5", "s2", "s1"], ["s3", "s7"], ["s6"], ["s4"]]


def test_a_line_lead_names_every_issue_and_leads_with_the_first():
    a, b = _s(16, "p1", "line:flare", age_h=4), _s(11, "p1", "line:flare", age_h=3)
    b["evidence"]["title"] = "Follow-up to #12: brushes"
    (task,) = lead_planner.plan(1, [a, b], 3).tasks
    assert task.recipe == LEAD_RUN_KIND
    assert task.evidence_refs == ["s16", "s11"]
    assert task.title == "Feature 16 + 1 more on line:flare"
    assert task.why == (
        f"Feature issues #16, #11 on line:flare: https://github.com/{SLUG}/issues/16 "
        f"https://github.com/{SLUG}/issues/11"
    )
    assert "\n" not in task.why
    text = f"{task.title}\n\n{task.why}"
    assert cloud_station._ISSUE.search(text).group(1) == "16"  # the station's lead.issue
    assert cloud_station._lead_numbers(text) == [16, 11]
    assert task.expected_outcome.startswith("#16, #11 shipped by their feature lead")


@pytest.mark.parametrize(
    ("n", "groups"),
    [(2, [["s1", "s2"], ["s3", "s4"], ["s5"]]), (4, [["s1", "s2", "s3", "s4"], ["s5"]])],
)
def test_the_line_max_caps_the_features_per_lead(n, groups):
    """Mutation: the cap dropped puts all five on one lead."""
    sightings = [_s(i, "p1", "line:flare", age_h=10 - i) for i in range(1, 6)]
    assert _groups(lead_planner.plan(1, sightings, 10, line_max=n)) == groups


def test_the_budget_counts_leads_not_features():
    """The in-flight cap counts runs (sessions): 2 of headroom is 2 leads."""
    sightings = [_s(i, "p1", "line:flare", age_h=10 - i) for i in range(1, 8)]
    assert _groups(lead_planner.plan(1, sightings, 2)) == [["s1", "s2", "s3"], ["s4", "s5", "s6"]]


def test_a_line_max_of_one_keeps_one_lead_per_issue():
    """``POCKETPAW_BELT_LINE_LEAD_MAX=1`` restores the one-issue lead, its title
    and why exactly as before."""
    sightings = [_s(1, "p2", "line:flare", age_h=5), _s(2, "p1", "line:flare", age_h=3)]
    plan = lead_planner.plan(1, sightings, 10, line_max=1)
    assert _groups(plan) == [["s2"], ["s1"]]
    assert [(t.title, t.why) for t in plan.tasks] == [
        ("Feature 2", f"Feature issue #2: https://github.com/{SLUG}/issues/2"),
        ("Feature 1", f"Feature issue #1: https://github.com/{SLUG}/issues/1"),
    ]


@pytest.mark.parametrize(("env", "n"), [(None, 3), ("1", 1), ("5", 5), ("0", 1), ("x", 3)])
def test_the_line_max_reads_the_env(monkeypatch, env, n):
    if env is None:
        monkeypatch.delenv("POCKETPAW_BELT_LINE_LEAD_MAX", raising=False)
    else:
        monkeypatch.setenv("POCKETPAW_BELT_LINE_LEAD_MAX", env)
    assert lead_planner.line_max() == n


def test_the_breaker_counts_per_issue_on_a_line():
    """Two issues of one line: #1's leads failed 3 times, #2's once. #1 is held
    and named by ``trips``; #2 still gets a lead, alone. Mutation: a line held
    whole when one issue trips plans nothing."""
    tripped = _failed(1, 3)
    tripped["labels"].append("line:flare")
    once = _failed(2, 1)
    once["labels"].append("line:flare")
    assert _groups(lead_planner.plan(4, [tripped, once], 5)) == [["s2"]]
    assert [s["id"] for s, _ in lead_planner.trips([tripped, once])] == ["s1"]


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


@pytest.mark.usefixtures("store", "graph", "cloud_station_wired")
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


@pytest.mark.usefixtures("graph", "journal", "cloud_station_wired")
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


@pytest.mark.usefixtures("store", "graph", "journal", "cloud_station_wired")
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
    assert foreman_calls == [] and RecorderDispatcher.instances[0].calls == []


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


# ---------------------------------------------------------------------------
# never twice: in flight, needs-human, failed
# ---------------------------------------------------------------------------


async def _run_for(store, mid: str, plan_id: str, index: int, **blob):  # noqa: F811
    """A lead run row for task ``index`` of plan ``plan_id``, as the station files it."""
    return await _seed_run(
        store,
        mid,
        f"lead {index}",
        plan_action_id=plan_id,
        task_index=index,
        station_pending=True,
        diff="",
        recipe="feature-lead",
        **blob,
    )


def _refs(blob: dict) -> list:
    return [t["evidence_refs"] for t in blob["plan"]["tasks"]]


@pytest.mark.usefixtures("graph", "journal", "cloud_station_wired")
async def test_a_lead_whose_run_is_in_flight_is_not_planned_again(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
):
    """No ``Fixes #N`` PR yet: the run row alone keeps the issue in flight."""
    mid, ids = await _lead_mandate(
        tmp_path,
        monkeypatch,
        _lead_gh(),
        run_kind="feature-lead",
        budget={"max_tasks_per_shift": 2, "max_in_flight": 10},
    )
    first = await mandate_service.trigger_shift(WS, USER, mid)
    plan_id = first["shift"]["plan_action_id"]
    assert _refs((await store.get_action(plan_id)).parameters["_belt_plan"]) == [
        [ids[16]],
        [ids[11]],
    ]
    await _run_for(store, mid, plan_id, 1, headless_state="cloud")
    await _run_for(store, mid, plan_id, 2, headless_state="cloud")

    second = await mandate_service.trigger_shift(WS, USER, mid)

    blob = (await store.get_action(second["shift"]["plan_action_id"])).parameters["_belt_plan"]
    assert _refs(blob) == [[ids[10]], [ids[15]]]


def _label(monkeypatch, gh: FakeGh, n: int, on: bool) -> None:
    """Put ``needs-human`` on issue ``n`` (the station's release) or take it off
    (a human re-arming it); the next shift reads the open issue list afresh."""
    (issue,) = [i for i in gh.issues if i["number"] == n]
    issue["labels"] = [lb for lb in issue["labels"] if lb["name"] != "needs-human"]
    if on:
        issue["labels"].append({"name": "needs-human", "color": "d93f0b", "description": ""})
    monkeypatch.setattr(patrols, "_OPEN_ISSUES", {})


async def _plan_refs(store, out: dict) -> list:  # noqa: F811
    return _refs((await store.get_action(out["shift"]["plan_action_id"])).parameters["_belt_plan"])


@pytest.mark.usefixtures("graph", "journal", "cloud_station_wired")
async def test_a_released_lead_is_held_until_a_human_re_arms_it(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
):
    """#16's lead was released (labelled needs-human, run rejected), #11's
    failed once. Held while the label is on; a human takes it off and the next
    shift plans #16 again, beside #11's second try. A second release holds #16
    again."""
    gh = _lead_gh()
    mid, ids = await _lead_mandate(
        tmp_path,
        monkeypatch,
        gh,
        run_kind="feature-lead",
        budget={"max_tasks_per_shift": 2, "max_in_flight": 10},
    )
    first = await mandate_service.trigger_shift(WS, USER, mid)
    plan_id = first["shift"]["plan_action_id"]
    released = await _run_for(store, mid, plan_id, 1, headless_state=None)
    _label(monkeypatch, gh, 16, on=True)
    await store.reject(released.id, reason="feature lead silent 30m after two nudges")
    await _run_for(store, mid, plan_id, 2, headless_error="TRACK: gh pr list failed")

    second = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, second) == [[ids[11]], [ids[10]]]
    for i in (1, 2):  # #11 (its second try) and #10 go in flight
        await _run_for(store, mid, second["shift"]["plan_action_id"], i, headless_state="cloud")

    _label(monkeypatch, gh, 16, on=False)
    third = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, third) == [[ids[16]], [ids[15]]]

    again = await _run_for(store, mid, third["shift"]["plan_action_id"], 1, headless_state=None)
    _label(monkeypatch, gh, 16, on=True)
    await store.reject(again.id, reason="feature lead not done after 8h")
    await _run_for(store, mid, third["shift"]["plan_action_id"], 2, headless_state="cloud")

    fourth = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, fourth) == [[ids[17]]]
    await _run_for(store, mid, fourth["shift"]["plan_action_id"], 1, headless_state="cloud")

    # A third release, re-armed again: releases never trip the breaker.
    _label(monkeypatch, gh, 16, on=False)
    fifth = await mandate_service.trigger_shift(WS, USER, mid)
    third_try = await _run_for(store, mid, fifth["shift"]["plan_action_id"], 1)
    await store.reject(third_try.id, reason="feature lead silent 30m after two nudges")
    sixth = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, sixth) == [[ids[16]]]


class FlagGh(FakeGh):
    """``FakeGh`` that also takes the breaker's label and comment writes."""

    async def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] in (["gh", "issue", "comment"], ["gh", "issue", "edit"]) or argv[:2] == [
            "gh",
            "label",
        ]:
            self.calls.append(list(argv))
            return 0, "", ""
        return await super().__call__(argv)


def _comments(gh: FakeGh, n: int) -> list[str]:
    return [a[-1] for a in gh.calls if a[:4] == ["gh", "issue", "comment", str(n)]]


@pytest.mark.usefixtures("graph", "journal", "cloud_station_wired")
async def test_a_re_filed_issue_keeps_its_failures_and_trips_the_breaker(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
):
    """#11 failed twice under a sighting the patrol then resolved (once kept on
    the sighting, once a run citing it), and the issue was re-filed as a new
    sighting. Its next failure trips the breaker: #11 is labelled needs-human
    with a one-line reason and held. Held again while the label is on (no
    second comment); a human taking it off re-arms it. Mutation: counting only
    the free sighting's own runs plans a fourth try."""
    gh = FlagGh(_lead_gh().issues, states={13: "CLOSED", 412: "OPEN"}, views={11: {"labels": []}})
    gh.prs = [{"number": 160, "body": "Fixes #14"}]
    mid, ids = await _lead_mandate(
        tmp_path,
        monkeypatch,
        gh,
        run_kind="feature-lead",
        budget={"max_tasks_per_shift": 2, "max_in_flight": 10},
    )
    old = await SightingDoc.get(ids[11])
    old.evidence = {**old.evidence, "lead_failed": ["old-a"]}
    await old.save()
    first = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, first) == [[ids[16]], [ids[11]]]
    await _run_for(store, mid, first["shift"]["plan_action_id"], 1, headless_state="cloud")
    await _run_for(store, mid, first["shift"]["plan_action_id"], 2, headless_error="LAUNCH: x")
    refiled = SightingDoc(
        workspace=WS,
        mandate_id=mid,
        patrol=old.patrol,
        severity=old.severity,
        summary=old.summary,
        evidence={k: v for k, v in old.evidence.items() if k != "lead_failed"},
        ts=old.ts,
    )
    old.resolved_by_run = "patrol:issue-left-line"
    await old.save()
    await refiled.insert()

    second = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, second) == [[str(refiled.id)], [ids[10]]]  # 2 < 3: tried
    await _run_for(store, mid, second["shift"]["plan_action_id"], 1, headless_error="LAUNCH: y")
    await _run_for(store, mid, second["shift"]["plan_action_id"], 2, headless_state="cloud")

    third = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, third) == [[ids[15]], [ids[17]]]
    (reason,) = _comments(gh, 11)
    assert reason.startswith("Belt: 3 feature lead attempts failed (") and "old-a" in reason
    assert ["gh", "issue", "edit", "11", "-R", SLUG, "--add-label", "needs-human"] in gh.calls
    kept = (await SightingDoc.get(refiled.id)).evidence
    assert len(kept["lead_failed"]) == 3 and kept["lead_breaker_at"] == 3
    for i in (1, 2):
        await _run_for(store, mid, third["shift"]["plan_action_id"], i, headless_state="cloud")

    _label(monkeypatch, gh, 11, on=True)
    fourth = await mandate_service.trigger_shift(WS, USER, mid)
    assert fourth["shift"]["state"] == "stood_down"
    assert len(_comments(gh, 11)) == 1

    _label(monkeypatch, gh, 11, on=False)  # a human re-arms it
    fifth = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, fifth) == [[str(refiled.id)]]


async def test_the_breaker_flag_counts_a_label_already_on():
    """The cached open list said no label, the issue has it: no second
    comment, and the trip is kept (True)."""
    gh = FlagGh([], views={11: {"labels": [{"name": "needs-human"}]}})
    assert await patrols.flag_needs_human(f"github:{SLUG}#11", "why", gh=gh) is True
    assert not _comments(gh, 11)
    gh.views = {11: {"labels": []}}
    assert await patrols.flag_needs_human(f"github:{SLUG}#11", "why", gh=gh) is True
    assert _comments(gh, 11) == ["why"]


def _line_gh() -> FakeGh:
    """``_lead_gh`` with lines: #16, #11 and #10 on line:flare, #15 on line:halo."""
    gh = _lead_gh()
    lines = {16: "flare", 11: "flare", 10: "flare", 15: "halo"}
    for issue in gh.issues:
        if issue["number"] in lines:
            label = {"name": f"line:{lines[issue['number']]}", "color": "fff", "description": ""}
            issue["labels"].append(label)
    return gh


@pytest.mark.usefixtures("graph", "journal", "cloud_station_wired")
async def test_a_line_lead_owns_each_of_its_issues(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
):
    """One lead takes #16, #11 and #10 of line:flare (p1, p1, p2). While it is
    in flight none of them is planned again; the next lead is #15 of line:halo.
    Mutation: citing only the first issue frees #11 and #10 for the second shift."""
    monkeypatch.delenv("POCKETPAW_BELT_LINE_LEAD_MAX", raising=False)
    mid, ids = await _lead_mandate(
        tmp_path,
        monkeypatch,
        _line_gh(),
        run_kind="feature-lead",
        budget={"max_tasks_per_shift": 1, "max_in_flight": 10},
    )
    first = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, first) == [[ids[16], ids[11], ids[10]]]
    await _run_for(store, mid, first["shift"]["plan_action_id"], 1, headless_state="cloud")

    second = await mandate_service.trigger_shift(WS, USER, mid)

    assert await _plan_refs(store, second) == [[ids[15]]]


@pytest.mark.usefixtures("graph", "journal", "cloud_station_wired")
async def test_a_failed_line_lead_counts_against_each_of_its_issues(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
):
    """#16's leads failed twice before. A line lead on #16, #11 and #10 fails:
    #16 trips at 3 and is held with needs-human, while #11 and #10, one failure
    each, get the next lead together. Mutation: a failure counted once per run,
    not per issue, never trips #16."""
    monkeypatch.delenv("POCKETPAW_BELT_LINE_LEAD_MAX", raising=False)
    gh = FlagGh(_line_gh().issues, states={13: "CLOSED", 412: "OPEN"}, views={16: {"labels": []}})
    gh.prs = [{"number": 160, "body": "Fixes #14"}]
    mid, ids = await _lead_mandate(
        tmp_path,
        monkeypatch,
        gh,
        run_kind="feature-lead",
        budget={"max_tasks_per_shift": 1, "max_in_flight": 10},
    )
    old = await SightingDoc.get(ids[16])
    old.evidence = {**old.evidence, "lead_failed": ["old-a", "old-b"]}
    await old.save()
    first = await mandate_service.trigger_shift(WS, USER, mid)
    assert await _plan_refs(store, first) == [[ids[16], ids[11], ids[10]]]
    await _run_for(store, mid, first["shift"]["plan_action_id"], 1, headless_error="LAUNCH: x")

    second = await mandate_service.trigger_shift(WS, USER, mid)

    assert await _plan_refs(store, second) == [[ids[11], ids[10]]]
    assert ["gh", "issue", "edit", "16", "-R", SLUG, "--add-label", "needs-human"] in gh.calls
    assert len((await SightingDoc.get(ids[16])).evidence["lead_failed"]) == 3
    for n in (11, 10):
        assert len((await SightingDoc.get(ids[n])).evidence["lead_failed"]) == 1


@pytest.mark.usefixtures("store", "graph", "journal", "cloud_station_wired")
async def test_an_issue_labelled_needs_human_after_its_sighting_is_skipped(
    tmp_path, mongo_db, monkeypatch
):
    gh = _lead_gh()
    mid, ids = await _lead_mandate(tmp_path, monkeypatch, gh, run_kind="feature-lead")
    # The stall ladder labels #16 after the patrol filed it; the next shift's
    # open issue list carries the label.
    (sixteen,) = [i for i in gh.issues if i["number"] == 16]
    sixteen["labels"].append({"name": "needs-human", "color": "d93f0b", "description": ""})
    monkeypatch.setattr(patrols, "_OPEN_ISSUES", {})

    out = await mandate_service.trigger_shift(WS, USER, mid)

    blob = (
        await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    ).parameters["_belt_plan"]
    assert _refs(blob) == [[ids[n]] for n in (11, 10, 15, 17)]


class LimitedGh(FakeGh):
    """``FakeGh`` whose unfiltered open issue list stops short of ``hidden`` (an
    old issue past ``--limit`` on a busy repo), while a ``--label`` list returns
    every issue carrying that label."""

    hidden: set[int] = set()

    async def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] == ["gh", "issue", "list"] and "all" not in argv:
            self.calls.append(list(argv))
            if "--label" in argv:
                want = argv[argv.index("--label") + 1]
                rows = [i for i in self.issues if want in {lb["name"] for lb in i["labels"]}]
            else:
                rows = [i for i in self.issues if i["number"] not in self.hidden]
            return 0, json.dumps(rows), ""
        return await super().__call__(argv)


@pytest.mark.usefixtures("store", "graph", "journal", "cloud_station_wired")
async def test_needs_human_on_an_issue_past_the_open_list_limit_is_skipped(
    tmp_path, mongo_db, monkeypatch
):
    """The repo has more open issues than the open list returns, so #16 is not
    on it: the planner reads its labels fresh off a ``--label`` list instead of
    the stale ones filed with its sighting. Mutation: reading only the
    unfiltered (or a cached) list plans #16 again."""
    base = _lead_gh()
    gh = LimitedGh(base.issues, states=base.states, prs=base.prs)
    mid, ids = await _lead_mandate(tmp_path, monkeypatch, gh, run_kind="feature-lead")
    # A read before the label landed warms the cached open list too.
    await patrols.open_issues(
        (await mandate_service.get_mandate(WS, USER, mid))["surface"]["repo_id"]
    )
    (sixteen,) = [i for i in gh.issues if i["number"] == 16]
    sixteen["labels"].append({"name": "needs-human", "color": "d93f0b", "description": ""})
    gh.hidden = {16}
    # One view answers the patrol and the dependency check, so #16 stays open
    # and unblocked: only the label can hold it.
    gh.views = {16: {"state": "OPEN", "body": sixteen["body"], "labels": sixteen["labels"]}}

    out = await mandate_service.trigger_shift(WS, USER, mid)

    blob = (
        await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    ).parameters["_belt_plan"]
    assert _refs(blob) == [[ids[n]] for n in (11, 10, 15, 17)]


@pytest.mark.usefixtures("store", "graph", "journal", "cloud_station_wired")
async def test_a_beta_only_line_skips_p3_issues(tmp_path, mongo_db, monkeypatch):
    """``PATCH .../charter {"beta_only": true}``: p3 is out of the beta, so the
    p3 #15 is not planned; off (the default) plans it, as above. Mutation: the
    flag not reaching the planner plans #15."""
    mid, ids = await _lead_mandate(tmp_path, monkeypatch, _lead_gh(), run_kind="feature-lead")
    detail = await mandate_service.update_charter(WS, USER, mid, {"beta_only": True})
    assert detail["mandate"]["charter"]["beta_only"] is True

    out = await mandate_service.trigger_shift(WS, USER, mid)

    blob = (
        await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    ).parameters["_belt_plan"]
    assert _refs(blob) == [[ids[n]] for n in (16, 11, 10, 17)]


@pytest.mark.usefixtures("store", "graph", "journal", "cloud_station_wired")
async def test_a_lead_plan_whose_policy_record_fails_waits_at_the_gate(
    tmp_path, mongo_db, monkeypatch
):
    """The audit write failing must not approve blind: the plan stays pending."""
    from pocketpaw_ee.cloud._core import proposals

    real = proposals.update_action_blob

    async def failing(*, store, action_id, param_key, updates):  # noqa: F811
        if "policy" in updates:
            raise RuntimeError("store down")
        return await real(store=store, action_id=action_id, param_key=param_key, updates=updates)

    monkeypatch.setattr(proposals, "update_action_blob", failing)
    mid, _ids = await _lead_mandate(tmp_path, monkeypatch, _lead_gh(), run_kind="feature-lead")

    out = await mandate_service.trigger_shift(WS, USER, mid)

    assert out["shift"]["state"] == "in_gate"
    action = await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    assert action.status == ActionStatus.PENDING and action.approved_by is None
    assert "policy" not in action.parameters["_belt_plan"]
    assert RecorderDispatcher.instances[0].calls == []


# ---------------------------------------------------------------------------
# cloud only: a lead mandate needs the cloud develop station
# ---------------------------------------------------------------------------


def _lead_body(tmp_path) -> dict:
    return {
        "name": "flare leads",
        "surface": {"repo_id": str(tmp_path)},
        "charter": {
            "goal": "ship flare",
            "run_kind": "feature-lead",
            "issue_labels": ["feature-lead"],
        },
        "patrols": ["issues"],
    }


@pytest.mark.parametrize(
    "env,wired",
    [
        ("bus", LeadRecorder),  # approvals never reach the wired station
        ("headless", None),  # nothing wired
        ("headless", RecorderDispatcher),  # a station that runs no leads
    ],
)
async def test_a_lead_mandate_is_refused_without_the_cloud_station(
    tmp_path, mongo_db, monkeypatch, env, wired
):
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", env)
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", wired() if wired else None)

    with pytest.raises(ValidationError) as err:
        await mandate_service.create_mandate(WS, USER, _lead_body(tmp_path))

    assert err.value.status_code == 422
    assert err.value.code == "mandate.lead_needs_cloud_station"
    # A task mandate is untouched by the rule.
    body = _lead_body(tmp_path)
    body["charter"]["run_kind"] = "task"
    assert (await mandate_service.create_mandate(WS, USER, body))["mandate"]["id"]


@pytest.mark.usefixtures("store", "graph", "journal")
async def test_a_lead_shift_is_refused_once_the_cloud_station_is_gone(
    tmp_path,
    mongo_db,
    monkeypatch,
    cloud_station_wired,
):
    mid = (await mandate_service.create_mandate(WS, USER, _lead_body(tmp_path)))["mandate"]["id"]
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", None)

    with pytest.raises(ValidationError) as err:
        await mandate_service.trigger_shift(WS, USER, mid)

    assert err.value.code == "mandate.lead_needs_cloud_station"
    assert await ShiftDoc.find(ShiftDoc.mandate_id == mid).count() == 0  # no half-open shift


def test_the_cloud_station_runs_leads():
    assert cloud_station.CloudTaskDispatcher.runs_leads is True


def test_the_expected_outcome_names_slices_and_keeps_the_v2_rule():
    # The planner reads no packet: the outcome names the v3 loop and the v2 rule.
    (task,) = lead_planner.plan(1, [_s(42, "p1")], 3).tasks
    out = task.expected_outcome
    assert out.startswith("#42 shipped by its feature lead, slice by slice")
    assert "every slice done on a green ci.yml run that holds its last PR" in out
    assert "never wait on CI or critics" in out and "at most 2 fix rounds per slice" in out
    assert "A feature without slices: a merged feature-done PR, dev CI green" in out
