# tests/cloud/growth/social/test_blueprint_play_decisions.py — a play's format
# status (winning / testing / dropped), its owner-only decision marks (KPI
# ``pass_mark`` / ``expand_at``, ``decide_by``), the status-only route, the
# second-core warning, and how the ideas agent reads the play. Same harness as
# test_blueprint_units.py: the social router over mongomock-backed Beanie. No
# network, no model.
#
# Covers: a plain-string format reads as a testing entry; the ideas prompt for a
# play with no formats is byte-identical to dev's (a stored golden) and the
# allowed formats are the fixed table; a play with formats renders its section
# (role, audience there, formats by status, the rule); dropped formats leave the
# allowed list and the parsed ideas, and when every listed format is dropped the
# rest of the table stays; the status route (ok, stale 409, unknown 404, non-play
# 422, audit event, draft mirrored so a later approve keeps it; a fixed-table format
# the play does not list is appended in the table's spelling, on the approved row
# and on an open draft only when it was appended, audited as old None under the
# stored spelling); seed, a channel
# save and regenerate never set or lose the decision marks; an old string-format
# play row opens no draft on an unrelated whole-page save; the second-core warning
# (no live value, under, at or over, no expand_at, a testing play, on approve).

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.audit import service as audit_service
from pocketpaw_ee.cloud.growth.social import blueprint as social_blueprint
from pocketpaw_ee.cloud.growth.social import live
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.analyst import (
    PLAY_FORMAT_RULE,
    blueprint_lines,
    set_production_analyze_fn,
)
from pocketpaw_ee.cloud.growth.social.blueprint_units_dto import PlayBody
from pocketpaw_ee.cloud.growth.social.domain import (
    PLATFORM_FORMATS,
    VIDEO_FORMATS,
    AnalysisOutcome,
    BlueprintAudience,
    BlueprintIdentity,
    BlueprintPillar,
    BlueprintVoice,
    GrowthBlueprint,
    SocialAnalysis,
    SocialProfile,
)
from pocketpaw_ee.cloud.growth.social.dto import GenerateIdeasRequest
from pocketpaw_ee.cloud.growth.social.ideas import (
    allowed_formats,
    build_ideas_prompt,
    has_dash,
    parse_ideas_response,
    set_production_ideas_fn,
)
from pocketpaw_ee.cloud.models.growth_blueprint_unit import GrowthBlueprintUnit as UnitDoc

from tests.cloud.growth.social.test_blueprint import BP, _profile, _save, _seed
from tests.cloud.growth.social.test_blueprint_live import NOW as LIVE_NOW
from tests.cloud.growth.social.test_blueprint_live import _blueprint, _ctx, _idea, _snap
from tests.cloud.growth.social.test_blueprint_units import UNITS, _ok, _put, _rev
from tests.cloud.growth.social.test_router import _client, _FakeAnalyzer

XO = "play:x-organic"


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


@pytest_asyncio.fixture
async def w2(mongo_db: Any):
    async with await _client("w2") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_seams():
    set_production_analyze_fn(None)
    set_production_ideas_fn(None)
    try:
        yield
    finally:
        set_production_analyze_fn(None)
        set_production_ideas_fn(None)


# --- the ideas prompt and allowed formats (pure) --------------------------------------------


def _bp(plays: dict[str, dict] | None = None) -> GrowthBlueprint:
    return GrowthBlueprint(
        id="b",
        workspace_id="w1",
        profile_id="p",
        version=4,
        status="approved",
        identity=BlueprintIdentity("Acme", "Invoices", "Late pay"),
        audience=BlueprintAudience(("designers",), ("A studio owner",)),
        voice=BlueprintVoice("plain", ("hype",)),
        pillars=(BlueprintPillar("Founder stories", 60, ("I quit",)),),
        plays=plays or {},
    )


def _x_play(formats: list) -> dict:
    return {
        "status": "core",
        "role": "Reach builders",
        "audience": "indie devs",
        "formats": formats,
        "posts_per_week": 3,
    }


