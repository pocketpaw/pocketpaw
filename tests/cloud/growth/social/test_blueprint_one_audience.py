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
from pocketpaw_ee.cloud.models.growth_blueprint import GrowthBlueprint as BlueprintDoc
from pocketpaw_ee.cloud.models.growth_blueprint_unit import GrowthBlueprintUnit as UnitDoc

from tests.cloud.growth.social.test_blueprint import _profile
from tests.cloud.growth.social.test_blueprint_units import UNITS, _ok, _put
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

    # A new client's explicit [] clears it: only a missing key is carried.
    cleared = {**draft, "positioning": {**draft["positioning"], "alternatives": []}}
    draft = (await _put(w1, pid, "strategy", cleared, 2))["draft"]["body"]
    assert draft["positioning"]["alternatives"] == []


# --- the migration script -------------------------------------------------------------------


def _db(rows: list[dict]):
    db = mongomock.MongoClient()["paw-cloud"]
    coll = db["growth_blueprint_units"]
    for i, (status, body) in enumerate(rows):
        coll.insert_one(
            {
                "workspace": "w1",
                "profile_id": f"p{i}",
                "unit": "strategy",
                "version": 1,
                "status": status,
                "rev": 3,
                "body": copy.deepcopy(body),
            }
        )
    return db


_ROWS = [
    ("approved", _OLD),
    ("draft", _OLD),
    ("superseded", _OLD),
    ("approved", fold_strategy(_OLD)),
]


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
    assert "3 strategy row(s) approved or draft, 3 processed, 2 to change" in out
    assert "before" in out and "after" in out


def test_the_real_run_rewrites_approved_and_draft_rows_and_bumps_rev(capsys):
    db = _db(_ROWS)
    script = _script()
    assert script.run(db, apply=True, dump=None) == 0
    rows = {r["status"]: r for r in db["growth_blueprint_units"].find({"profile_id": "p0"})}
    assert rows["approved"]["body"] == fold_strategy(_OLD) and rows["approved"]["rev"] == 4
    draft = db["growth_blueprint_units"].find_one({"profile_id": "p1"})
    assert draft["body"]["gtm"]["icp"]["target"] == _TARGET
    superseded = db["growth_blueprint_units"].find_one({"profile_id": "p2"})
    assert superseded["body"] == _OLD and superseded["rev"] == 3
    untouched = db["growth_blueprint_units"].find_one({"profile_id": "p3"})
    assert untouched["rev"] == 3
    assert "0 still old-shape -> OK" in capsys.readouterr().out
    # A re-run processes every row again and changes none.
    assert script.run(db, apply=True, dump=None) == 0
    assert "3 processed, 0 changed" in capsys.readouterr().out


def test_the_count_check_fails_when_a_row_could_not_be_written(capsys):
    db = _db(_ROWS[:1])
    coll = db["growth_blueprint_units"]

    class _Raced:
        """The row changed between the read and the write: nothing matches its rev."""

        def __getattr__(self, name):
            return getattr(coll, name)

        def update_one(self, match, update):
            return coll.update_one({**match, "rev": -1}, update)

    assert _script().run({"growth_blueprint_units": _Raced()}, apply=True, dump=None) == 1
    out = capsys.readouterr().out
    assert "NOT WRITTEN" in out and "1 still old-shape -> MISMATCH" in out


@pytest.mark.parametrize("argv", [[], ["--dry-run"], ["--apply"]])
def test_every_run_needs_a_dump_path(monkeypatch, argv):
    monkeypatch.setenv("CLOUD_MONGODB_URI", "mongodb://localhost:1/paw-cloud")
    with pytest.raises(SystemExit) as exc:
        _script().main(argv)
    assert exc.value.code == 2
