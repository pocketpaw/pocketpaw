# tests/cloud/growth/social/test_blueprint_one_audience.py — the strategy's one
# audience: ``gtm.icp`` keeps ``target`` / ``not_for`` / ``triggers``, the retired
# ``segments`` / ``icp_sentences`` fold into ``target``, and
# ``market_research.alternatives`` lives at ``positioning.alternatives``.
#
# Covers the pure fold (``fold_strategy``), the read-side fold of an old stored row
# (wire and the agents' ``blueprint_lines``), the mixed-shape save (an old editor
# saving over a migrated row loses nothing and never folds twice), and the
# ``scripts/migrate_growth_strategy_one_audience.py`` run on mongomock: the dry run
# writes nothing but the dump, the real run rewrites, and the count check fails on
# a row it could not write.

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import mongomock
import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.growth.social import blueprint as social_blueprint
from pocketpaw_ee.cloud.growth.social.analyst import blueprint_lines
from pocketpaw_ee.cloud.growth.social.blueprint_units_dto import StrategyBody, fold_strategy
from pocketpaw_ee.cloud.growth.social.domain import SocialAnalysis
from pocketpaw_ee.cloud.growth.social.dto import fold_icp
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as BlueprintDoc
from pocketpaw_ee.cloud.models.growth_blueprint_unit import GrowthBlueprintUnit as UnitDoc
from pydantic import ValidationError

from tests.cloud.growth.social.test_blueprint import BP, _profile, _seed
from tests.cloud.growth.social.test_blueprint_units import UNITS, _ok, _put, _rev
from tests.cloud.growth.social.test_router import _client

_SCRIPT = (
    Path(__file__).resolve().parents[4] / "scripts" / "migrate_growth_strategy_one_audience.py"
)

_OLD = {
    "market_research": {"main_challenge": "churn", "alternatives": ["Excel", "notion"]},
    "positioning": {"usp": "fast", "alternatives": ["Notion"]},
    "gtm": {
        "icp": {
            "target": "Indie devs",
            "segments": ["studios", "agencies"],
            "icp_sentences": ["I hate invoicing"],
            "not_for": ["banks"],
            "triggers": ["first hire"],
        }
    },
}
_TARGET = "Indie devs; Segments: studios, agencies; In their words: I hate invoicing"


