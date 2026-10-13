# tests/cloud/test_belt_overlapping_shifts.py — a line's in-flight cap.
#
# Shifts overlap: a line starts a shift while earlier runs are in flight, as
# long as it has headroom under the charter's ``max_in_flight`` (unset =
# ``max_tasks_per_shift``, so a charter that predates the field keeps its
# meaning). The shift's task budget is that headroom, never over
# ``max_tasks_per_shift``, and it is what the foreman is told and what the auto
# plan gate checks. A run settling frees a slot and wakes the line; the
# minimum gap still floors shift starts. The foreman plans only from sightings
# nothing works yet: the in-flight ones ride apart in its context, and a task
# citing one is dropped. The ``issues`` patrol never sights an issue twice.
# Real store, real trigger, mock foreman; the clock and gh are injected.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud._core.errors import ValidationError  # noqa: E402
from pocketpaw_ee.cloud.mandates import foreman, patrols  # noqa: E402
from pocketpaw_ee.cloud.mandates import scheduler as scheduler_mod  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import MandateDoc, ShiftDoc  # noqa: E402

import pocketpaw.stores as stores  # noqa: E402
from tests.cloud.test_belt_github_issues_patrol import (  # noqa: E402
    FakeGh,
    _fresh_gh_caches,  # noqa: F401 — autouse: the gh caches are per test
    _github_mandate,
    _issue,
)
from tests.cloud.test_belt_mandates import (  # noqa: E402,F401
    USER,
    WS,
    _seed_run,
    foreman_calls,
    graph,
    journal,
    mock_llm,
    store,
)
from tests.cloud.test_belt_plan_policy import GOOD, _clef  # noqa: E402

pytestmark = pytest.mark.usefixtures("any_repo_root", "store")


async def _mandate(cadence: str = "continuous", **budget) -> str:
    created = await mandate_service.create_mandate(
        WS,
        USER,
        {
            "name": "flare",
            "surface": {"repo_id": "/tmp/x"},
            "charter": {"goal": "keep it healthy", "cadence": cadence, "budget": budget},
        },
    )
    return created["mandate"]["id"]


async def _sightings(mandate_id: str, n: int) -> None:
    for i in range(n):
        await mandate_service.file_feedback(
            WS, USER, mandate_id, {"text": f"signal {i}", "severity": 3, "source": "support"}
        )


async def _runs(mandate_id: str, n: int) -> list:
    """``n`` queued runs on the line (in flight), through the store the fixture
    wired into ``get_instinct_store``."""
    st = stores.get_instinct_store()
    return [await _seed_run(st, mandate_id, f"run {i}", station_pending=True) for i in range(n)]


async def _done_shift(mandate_id: str, age: timedelta) -> None:
    shift = ShiftDoc(workspace=WS, mandate_id=mandate_id, no=1, state="done")
    await shift.insert()
    await shift.set({ShiftDoc.createdAt: datetime.now(UTC) - age})


async def _fired(mandate_id: str) -> bool:
    async def trigger(workspace_id, user_id, mid):
        return {}

    return mandate_id in await scheduler_mod.run_scheduler_tick(trigger=trigger)


# ---------------------------------------------------------------------------
# The scheduler: headroom starts a shift, the cap holds it, a settle tops up
# ---------------------------------------------------------------------------


async def test_the_scheduler_starts_a_shift_while_runs_are_in_flight_until_the_cap(mongo_db):
    mid = await _mandate(max_tasks_per_shift=3, max_in_flight=2)
    await _sightings(mid, 3)
    await _done_shift(mid, timedelta(minutes=30))
    await _runs(mid, 1)
    assert await _fired(mid), "1 of 2 in flight: headroom for one more"

    await _runs(mid, 1)
    assert not await _fired(mid), "2 of 2 in flight: the line is at its cap"


async def test_a_settling_run_wakes_a_full_line_for_a_top_up(mongo_db, store):  # noqa: F811
    mid = await _mandate(max_tasks_per_shift=2)
    await _sightings(mid, 3)
    await _done_shift(mid, timedelta(minutes=30))
    first, _ = await _runs(mid, 2)
    assert not await _fired(mid), "2 of 2 in flight"

    await store.reject(first.id, reason="silent worker", rejector="system:belt-cloud")
    assert await _fired(mid), "a run settled: one slot free"


