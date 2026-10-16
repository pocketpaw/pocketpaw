# tests/cloud/test_belt_shift_lock.py — one shift plans at a time per mandate.
#
# On 2026-10-17 a hand-fired shift and a scheduler shift of one lead mandate ran
# 0.4 s apart. Both read the line before either filed its runs, so both planned
# the same features and each feature got two leads. A shift now claims the
# mandate's ``shift_lock`` atomically before it reads anything; a second shift
# refuses with a 409 and opens no shift row. The lock expires, so a shift that
# crashed holding it cannot wedge the line. Inside the lock the planner also
# drops every issue a pending run of the mandate owns: ``lead.issues``,
# ``lead.issue``, or the issues its task names before the station launched it.
# Real store (mongomock), real trigger and executor; the dispatcher files each
# run row the way the station does; gh is a fake executor.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud._core.errors import ConflictError  # noqa: E402
from pocketpaw_ee.cloud.belt import headless  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import MandateDoc, ShiftDoc  # noqa: E402

from tests.cloud.test_belt_github_issues_patrol import (  # noqa: E402
    _fresh_gh_caches,  # noqa: F401 — autouse: the gh caches are per test
)
from tests.cloud.test_belt_lead_planner import (  # noqa: E402
    LeadRecorder,
    _lead_mandate,
    _line_gh,
    _plan_refs,
)
from tests.cloud.test_belt_mandates import (  # noqa: E402,F401
    USER,
    WS,
    _seed_run,
    dispatcher,
    graph,
    journal,
    mock_llm,
    store,
)

pytestmark = pytest.mark.usefixtures("any_repo_root")


class FilingRecorder(LeadRecorder):
    """The cloud station's dispatch: files the queued run row before it returns."""

    async def dispatch(self, *, workspace_id, mandate_id, shift_no, plan_action_id, index, task):
        ref = await super().dispatch(
            workspace_id=workspace_id,
            mandate_id=mandate_id,
            shift_no=shift_no,
            plan_action_id=plan_action_id,
            index=index,
            task=task,
        )
        await _seed_run(
            self.store,
            mandate_id,
            f"{task.get('title')}\n\n{task.get('why')}",
            plan_action_id=plan_action_id,
            task_index=index,
            shift_no=shift_no,
            station_pending=True,
            diff="",
            recipe="feature-lead",
        )
        return ref


@pytest.fixture
def filing_station(monkeypatch, dispatcher, store):  # noqa: F811
    monkeypatch.setenv("POCKETPAW_MANDATE_DISPATCHER", "headless")
    recorder = FilingRecorder()
    recorder.store = store
    monkeypatch.setattr(headless, "_PRODUCTION_DISPATCHER", recorder)
    monkeypatch.delenv("POCKETPAW_BELT_LINE_LEAD_MAX", raising=False)
    return recorder


async def _mandate(tmp_path, monkeypatch) -> tuple[str, dict]:
    """A lead line: #16, #11, #10 on line:flare, #15 on line:halo, #17 alone."""
    return await _lead_mandate(
        tmp_path,
        monkeypatch,
        _line_gh(),
        run_kind="feature-lead",
        budget={"max_tasks_per_shift": 4, "max_in_flight": 10},
    )


def _lock_of(raw: dict | None) -> object:
    return (raw or {}).get("shift_lock")


async def _raw(mid: str) -> dict | None:
    from bson import ObjectId

    return await MandateDoc.get_pymongo_collection().find_one({"_id": ObjectId(mid)})