def _script():
    spec = importlib.util.spec_from_file_location("migrate_one_audience", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest_asyncio.fixture
async def w1(mongo_db: Any):
    async with await _client("w1") as client:
        yield client


# --- the fold ------------------------------------------------------------------------------


def test_an_old_shape_body_folds_into_target_and_moves_alternatives():
    before = copy.deepcopy(_OLD)
    body = StrategyBody.model_validate(_OLD).model_dump(mode="json")
    assert _OLD == before, "the fold must not touch its input"
    assert body["gtm"]["icp"] == {
        "target": _TARGET,
        "not_for": ["banks"],
        "triggers": ["first hire"],
    }
    # Appended after what positioning already had; "notion" is already there (any case).
    assert body["positioning"]["alternatives"] == ["Notion", "Excel"]
    assert "alternatives" not in body["market_research"]
    assert body["market_research"]["main_challenge"] == "churn"
    # Idempotent: the new shape folds to itself.
    assert StrategyBody.model_validate(body).model_dump(mode="json") == body


def test_empty_old_keys_fold_nothing_and_never_empty_a_new_field():
    body = {
        "market_research": {"alternatives": []},
        "positioning": {"alternatives": ["Notion"]},
        "gtm": {"icp": {"target": "Indie devs", "segments": [], "icp_sentences": ["  "]}},
    }
    out = fold_strategy(body)
    assert out["gtm"]["icp"] == {"target": "Indie devs"}
    assert out["positioning"]["alternatives"] == ["Notion"]
    assert out["market_research"] == {}


def test_a_repeat_fold_does_not_append_the_same_part_twice():
    once = fold_strategy(_OLD)
    again = fold_strategy({**once, "gtm": {"icp": {**once["gtm"]["icp"], **_OLD["gtm"]["icp"]}}})
    assert again["gtm"]["icp"]["target"] == _TARGET


def test_regenerate_seeds_only_an_empty_target_so_a_repeat_never_grows_it():
    analysis = SocialAnalysis(audience="studio owners")
    empty = social_blueprint._regenerated({"audience": {}}, analysis, "")
    assert empty["audience"] == {"target": "Segments: studio owners", "not_for": []}
    owned = {"audience": {"target": "Indie devs", "not_for": ["banks"]}}
    once = social_blueprint._regenerated(owned, analysis, "")
    twice = social_blueprint._regenerated(once, analysis, "")
    assert once["audience"] == twice["audience"] == owned["audience"]


def test_a_resend_with_one_new_item_adds_just_that_item():
    once = fold_strategy(_OLD)["gtm"]["icp"]
    again = fold_icp({**once, "segments": ["Studios", "agencies", "solo founders"]})
    assert again["target"] == (
        "Indie devs; Segments: studios, agencies, solo founders; In their words: I hate invoicing"
    )
    # Items with commas inside still match themselves.
    said = {"target": "x", "icp_sentences": ["Teams of 2, 3 people"]}
    assert fold_icp(fold_icp(said)) == fold_icp(said)


def test_trailing_punctuation_does_not_meet_the_separator():
    assert fold_icp({"target": "Founders.", "segments": ["a"]})["target"] == "Founders; Segments: a"


def test_a_fold_past_a_cap_is_left_undone_so_nothing_is_lost():
    overflow: list = []
    out = fold_strategy(_LONG, overflow=overflow)
    assert out["gtm"]["icp"] == _LONG["gtm"]["icp"]
    assert overflow == [("gtm.icp.target", 2021, 2000)]
    with pytest.raises(ValidationError):  # the save is refused rather than cut
        StrategyBody.model_validate(_LONG)
    many = {
        "market_research": {"alternatives": [f"new {i}" for i in range(5)]},
        "positioning": {"alternatives": [f"old {i}" for i in range(20)]},
    }
    assert fold_strategy(many) == many
    cut = fold_strategy(many, truncate=True)["positioning"]["alternatives"]
    assert len(cut) == 20


@pytest.mark.parametrize("positioning", [None, "garbage", "missing"])
def test_alternatives_move_creates_a_missing_positioning(positioning):
    body = {"market_research": {"alternatives": ["Excel"]}}
    if positioning != "missing":
        body["positioning"] = positioning
    assert fold_strategy(body)["positioning"] == {"alternatives": ["Excel"]}


# --- reads fold a stored old-shape row ------------------------------------------------------


async def test_an_old_shape_legacy_row_folds_for_the_agents_and_through_the_migration(w1):
    pid = await _profile()
    await BlueprintDoc(
        workspace="w1",
        profile_id=pid,
        version=4,
        status="approved",
        identity={"summary": "Acme", "product": "", "problem": ""},
        audience={"segments": ["design studios"], "icp_sentences": ["We bill late"]},
        voice={"tone": "calm", "avoid": []},
        pillars=[],
    ).insert()
    folded = "Segments: design studios; In their words: We bill late"
    bp = await social_blueprint.approved_for("w1", pid)  # unmigrated: the legacy row
    assert bp.audience.target == folded
    await social_blueprint.migrate_profile("w1", pid)
    strategy = await UnitDoc.find_one({"profile_id": pid, "unit": "strategy"})
    assert strategy.body["gtm"]["icp"]["target"] == folded
    assert "segments" not in strategy.body["gtm"]["icp"]


async def _old_approved_strategy(client, pid: str) -> None:
    await _put(client, pid, "brand", {"who_we_are": {"summary": "Acme"}}, 0)
    await _ok(client, pid, "brand")
    await UnitDoc(
        workspace="w1",
        profile_id=pid,
        unit="strategy",
        version=1,
        status="approved",
        body=copy.deepcopy(_OLD),
    ).insert()


async def test_an_old_stored_row_reads_in_the_new_shape_on_the_wire_and_in_the_prompt(w1):
    pid = await _profile()
    await _old_approved_strategy(w1, pid)
    units = (await w1.get(UNITS, params={"profile_id": pid})).json()["units"]
    body = next(u for u in units if u["unit"] == "strategy")["approved"]["body"]
    assert body["gtm"]["icp"]["target"] == _TARGET
    assert "segments" not in body["gtm"]["icp"]
    assert body["positioning"]["alternatives"] == ["Notion", "Excel"]

    bp = await social_blueprint.approved_for("w1", pid)
    assert bp.audience.target == _TARGET and bp.audience.not_for == ("banks",)
    text = "\n".join(blueprint_lines(bp))
    assert f"Audience: {_TARGET}" in text
    assert "Not for: banks" in text
    assert "Ideal customers" not in text


# --- mixed shape: an old editor saving over a migrated row ----------------------------------


async def test_an_old_client_save_over_a_migrated_row_loses_nothing_and_never_folds_twice(w1):
    pid = await _profile()
    migrated = StrategyBody.model_validate(_OLD).model_dump(mode="json")
    await _put(w1, pid, "strategy", migrated, 0)
    await _ok(w1, pid, "strategy")
    # An old editor's body: its own empty old lists, no positioning.alternatives key.
    old_client = copy.deepcopy(migrated)
    old_client["positioning"].pop("alternatives")
    old_client["market_research"]["alternatives"] = []
    old_client["gtm"]["icp"]["segments"] = ["studios", "agencies"]
    old_client["gtm"]["icp"]["icp_sentences"] = []
    for base in (1, 2):  # the second save opens on the draft the first one wrote
        state = await _put(w1, pid, "strategy", old_client, base)
        draft = state["draft"]["body"]
        assert draft["positioning"]["alternatives"] == ["Notion", "Excel"]
        assert draft["gtm"]["icp"]["target"] == _TARGET

    # A typed old alternative appends after the carried ones.
    old_client["market_research"]["alternatives"] = ["Sheets"]
    draft = (await _put(w1, pid, "strategy", old_client, 2))["draft"]["body"]
    assert draft["positioning"]["alternatives"] == ["Notion", "Excel", "Sheets"]

    # Over the item cap the save is refused, never cut.
    full = {**old_client, "market_research": {"alternatives": ["One more"]}}
    stored = {
        **draft,
        "positioning": {**draft["positioning"], "alternatives": [f"a{i}" for i in range(20)]},
    }
    await _put(w1, pid, "strategy", stored, 2)
    resp = await w1.put(
        f"{UNITS}/strategy/draft",
        params={"profile_id": pid},
        json={"body": full, "base_version": 2, "base_rev": await _rev(pid, "strategy")},
    )
    assert resp.status_code == 422, resp.text
    draft = stored

    # A new client's explicit [] clears it: only a missing key is carried.
    cleared = {**draft, "positioning": {**draft["positioning"], "alternatives": []}}
    draft = (await _put(w1, pid, "strategy", cleared, 2))["draft"]["body"]
    assert draft["positioning"]["alternatives"] == []


# --- the legacy whole-page audience edit ---------------------------------------------------


async def _legacy_audience(client, pid: str, audience: dict, status: int = 200) -> dict:
    resp = await client.put(
        f"{BP}/draft",
        params={"profile_id": pid},
        json={"audience": audience},
    )
    assert resp.status_code == status, resp.text
    return resp.json()


async def test_a_legacy_audience_edit_replaces_the_target_and_folds_only_new_items(w1):
    pid = await _profile()
    await _seed(w1, pid)
    state = await _legacy_audience(w1, pid, {"target": "T", "not_for": ["banks"]})
    assert state["draft"]["audience"] == {"target": "T", "not_for": ["banks"]}
    state = await _legacy_audience(w1, pid, {"segments": ["a", "b"]})
    assert state["draft"]["audience"]["target"] == "T; Segments: a, b"
    state = await _legacy_audience(w1, pid, {"segments": ["a", "b", "c"]})
    assert state["draft"]["audience"] == {"target": "T; Segments: a, b, c", "not_for": ["banks"]}
    # The owner's target edit replaces, folds included.
    state = await _legacy_audience(w1, pid, {"target": "Solo founders"})
    assert state["draft"]["audience"]["target"] == "Solo founders"
    # Over the cap once folded: refused, nothing written.
    long = {"target": "t" * 1990, "segments": ["more than ten chars"]}
    resp = await _legacy_audience(w1, pid, long, status=422)
    assert resp["error"]["code"] == "social.blueprint_invalid_body"
    strategy = await UnitDoc.find_one({"profile_id": pid, "unit": "strategy", "status": "draft"})
    assert strategy.body["gtm"]["icp"]["target"] == "Solo founders"


# --- the migration script -------------------------------------------------------------------


def _db(rows: list[tuple], *, rev: bool = True):
    db = mongomock.MongoClient()["paw-cloud"]
    coll = db["growth_blueprint_units"]
    for i, (status, body) in enumerate(rows):
        doc = {
            "workspace": "w1",
            "profile_id": f"p{i}",
            "unit": "strategy",
            "version": 1,
            "status": status,
            "body": copy.deepcopy(body),
        }
        if rev:
            doc["rev"] = 3
        coll.insert_one(doc)
    return db


_ROWS = [
    ("approved", _OLD),
    ("draft", _OLD),
    ("superseded", _OLD),
    ("approved", fold_strategy(_OLD)),
]
_LONG = {"gtm": {"icp": {"target": "t" * 1990, "segments": ["more than ten chars"]}}}


def test_the_dry_run_writes_nothing_but_the_dump(tmp_path, capsys):
    db = _db(_ROWS)
    before = list(db["growth_blueprint_units"].find())
    dump = tmp_path / "strategy.json"
    assert _script().run(db, apply=False, dump=str(dump)) == 0
    assert list(db["growth_blueprint_units"].find()) == before
    dumped = json.loads(dump.read_text())
    assert [d["status"] for d in dumped] == ["approved", "draft", "approved"]
    assert dumped[0]["body"] == _OLD and {"_id", "profile_id", "version", "rev"} <= set(dumped[0])
    out = capsys.readouterr().out
    assert "3 matched, 2 would write, 1 already new shape, 0 raced" in out
    assert "before" in out and "after" in out


def test_an_existing_dump_is_never_overwritten(tmp_path, monkeypatch):
    dump = tmp_path / "strategy.json"
    dump.write_text("the restore copy")
    with pytest.raises(FileExistsError):
        _script().run(_db(_ROWS), apply=True, dump=str(dump))
    assert dump.read_text() == "the restore copy"
    monkeypatch.setenv("CLOUD_MONGODB_URI", "mongodb://localhost:1/paw-cloud")
    with pytest.raises(SystemExit) as exc:
        _script().main(["--apply", "--dump", str(dump)])
    assert exc.value.code == 2 and dump.read_text() == "the restore copy"


def test_the_real_run_rewrites_approved_and_draft_rows_and_bumps_rev(tmp_path, capsys):
    db = _db(_ROWS)
    script = _script()
    assert script.run(db, apply=True, dump=str(tmp_path / "a.json")) == 0
    rows = {r["status"]: r for r in db["growth_blueprint_units"].find({"profile_id": "p0"})}
    assert rows["approved"]["body"] == fold_strategy(_OLD) and rows["approved"]["rev"] == 4
    draft = db["growth_blueprint_units"].find_one({"profile_id": "p1"})
    assert draft["body"]["gtm"]["icp"]["target"] == _TARGET
    superseded = db["growth_blueprint_units"].find_one({"profile_id": "p2"})
    assert superseded["body"] == _OLD and superseded["rev"] == 3
    untouched = db["growth_blueprint_units"].find_one({"profile_id": "p3"})
    assert untouched["rev"] == 3
    out = capsys.readouterr().out
    assert "3 matched, 2 written, 1 already new shape, 0 raced" in out and "-> OK" in out
    # A re-run writes nothing and still passes.
    assert script.run(db, apply=True, dump=str(tmp_path / "b.json")) == 0
    assert "0 written, 3 already new shape" in capsys.readouterr().out


def test_a_row_with_no_rev_is_written(tmp_path):
    db = _db(_ROWS[:1], rev=False)
    assert _script().run(db, apply=True, dump=str(tmp_path / "a.json")) == 0
    row = db["growth_blueprint_units"].find_one()
    assert row["body"]["gtm"]["icp"]["target"] == _TARGET and row["rev"] == 1


def test_a_truncating_fold_is_reported_and_refused_unless_allowed(tmp_path, capsys):
    db = _db([("approved", _LONG)])
    script = _script()
    assert script.run(db, apply=False, dump=str(tmp_path / "a.json")) == 1
    out = capsys.readouterr().out
    assert "TRUNCATED gtm.icp.target: 2021 -> 2000" in out and "WARN: --apply would refuse" in out
    assert "0 would write, 0 already new shape" in out and "(1 would refuse)" in out
    assert out.rstrip().endswith("-> WARN")
    assert script.run(db, apply=True, dump=str(tmp_path / "b.json")) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "1 truncating (1 refused), 1 still old-shape -> MISMATCH" in out
    assert db["growth_blueprint_units"].find_one()["body"] == _LONG
    dump = tmp_path / "c.json"
    assert script.run(db, apply=True, dump=str(dump), allow_truncate=True) == 0
    assert json.loads(dump.read_text())[0]["body"] == _LONG  # the full text is kept
    icp = db["growth_blueprint_units"].find_one()["body"]["gtm"]["icp"]
    assert len(icp["target"]) == 2000 and "segments" not in icp


def test_the_workspace_and_profile_filters_canary_one_brand(tmp_path):
    db = _db(_ROWS)
    assert _script().run(db, apply=True, dump=str(tmp_path / "a.json"), profile="p1") == 0
    bodies = {r["profile_id"]: r["body"] for r in db["growth_blueprint_units"].find()}
    assert bodies["p0"] == _OLD and bodies["p1"] == fold_strategy(_OLD)
    assert _script().run(db, apply=True, dump=str(tmp_path / "b.json"), workspace="w9") == 0
    assert bodies["p0"] == db["growth_blueprint_units"].find_one({"profile_id": "p0"})["body"]


def test_the_count_check_fails_when_a_row_could_not_be_written(tmp_path, capsys):
    db = _db(_ROWS[:1])
    coll = db["growth_blueprint_units"]

    class _Raced:
        """The row changed between the read and the write: nothing matches its rev."""

        def __getattr__(self, name):
            return getattr(coll, name)

        def update_one(self, match, update):
            return coll.update_one({**match, "rev": -1}, update)

    dump = str(tmp_path / "a.json")
    assert _script().run({"growth_blueprint_units": _Raced()}, apply=True, dump=dump) == 1
    out = capsys.readouterr().out
    assert "NOT WRITTEN" in out and "1 raced" in out and "1 still old-shape -> MISMATCH" in out


def test_a_failed_read_leaves_no_dump_behind(tmp_path):
    class _Down:
        def find(self, query):
            raise RuntimeError("mongo is down")

    dump = tmp_path / "a.json"
    with pytest.raises(RuntimeError):
        _script().run({"growth_blueprint_units": _Down()}, apply=False, dump=str(dump))
    assert not dump.exists()


@pytest.mark.parametrize("argv", [[], ["--dry-run"], ["--apply"]])
def test_every_run_needs_a_dump_path(monkeypatch, argv):
    monkeypatch.setenv("CLOUD_MONGODB_URI", "mongodb://localhost:1/paw-cloud")
    with pytest.raises(SystemExit) as exc:
        _script().main(argv)
    assert exc.value.code == 2
