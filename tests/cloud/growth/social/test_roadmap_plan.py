# tests/cloud/growth/social/test_roadmap_plan.py — the Blueprint roadmap and the
# weekly Plan (``PUT /blueprint/draft`` with a roadmap, ``GET /plan``). Same
# harness as test_blueprint.py: the social router over mongomock-backed Beanie.
# "Today" is frozen through ``blueprint._today``. The plan tests insert legacy
# Blueprint rows, so they read through the not-yet-migrated fallback; route-driven
# saves land in the per-unit store (the roadmap lives on the ``plan`` unit).
#
# Covers: roadmap validation (dates, milestone in phase, channel enum, cadence
# 0..21, link scheme); a draft save stores the roadmap with ISO-string dates and
# minted ids; regenerate keeps an owner roadmap and seeds a first one only when
# the base has none; seed writes the first roadmap at exact dates; active-phase
# edges and overlap; slot counts and the weekday spreading rule; post channels
# only; scheduled ideas placed and kept when the cadence drops; unscheduled
# approved ideas fill; the draft is never read; tenancy on workspace and profile.

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.growth.social import blueprint as social_blueprint
from pocketpaw_ee.cloud.growth.social.analyst import set_production_analyze_fn
from pocketpaw_ee.cloud.growth.social.domain import (
    AnalysisOutcome,
    BlueprintRoadmap,
    GrowthBlueprint,
    RoadmapPhase,
    SocialAnalysis,
)
from pocketpaw_ee.cloud.growth.social.plan import active_phase
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as BlueprintDoc
from pocketpaw_ee.cloud.models.growth_blueprint_unit import GrowthBlueprintUnit as UnitDoc
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as IdeaDoc

from tests.cloud.growth.social.test_blueprint import _profile, _save, _seed
from tests.cloud.growth.social.test_router import BASE, _client, _FakeAnalyzer

BP = f"{BASE}/blueprint"
PLAN = f"{BASE}/plan"
TODAY = date(2030, 1, 9)  # a Wednesday
WEEK = "2030-W02"
MON = date.fromisocalendar(2030, 2, 1)  # 2030-01-07


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    async with await _client("w2") as client:
        yield client


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(social_blueprint, "_today", lambda: TODAY)
    set_production_analyze_fn(None)
    yield
    set_production_analyze_fn(None)


def _phase(start: str, end: str, **extra: Any) -> dict:
    return {"name": "P", "start": start, "end": end, **extra}


def _d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


# --- roadmap validation and edits ---------------------------------------------------------


@pytest.mark.parametrize(
    "phase",
    [
        _phase("2030-02-02", "2030-02-01"),
        _phase("2030-02-01", "2030-02-05", milestones=[{"title": "m", "date": "2030-02-06"}]),
        _phase("2030-02-01", "2030-02-05", milestones=[{"title": "m", "date": "2030-01-31"}]),
        _phase("2030-02-01", "2030-02-05", cadence=[{"channel": "tiktok"}]),
        _phase("2030-02-01", "2030-02-05", cadence=[{"channel": "x", "posts_per_week": 22}]),
        _phase("2030-02-01", "2030-02-05", cadence=[{"channel": "x", "posts_per_week": -1}]),
        _phase("2030-02-01", "2030-02-05", cadence=[{"channel": "x"}, {"channel": "x"}]),
        _phase(
            "2030-02-01",
            "2030-02-05",
            milestones=[{"title": "m", "date": "2030-02-02", "link": "javascript:alert(1)"}],
        ),
        _phase(
            "2030-02-01",
            "2030-02-05",
            milestones=[{"title": "m", "date": "2030-02-02", "status": "x"}],
        ),
    ],
)
async def test_a_bad_roadmap_is_422(w1, phase):
    pid = await _profile()
    await _seed(w1, pid)
    resp = await w1.put(
        f"{BP}/draft", params={"profile_id": pid}, json={"roadmap": {"phases": [phase]}}
    )
    assert resp.status_code == 422, resp.text