@pytest.mark.usefixtures("graph", "journal")
async def test_two_concurrent_shifts_plan_each_issue_once(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
    filing_station,
):
    mid, ids = await _mandate(tmp_path, monkeypatch)

    results = await asyncio.gather(
        mandate_service.trigger_shift(WS, USER, mid),
        mandate_service.trigger_shift(WS, USER, mid),
        return_exceptions=True,
    )

    runs = [c["task"]["evidence_refs"] for c in filing_station.calls]
    cited = [ref for group in runs for ref in group]
    assert sorted(cited) == sorted(ids[n] for n in (16, 11, 10, 15, 17)), "one lead per issue"
    planned = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(planned) == 1, results
    assert [type(r) for r in refused] == [ConflictError]
    assert "a shift is already planning" in str(refused[0])
    shifts = await ShiftDoc.find(ShiftDoc.mandate_id == mid).to_list()
    assert len(shifts) == 1, "the refused shift opens no shift row"
    assert _lock_of(await _raw(mid)) is None, "the shift releases its lock"


@pytest.mark.usefixtures("graph", "journal")
async def test_a_held_lock_refuses_and_a_crashed_shifts_lock_expires(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
    filing_station,
):
    from bson import ObjectId

    mid, _ids = await _mandate(tmp_path, monkeypatch)
    coll = MandateDoc.get_pymongo_collection()
    now = datetime.now(UTC)

    held = {"token": "live", "expires_at": now + timedelta(minutes=5)}
    await coll.update_one({"_id": ObjectId(mid)}, {"$set": {"shift_lock": held}})
    with pytest.raises(ConflictError):
        await mandate_service.trigger_shift(WS, USER, mid)
    assert await ShiftDoc.find(ShiftDoc.mandate_id == mid).to_list() == []
    assert _lock_of(await _raw(mid))["token"] == "live", "a refusal never frees the holder's lock"

    # The holder crashed: its lock outlived its expiry.
    crashed = {"token": "crashed", "expires_at": now - timedelta(seconds=1)}
    await coll.update_one({"_id": ObjectId(mid)}, {"$set": {"shift_lock": crashed}})

    out = await mandate_service.trigger_shift(WS, USER, mid)

    assert out["shift"]["task_count"] == 3
    assert _lock_of(await _raw(mid)) is None


@pytest.mark.usefixtures("graph", "journal")
async def test_a_mandate_save_during_a_shift_keeps_the_lock(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
    filing_station,
):
    """``shift_lock`` lives off the Beanie model, so a charter edit saving a
    stale MandateDoc mid-shift cannot clear it."""
    from bson import ObjectId

    mid, _ids = await _mandate(tmp_path, monkeypatch)
    doc = await MandateDoc.get(ObjectId(mid))
    held = {"token": "live", "expires_at": datetime.now(UTC) + timedelta(minutes=5)}
    await MandateDoc.get_pymongo_collection().update_one(
        {"_id": ObjectId(mid)}, {"$set": {"shift_lock": held}}
    )

    doc.name = "renamed"
    await doc.save()

    assert _lock_of(await _raw(mid))["token"] == "live"


@pytest.mark.usefixtures("graph", "journal")
@pytest.mark.parametrize(
    ("task", "lead"),
    [
        # Queued, not launched: the station has not resolved ``lead`` yet.
        ("Layer panel + 1 more on line:flare\n\nFeature issues #16, #11 on line:flare", None),
        # Launched: the run's ``lead.issues`` names them.
        ("A line lead filed by hand", {"issue": 16, "issues": [16, 11]}),
    ],
    ids=["queued-task", "launched-lead-issues"],
)
async def test_the_planner_skips_issues_a_pending_run_owns(
    tmp_path,
    mongo_db,
    monkeypatch,
    store,  # noqa: F811
    filing_station,
    task,
    lead,  # noqa: F811
):
    """A pending lead run of the mandate owns #16 and #11 through its task or
    its ``lead.issues``, with no plan task citing their sightings (filed by
    hand, or its plan out of the runs window): neither is planned again."""
    mid, ids = await _mandate(tmp_path, monkeypatch)
    await _seed_run(
        store,
        mid,
        task,
        station_pending=True,
        diff="",
        recipe="feature-lead",
        **({"lead": lead, "headless_state": "cloud"} if lead else {}),
    )

    out = await mandate_service.trigger_shift(WS, USER, mid)

    assert await _plan_refs(store, out) == [[ids[10]], [ids[15]], [ids[17]]]
