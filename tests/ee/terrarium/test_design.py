# tests/ee/terrarium/test_design.py — the design verb: a citizen holding the
# workshop may draw a building. Pins that the verb is GRANTED (no workshop, no
# design, no charge), that a design is validated against the frontend's schema
# and an invalid one is dropped unpaid, that the body a citizen pays for is
# canonical JSON, that the name rides the same outbound moderation a book does,
# that ``costs.design`` defaults to the craft price, that a build may carry a
# design the citizen OWNS and nothing else, and that the prompt shows the verb
# only once the workshop is held.

from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import world  # noqa: E402
from pocketpaw_ee.terrarium.design import Design, validate_design  # noqa: E402
from pocketpaw_ee.terrarium.physics import (  # noqa: E402
    load_physics,
    parse_physics,
    seed_physics_path,
)

from .conftest import create_universe  # noqa: E402

HUT = {
    "version": 1,
    "name": "Hut",
    "footprint": {"w": 2, "d": 2},
    "parts": [
        {
            "kind": "box",
            "at": [0, 0, 0],
            "size": [2, 2, 2],
            "color": "stone",
            "features": [{"kind": "door"}, {"kind": "window", "face": "right", "u": 0.3}],
        },
        {"kind": "prism", "at": [0, 2, 0], "size": [2, 1, 2], "color": "brick", "rotate": 90},
    ],
}


def physics(**over):
    raw = load_physics(seed_physics_path("dust")).model_dump()
    raw.update(over)
    return parse_physics(raw)


def citizen(**over):
    base = {"id": "c1", "name": "Vela", "balance": 100, "charter": "keep the ledger honest"}
    base.update(over)
    return world.CitizenSnapshot(**base)


def decide(*acts):
    return world.Decision(thought="thinking", acts=[world.Act(**a) for a in acts])


def _hut(**over):
    d = copy.deepcopy(HUT)
    d.update(over)
    return d


# ---------------------------------------------------------------------------
# The pure engine
# ---------------------------------------------------------------------------


def test_a_design_without_the_workshop_is_dropped_and_not_charged():
    out = world.apply_acts(physics(), citizen(), decide({"verb": "design", "design": HUT}))
    assert [e.kind for e in out.events] == ["think"]
    assert out.artifacts == []
    assert out.balance_delta == -2
    assert any("needs workshop" in d for d in out.dropped)


def test_with_the_workshop_a_design_lands_and_costs_the_design_price():
    out = world.apply_acts(
        physics(), citizen(unlocked=("workshop",)), decide({"verb": "design", "design": HUT})
    )
    assert [e.kind for e in out.events] == ["think", "design"]
    assert out.events[1].body == "Vela designed Hut"
    assert out.events[1].cost == -8
    assert out.events[1].artifact_index == 0
    assert out.balance_delta == -10
    art = out.artifacts[0]
    assert (art.kind, art.name, art.mime) == ("design", "Hut", "application/json")
    assert out.dropped == []


def test_an_explicit_design_cost_is_what_is_charged():
    costs = physics().costs.model_dump() | {"design": 3}
    out = world.apply_acts(
        physics(costs=costs),
        citizen(unlocked=("workshop",)),
        decide({"verb": "design", "design": HUT}),
    )
    assert out.events[1].cost == -3


def test_the_design_cost_defaults_to_the_craft_price():
    raw = load_physics(seed_physics_path("dust")).model_dump()
    raw["costs"] = {"craft": 11}
    assert parse_physics(raw).costs.design == 11
    assert load_physics(seed_physics_path("dust")).costs.design == physics().costs.craft


def test_a_design_may_ride_as_a_json_string_in_text():
    out = world.apply_acts(
        physics(),
        citizen(unlocked=("workshop",)),
        decide({"verb": "design", "text": json.dumps(HUT)}),
    )
    assert out.artifacts and out.artifacts[0].kind == "design"