async def test_the_minimum_gap_floors_shift_starts_even_with_headroom(
    mongo_db,
    store,  # noqa: F811
    monkeypatch,
):
    mid = await _mandate(max_tasks_per_shift=2, max_in_flight=4)
    await _sightings(mid, 2)
    await _done_shift(mid, timedelta(minutes=2))
    (run,) = await _runs(mid, 1)
    await store.reject(run.id, reason="settled", rejector="system:belt-cloud")
    assert not await _fired(mid), "headroom and a settled run, but inside the 5 minute gap"

    monkeypatch.setenv("POCKETPAW_MANDATE_CONTINUOUS_MIN_GAP_MINUTES", "1")
    assert await _fired(mid)


# ---------------------------------------------------------------------------
# The shift budget is the headroom
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("journal", "graph")
@pytest.mark.parametrize(
    ("budget", "in_flight", "expected"),
    [
        (
            {"max_tasks_per_shift": 3, "max_in_flight": 3},
            1,
            {"in_flight": 1, "cap": 3, "budget": 2},
        ),
        (
            {"max_tasks_per_shift": 3, "max_in_flight": 10},
            1,
            {"in_flight": 1, "cap": 10, "budget": 3},
        ),
    ],
)
async def test_the_shift_budget_is_the_headroom(
    mongo_db,
    foreman_calls,  # noqa: F811
    caplog,
    budget,
    in_flight,
    expected,
):
    mid = await _mandate("manual", **budget)
    await _sightings(mid, 4)
    await _runs(mid, in_flight)

    with caplog.at_level(logging.INFO, logger="pocketpaw_ee.cloud.mandates.service"):
        out = await mandate_service.trigger_shift(WS, USER, mid)

    call = foreman_calls[-1]
    assert call.context.capacity == expected
    n = expected["budget"]
    assert f"Plan AT MOST {n} task(s) this shift" in call.prompt
    assert (
        f"Runs in flight on this line: {in_flight} of a cap of {expected['cap']}, "
        f"so this shift may start at most {n} more." in call.prompt
    )
    assert out["shift"]["task_count"] == n
    line = f"line flare shift 1: in_flight={in_flight} cap={expected['cap']} budget={n}"
    assert line in caplog.text


@pytest.mark.usefixtures("journal", "graph")
async def test_a_full_line_refuses_a_shift_without_opening_one(mongo_db):
    mid = await _mandate("manual", max_tasks_per_shift=2)
    await _sightings(mid, 3)
    await _runs(mid, 2)

    with pytest.raises(ValidationError) as err:
        await mandate_service.trigger_shift(WS, USER, mid)

    assert err.value.code == "mandate.at_capacity"
    assert await ShiftDoc.find(ShiftDoc.mandate_id == mid).count() == 0, "no shift row holds it"


# ---------------------------------------------------------------------------
# No double work: the foreman plans only what nothing works yet
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("journal", "graph")
async def test_the_foreman_context_excludes_in_flight_issues(
    tmp_path,
    mongo_db,
    monkeypatch,
    foreman_calls,  # noqa: F811
):
    """Issue #12 has an open PR (in flight), #13 has none. The foreman's
    plannable list is #13 alone; #12 rides in its in-flight list, and a task
    it plans on #12 anyway is dropped."""
    gh = FakeGh(
        [_issue(12, "suite"), _issue(13, "suite")], prs=[{"number": 160, "body": "Fixes #12"}]
    )
    monkeypatch.setattr(patrols, "_default_gh", gh)
    mid = await _github_mandate(tmp_path, monkeypatch, gh, cadence="manual")
    await mandate_service.run_patrols(WS, USER, mid)
    ids = {
        s["evidence"]["number"]: s["id"]
        for s in (await mandate_service.list_sightings(WS, USER, mid))["sightings"]
    }
    foreman.set_mock_plan(
        {
            "shift_no": 1,
            "tasks": [
                {
                    "title": f"fix #{n}",
                    "why": "issue",
                    "evidence_refs": [ids[n]],
                    "expected_outcome": "bugs down",
                }
                for n in (12, 13)
            ],
        }
    )

    out = await mandate_service.trigger_shift(WS, USER, mid)

    ctx = foreman_calls[-1].context
    assert [s["id"] for s in ctx.sightings] == [ids[13]]
    assert [(s["id"], s["open_pr"]) for s in ctx.in_flight] == [(ids[12], 160)]
    assert (
        "== IN FLIGHT (already being worked: never plan these again) ==" in foreman_calls[-1].prompt
    )
    blob = (
        await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    ).parameters["_belt_plan"]
    assert [t["evidence_refs"] for t in blob["plan"]["tasks"]] == [[ids[13]]]


