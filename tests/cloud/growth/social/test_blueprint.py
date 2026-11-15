# tests/cloud/growth/social/test_blueprint.py — the Brand Blueprint
# (``/api/v1/growth/social/blueprint...``). Same harness as test_router.py: the
# social router over mongomock-backed Beanie with per-workspace clients and a
# fake analyser. No network, no model.
#
# Covers: seed maps every section from the profile (analysis, quota, active ICPs
# of this workspace only) and is idempotent; draft save replaces only the sections
# sent, opens a draft from the approved, refuses status/version keys; approve
# supersedes the previous approved (one approved, one draft at most); regenerate
# writes a new draft version, supersedes only the old draft, never touches the
# approved, carries goals / channels / weights over and drops text with a dash or
# an invented number; diff per section; RBAC (writes need growth.manage);
# tenancy on workspace and on profile; the doc -> domain -> response round trip.

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.growth.social import blueprint as social_blueprint
from pocketpaw_ee.cloud.growth.social.analyst import set_production_analyze_fn
from pocketpaw_ee.cloud.growth.social.domain import (
    AnalysisOutcome,
    GrowthBlueprint,
    SocialAnalysis,
)
from pocketpaw_ee.cloud.growth.social.dto import GrowthBlueprintResponse
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as BlueprintDoc
from pocketpaw_ee.cloud.models.icp import Icp as IcpDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile as ProfileDoc

from tests.cloud.growth.social.test_router import BASE, _client, _FakeAnalyzer

BP = f"{BASE}/blueprint"

_ANALYSIS = {
    "summary": "Acme makes invoicing simple.",
    "product": "Invoicing app",
    "problem": "Chasing late payments",
    "audience": "freelance designers; small agencies",
    "tone": "plain and warm",
    "avoid": ["hype"],
    "content_pillars": ["Speed", "Cash flow", "Stories"],
    "hooks": ["h1", "h2", "h3", "h4"],
}


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
    try:
        yield
    finally:
        set_production_analyze_fn(None)


async def _profile(workspace: str = "w1", **extra: Any) -> str:
    doc = ProfileDoc(
        workspace=workspace,
        company_name="Acme",
        website="https://acme.test",
        description={"product": "Invoicing for 2 person studios", "audience": "designers"},
        analysis_status="ready",
        analysis=dict(_ANALYSIS),
        weekly_post_quota=7,
        **extra,
    )
    await doc.insert()
    return str(doc.id)