_TOO_MANY_PARTS = _hut(parts=[HUT["parts"][0]] * 25)
_OUTSIDE = _hut(parts=[{"kind": "box", "at": [1, 0, 0], "size": [2, 1, 1], "color": "sand"}])
_HEX = _hut(parts=[{"kind": "box", "at": [0, 0, 0], "size": [1, 1, 1], "color": "#ff0000"}])
_TALL = _hut(parts=[{"kind": "box", "at": [0, 0, 0], "size": [1, 5, 1], "color": "sand"}])
_NINE_FEATURES = _hut(
    parts=[
        {
            "kind": "box",
            "at": [0, 0, 0],
            "size": [1, 1, 1],
            "color": "sand",
            "features": [{"kind": "lamp"}] * 9,
        }
    ]
)


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (_TOO_MANY_PARTS, "at most 24 parts"),
        (_OUTSIDE, "outside the footprint"),
        (_HEX, "palette name"),
        (_TALL, "taller than 4"),
        (_NINE_FEATURES, "at most 8 features"),
        ("a hut with a red door", "a design is an object"),
        (["not", "an", "object"], "a design is an object"),
    ],
)
def test_each_schema_violation_is_dropped_with_its_reason_and_unpaid(bad, reason):
    act = (
        {"verb": "design", "text": bad}
        if isinstance(bad, str)
        else {"verb": "design", "design": bad}
    )
    out = world.apply_acts(physics(), citizen(unlocked=("workshop",)), decide(act))
    assert [e.kind for e in out.events] == ["think"]
    assert out.artifacts == []
    assert out.balance_delta == -2
    assert any(d.startswith("design: ") and reason in d for d in out.dropped), out.dropped


def test_the_artifact_body_is_canonical_json_that_round_trips():
    out = world.apply_acts(
        physics(), citizen(unlocked=("workshop",)), decide({"verb": "design", "design": HUT})
    )
    body = out.artifacts[0].body
    assert " " not in body and "\n" not in body
    obj = json.loads(body)
    assert list(obj) == sorted(obj)
    again = validate_design(obj)
    assert isinstance(again, Design)
    assert again.canonical() == body
    assert obj["parts"][1]["rotate"] == 90 and obj["parts"][0]["features"][1]["u"] == 0.3


def test_a_build_carries_the_design_id_it_named():
    out = world.apply_acts(
        physics(), citizen(), decide({"verb": "build", "name": "hut", "design_id": "abc"})
    )
    assert out.artifacts[0].kind == "structure"
    assert out.artifacts[0].design_id == "abc"


def test_the_prompt_shows_design_only_with_the_workshop():
    phys = physics()

    def prefix(unlocked):
        c = citizen(unlocked=unlocked)
        digest = world.build_digest(
            day=1, tick=0, pool=10, citizen=c, ledger=[], nearby_speech=[], new_artifacts=[],
            weather=[], viewer_messages=[], memories=[], constitution=[],
        )  # fmt: skip
        return citizen_llm.build_prompt_parts(phys, c, digest)[0]

    def verbs(text):
        return json.loads(text.split("== VERBS THIS WORLD ALLOWS ==\n", 1)[1].splitlines()[0])

    without = prefix(())
    assert "design" not in verbs(without) and "== DESIGN" not in without
    with_ = prefix(("workshop",))
    assert "design" in verbs(with_) and "== DESIGN" in with_
    assert "stone" in with_ and "24 parts" in with_


# ---------------------------------------------------------------------------
# Through the service: landing, moderation, ownership, the wires
# ---------------------------------------------------------------------------

pytestmark_async = pytest.mark.asyncio

# A world where the workshop is the first and cheapest node, so the mock
# citizen builds it on tick 2 (tick 1 is the charter).
WORKSHOP_FIRST = {"workshop": {"cost": 1, "needs": [], "grants": ["design"]}}


def _workshop_universe(client, **over):
    uni = create_universe(
        client, founders=over.pop("founders", 1), tech_tree=WORKSHOP_FIRST, **over
    )
    assert client.post(f"/terrarium/universes/{uni['id']}/tick?n=2").status_code == 200
    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]
    assert all("workshop" in c["unlocked"] for c in citizens), citizens
    return uni, citizens


def _tick(client, uni, decision):
    citizen_llm.set_mock_decision(decision)
    res = client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    assert res.status_code == 200, res.text
    return res.json()


