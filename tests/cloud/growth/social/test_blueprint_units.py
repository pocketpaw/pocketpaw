# tests/cloud/growth/social/test_blueprint_units.py — the per-unit Blueprint
# (``/api/v1/growth/social/blueprint/units...``), its composite read and the
# legacy-row migration. Same harness as test_blueprint.py: the social router over
# mongomock-backed Beanie, no network, no model.
#
# Covers: draft, approve and supersede run per unit; a stale ``base_version`` or
# ``base_rev`` (two tabs on one draft) is a 409; unit ids outside the play catalog
# and bad bodies are 422 (creator extras need disclosure and usage rights);
# cross-unit goal checks on save and approve; ``approved_for`` is None until brand
# AND strategy are approved, derives channels from active acting plays only, and
# reads a migrated legacy row exactly as the legacy row read; the whole-page routes
# round-trip through the units; the field-level unit diff; ``growth_branches`` on
# the profile; non-zero cadences no play carries (422 on the whole-page save,
# ``dropped`` in a migration report); the migration script (dry run writes nothing,
# real run, idempotent, count check over every mapped unit), a migration that dies
# part way (legacy still read, the next run resumes) and the legacy fallback read
# plus migrate-on-write.

from __future__ import annotations

import dataclasses
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.growth.social import blueprint as social_blueprint
from pocketpaw_ee.cloud.growth.social import play_catalog
from pocketpaw_ee.cloud.growth.social.domain import BlueprintChannel
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as BlueprintDoc
from pocketpaw_ee.cloud.models.growth_blueprint_unit import GrowthBlueprintUnit as UnitDoc

from tests.cloud.growth.social.test_blueprint import BP, _approve, _profile, _save, _seed
from tests.cloud.growth.social.test_router import PROFILE, _client

UNITS = f"{BP}/units"
_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "migrate_growth_blueprint_units.py"


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


async def _rev(pid: str, unit: str) -> int:
    """The rev of the unit's current row (draft, else approved), as an editor reads it."""
    docs = await UnitDoc.find(
        {"workspace": "w1", "profile_id": pid, "unit": unit, "status": {"$ne": "superseded"}}
    ).to_list()
    current = next((d for d in docs if d.status == "draft"), docs[0] if docs else None)
    return current.rev if current else 0


async def _put(
    client, pid: str, unit: str, body: dict, base: int, status: int = 200, rev: int | None = None
) -> dict:
    rev = await _rev(pid, unit) if rev is None else rev
    resp = await client.put(
        f"{UNITS}/{unit}/draft",
        params={"profile_id": pid},
        json={"body": body, "base_version": base, "base_rev": rev},
    )
    assert resp.status_code == status, resp.text
    return resp.json()


async def _ok(client, pid: str, unit: str, status: int = 200) -> dict:
    resp = await client.post(f"{UNITS}/{unit}/approve", params={"profile_id": pid})
    assert resp.status_code == status, resp.text
    return resp.json()


async def _chain(pid: str, unit: str) -> dict[int, str]:
    docs = await UnitDoc.find({"workspace": "w1", "profile_id": pid, "unit": unit}).to_list()
    return {d.version: d.status for d in docs}


_STRATEGY = {"goals": {"objectives": [{"id": "g1", "metric": "signups", "target": 40}]}}


async def _approved_core(client, pid: str) -> None:
    await _put(client, pid, "brand", {"who_we_are": {"summary": "Acme"}}, 0)
    await _put(client, pid, "strategy", _STRATEGY, 0)
    await _ok(client, pid, "brand")
    await _ok(client, pid, "strategy")


# --- per-unit chains ---------------------------------------------------------------------