@pytest.mark.usefixtures("journal", "graph")
async def test_a_plan_of_only_in_flight_work_stands_down(tmp_path, mongo_db, monkeypatch):
    gh = FakeGh([_issue(12, "suite")], prs=[{"number": 160, "body": "Fixes #12"}])
    monkeypatch.setattr(patrols, "_default_gh", gh)
    mid = await _github_mandate(tmp_path, monkeypatch, gh, cadence="manual")
    await mandate_service.run_patrols(WS, USER, mid)
    (sighting,) = (await mandate_service.list_sightings(WS, USER, mid))["sightings"]
    foreman.set_mock_plan(
        {
            "shift_no": 1,
            "tasks": [
                {
                    "title": "fix #12",
                    "why": "issue",
                    "evidence_refs": [sighting["id"]],
                    "expected_outcome": "bugs down",
                }
            ],
        }
    )

    out = await mandate_service.trigger_shift(WS, USER, mid)

    assert out["shift"]["state"] == "stood_down"
    assert (
        out["shift"]["no_action_reason"] == "every planned task cites a sighting already in flight"
    )


async def test_the_patrol_never_re_sights_a_claimed_issue(tmp_path, mongo_db, monkeypatch):
    """Every due check senses issues; #12 is sighted once, however many ticks
    pass while a PR works it."""
    gh = FakeGh([_issue(12, "suite")], prs=[{"number": 160, "body": "Fixes #12"}])
    mid = await _github_mandate(tmp_path, monkeypatch, gh, cadence="continuous")
    for _ in range(3):
        await mandate_service.list_cadence_due(datetime.now(UTC))
    sightings = (await mandate_service.list_sightings(WS, USER, mid))["sightings"]
    assert [s["evidence"]["number"] for s in sightings] == [12]


# ---------------------------------------------------------------------------
# The plan gate checks the shift budget
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("journal", "graph")
async def test_the_auto_plan_gate_uses_the_shift_budget(mongo_db, monkeypatch):
    """Three tasks fit the charter's max of 3 but not the shift's budget of 2
    (one run already in flight): the gate escalates on the shift budget."""
    monkeypatch.setenv("POCKETPAW_BELT_PLAN_GATE", "auto")
    _clef(monkeypatch, GOOD)
    mid = await _mandate("manual", max_tasks_per_shift=3)
    await _sightings(mid, 3)
    await _runs(mid, 1)
    ids = [s["id"] for s in (await mandate_service.list_sightings(WS, USER, mid))["sightings"]]
    foreman.set_mock_plan(
        {
            "shift_no": 1,
            "tasks": [
                {"title": f"t{i}", "why": "w", "evidence_refs": [sid], "expected_outcome": "x up"}
                for i, sid in enumerate(ids)
            ],
        }
    )

    out = await mandate_service.trigger_shift(WS, USER, mid)

    assert out["shift"]["state"] == "in_gate"
    blob = (
        await stores.get_instinct_store().get_action(out["shift"]["plan_action_id"])
    ).parameters["_belt_plan"]
    assert blob["policy"]["rule"] == "over_budget"
    assert blob["policy"]["reason"].startswith("3 tasks > the shift budget of 2")
    assert blob["budget_max_tasks"] == 3, "the executor still re-validates the charter max"


# ---------------------------------------------------------------------------
# A charter that predates max_in_flight keeps its meaning
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("journal", "graph")
async def test_an_old_charter_without_max_in_flight_behaves_as_before(mongo_db, foreman_calls):  # noqa: F811
    mid = await _mandate(max_tasks_per_shift=2)
    await MandateDoc.get_pymongo_collection().update_many(
        {}, {"$unset": {"charter.budget.max_in_flight": ""}}
    )
    doc = await MandateDoc.find_one(MandateDoc.workspace == WS)
    assert doc.charter.budget.max_in_flight is None
    detail = await mandate_service.get_mandate(WS, USER, mid)
    assert detail["charter"]["budget"]["max_in_flight"] is None
    await _sightings(mid, 3)

    # No run in flight: the full max_tasks_per_shift, as before.
    out = await mandate_service.trigger_shift(WS, USER, mid)
    assert foreman_calls[-1].context.capacity == {"in_flight": 0, "cap": 2, "budget": 2}
    assert out["shift"]["task_count"] == 2

    # max_tasks_per_shift runs in flight: the line waits, as it did.
    await ShiftDoc.find(ShiftDoc.mandate_id == mid).update({"$set": {"state": "done"}})
    await ShiftDoc.find(ShiftDoc.mandate_id == mid).update(
        {"$set": {"createdAt": datetime.now(UTC) - timedelta(minutes=30)}}
    )
    await _runs(mid, 2)
    assert not await _fired(mid)