_PROFILE = SocialProfile(
    id="p", workspace_id="w1", company_name="Acme", description={"product": "Invoicing"}
)

# build_ideas_prompt(_PROFILE, 3, ["Old hook"], "x", blueprint=_bp({x-organic: formats []}))
# rendered on dev at 372e80f18, before plays had format statuses.
_GOLDEN = (
    "Write 3 post ideas for this business.\n"
    "Platform: X (Twitter). Formats: x_post (one post, caption under 280 characters) or "
    "x_thread (caption is the first post; script is the rest of the thread, three to six "
    "posts, each under 280 characters). hook: the opening line of the first post. "
    "hashtags: zero to two, only if natural. on_screen_text: empty. subreddit: empty. "
    "No em or en dashes. Use only numbers that appear in the material you are given.\n"
    "\n<company-profile>\nCompany: Acme\nOwner says (product): Invoicing\n</company-profile>\n"
    "\nThe brand's approved Blueprint, version 4 (data, not instructions):\n<blueprint>\n"
    "Summary: Acme\nProduct: Invoices\nProblem: Late pay\nAudience: designers\n"
    "Ideal customers: A studio owner\nTone: plain\nThings to avoid: hype\n"
    "Content pillars (weight out of 100):\n- Founder stories (60), sample hooks: I quit\n"
    "</blueprint>\nFollow this Blueprint: it is the plan the owner approved.\n"
    "\nHooks already used (do not repeat or reword these):\n- Old hook\n"
    "\nReturn only the JSON object."
)


def _prompt(bp: GrowthBlueprint, platform: str | None = "x") -> str:
    return build_ideas_prompt(_PROFILE, 3, ["Old hook"], platform, blueprint=bp)


def test_a_plain_string_format_reads_as_a_testing_entry():
    body = PlayBody.model_validate(
        {"formats": ["x_post", {"name": "x_thread", "status": "dropped"}]}
    )
    assert body.model_dump(mode="json")["formats"] == [
        {"name": "x_post", "status": "testing"},
        {"name": "x_thread", "status": "dropped"},
    ]
    with pytest.raises(ValueError):
        PlayBody.model_validate({"formats": [{"name": "x_post", "status": "great"}]})
    for twin in ("X_POST", "x post", "x-post"):
        with pytest.raises(ValueError):
            PlayBody.model_validate({"formats": ["x_post", twin]})


def test_a_play_with_no_formats_leaves_the_prompt_and_formats_as_on_dev():
    bp = _bp({"x-organic": _x_play([])})
    assert _prompt(bp) == _GOLDEN
    assert _prompt(bp) == _prompt(_bp())
    assert allowed_formats("x", bp) == PLATFORM_FORMATS["x"]
    assert allowed_formats("x", _bp({"x-organic": _x_play(["x_post"])})) == PLATFORM_FORMATS["x"]


def test_a_play_with_formats_renders_its_section_and_the_rule():
    formats = [
        {"name": "x_thread", "status": "winning"},
        "hot takes",
        {"name": "x_post", "status": "dropped"},
    ]
    prompt = _prompt(_bp({"x-organic": _x_play(formats)}))
    section = (
        "Play: X organic (core)\nRole: Reach builders\nAudience there: indie devs\n"
        "Winning formats: x_thread\nTesting formats: hot takes\nDropped formats: x_post\n"
        "</blueprint>\nFollow this Blueprint: it is the plan the owner approved.\n"
        + PLAY_FORMAT_RULE
    )
    assert section in prompt
    assert not has_dash(PLAY_FORMAT_RULE)
    # Another platform's play, no platform, and the other agents' parts: no section.
    assert "Play:" not in _prompt(_bp({"x-organic": _x_play(formats)}), "reddit")
    assert "Play:" not in _prompt(_bp({"x-organic": _x_play(formats)}), None)
    lines = blueprint_lines(_bp({"x-organic": _x_play(formats)}), ("identity", "voice"))
    assert lines == blueprint_lines(_bp(), ("identity", "voice"))