async def test_each_unit_drafts_approves_and_supersedes_on_its_own(w1):
    pid = await _profile()
    state = await _put(w1, pid, "brand", {"brand_story": "Born in a garage"}, 0)
    assert state["draft"]["version"] == 1 and state["approved"] is None
    assert state["draft"]["body"]["brand_story"] == "Born in a garage"
    assert state["draft"]["body"]["tone_of_voice"] == {"tone": "", "do": [], "avoid": []}

    state = await _ok(w1, pid, "brand")
    assert state["approved"]["version"] == 1 and state["draft"] is None
    await _put(w1, pid, "brand", {"brand_story": "Second"}, 1)
    await _ok(w1, pid, "brand")
    assert await _chain(pid, "brand") == {1: "superseded", 2: "approved"}
    assert await _chain(pid, "strategy") == {}

    await _ok(w1, pid, "strategy", status=409)  # no draft to approve
    resp = await w1.get(UNITS, params={"profile_id": pid})
    body = resp.json()
    assert [u["unit"] for u in body["units"]] == ["brand"]
    acting = {p["id"] for p in body["catalog"]["plays"] if p["acts"]}
    assert acting == {
        "x-organic",
        "x-authority",
        "reddit-community",
        "linkedin-content",
        "cold-email",
        "linkedin-outreach",
    }