async def _seed(client, pid: str) -> dict:
    resp = await client.post(f"{BP}/seed", params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _approve(client, pid: str) -> dict:
    resp = await client.post(f"{BP}/approve", params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _save(client, pid: str, body: dict) -> dict:
    resp = await client.put(f"{BP}/draft", params={"profile_id": pid}, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _statuses(pid: str) -> dict[int, str]:
    docs = await BlueprintDoc.find({"workspace": "w1", "profile_id": pid}).to_list()
    return {d.version: d.status for d in docs}


# --- seed ------------------------------------------------------------------------------


async def test_seed_maps_every_section_and_is_idempotent(w1):
    pid = await _profile()
    await IcpDoc(workspace="w1", name="a", criteria="  Studios   with 2 to 10 people ").insert()
    await IcpDoc(workspace="w1", name="b", criteria="Paused one", status="paused").insert()
    await IcpDoc(workspace="w2", name="c", criteria="Other tenant").insert()

    state = await _seed(w1, pid)
    draft = state["draft"]
    assert state["approved"] is None
    assert (draft["version"], draft["status"], draft["profile_id"]) == (1, "draft", pid)
    assert draft["workspace_id"] == "w1" and draft["created_by"] == "u1"
    assert draft["identity"] == {
        "summary": "Acme makes invoicing simple.",
        "product": "Invoicing app",
        "problem": "Chasing late payments",
    }
    assert draft["audience"] == {
        "segments": ["freelance designers", "small agencies"],
        "icp_sentences": ["Studios with 2 to 10 people"],
    }
    assert draft["voice"] == {"tone": "plain and warm", "avoid": ["hype"]}
    assert draft["pillars"] == [
        {"name": "Speed", "weight": 34, "hooks": ["h1", "h4"]},
        {"name": "Cash flow", "weight": 33, "hooks": ["h2"]},
        {"name": "Stories", "weight": 33, "hooks": ["h3"]},
    ]
    assert draft["channels"] == [{"channel": "x", "posts_per_week": 7, "sends_per_week": 0}]
    assert draft["goals"] == []
    assert draft["roadmap"] == {"phases": []}
    assert draft["learning_ids"] == [] and draft["approved_by"] is None

    again = await _seed(w1, pid)
    assert again["draft"]["id"] == draft["id"]
    assert await BlueprintDoc.find({"workspace": "w1"}).count() == 1


async def test_seed_without_analysis_falls_back_to_what_the_owner_typed(w1):
    doc = ProfileDoc(workspace="w1", description={"product": "Typed product", "tone": "dry"})
    await doc.insert()
    draft = (await _seed(w1, str(doc.id)))["draft"]
    assert draft["identity"]["product"] == "Typed product"
    assert draft["voice"]["tone"] == "dry"
    assert draft["pillars"] == []


# --- draft save and approve --------------------------------------------------------------


async def test_draft_save_replaces_only_sent_sections_and_mints_goal_ids(w1):
    pid = await _profile()
    await _seed(w1, pid)
    state = await _save(w1, pid, {"goals": [{"metric": "signups", "target": 40, "channel": "x"}]})
    draft = state["draft"]
    assert draft["version"] == 1
    assert draft["identity"]["summary"] == "Acme makes invoicing simple."
    (goal,) = draft["goals"]
    assert goal["id"] and (goal["metric"], goal["target"], goal["channel"]) == ("signups", 40, "x")


@pytest.mark.parametrize(
    "body",
    [
        {"status": "approved"},
        {"version": 9},
        {"approved_by": "u2"},
        {"roadmap": {"phases": []}},
        {"pillars": [{"name": "A", "weight": 101}]},
        {"pillars": [{"name": "A"}, {"name": "a"}]},
        {"channels": [{"channel": "tiktok"}]},
        {"goals": [{"metric": "m", "target": -1}]},
    ],
)
async def test_draft_save_refuses_non_editable_or_bad_keys(w1, body):
    pid = await _profile()
    await _seed(w1, pid)
    resp = await w1.put(f"{BP}/draft", params={"profile_id": pid}, json=body)
    assert resp.status_code == 422, resp.text


async def test_draft_save_before_seed_is_404(w1):
    pid = await _profile()
    resp = await w1.put(f"{BP}/draft", params={"profile_id": pid}, json={})
    assert resp.status_code == 404


async def test_approve_supersedes_the_previous_approved(w1):
    pid = await _profile()
    await _seed(w1, pid)
    first = (await _approve(w1, pid))["approved"]
    assert first["version"] == 1 and first["approved_by"] == "u1" and first["approved_at"]

    state = await _save(w1, pid, {"identity": {"summary": "New summary"}})
    assert state["draft"]["version"] == 2
    assert state["draft"]["voice"] == first["voice"]  # opened from the approved
    assert state["approved"]["id"] == first["id"]

    state = await _approve(w1, pid)
    assert state["approved"]["version"] == 2 and state["draft"] is None
    assert await _statuses(pid) == {1: "superseded", 2: "approved"}

    resp = await w1.post(f"{BP}/approve", params={"profile_id": pid})
    assert resp.status_code == 409


# --- regenerate --------------------------------------------------------------------------


async def test_regenerate_writes_a_new_draft_and_never_touches_the_approved(w1):
    pid = await _profile()
    await _seed(w1, pid)
    await _approve(w1, pid)
    goals = [{"id": "g1", "metric": "signups", "target": 40, "channel": "x"}]
    base = (
        await _save(
            w1,
            pid,
            {
                "goals": goals,
                "pillars": [{"name": "Speed", "weight": 60, "hooks": ["old hook"]}],
                "channels": [
                    {"channel": "x", "posts_per_week": 5, "sends_per_week": 0},
                    {"channel": "email", "posts_per_week": 0, "sends_per_week": 30},
                ],
            },
        )
    )["draft"]
    approved_before = (await BlueprintDoc.find_one({"status": "approved"})).model_dump()

    fake = _FakeAnalyzer(
        AnalysisOutcome(
            analysis=SocialAnalysis(
                summary="Fresh summary for 2 person studios.",
                product="Ships 50% faster",  # number the owner never typed
                problem="Late invoices",
                audience="studio owners",
                tone="calm — never loud",  # em dash
                avoid=["jargon", "Top 10 lists"],
                content_pillars=["Speed", "Craft"],
                hooks=["Get paid 3x sooner", "Stop chasing"],
            )
        )
    )
    set_production_analyze_fn(fake)
    resp = await w1.post(f"{BP}/regenerate", params={"profile_id": pid})
    assert resp.status_code == 200, resp.text
    state = resp.json()
    draft = state["draft"]

    assert draft["version"] == 3 and draft["id"] != base["id"]
    assert await _statuses(pid) == {1: "approved", 2: "superseded", 3: "draft"}
    approved_after = (await BlueprintDoc.find_one({"status": "approved"})).model_dump()
    assert approved_after == approved_before

    # Text that passes the checks is taken; a number of the owner's ("2") is fine.
    assert draft["identity"]["summary"] == "Fresh summary for 2 person studios."
    assert draft["identity"]["problem"] == "Late invoices"
    # Text that fails keeps the previous value, or the item is dropped.
    assert draft["identity"]["product"] == base["identity"]["product"]
    assert draft["voice"]["tone"] == base["voice"]["tone"]
    assert draft["voice"]["avoid"] == ["jargon"]
    # Numbers are carried over, never generated: weights by name, a new pillar 0.
    assert draft["pillars"] == [
        {"name": "Speed", "weight": 60, "hooks": ["Stop chasing"]},
        {"name": "Craft", "weight": 0, "hooks": []},
    ]
    assert draft["goals"] == base["goals"] == goals
    assert draft["channels"] == base["channels"]
    assert draft["roadmap"] == base["roadmap"]


async def test_regenerate_needs_an_analyser_and_a_blueprint(w1):
    pid = await _profile()
    resp = await w1.post(f"{BP}/regenerate", params={"profile_id": pid})
    assert resp.status_code == 503
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(analysis=SocialAnalysis())))
    resp = await w1.post(f"{BP}/regenerate", params={"profile_id": pid})
    assert resp.status_code == 404
    await _seed(w1, pid)
    set_production_analyze_fn(_FakeAnalyzer(AnalysisOutcome(error="nope")))
    resp = await w1.post(f"{BP}/regenerate", params={"profile_id": pid})
    assert resp.status_code == 502
    assert await _statuses(pid) == {1: "draft"}


# --- diff --------------------------------------------------------------------------------


async def test_diff_reports_added_removed_and_changed_per_section(w1):
    pid = await _profile()
    await _seed(w1, pid)
    await _approve(w1, pid)
    await _save(
        w1,
        pid,
        {
            "identity": {"summary": "Changed", "product": "", "problem": "Chasing late payments"},
            "pillars": [
                {"name": "Speed", "weight": 50, "hooks": ["h1", "h4"]},
                {"name": "Cash flow", "weight": 33, "hooks": ["h2"]},
                {"name": "Pricing", "weight": 17},
            ],
            "goals": [{"id": "g1", "metric": "stars", "target": 100}],
        },
    )
    resp = await w1.get(f"{BP}/diff", params={"profile_id": pid, "from": 1, "to": 2})
    assert resp.status_code == 200, resp.text
    sections = resp.json()["sections"]
    assert sections["identity"] == {"added": [], "removed": ["product"], "changed": ["summary"]}
    assert sections["pillars"] == {
        "added": ["Pricing"],
        "removed": ["Stories"],
        "changed": ["Speed"],
    }
    assert sections["goals"] == {"added": ["g1"], "removed": [], "changed": []}
    for name in ("audience", "voice", "channels", "roadmap"):
        assert sections[name] == {"added": [], "removed": [], "changed": []}

    resp = await w1.get(f"{BP}/diff", params={"profile_id": pid, "from": 1, "to": 7})
    assert resp.status_code == 404


async def test_diff_of_a_partly_sent_section_that_means_the_same_is_empty(w1):
    pid = await _profile()
    await _seed(w1, pid)
    await _approve(w1, pid)
    await _save(w1, pid, {"channels": [{"channel": "x", "posts_per_week": 7}]})
    resp = await w1.get(f"{BP}/diff", params={"profile_id": pid, "from": 1, "to": 2})
    assert resp.json()["sections"]["channels"] == {"added": [], "removed": [], "changed": []}


# --- RBAC and tenancy --------------------------------------------------------------------


async def test_writes_need_growth_manage(mongo_db):
    pid = await _profile()
    async with await _client("w1", role="member") as member:
        assert (await member.get(BP, params={"profile_id": pid})).status_code == 200
        for method, path in (
            ("post", "/seed"),
            ("put", "/draft"),
            ("post", "/regenerate"),
            ("post", "/approve"),
        ):
            kwargs: dict[str, Any] = {"params": {"profile_id": pid}}
            if method == "put":
                kwargs["json"] = {}
            resp = await getattr(member, method)(f"{BP}{path}", **kwargs)
            assert resp.status_code == 403, (path, resp.text)
    assert await BlueprintDoc.find({}).count() == 0


async def test_tenancy_on_workspace_and_profile(w1, w2):
    pid = await _profile()
    other = await _profile()
    await _seed(w1, pid)
    await _approve(w1, pid)

    for method, path in (("get", ""), ("post", "/seed"), ("post", "/approve")):
        resp = await getattr(w2, method)(f"{BP}{path}", params={"profile_id": pid})
        assert resp.status_code == 404, path
    resp = await w2.get(f"{BP}/diff", params={"profile_id": pid, "from": 1, "to": 1})
    assert resp.status_code == 404

    state = (await w1.get(BP, params={"profile_id": other})).json()
    assert state == {"approved": None, "draft": None}
    resp = await w1.get(f"{BP}/diff", params={"profile_id": other, "from": 1, "to": 1})
    assert resp.status_code == 404

    assert (await social_blueprint.approved_for("w1", pid)).version == 1
    assert await social_blueprint.approved_for("w2", pid) is None
    assert await social_blueprint.approved_for("w1", other) is None


# --- mapper round trip -------------------------------------------------------------------

_RENAMED = {"workspace": "workspace_id", "createdAt": "created_at", "updatedAt": "updated_at"}
_NOT_MAPPED = {"revision_id"}


async def test_every_doc_field_survives_domain_and_response(mongo_db):
    when = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    doc = BlueprintDoc(
        workspace="w1",
        profile_id="p1",
        version=4,
        status="approved",
        identity={"summary": "s", "product": "p", "problem": "q"},
        audience={"segments": ["a"], "icp_sentences": ["i"]},
        voice={"tone": "t", "avoid": ["v"]},
        pillars=[{"name": "n", "weight": 40, "hooks": ["h"]}],
        channels=[{"channel": "email", "posts_per_week": 1, "sends_per_week": 9}],
        goals=[{"id": "g", "metric": "m", "target": 3.5, "channel": "x"}],
        roadmap={"phases": [{"name": "later"}]},
        learning_ids=["l1"],
        created_by="u1",
        approved_by="u2",
        approved_at=when,
    )
    await doc.insert()
    doc = await BlueprintDoc.get(doc.id)

    domain_fields = {f.name for f in dataclasses.fields(GrowthBlueprint)}
    doc_fields = {_RENAMED.get(n, n) for n in BlueprintDoc.model_fields} - _NOT_MAPPED
    assert doc_fields == domain_fields, "a doc field has no domain field (or the reverse)"
    assert set(GrowthBlueprintResponse.model_fields) == domain_fields

    domain = social_blueprint._to_domain(doc)
    out = social_blueprint._to_response(domain).model_dump()
    raw = doc.model_dump()
    for name in BlueprintDoc.model_fields:
        if name in _NOT_MAPPED:
            continue
        key = _RENAMED.get(name, name)
        expected = raw[name]
        if key == "id":
            expected = str(expected)
        if isinstance(expected, datetime):
            got = out[key]
            assert got and got.startswith(expected.strftime("%Y-%m-%dT%H:%M")), key
            continue
        assert out[key] == expected, f"{name} was dropped or changed by a mapper"