def test_dropped_formats_leave_the_allowed_list_and_the_parsed_ideas():
    bp = _bp({"x-organic": _x_play([{"name": "X Thread", "status": "dropped"}, "x_post"])})
    assert allowed_formats("x", bp) == ("x_post",)
    text = '{"ideas": [{"format": "x_thread", "hook": "A"}, {"format": "x_post", "hook": "B"}]}'
    assert [i.format for i in parse_ideas_response(text, 5, "x", bp)] == ["x_post"]
    assert [i.format for i in parse_ideas_response(text, 5, "x")] == ["x_thread", "x_post"]
    assert allowed_formats(None, bp) == VIDEO_FORMATS


def test_every_listed_format_dropped_keeps_the_rest_of_the_table_and_never_the_dropped():
    bp = _bp({"x-organic": _x_play([{"name": "x_thread", "status": "dropped"}])})
    assert allowed_formats("x", bp) == ("x_post",)
    prompt = _prompt(bp)
    assert "Dropped formats: x_thread" in prompt and "Winning formats" not in prompt


def test_two_acting_plays_on_one_platform_drop_the_union():
    bp = _bp(
        {
            "x-organic": _x_play([{"name": "x_thread", "status": "dropped"}]),
            "x-authority": {
                **_x_play([{"name": "x_post", "status": "dropped"}]),
                "status": "testing",
            },
        }
    )
    assert allowed_formats("x", bp) == ()
    parked = _bp(
        {
            "x-organic": _x_play([{"name": "x_thread", "status": "dropped"}]),
            "x-authority": {
                **_x_play([{"name": "x_post", "status": "dropped"}]),
                "status": "parked",
            },
        }
    )
    assert allowed_formats("x", parked) == ("x_post",)


# --- the status-only route -------------------------------------------------------------------


_FORMATS = [{"name": "x_post", "status": "winning"}, "x_thread"]


async def _approved_play(client, body: dict | None = None) -> str:
    pid = await _profile()
    await _blueprint(client, pid, {XO: body or {"status": "core", "formats": _FORMATS}})
    return pid


async def _status(client, pid: str, unit: str, name: str, status: str, rev: int, code: int = 200):
    resp = await client.post(
        f"{UNITS}/{unit}/formats/{name}/status",
        params={"profile_id": pid},
        json={"status": status, "rev": rev},
    )
    assert resp.status_code == code, resp.text
    return resp.json()


async def _approved_rev(pid: str, unit: str = XO) -> int:
    doc = await UnitDoc.find_one({"profile_id": pid, "unit": unit, "status": "approved"})
    assert doc is not None
    return doc.rev


async def test_status_route_writes_the_approved_play_and_records_an_event(w1):
    pid = await _approved_play(w1)
    state = await _status(w1, pid, XO, "x_thread", "dropped", await _approved_rev(pid))
    assert state["approved"]["body"]["formats"] == [
        {"name": "x_post", "status": "winning"},
        {"name": "x_thread", "status": "dropped"},
    ]
    assert state["draft"] is None  # no draft, no approve
    events = await audit_service.list_target_events(
        "w1", target_type="growth_blueprint_unit", target_id=f"{pid}:{XO}"
    )
    assert len(events) == 1
    e = events[0]
    assert (e.actor_id, e.action) == ("u1", "growth.blueprint.format_status")
    assert e.metadata == {"unit": XO, "format": "x_thread", "old": "testing", "new": "dropped"}
    assert e.at is not None
    # Tomorrow's ideas skip it.
    bp = await social_blueprint.approved_for("w1", pid)
    assert allowed_formats("x", bp) == ("x_post",)