async def test_draft_save_changes_the_roadmap(w1):
    pid = await _profile()
    await _seed(w1, pid)
    phase = _phase(
        "2030-02-01",
        "2030-02-05",
        cadence=[
            {"channel": "x", "posts_per_week": 21},
            {"channel": "reddit", "posts_per_week": 0},
        ],
        goal_ids=["g1"],
        milestones=[
            {"title": "start", "date": "2030-02-01"},
            {"title": "end", "date": "2030-02-05", "status": "done", "link": "https://a.test/x"},
        ],
    )
    goals = [{"id": "g1", "metric": "signups", "target": 10}]
    draft = (await _save(w1, pid, {"goals": goals, "roadmap": {"phases": [phase]}}))["draft"]
    (got,) = draft["roadmap"]["phases"]
    assert got["id"] and all(m["id"] for m in got["milestones"])
    assert (got["start"], got["end"], got["goal_ids"]) == ("2030-02-01", "2030-02-05", ["g1"])
    assert got["cadence"] == phase["cadence"]
    assert [m["status"] for m in got["milestones"]] == ["planned", "done"]
    stored = await UnitDoc.find_one(
        {"workspace": "w1", "profile_id": pid, "unit": "plan", "status": "draft"}
    )
    assert isinstance(stored.body["roadmap"]["phases"][0]["start"], str)  # BSON has no date