def _artifacts(client, uni, kind, name=None):
    arts = client.get(f"/terrarium/universes/{uni['id']}/artifacts").json()["artifacts"]
    return [a for a in arts if a["kind"] == kind and (name is None or a["name"] == name)]


def _events(client, uni, kind):
    events = client.get(f"/terrarium/universes/{uni['id']}/events").json()["events"]
    return [e for e in events if e["kind"] == kind]


@pytest.mark.asyncio
async def test_a_design_lands_as_a_json_file_and_a_design_event(client, monkeypatch):
    calls: list[dict] = []

    async def fake_write_text_file(**kw):
        calls.append(kw)

        class _Rec:
            id = "file-design-1"

        return _Rec()

    import pocketpaw_ee.cloud.uploads.service as uploads

    monkeypatch.setattr(uploads, "write_text_file", fake_write_text_file)
    uni, (c,) = _workshop_universe(client)
    before = c["balance"]
    _tick(client, uni, {"thought": "a hut", "acts": [{"verb": "design", "design": HUT}]})

    designs = _artifacts(client, uni, "design")
    assert len(designs) == 1
    d = designs[0]
    assert d["name"] == "Hut" and d["mime"] == "application/json"
    assert d["file_id"] == "file-design-1" and d["design_id"] is None
    assert calls[-1]["filename"] == "Hut.json" and calls[-1]["mime"] == "application/json"
    assert json.loads(calls[-1]["content"])["name"] == "Hut"

    events = _events(client, uni, "design")
    assert len(events) == 1
    assert events[0]["body"].endswith("designed Hut")
    assert events[0]["cost"] == -8
    assert events[0]["artifact_id"] == d["id"]
    assert events[0]["data"] == {"design_id": d["id"]}

    after = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    assert after["balance"] == before - 2 - 8


@pytest.mark.asyncio
async def test_a_design_whose_name_fails_moderation_is_withheld(client):
    uni, _ = _workshop_universe(client)
    _tick(
        client, uni, {"thought": "", "acts": [{"verb": "design", "design": _hut(name="kys tower")}]}
    )
    (d,) = _artifacts(client, uni, "design")
    assert d["name"] == "[withheld]"
    events = _events(client, uni, "design")
    assert events and events[0]["cost"] == -8, "withheld still costs"


@pytest.mark.asyncio
async def test_a_build_from_an_owned_design_carries_it_on_both_wires(client, monkeypatch):
    uni, _ = _workshop_universe(client, public=True)
    _tick(client, uni, {"thought": "", "acts": [{"verb": "design", "design": HUT}]})
    (d,) = _artifacts(client, uni, "design")
    _tick(
        client,
        uni,
        {"thought": "", "acts": [{"verb": "build", "name": "my hut", "design_id": d["id"]}]},
    )
    (s,) = _artifacts(client, uni, "structure", "my hut")
    assert s["design_id"] == d["id"]

    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    public = client.get(f"/terrarium/public/universes/{uni['id']}/artifacts").json()["artifacts"]
    assert [a["design_id"] for a in public if a["name"] == "my hut"] == [d["id"]]


@pytest.mark.asyncio
async def test_a_foreign_or_missing_design_id_builds_plain(client):
    uni, citizens = _workshop_universe(client, founders=2)
    _tick(client, uni, {"thought": "", "acts": [{"verb": "design", "design": HUT}]})
    designs = _artifacts(client, uni, "design")
    assert len(designs) == 2
    by_author = {d["author"]: d["id"] for d in designs}
    owner, other = citizens[0]["name"], citizens[1]["name"]

    # Everyone claims the FIRST citizen's design.
    _tick(
        client,
        uni,
        {"thought": "", "acts": [{"verb": "build", "name": "hut", "design_id": by_author[owner]}]},
    )
    built = {s["author"]: s["design_id"] for s in _artifacts(client, uni, "structure", "hut")}
    assert built[owner] == by_author[owner]
    assert built[other] is None, "a citizen cannot build another's design"

    _tick(
        client, uni, {"thought": "", "acts": [{"verb": "build", "name": "x", "design_id": "nope"}]}
    )
    later = _artifacts(client, uni, "structure", "x")
    assert later and all(s["design_id"] is None for s in later)