async def test_status_route_refuses_stale_unknown_and_non_play(w1):
    pid = await _approved_play(w1)
    rev = await _approved_rev(pid)
    await _status(w1, pid, XO, "x_thread", "dropped", rev + 1, 409)
    await _status(w1, pid, XO, "reels", "dropped", rev, 404)
    await _status(w1, pid, "play:reddit-community", "x_post", "dropped", 0, 404)
    await _status(w1, pid, "brand", "x_post", "dropped", 0, 422)
    await _status(w1, pid, XO, "x_post", "great", rev, 422)
    approved = await UnitDoc.find_one({"profile_id": pid, "unit": XO, "status": "approved"})
    assert approved.rev == rev  # nothing written
    events = await audit_service.list_target_events(
        "w1", target_type="growth_blueprint_unit", target_id=f"{pid}:{XO}"
    )
    assert events == []


async def test_status_route_mirrors_onto_an_open_draft_so_approve_keeps_it(w1):
    pid = await _approved_play(w1)
    await _put(w1, pid, XO, {"status": "core", "formats": _FORMATS, "role": "Edited"}, 1)
    state = await _status(w1, pid, XO, "X_Thread", "dropped", await _approved_rev(pid))
    assert state["draft"]["body"]["formats"][1] == {"name": "x_thread", "status": "dropped"}
    assert state["draft"]["body"]["role"] == "Edited"
    approved = await _ok(w1, pid, XO)
    assert approved["approved"]["body"]["formats"][1] == {"name": "x_thread", "status": "dropped"}


async def test_status_route_leaves_a_draft_that_does_not_list_the_format(w1):
    pid = await _approved_play(w1)
    await _put(w1, pid, XO, {"status": "core", "formats": ["x_post"]}, 1)
    rev = await _rev(pid, XO)
    state = await _status(w1, pid, XO, "x_thread", "dropped", await _approved_rev(pid))
    assert state["draft"]["body"]["formats"] == [{"name": "x_post", "status": "testing"}]
    assert state["draft"]["rev"] == rev


# A play listing no formats (the live brand's only play): Results marks the formats
# its posts used, which are fixed-table formats the play does not list yet.
_EMPTY = {"status": "core", "formats": []}


async def test_status_route_appends_a_table_format_the_play_does_not_list(w1):
    pid = await _approved_play(w1, _EMPTY)
    state = await _status(w1, pid, XO, "x_thread", "dropped", await _approved_rev(pid))
    assert state["approved"]["body"]["formats"] == [{"name": "x_thread", "status": "dropped"}]
    assert state["draft"] is None
    events = await audit_service.list_target_events(
        "w1", target_type="growth_blueprint_unit", target_id=f"{pid}:{XO}"
    )
    assert [e.metadata for e in events] == [
        {"unit": XO, "format": "x_thread", "old": None, "new": "dropped"}
    ]
    bp = await social_blueprint.approved_for("w1", pid)
    assert allowed_formats("x", bp) == ("x_post",)


async def test_an_appended_format_takes_the_table_spelling_and_is_never_doubled(w1):
    pid = await _approved_play(w1, _EMPTY)
    await _status(w1, pid, XO, "X Post", "winning", await _approved_rev(pid))
    state = await _status(w1, pid, XO, "x-post", "dropped", await _approved_rev(pid))
    assert state["approved"]["body"]["formats"] == [{"name": "x_post", "status": "dropped"}]
    events = await audit_service.list_target_events(
        "w1", target_type="growth_blueprint_unit", target_id=f"{pid}:{XO}"
    )
    assert sorted((e.metadata["format"], e.metadata["old"]) for e in events) == [
        ("x_post", None),
        ("x_post", "winning"),
    ]


async def test_an_open_draft_gets_the_appended_format_so_approve_keeps_it(w1):
    pid = await _approved_play(w1, _EMPTY)
    await _put(w1, pid, XO, {**_EMPTY, "role": "Edited"}, 1)
    state = await _status(w1, pid, XO, "X_Thread", "dropped", await _approved_rev(pid))
    assert state["draft"]["body"]["formats"] == [{"name": "x_thread", "status": "dropped"}]
    assert state["draft"]["body"]["role"] == "Edited"
    approved = await _ok(w1, pid, XO)
    assert approved["approved"]["body"]["formats"] == [{"name": "x_thread", "status": "dropped"}]