async def test_phase_goal_ids_must_name_goals_of_the_draft(w1):
    pid = await _profile()
    await _seed(w1, pid)
    phase = _phase("2030-02-01", "2030-02-05", goal_ids=["g1"])
    resp = await w1.put(
        f"{BP}/draft", params={"profile_id": pid}, json={"roadmap": {"phases": [phase]}}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "social.blueprint_unknown_goal"
    # The goal already on the draft makes the same roadmap valid ...
    await _save(w1, pid, {"goals": [{"id": "g1", "metric": "signups", "target": 10}]})
    await _save(w1, pid, {"roadmap": {"phases": [phase]}})
    # ... and dropping a goal a phase still names is refused.
    resp = await w1.put(f"{BP}/draft", params={"profile_id": pid}, json={"goals": []})
    assert resp.status_code == 422, resp.text


async def test_seed_writes_the_first_roadmap_at_exact_dates(w1):
    pid = await _profile()
    draft = (await _seed(w1, pid))["draft"]
    this_week, next_four = draft["roadmap"]["phases"]
    assert (this_week["name"], this_week["start"], this_week["end"]) == (
        "This week",
        _d(0),
        _d(6),
    )
    assert (next_four["name"], next_four["start"], next_four["end"]) == (
        "Next 4 weeks",
        _d(7),
        _d(34),
    )
    for phase in (this_week, next_four):
        assert phase["cadence"] == [{"channel": "x", "posts_per_week": 7}]
        assert phase["milestones"] == [] and phase["goal_ids"] == []


async def test_regenerate_keeps_an_owner_roadmap(w1):
    pid = await _profile()
    await _seed(w1, pid)
    mine = {"phases": [_phase("2030-03-01", "2030-03-02", id="mine")]}
    base = (await _save(w1, pid, {"roadmap": mine}))["draft"]
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(analysis=SocialAnalysis(summary="N"))))
    resp = await w1.post(f"{BP}/regenerate", params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft"]["roadmap"] == base["roadmap"]


async def test_regenerate_seeds_a_roadmap_when_the_base_has_none(w1):
    pid = await _profile()
    await BlueprintDoc(
        workspace="w1",
        profile_id=pid,
        version=1,
        status="approved",
        channels=[
            {"channel": "x", "posts_per_week": 3, "sends_per_week": 0},
            {"channel": "reddit", "posts_per_week": 30, "sends_per_week": 0},
            {"channel": "email", "posts_per_week": 0, "sends_per_week": 200},
        ],
    ).insert()
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(analysis=SocialAnalysis(summary="N"))))
    resp = await w1.post(f"{BP}/regenerate", params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    phases = resp.json()["draft"]["roadmap"]["phases"]
    assert [(p["start"], p["end"]) for p in phases] == [(_d(0), _d(6)), (_d(7), _d(34))]
    # The channels' own numbers, capped at the cadence max of 21.
    assert phases[0]["cadence"] == [
        {"channel": "x", "posts_per_week": 3},
        {"channel": "reddit", "posts_per_week": 21},
        {"channel": "email", "posts_per_week": 0},
    ]
    assert resp.json()["approved"]["roadmap"] == {"phases": []}


# --- active phase -----------------------------------------------------------------------


def _bp(*phases: RoadmapPhase, channels: tuple = ()) -> GrowthBlueprint:
    return GrowthBlueprint(
        id="b",
        workspace_id="w1",
        profile_id="p",
        version=1,
        status="approved",
        channels=channels,
        roadmap=BlueprintRoadmap(phases=phases),
    )


def test_active_phase_edges_and_overlap():
    a = RoadmapPhase(id="a", name="A", start=date(2030, 1, 5), end=date(2030, 1, 10))
    b = RoadmapPhase(id="b", name="B", start=date(2030, 1, 3), end=date(2030, 1, 6))
    c = RoadmapPhase(id="c", name="C", start=date(2030, 1, 3), end=date(2030, 1, 20))
    bp = _bp(a, b, c)
    assert active_phase(bp, date(2030, 1, 2)) is None
    assert active_phase(bp, date(2030, 1, 3)) is b  # start day; b and c tie, b listed first
    assert active_phase(bp, date(2030, 1, 6)) is b  # end day; earliest start beats a
    assert active_phase(bp, date(2030, 1, 7)) is c  # c starts before a
    assert active_phase(_bp(a), date(2030, 1, 10)) is a
    assert active_phase(_bp(a), date(2030, 1, 11)) is None


# --- the weekly plan --------------------------------------------------------------------


async def _approved(pid: str, workspace: str = "w1", status: str = "approved", **fields: Any):
    doc = BlueprintDoc(workspace=workspace, profile_id=pid, version=1, status=status, **fields)
    await doc.insert()
    return doc


async def _idea(pid: str, workspace: str = "w1", **fields: Any) -> str:
    doc = IdeaDoc(workspace=workspace, profile=pid, format="text", hook="h", **fields)
    await doc.insert()
    return str(doc.id)


async def _plan(client, pid: str, week: str = WEEK) -> dict:
    resp = await client.get(PLAN, params={"profile_id": pid, "week": week})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _counts(plan: dict, channel: str) -> list[int]:
    return [sum(s["channel"] == channel for s in d["slots"]) for d in plan["days"]]


def _ch(channel: str, posts: int, sends: int = 0) -> dict:
    return {"channel": channel, "posts_per_week": posts, "sends_per_week": sends}


async def test_slot_counts_spread_by_weekday_and_skip_send_channels(w1):
    pid = await _profile()
    await _approved(
        pid,
        channels=[_ch("x", 3), _ch("reddit", 8), _ch("email", 5, 100), _ch("whatsapp", 2, 50)],
    )
    plan = await _plan(w1, pid)
    assert plan["week"] == WEEK
    assert [d["date"] for d in plan["days"]] == [(MON + timedelta(i)).isoformat() for i in range(7)]
    assert _counts(plan, "x") == [1, 1, 1, 0, 0, 0, 0]
    assert _counts(plan, "reddit") == [2, 1, 1, 1, 1, 1, 1]
    assert {s["channel"] for d in plan["days"] for s in d["slots"]} == {"x", "reddit"}
    assert all(s["idea_id"] is None for d in plan["days"] for s in d["slots"])


async def test_a_phase_overrides_only_the_channels_it_names(w1):
    pid = await _profile()
    thu = (MON + timedelta(3)).isoformat()
    phase = _phase(thu, "2030-02-01", id="p", cadence=[{"channel": "x", "posts_per_week": 14}])
    await _approved(pid, channels=[_ch("x", 7), _ch("reddit", 7)], roadmap={"phases": [phase]})
    plan = await _plan(w1, pid)
    assert _counts(plan, "x") == [1, 1, 1, 2, 2, 2, 2]
    assert _counts(plan, "reddit") == [1] * 7  # not named by the phase: the channel default


async def test_a_phase_with_an_empty_cadence_keeps_the_channel_defaults(w1):
    pid = await _profile()
    phase = _phase(MON.isoformat(), "2030-02-01", id="p", cadence=[])
    await _approved(pid, channels=[_ch("x", 3), _ch("linkedin", 7)], roadmap={"phases": [phase]})
    plan = await _plan(w1, pid)
    assert _counts(plan, "x") == [1, 1, 1, 0, 0, 0, 0]
    assert _counts(plan, "linkedin") == [1] * 7


@pytest.mark.parametrize("week", ["2030-W01", "2030-W03"])
async def test_waiting_ideas_fill_only_the_current_week(w1, week):
    pid = await _profile()
    await _approved(pid, channels=[_ch("x", 7)])
    await _idea(pid, status="approved", platform="x")
    year, wk = int(week[:4]), int(week[-2:])
    sat = datetime.combine(date.fromisocalendar(year, wk, 6), datetime.min.time(), tzinfo=UTC)
    held = await _idea(pid, status="approved", platform="x", scheduled_at=sat)
    days = (await _plan(w1, pid, week))["days"]
    assert [s["idea_id"] for d in days for s in d["slots"]] == [None] * 5 + [held, None]


async def test_waiting_ideas_are_read_per_channel(w1, monkeypatch):
    from pocketpaw_ee.cloud.growth.social import service as social_service

    monkeypatch.setattr(social_service, "IDEA_LIST_LIMIT", 1)  # no shared cap is used
    pid = await _profile()
    await _approved(pid, channels=[_ch("x", 7), _ch("reddit", 7)])
    reddit = [await _idea(pid, status="approved", platform="reddit") for _ in range(3)]
    x = [await _idea(pid, status="approved", platform="x") for _ in range(2)]
    days = (await _plan(w1, pid))["days"]
    by = {
        c: [s["idea_id"] for d in days for s in d["slots"] if s["channel"] == c]
        for c in ("x", "reddit")
    }
    # From Wednesday (TODAY): 5 slots each, filled only from that channel's ideas.
    assert by["x"] == [None, None, *x, None, None, None]
    assert by["reddit"] == [None, None, *reddit, None, None]


async def test_scheduled_ideas_are_placed_kept_on_overflow_and_waiting_ideas_fill(w1):
    pid = await _profile()
    await _approved(pid, channels=[_ch("x", 7)])
    tue = datetime.combine(MON + timedelta(1), datetime.min.time(), tzinfo=UTC)
    late = await _idea(pid, status="approved", platform="x", scheduled_at=tue + timedelta(hours=18))
    early = await _idea(pid, status="approved", platform="x", scheduled_at=tue + timedelta(hours=9))
    old = await _idea(pid, status="approved", platform="x")
    new = await _idea(pid, status="approved", platform="")  # no platform reads as x
    await _idea(pid, status="approved", platform="x", post_ids=["posted"])  # already posted
    await _idea(pid, status="new", platform="x")  # not approved
    await _idea(pid, status="approved", platform="reddit")  # no reddit cadence
    await _idea(pid, status="skipped", platform="x", scheduled_at=tue + timedelta(hours=12))

    days = (await _plan(w1, pid))["days"]
    assert [s["idea_id"] for s in days[1]["slots"]] == [early, late]  # skipped one left out
    assert days[1]["slots"][0]["scheduled_at"].startswith(f"{tue.date().isoformat()}T09:00")
    # TODAY is Wednesday: Monday's empty slot stays empty; waiting ideas fill from today.
    assert [s["idea_id"] for s in days[0]["slots"]] == [None]
    assert [s["idea_id"] for s in days[2]["slots"]] == [old]
    assert [s["idea_id"] for s in days[3]["slots"]] == [new]
    assert days[2]["slots"][0]["scheduled_at"] is None
    assert [s["idea_id"] for d in days[4:] for s in d["slots"]] == [None] * 3


async def test_scheduled_ideas_never_move_when_the_cadence_changes(w1):
    pid = await _profile()
    bp = await _approved(pid, channels=[_ch("x", 7)])
    fri = datetime.combine(MON + timedelta(4), datetime.min.time(), tzinfo=UTC)
    held = await _idea(pid, status="approved", platform="x", scheduled_at=fri + timedelta(hours=12))
    await _idea(pid, status="approved", platform="x")
    before = (await _plan(w1, pid))["days"][4]["slots"]
    await bp.set({"channels": [_ch("x", 0)]})
    after = (await _plan(w1, pid))["days"]
    assert before == after[4]["slots"] and after[4]["slots"][0]["idea_id"] == held
    assert [len(d["slots"]) for d in after] == [0, 0, 0, 0, 1, 0, 0]


async def test_an_empty_week_and_no_approved_blueprint(w1):
    pid = await _profile()
    plan = await _plan(w1, pid)  # nothing seeded
    assert len(plan["days"]) == 7 and all(d["slots"] == [] for d in plan["days"])
    # A draft with a cadence is never read: only the approved one shapes the plan.
    await _approved(pid, status="draft", channels=[_ch("x", 21)])
    await _idea(pid, status="approved", platform="x")
    assert all(d["slots"] == [] for d in (await _plan(w1, pid))["days"])
    # A scheduled idea still shows with no approved blueprint (never dropped).
    sun = datetime.combine(MON + timedelta(6), datetime.min.time(), tzinfo=UTC)
    held = await _idea(pid, status="approved", platform="x", scheduled_at=sun)
    days = (await _plan(w1, pid))["days"]
    assert [s["idea_id"] for s in days[6]["slots"]] == [held]


async def test_posted_ideas_never_crowd_scheduled_ones_out_of_the_read(w1, monkeypatch):
    from pocketpaw_ee.cloud.growth.social import service as social_service

    monkeypatch.setattr(social_service, "IDEA_LIST_LIMIT", 2)
    pid = await _profile()
    await _approved(pid, channels=[_ch("x", 7)])
    for _ in range(3):
        await _idea(pid, status="approved", platform="x", post_ids=["posted"])
    wed = datetime.combine(MON + timedelta(2), datetime.min.time(), tzinfo=UTC)
    held = await _idea(pid, status="approved", platform="x", scheduled_at=wed)
    waiting = await _idea(pid, status="approved", platform="x")
    days = (await _plan(w1, pid))["days"]
    assert [s["idea_id"] for s in days[2]["slots"]] == [held]
    assert [s["idea_id"] for s in days[3]["slots"]] == [waiting]


async def test_a_sunday_2359_idea_lands_on_sunday_across_a_year_boundary(w1):
    pid = await _profile()
    await _approved(pid, channels=[_ch("x", 0)])
    late = datetime(2027, 1, 3, 23, 59, tzinfo=UTC)  # the Sunday of 2026-W53
    held = await _idea(pid, status="approved", platform="x", scheduled_at=late)
    plan = await _plan(w1, pid, "2026-W53")
    assert [d["date"] for d in (plan["days"][0], plan["days"][6])] == ["2026-12-28", "2027-01-03"]
    assert [s["idea_id"] for s in plan["days"][6]["slots"]] == [held]
    assert all(d["slots"] == [] for d in plan["days"][:6])
    days = (await _plan(w1, pid, "2030-W01"))["days"]
    assert days[0]["date"] == "2029-12-31" and days[6]["date"] == "2030-01-06"


async def test_a_duplicate_phase_or_milestone_id_is_422(w1):
    pid = await _profile()
    await _seed(w1, pid)
    m = {"id": "m1", "title": "t", "date": "2030-02-02"}
    for phases in (
        [_phase("2030-02-01", "2030-02-05", id="a"), _phase("2030-02-06", "2030-02-07", id="a")],
        [
            _phase("2030-02-01", "2030-02-05", id="a", milestones=[m]),
            _phase("2030-02-01", "2030-02-05", id="b", milestones=[m]),
        ],
    ):
        resp = await w1.put(
            f"{BP}/draft", params={"profile_id": pid}, json={"roadmap": {"phases": phases}}
        )
        assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "week", ["2030-2", "2030-W54", "2030-W53", "2030-W00", "2030W02", "x", "20301-W02"]
)
async def test_a_bad_week_is_422(w1, week):
    pid = await _profile()
    resp = await w1.get(PLAN, params={"profile_id": pid, "week": week})
    assert resp.status_code == 422, resp.text


async def test_plan_tenancy_on_workspace_and_profile(w1, w2):
    pid, other = await _profile(), await _profile()
    await _approved(pid, channels=[_ch("x", 7)])
    await _approved(other, channels=[_ch("x", 7)])
    await _approved(pid, workspace="w2", channels=[_ch("x", 7)])
    await _idea(other, status="approved", platform="x")
    await _idea(pid, workspace="w2", status="approved", platform="x")
    mon = datetime.combine(MON, datetime.min.time(), tzinfo=UTC)
    await _idea(other, status="approved", platform="x", scheduled_at=mon)
    await _idea(pid, workspace="w2", status="approved", platform="x", scheduled_at=mon)

    days = (await _plan(w1, pid))["days"]
    assert [len(d["slots"]) for d in days] == [1] * 7
    assert all(s["idea_id"] is None for d in days for s in d["slots"])
    assert (await w2.get(PLAN, params={"profile_id": pid, "week": WEEK})).status_code == 404