async def test_a_stale_base_version_is_409(w1):
    pid = await _profile()
    await _put(w1, pid, "brand", {"brand_story": "a"}, 0)
    resp = await w1.put(
        f"{UNITS}/brand/draft",
        params={"profile_id": pid},
        json={"body": {"brand_story": "b"}, "base_version": 0, "base_rev": 0},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "social.blueprint_stale"
    await _put(w1, pid, "brand", {"brand_story": "b"}, 1)  # editing the draft keeps v1
    await _ok(w1, pid, "brand")
    await _put(w1, pid, "brand", {"brand_story": "c"}, 0, status=409)
    assert (await _put(w1, pid, "brand", {"brand_story": "c"}, 1))["draft"]["version"] == 2


async def test_two_tabs_on_one_draft_the_second_save_is_409(w1):
    pid = await _profile()
    row = (await _put(w1, pid, "brand", {"brand_story": "a"}, 0))["draft"]
    assert row["rev"] == 0
    tab_a = await _put(w1, pid, "brand", {"brand_story": "tab a"}, 1, rev=row["rev"])
    assert tab_a["draft"]["rev"] == 1 and tab_a["draft"]["version"] == 1
    resp = await w1.put(
        f"{UNITS}/brand/draft",
        params={"profile_id": pid},
        json={"body": {"brand_story": "tab b"}, "base_version": 1, "base_rev": row["rev"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "social.blueprint_stale"
    stored = await UnitDoc.find_one({"profile_id": pid, "unit": "brand"})
    assert stored.body["brand_story"] == "tab a"
    # A missing base_rev is a 422, not a silent overwrite.
    resp = await w1.put(
        f"{UNITS}/brand/draft",
        params={"profile_id": pid},
        json={"body": {"brand_story": "c"}, "base_version": 1},
    )
    assert resp.status_code == 422


async def test_unknown_units_and_bad_bodies_are_422(w1):
    pid = await _profile()
    for unit in ("play:not-a-play", "goals", "play:"):
        resp = await w1.put(
            f"{UNITS}/{unit}/draft",
            params={"profile_id": pid},
            json={"body": {}, "base_version": 0, "base_rev": 0},
        )
        assert resp.status_code == 422, unit
    for unit, body in (
        ("brand", {"made_up": 1}),
        ("play:x-organic", {"posts_per_week": 51}),
        ("play:x-organic", {"status": "live"}),
        ("play:creator-collabs", {"creator": {"brief": "post about us"}}),
        ("play:creator-collabs", {"creator": {"disclosure": "#ad", "usage_rights": " "}}),
        ("play:google-ads", {"paid": {"landing_url": "ftp://x"}}),
    ):
        resp = await w1.put(
            f"{UNITS}/{unit}/draft",
            params={"profile_id": pid},
            json={"body": body, "base_version": 0, "base_rev": 0},
        )
        assert resp.status_code == 422, (unit, body)
    creator = {"creator": {"disclosure": "#ad", "usage_rights": "90 days, organic only"}}
    await _put(w1, pid, "play:creator-collabs", creator, 0)


# --- cross-unit goal checks --------------------------------------------------------------


async def test_plans_and_plays_must_name_goals_of_the_strategy(w1):
    pid = await _profile()
    phase = {"name": "Now", "start": "2030-01-01", "end": "2030-01-07", "goal_ids": ["g1"]}
    plan = {"roadmap": {"phases": [phase]}}
    resp = await w1.put(
        f"{UNITS}/plan/draft",
        params={"profile_id": pid},
        json={"body": plan, "base_version": 0, "base_rev": 0},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["goal_ids"] == ["g1"]
    await _put(w1, pid, "play:x-organic", {"goal_id": "g1"}, 0, status=422)

    await _put(w1, pid, "strategy", _STRATEGY, 0)  # a draft strategy is enough to save
    await _put(w1, pid, "plan", plan, 0)
    await _put(w1, pid, "play:x-organic", {"goal_id": "g1"}, 0)
    # ...but approving needs the goal in the APPROVED strategy.
    await _ok(w1, pid, "plan", status=422)
    await _ok(w1, pid, "strategy")
    await _ok(w1, pid, "plan")

    # A strategy that drops g1 cannot be approved while the plan and play name it.
    await _put(w1, pid, "strategy", {"goals": {"objectives": []}}, 1)
    resp = await w1.post(f"{UNITS}/strategy/approve", params={"profile_id": pid})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.blueprint_goal_in_use"
    assert resp.json()["error"]["details"]["units"] == {"plan": ["g1"], "play:x-organic": ["g1"]}


async def test_unit_ids_are_minted_and_the_unit_diff_is_field_level(w1):
    pid = await _profile()
    body = {"goals": {"objectives": [{"metric": "signups", "target": 5}]}}
    first = (await _put(w1, pid, "strategy", body, 0))["draft"]
    (objective,) = first["body"]["goals"]["objectives"]
    assert objective["id"] and objective["baseline"] is None
    await _ok(w1, pid, "strategy")
    edit = {
        **first["body"],
        "gtm": {"icp": {"segments": ["studios"]}},
        "positioning": {"usp": ""},
        "goals": {"objectives": [{**objective, "target": 9}]},
    }
    await _put(w1, pid, "strategy", edit, 1)
    resp = await w1.get(
        f"{UNITS}/strategy/diff",
        params={"profile_id": pid, "from_version": 1, "to_version": 2},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["fields"] == {
        "added": ["gtm.icp.segments"],
        "removed": [],
        "changed": ["goals.objectives"],
    }
    resp = await w1.get(
        f"{UNITS}/strategy/diff",
        params={"profile_id": pid, "from_version": 1, "to_version": 9},
    )
    assert resp.status_code == 404


# --- composite read ----------------------------------------------------------------------


async def test_approved_for_needs_brand_and_strategy_and_sums_active_acting_plays(w1):
    pid = await _profile()
    await _put(w1, pid, "brand", {"who_we_are": {"summary": "Acme"}}, 0)
    await _ok(w1, pid, "brand")
    assert await social_blueprint.approved_for("w1", pid) is None  # strategy not approved
    await _put(w1, pid, "strategy", _STRATEGY, 0)
    assert await social_blueprint.approved_for("w1", pid) is None  # only a draft
    await _ok(w1, pid, "strategy")

    for unit, body in (
        ("play:x-organic", {"status": "core", "posts_per_week": 3}),
        ("play:x-authority", {"status": "testing", "posts_per_week": 2}),
        ("play:reddit-community", {"status": "parked", "posts_per_week": 9}),
        ("play:cold-email", {"status": "core", "sends_per_week": 40}),
        ("play:seo-articles", {"status": "core", "posts_per_week": 4}),  # plan-only
    ):
        await _put(w1, pid, unit, body, 0)
        await _ok(w1, pid, unit)
    await _put(w1, pid, "play:linkedin-content", {"status": "core", "posts_per_week": 7}, 0)

    bp = await social_blueprint.approved_for("w1", pid)
    assert bp is not None and bp.version == 1 and bp.identity.summary == "Acme"
    assert [(c.channel, c.posts_per_week, c.sends_per_week) for c in bp.channels] == [
        ("x", 5, 0),
        ("email", 0, 40),
    ]
    assert [g.id for g in bp.goals] == ["g1"]
    assert bp.unit_versions["strategy"] == 1 and "play:linkedin-content" not in bp.unit_versions
    assert set(bp.plays) == {
        "x-organic",
        "x-authority",
        "reddit-community",
        "cold-email",
        "seo-articles",
    }


_LEGACY = {
    "identity": {"summary": "s", "product": "p", "problem": "q"},
    "audience": {"segments": ["a"], "icp_sentences": ["i"]},
    "voice": {"tone": "t", "avoid": ["v"]},
    "pillars": [{"name": "n", "weight": 40, "hooks": ["h"]}],
    # The three non-zero cadences no play carries (x sends, email posts, whatsapp)
    # are dropped by the migration and a 422 on the whole-page save.
    "channels": [
        {"channel": "x", "posts_per_week": 0, "sends_per_week": 4},
        {"channel": "reddit", "posts_per_week": 2, "sends_per_week": 0},
        {"channel": "email", "posts_per_week": 1, "sends_per_week": 9},
        {"channel": "whatsapp", "posts_per_week": 0, "sends_per_week": 6},
        {"channel": "linkedin", "posts_per_week": 3, "sends_per_week": 12},
    ],
    "goals": [{"id": "g", "metric": "m", "target": 3.5, "channel": "x"}],
    "roadmap": {
        "phases": [
            {
                "id": "ph1",
                "name": "later",
                "start": "2030-01-01",
                "end": "2030-01-31",
                "cadence": [{"channel": "reddit", "posts_per_week": 2}],
                "goal_ids": ["g"],
                "milestones": [
                    {
                        "id": "m1",
                        "title": "launch",
                        "date": "2030-01-15",
                        "status": "doing",
                        "link": "https://acme.test/launch",
                    }
                ],
            }
        ]
    },
    "learning_ids": ["l1"],
}

_MAPPED_CHANNELS = [
    {"channel": "x", "posts_per_week": 0, "sends_per_week": 0},
    {"channel": "reddit", "posts_per_week": 2, "sends_per_week": 0},
    {"channel": "email", "posts_per_week": 0, "sends_per_week": 9},
    {"channel": "linkedin", "posts_per_week": 3, "sends_per_week": 12},
]
_DROPPED = ["x.sends_per_week=4", "email.posts_per_week=1", "whatsapp.sends_per_week=6"]

_UNIT_ONLY = {"brand", "strategy", "plan", "plays", "unit_versions"}
_META = {"id", "created_at", "updated_at"}


def _fields(bp: Any) -> dict[str, Any]:
    return {
        f.name: getattr(bp, f.name)
        for f in dataclasses.fields(bp)
        if f.name not in _UNIT_ONLY | _META
    }


async def _legacy(pid: str, version: int, status: str, **sections: Any) -> BlueprintDoc:
    doc = BlueprintDoc(
        workspace="w1",
        profile_id=pid,
        version=version,
        status=status,
        created_by="u1",
        approved_by="u2" if status == "approved" else None,
        approved_at=datetime(2030, 1, 2, tzinfo=UTC) if status == "approved" else None,
        **{**_LEGACY, **sections},
    )
    await doc.insert()
    return doc


async def test_a_migrated_legacy_blueprint_reads_exactly_as_before(w1):
    pid = await _profile()
    legacy = await _legacy(pid, 3, "approved")
    # Before migration the agents read the legacy row (read-only fallback).
    before = await social_blueprint.approved_for("w1", pid)
    stored = await BlueprintDoc.get(legacy.id)
    assert _fields(before) == _fields(social_blueprint._to_domain(stored))
    assert await UnitDoc.find({}).count() == 0

    report = await social_blueprint.migrate_profile("w1", pid)
    assert report["status"] == "migrated"
    assert report["dropped"] == [f"approved:{f}" for f in _DROPPED]
    after = await social_blueprint.approved_for("w1", pid)
    expected = {
        **_fields(before),
        "channels": tuple(
            BlueprintChannel(c["channel"], c["posts_per_week"], c["sends_per_week"])
            for c in _MAPPED_CHANNELS
        ),
    }
    assert _fields(after) == expected  # only the unmapped cadences changed
    assert after.version == 3 and after.unit_versions["strategy"] == 3
    assert after.plays["linkedin-outreach"]["sends_per_week"] == 12
    assert after.plays["x-organic"]["status"] == "core"


async def test_whole_page_routes_round_trip_through_the_units(w1):
    pid = await _profile()
    await _seed(w1, pid)
    sections = {k: v for k, v in _LEGACY.items() if k not in ("learning_ids",)}
    resp = await w1.put(f"{BP}/draft", params={"profile_id": pid}, json=sections)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "social.blueprint_unmapped_channel"
    assert resp.json()["error"]["details"]["fields"] == _DROPPED
    # Zero cadences with no play drop silently.
    zeroes = {"channel": "whatsapp", "posts_per_week": 0, "sends_per_week": 0}
    state = await _save(w1, pid, {**sections, "channels": [*_MAPPED_CHANNELS, zeroes]})
    sections["channels"] = _MAPPED_CHANNELS
    draft = state["draft"]
    for name, value in sections.items():
        assert draft[name] == value, name
    units = (await w1.get(UNITS, params={"profile_id": pid})).json()["units"]
    by_unit = {u["unit"]: u for u in units}
    assert by_unit["brand"]["draft"]["body"]["who_we_are"] == _LEGACY["identity"]
    assert by_unit["play:linkedin-outreach"]["draft"]["body"]["sends_per_week"] == 12

    state = await _approve(w1, pid)
    assert state["draft"] is None
    approved = {k: state["approved"][k] for k in sections}
    assert approved == sections
    # Dropping a channel parks its play; the composed Blueprint no longer lists it.
    state = await _save(w1, pid, {"channels": _MAPPED_CHANNELS[:1]})
    assert [c["channel"] for c in state["draft"]["channels"]] == ["x"]
    assert by_unit["play:reddit-community"]["draft"]["body"]["status"] == "core"
    reddit = await UnitDoc.find_one(
        {"profile_id": pid, "unit": "play:reddit-community", "status": "draft"}
    )
    assert reddit.body["status"] == "parked"


# --- brand branches ----------------------------------------------------------------------


async def test_growth_branches_are_catalog_ids_on_the_profile(w1):
    pid = await _profile()
    got = (await w1.get(PROFILE, params={"profile_id": pid})).json()
    assert got["growth_branches"] == []
    resp = await w1.put(
        PROFILE, params={"profile_id": pid}, json={"growth_branches": ["paid", "pr", "paid"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["growth_branches"] == ["paid", "pr"]
    resp = await w1.put(PROFILE, params={"profile_id": pid}, json={"growth_branches": ["bogus"]})
    assert resp.status_code == 422
    resp = await w1.put(PROFILE, params={"profile_id": pid}, json={"growth_branches": None})
    assert resp.json()["growth_branches"] == []
    assert set(play_catalog.DEFAULT_BRANCHES) <= play_catalog.BRANCH_IDS


def test_every_play_has_a_branch_a_platform_and_a_peso_tag():
    for p in play_catalog.PLAYS:
        assert p.branch in play_catalog.BRANCH_IDS and p.platform and p.peso
        assert (p.channel is not None) == p.acts
    assert len(play_catalog.PLAY_IDS) == len(play_catalog.PLAYS)


# --- migration ---------------------------------------------------------------------------


def _script():
    spec = importlib.util.spec_from_file_location("migrate_growth_blueprint_units", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def test_migration_dry_run_real_run_idempotent_and_count_check(w1):
    a, b, c = await _profile(), await _profile(), await _profile()
    await _legacy(a, 1, "superseded")
    await _legacy(a, 2, "approved")
    await _legacy(a, 3, "draft", voice={"tone": "new", "avoid": []})
    await _legacy(b, 1, "draft")  # never approved
    await _approved_core(w1, c)  # already on units: skipped
    script = _script()

    dry = await script.migrate(dry_run=True)
    assert await UnitDoc.find({"profile_id": {"$in": [a, b]}}).count() == 0
    assert dry["brands"][f"w1/{a}"]["status"] == "migrated"
    assert dry["legacy_approved"] == 1 and dry["unit_approved"] == 0  # a not yet migrated

    real = await script.migrate(dry_run=False)
    assert real["ok"] and real["legacy_approved"] == real["unit_approved"] == 1
    assert await _chain(a, "brand") == {2: "approved", 3: "draft"}  # voice changed
    assert await _chain(a, "strategy") == {2: "approved"}
    assert await _chain(b, "strategy") == {1: "draft"}
    assert real["brands"].get(f"w1/{c}") is None  # c has no legacy rows

    count = await UnitDoc.find({}).count()
    again = await script.migrate(dry_run=False)
    assert {r["status"] for r in again["brands"].values()} == {"skipped"}
    assert await UnitDoc.find({}).count() == count
    assert await BlueprintDoc.find({}).count() == 4  # the legacy rows are kept


async def test_a_write_migrates_the_brand_first(w1):
    pid = await _profile()
    await _legacy(pid, 4, "approved")
    state = await _save(w1, pid, {"identity": {"summary": "edited"}})
    assert state["approved"]["version"] == 4 and state["draft"]["identity"]["summary"] == "edited"
    assert await _chain(pid, "brand") == {4: "approved", 5: "draft"}
    assert (await social_blueprint.approved_for("w1", pid)).identity.summary == "s"


async def test_a_migration_that_dies_part_way_reads_legacy_and_resumes(w1, monkeypatch):
    pid = await _profile()
    await _legacy(pid, 2, "approved")
    before = await social_blueprint.approved_for("w1", pid)
    real = UnitDoc.insert

    async def flaky(self, *args, **kwargs):
        if self.unit == "plan":
            raise RuntimeError("connection blip")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(UnitDoc, "insert", flaky)
    with pytest.raises(RuntimeError):
        await social_blueprint.migrate_profile("w1", pid)
    # Strategy is written last, so the brand still reads its legacy Blueprint.
    assert await _chain(pid, "brand") == {2: "approved"}
    assert await _chain(pid, "strategy") == {}
    assert _fields(await social_blueprint.approved_for("w1", pid)) == _fields(before)
    assert (await w1.get(BP, params={"profile_id": pid})).json()["approved"]["version"] == 2

    monkeypatch.setattr(UnitDoc, "insert", real)
    report = await social_blueprint.migrate_profile("w1", pid)  # resumes
    assert report["status"] == "migrated"
    assert await _chain(pid, "plan") == {2: "approved"}
    assert await _chain(pid, "strategy") == {2: "approved"}
    assert await social_blueprint.migration_counts() == (1, 1)
    assert (await social_blueprint.approved_for("w1", pid)).identity == before.identity


async def test_the_count_check_needs_every_mapped_unit(w1):
    pid = await _profile()
    await _legacy(pid, 1, "approved")
    await social_blueprint.migrate_profile("w1", pid)
    assert await social_blueprint.migration_counts() == (1, 1)
    await UnitDoc.find_one({"profile_id": pid, "unit": "play:reddit-community"}).delete()
    assert await social_blueprint.migration_counts() == (1, 0)


async def test_a_stale_whole_page_approve_writes_nothing(w1, monkeypatch):
    pid = await _profile()
    await _seed(w1, pid)
    real = social_blueprint._rows
    calls = {"n": 0}

    async def other_tab_saves_first(workspace_id: str, profile_id: str):
        rows = await real(workspace_id, profile_id)
        calls["n"] += 1
        if calls["n"] == 1:  # another tab edits the plan after approve read its rows
            await UnitDoc.get_pymongo_collection().update_one(
                {"profile_id": pid, "unit": "plan", "status": "draft"}, {"$inc": {"rev": 1}}
            )
        return rows

    monkeypatch.setattr(social_blueprint, "_rows", other_tab_saves_first)
    resp = await w1.post(f"{BP}/approve", params={"profile_id": pid})
    assert resp.status_code == 409, resp.text
    assert await UnitDoc.find({"profile_id": pid, "status": "approved"}).count() == 0


async def test_a_save_landing_between_read_and_write_is_409(w1, monkeypatch):
    pid = await _profile()
    await _put(w1, pid, "brand", {"brand_story": "a"}, 0)
    real = social_blueprint._rows

    async def other_tab_writes_after_our_read(workspace_id: str, profile_id: str):
        rows = await real(workspace_id, profile_id)
        monkeypatch.setattr(social_blueprint, "_rows", real)
        await UnitDoc.get_pymongo_collection().update_one(
            {"profile_id": pid, "unit": "brand", "status": "draft"},
            {"$set": {"body.brand_story": "tab b"}, "$inc": {"rev": 1}},
        )
        return rows

    monkeypatch.setattr(social_blueprint, "_rows", other_tab_writes_after_our_read)
    await _put(w1, pid, "brand", {"brand_story": "tab a"}, 1, rev=0, status=409)
    stored = await UnitDoc.find_one({"profile_id": pid, "unit": "brand"})
    assert stored.body["brand_story"] == "tab b"