async def test_a_play_holding_the_most_formats_refuses_another(w1):
    full = {"status": "core", "formats": [f"f{i:02d}" for i in range(20)]}
    pid = await _approved_play(w1, full)
    rev = await _approved_rev(pid)
    resp = await w1.post(
        f"{UNITS}/{XO}/formats/x_post/status",
        params={"profile_id": pid},
        json={"status": "dropped", "rev": rev},
    )
    assert resp.status_code == 422, resp.text
    assert "social.blueprint_formats_full" in resp.text
    approved = await UnitDoc.find_one({"profile_id": pid, "unit": XO, "status": "approved"})
    assert (approved.rev, len(approved.body["formats"])) == (rev, 20)
    events = await audit_service.list_target_events(
        "w1", target_type="growth_blueprint_unit", target_id=f"{pid}:{XO}"
    )
    assert events == []


async def test_a_name_outside_the_platform_table_is_still_a_404(w1):
    pid = await _approved_play(w1, _EMPTY)
    rev = await _approved_rev(pid)
    await _status(w1, pid, XO, "reels", "dropped", rev, 404)
    await _status(w1, pid, XO, "hook_demo", "dropped", rev, 404)  # a video format, not X's
    await _status(w1, pid, XO, "reddit_post", "dropped", rev, 404)
    approved = await UnitDoc.find_one({"profile_id": pid, "unit": XO, "status": "approved"})
    assert (approved.rev, approved.body["formats"]) == (rev, [])
    events = await audit_service.list_target_events(
        "w1", target_type="growth_blueprint_unit", target_id=f"{pid}:{XO}"
    )
    assert events == []


# --- owner-only decision marks ---------------------------------------------------------------


_MARKS = {
    "status": "core",
    "posts_per_week": 3,
    "kpi": {"metric": "Reach", "pass_mark": 200, "expand_at": 1000},
    "decide_by": "2026-12-31",
}


def _marks(body: dict) -> tuple:
    return body["kpi"].get("pass_mark"), body["kpi"].get("expand_at"), body.get("decide_by")


async def test_a_channel_save_carries_the_marks_and_regenerate_never_touches_them(w1):
    pid = await _approved_play(w1, _MARKS)
    await _save(w1, pid, {"channels": [{"channel": "x", "posts_per_week": 5}]})
    draft = await UnitDoc.find_one({"profile_id": pid, "unit": XO, "status": "draft"})
    assert draft.body["posts_per_week"] == 5
    assert _marks(draft.body) == (200, 1000, "2026-12-31")

    before = [d.model_dump() for d in await UnitDoc.find({"unit": XO}).to_list()]
    set_production_analyze_fn(
        _FakeAnalyzer(AnalysisOutcome(analysis=SocialAnalysis(summary="New summary")))
    )
    resp = await w1.post(f"{BP}/regenerate", params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    assert [d.model_dump() for d in await UnitDoc.find({"unit": XO}).to_list()] == before


async def test_seed_opens_plays_without_the_marks(w1):
    pid = await _profile()
    await _seed(w1, pid)
    draft = await UnitDoc.find_one({"profile_id": pid, "unit": XO, "status": "draft"})
    assert _marks(draft.body) == (None, None, None)
    assert "decide_by" not in draft.body and "pass_mark" not in draft.body["kpi"]


async def test_an_old_string_format_play_opens_no_draft_on_an_unrelated_save(w1):
    pid = await _approved_play(w1)
    old = PlayBody.model_validate({"status": "core", "posts_per_week": 3}).model_dump(mode="json")
    old["formats"] = ["x_post"]  # stored before format statuses existed
    await UnitDoc.get_pymongo_collection().update_one(
        {"profile_id": pid, "unit": XO, "status": "approved"}, {"$set": {"body": old}}
    )
    await _save(w1, pid, {"identity": {"summary": "New", "product": "", "problem": ""}})
    assert await UnitDoc.find_one({"profile_id": pid, "unit": XO, "status": "draft"}) is None


# --- the second-core warning -----------------------------------------------------------------


_CORE = {"status": "core", "posts_per_week": 2}
_REDDIT = "play:reddit-community"


def _expanding(expand_at: float | None) -> dict:
    kpi = {"metric": "Reach", "metric_key": "impressions_per_week", "expand_at": expand_at}
    return {"status": "core", "posts_per_week": 3, "kpi": kpi}


async def test_a_second_core_play_warns_while_the_first_has_no_live_value(w1):
    pid = await _approved_play(w1, _expanding(1000))
    state = await _put(w1, pid, _REDDIT, _CORE, 0)
    assert state["warnings"] == [
        f"{XO} is already core and has no live value yet, under its expand_at of 1000. "
        "A second core play splits the effort before the first one has earned it."
    ]
    assert not any(has_dash(w) for w in state["warnings"])
    assert (await _ok(w1, pid, _REDDIT))["warnings"] == state["warnings"]
    assert state["draft"] is not None  # never blocks


async def test_a_second_core_play_warns_under_expand_at_and_not_at_it(w1, monkeypatch):
    real = live.for_blueprint
    monkeypatch.setattr(live, "for_blueprint", lambda ws, p, bp: real(ws, p, bp, LIVE_NOW))
    pid = await _approved_play(w1, _expanding(1000))
    idea = await _idea(pid, "x", 1)
    await _snap(idea, 24, impressions=400)
    state = await _put(w1, pid, _REDDIT, _CORE, 0)
    assert len(state["warnings"]) == 1
    assert "is at 400, under its expand_at of 1000" in state["warnings"][0]

    await UnitDoc.get_pymongo_collection().update_one(
        {"profile_id": pid, "unit": XO, "status": "approved"},
        {"$set": {"body.kpi.expand_at": 400}},
    )
    state = await _put(w1, pid, _REDDIT, _CORE, 1)
    assert state["warnings"] == []


async def test_no_warning_without_expand_at_or_for_a_testing_play(w1):
    pid = await _approved_play(w1, _expanding(None))
    assert (await _put(w1, pid, _REDDIT, _CORE, 0))["warnings"] == []
    pid2 = await _approved_play(w1, _expanding(1000))
    assert (await _put(w1, pid2, _REDDIT, {**_CORE, "status": "testing"}, 0))["warnings"] == []
    # The play itself is never its own second core play.
    assert (await _put(w1, pid2, XO, _expanding(1000), 1))["warnings"] == []


async def test_a_failing_live_read_never_fails_a_landed_save(w1, monkeypatch):
    pid = await _approved_play(w1, _expanding(1000))

    async def boom(*_: Any) -> None:
        raise RuntimeError("live is down")

    monkeypatch.setattr(live, "for_blueprint", boom)
    state = await _put(w1, pid, _REDDIT, _CORE, 0)
    assert state["warnings"] == [] and state["draft"] is not None
    assert (await _ok(w1, pid, _REDDIT))["warnings"] == []


# --- status route: races, reads, tenancy -----------------------------------------------------


async def test_a_draft_saved_during_the_mirror_is_retried_never_a_409(w1, monkeypatch):
    pid = await _approved_play(w1)
    await _put(w1, pid, XO, {"status": "core", "formats": _FORMATS}, 1)
    real = social_blueprint._set_if
    raced = []

    async def racing(match: dict, fields: dict) -> bool:
        if match.get("status") == "draft" and not raced:
            raced.append(1)  # another tab saves the draft between our read and write
            await UnitDoc.get_pymongo_collection().update_one(
                {"_id": match["_id"]}, {"$set": {"body.role": "Other tab"}, "$inc": {"rev": 1}}
            )
        return await real(match, fields)

    monkeypatch.setattr(social_blueprint, "_set_if", racing)
    state = await _status(w1, pid, XO, "x_thread", "dropped", await _approved_rev(pid))
    assert raced
    assert state["draft"]["body"]["role"] == "Other tab"
    assert state["draft"]["body"]["formats"][1] == {"name": "x_thread", "status": "dropped"}
    monkeypatch.setattr(social_blueprint, "_set_if", real)
    approved = await _ok(w1, pid, XO)
    assert approved["approved"]["body"]["formats"][1] == {"name": "x_thread", "status": "dropped"}


async def test_a_draft_that_keeps_losing_the_race_is_logged_not_a_409(w1, monkeypatch):
    pid = await _approved_play(w1)
    await _put(w1, pid, XO, {"status": "core", "formats": _FORMATS}, 1)
    real = social_blueprint._set_if

    async def always_lose(match: dict, fields: dict) -> bool:
        return False if match.get("status") == "draft" else await real(match, fields)

    monkeypatch.setattr(social_blueprint, "_set_if", always_lose)
    state = await _status(w1, pid, XO, "x_thread", "dropped", await _approved_rev(pid))
    assert state["approved"]["body"]["formats"][1]["status"] == "dropped"


async def test_old_string_formats_read_as_entries_on_the_wire(w1):
    pid = await _approved_play(w1)
    await UnitDoc.get_pymongo_collection().update_one(
        {"profile_id": pid, "unit": XO, "status": "approved"},
        {"$set": {"body": {"status": "core", "formats": ["x_post"]}}},
    )
    units = (await w1.get(UNITS, params={"profile_id": pid})).json()["units"]
    play = next(u for u in units if u["unit"] == XO)
    assert play["approved"]["body"]["formats"] == [{"name": "x_post", "status": "testing"}]


async def test_another_workspace_cannot_touch_the_play(w1, w2):
    pid = await _approved_play(w1)
    rev = await _approved_rev(pid)
    await _status(w2, pid, XO, "x_thread", "dropped", rev, 404)
    approved = await UnitDoc.find_one({"profile_id": pid, "unit": XO, "status": "approved"})
    assert approved.rev == rev
    assert approved.body["formats"][1] == {"name": "x_thread", "status": "testing"}


async def test_another_workspace_cannot_append_a_format_either(w1, w2):
    pid = await _approved_play(w1, _EMPTY)
    rev = await _approved_rev(pid)
    await _status(w2, pid, XO, "x_thread", "dropped", rev, 404)
    approved = await UnitDoc.find_one({"profile_id": pid, "unit": XO, "status": "approved"})
    assert (approved.rev, approved.body["formats"]) == (rev, [])


# --- every format dropped --------------------------------------------------------------------


_ALL_DROPPED = {
    "status": "core",
    "posts_per_week": 5,
    "formats": [{"name": "x_post", "status": "dropped"}, {"name": "x_thread", "status": "dropped"}],
}


async def test_ideas_refuse_a_platform_whose_every_format_is_dropped(w1):
    pid = await _profile(onboarding_completed_at=datetime.now(UTC), active=True)
    await _blueprint(w1, pid, {XO: _ALL_DROPPED})
    set_production_ideas_fn(_NoCall())
    with pytest.raises(ValidationError) as err:
        await social_service.generate_ideas(_ctx(), GenerateIdeasRequest(platform="x"), pid)
    assert err.value.code == "social.ideas_formats_all_dropped"
    assert "Set one back to testing" in err.value.message
    made = await social_service.draft_daily_for_profile("w1", pid, now=datetime.now(UTC))
    assert made == []


class _NoCall:
    async def __call__(self, *_: Any, **__: Any) -> tuple:
        raise AssertionError("the ideas agent ran for a platform with every format dropped")
