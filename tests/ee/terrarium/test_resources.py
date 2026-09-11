# tests/ee/terrarium/test_resources.py — the resource layer, wave 1.
#
# Pins: every new physics field defaults empty and validates loudly; a build or
# verb whose bundle the citizen cannot cover is DROPPED and written as a
# zero-cost gate row naming only what is short; the spring swaps 4 of A for 1
# of B against the citizen's own stock at the speak price; a founder card's
# stock lands on the citizen; the day harvests one unit per held producing
# node into the holder's stock (one harvest row per citizen per day), scaled
# by a live rain (2) or drought (0) mark, which _fire_weather writes for two
# world days; the watched and the batched tick harvest the same Journal; the
# robber halves a hoard over stock_cap only when raids is on; the stable
# prefix is byte-identical to the pre-resource format when a world declares
# none; a rung change writes exactly one era row.
#
# Mutation anchors live in tests/mutations/terrarium_resources.json.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import scheduler, service, world  # noqa: E402
from pocketpaw_ee.terrarium.domain import (  # noqa: E402
    ZERO_COST_KINDS,
    CitizenDoc,
    EventDoc,
    UniverseDoc,
)
from pocketpaw_ee.terrarium.physics import (  # noqa: E402
    PhysicsError,
    load_physics,
    parse_physics,
    seed_physics_path,
)

from .conftest import create_universe, dust_physics  # noqa: E402
from .test_dormant_batch import fake_batch  # noqa: E402, F401

ONE_TICK_DAYS = {"world_day_seconds": 3600, "ticks_per_day": 1, "dormant_ticks_per_day": 1}
BUILD_WELL = {"thought": "water first", "acts": [{"verb": "build", "node": "well"}]}


def physics(**over):
    raw = load_physics(seed_physics_path("dust")).model_dump()
    raw.update(over)
    return parse_physics(raw)


def bare_physics(**over):
    """Dust with the resource layer stripped — what every pre-resource file looks like."""
    raw = load_physics(seed_physics_path("dust")).model_dump()
    raw.update(resources=[], stock_costs={}, stock_cap=None)
    for node in raw["tech_tree"].values():
        node.update(produces=None, stock_cost={})
    raw.update(over)
    return parse_physics(raw)


def citizen(**over):
    base = {"id": "c1", "name": "Vela", "balance": 100, "charter": "keep the ledger honest"}
    base.update(over)
    return world.CitizenSnapshot(**base)


def decide(*acts):
    return world.Decision(thought="thinking", acts=[world.Act(**a) for a in acts])


def _card(**over) -> dict:
    card = {"name": "Ada", "role": "the mathematician", "charter": "Count.", "values": ["logic"]}
    card.update(over)
    return card


async def _events(universe_id: str, kind: str) -> list[EventDoc]:
    return (
        await EventDoc.find(EventDoc.universe_id == universe_id, EventDoc.kind == kind)
        .sort("+seq")
        .to_list()
    )


async def _only_citizen(universe_id: str) -> CitizenDoc:
    docs = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    assert len(docs) == 1
    return docs[0]


# ---------------------------------------------------------------------------
# physics: defaults and validation
# ---------------------------------------------------------------------------


def test_every_resource_field_defaults_empty():
    p = bare_physics()
    assert p.resources == [] and p.stock_costs == {} and p.stock_cap is None
    assert all(n.produces is None and n.stock_cost == {} for n in p.tech_tree.values())
    assert physics(founder_cards=[_card()], founders=1).founder_cards[0].stock == {}


def test_dust_declares_five_resources_and_two_new_nodes():
    p = physics()
    assert p.resources == ["water", "grain", "timber", "stone", "ink"]
    assert p.tech_tree["woodcut"].needs == ["well"] and p.tech_tree["woodcut"].produces == "timber"
    assert p.tech_tree["quarry"].needs == ["farm"] and p.tech_tree["quarry"].produces == "stone"
    assert p.tech_tree["launch"].stock_cost == dict.fromkeys(p.resources, 6)
    assert p.stock_costs == {"write": {"ink": 1}, "craft": {"timber": 1, "stone": 1}}


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"resources": ["water", "water"]}, "unique"),
        ({"stock_costs": {"craft": {"gold": 1}}}, "undeclared"),
        ({"stock_costs": {"craft": {"timber": 0}}}, "positive"),
        ({"stock_costs": {"dance": {"timber": 1}}}, "known verb"),
        ({"stock_cap": 0}, "stock_cap"),
        ({"founders": 1, "founder_cards": [_card(stock={"gold": 1})]}, "undeclared"),
    ],
)
def test_bad_resource_layers_are_rejected_by_name(over, needle):
    with pytest.raises(PhysicsError, match=needle):
        physics(**over)


def test_produces_and_node_bundles_must_name_a_declared_resource():
    raw = dust_physics()
    raw["tech_tree"]["well"]["produces"] = "gold"
    with pytest.raises(PhysicsError, match="produces"):
        parse_physics(raw)
    raw = dust_physics()
    raw["tech_tree"]["farm"]["stock_cost"] = {"gold": 2}
    with pytest.raises(PhysicsError, match="undeclared"):
        parse_physics(raw)


# ---------------------------------------------------------------------------
# world: bundles gate, the gate row names the short, the spring is the bank
# ---------------------------------------------------------------------------


def test_a_build_short_of_its_bundle_is_dropped_and_written_as_a_gate_row():
    out = world.apply_acts(
        physics(),
        citizen(unlocked=("well",), stock={"water": 1}),
        decide({"verb": "build", "node": "farm"}),
    )
    assert out.unlocked == [] and out.artifacts == []
    assert [e.kind for e in out.events] == ["think", "gate"]
    gate = out.events[1]
    assert gate.cost == 0 and gate.node == "farm"
    assert gate.data == {"short": {"water": 1}}, "only the MISSING amount, not the bundle"
    assert out.balance_delta == -2, "a dropped build is not charged"
    assert out.stock_delta == {}


def test_a_covered_bundle_is_charged_and_a_tick_cannot_spend_a_unit_twice():
    out = world.apply_acts(
        physics(),
        citizen(unlocked=("well",), stock={"water": 2}, balance=500),
        decide({"verb": "build", "node": "farm"}, {"verb": "build", "node": "woodcut"}),
    )
    assert out.unlocked == ["farm"]
    assert out.stock_delta == {"water": -2}
    assert [e.kind for e in out.events] == ["think", "build", "gate"]
    assert out.events[2].data == {"short": {"water": 1}}


def test_a_verb_bundle_gates_too_and_the_charter_is_exempt():
    out = world.apply_acts(physics(), citizen(), decide({"verb": "craft", "text": "a cup"}))
    assert [e.kind for e in out.events] == ["think", "gate"]
    assert out.events[1].data == {"short": {"timber": 1, "stone": 1}}
    # The zero ritual: nobody is born holding ink, and the charter is not a book.
    out = world.apply_acts(
        physics(), citizen(charter=None), decide({"verb": "write", "text": "Rules."})
    )
    assert out.charter == "Rules." and out.stock_delta == {}
    # A later book does need the ink.
    out = world.apply_acts(physics(), citizen(), decide({"verb": "write", "text": "a song"}))
    assert [e.kind for e in out.events] == ["think", "gate"]


def test_the_spring_swaps_four_for_one_at_the_speak_price():
    out = world.apply_acts(
        physics(),
        citizen(stock={"water": 5}),
        decide({"verb": "trade", "to": "spring", "give": {"water": 4}, "want": {"stone": 1}}),
    )
    assert [e.kind for e in out.events] == ["think", "trade"]
    assert out.events[1].cost == -physics().costs.speak
    assert out.stock_delta == {"water": -4, "stone": 1}
    assert out.transfers == [] and out.pool_delta == 2 + physics().costs.speak
    assert "spring" in out.events[1].body


@pytest.mark.parametrize(
    "act",
    [
        {"verb": "trade", "to": "spring", "give": {"water": 3}, "want": {"stone": 1}},
        {"verb": "trade", "to": "spring", "give": {"water": 4}, "want": {"water": 1}},
        {"verb": "trade", "to": "spring", "give": {"water": 4}, "want": {"gold": 1}},
        {"verb": "trade", "to": "spring", "give": {"water": 4, "grain": 4}, "want": {"stone": 2}},
    ],
)
def test_a_malformed_spring_swap_is_dropped_unpaid(act):
    out = world.apply_acts(physics(), citizen(stock={"water": 8, "grain": 8}), decide(act))
    assert [e.kind for e in out.events] == ["think"]
    assert out.stock_delta == {} and out.balance_delta == -2


def test_a_spring_swap_the_citizen_cannot_cover_is_a_gate_row():
    out = world.apply_acts(
        physics(),
        citizen(stock={"water": 3}),
        decide({"verb": "trade", "to": "spring", "give": {"water": 4}, "want": {"stone": 1}}),
    )
    assert [e.kind for e in out.events] == ["think", "gate"]
    assert out.events[1].data == {"short": {"water": 1}}


def test_a_credit_trade_is_unchanged_by_the_spring():
    out = world.apply_acts(
        physics(), citizen(), decide({"verb": "trade", "to": "Nim", "amount": 5})
    )
    assert out.transfers == [("Nim", 5)] and out.pool_delta == 2


def test_the_new_kinds_are_zero_cost_and_a_world_without_resources_still_passes():
    assert {"harvest", "raid", "gate", "era"} <= ZERO_COST_KINDS
    out = world.apply_acts(
        bare_physics(), citizen(unlocked=("well",)), decide({"verb": "build", "node": "farm"})
    )
    assert out.unlocked == ["farm"], "no resources declared: every bundle is empty"


# ---------------------------------------------------------------------------
# service: founder stock, charging, the harvest, weather marks, the robber
# ---------------------------------------------------------------------------


def test_a_founder_cards_stock_is_seeded_onto_the_citizen(client):
    uni = create_universe(client, founders=1, founder_cards=[_card(stock={"water": 2})])
    cit = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    assert cit["stock"] == {"water": 2}
    ledger = client.get(f"/terrarium/universes/{uni['id']}").json()["ledger"]
    assert ledger[0]["stock"] == {"water": 2}


def test_a_generic_founder_starts_with_nothing(client):
    uni = create_universe(client, founders=1)
    cit = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    assert cit["stock"] == {}


async def test_a_landed_build_charges_its_bundle_from_the_stock(client):
    uni = create_universe(client, founders=1, founder_cards=[_card(stock={"water": 2})])
    citizen_llm.set_mock_decision(BUILD_WELL)
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    citizen_llm.set_mock_decision({"thought": "", "acts": [{"verb": "build", "node": "farm"}]})
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    doc = await _only_citizen(uni["id"])
    assert doc.unlocked == ["farm", "well"]
    assert doc.stock == {}, "2 water spent; an empty key is dropped, not left at 0"
    assert await _events(uni["id"], "gate") == []


async def test_the_day_harvests_one_unit_per_held_producing_node(client):
    uni = create_universe(client, founders=1, time=ONE_TICK_DAYS)
    citizen_llm.set_mock_decision(BUILD_WELL)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    doc = await _only_citizen(uni["id"])
    assert doc.stock == {"water": 2}
    rows = await _events(uni["id"], "harvest")
    assert [(r.actor, r.cost, r.data) for r in rows] == [
        ("Ada" if doc.name == "Ada" else doc.name, 0, {"got": {"water": 1}})
    ] * 2
    assert [r.day for r in rows] == [2, 3], "written on the new day, once per day"
    cit = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"][0]
    assert cit["stock"] == {"water": 2}


async def test_a_hibernating_citizen_does_not_harvest(client):
    uni = create_universe(client, founders=1, time=ONE_TICK_DAYS)
    citizen_llm.set_mock_decision(BUILD_WELL)
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    doc = await _only_citizen(uni["id"])
    doc.state = "hibernating"
    await doc.save()
    citizen_llm.set_mock_decision({"thought": "", "acts": []})
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    assert len(await _events(uni["id"], "harvest")) == 1


async def test_rain_doubles_and_drought_zeroes_the_yield_under_the_mark(client):
    uni = create_universe(client, founders=1, time=ONE_TICK_DAYS)
    citizen_llm.set_mock_decision(BUILD_WELL)
    client.post(f"/terrarium/universes/{uni['id']}/tick")  # day 1 -> 2: the well, 1 water
    doc = await _only_citizen(uni["id"])
    u = await UniverseDoc.get(uni["id"])
    u.weather_marks = [{"kind": "rain", "x": doc.x, "y": doc.y, "expires_day": 99}]
    await u.save()
    client.post(f"/terrarium/universes/{uni['id']}/tick")  # day 2 -> 3: rain, 2 water
    u = await UniverseDoc.get(uni["id"])
    u.weather_marks = [{"kind": "drought", "x": doc.x, "y": doc.y, "expires_day": 99}]
    await u.save()
    client.post(f"/terrarium/universes/{uni['id']}/tick")  # day 3 -> 4: drought, nothing
    u = await UniverseDoc.get(uni["id"])
    u.weather_marks = [{"kind": "rain", "x": doc.x + 60, "y": doc.y, "expires_day": 99}]
    await u.save()
    client.post(f"/terrarium/universes/{uni['id']}/tick")  # day 4 -> 5: rain elsewhere, 1
    got = [r.data["got"] for r in await _events(uni["id"], "harvest")]
    assert got == [{"water": 1}, {"water": 2}, {"water": 1}], "a dry day writes no harvest row"
    assert (await _only_citizen(uni["id"])).stock == {"water": 4}


async def test_a_fired_rain_leaves_a_mark_for_two_days_and_it_is_pruned(client):
    uni = create_universe(client, founders=1, time=ONE_TICK_DAYS)
    from pocketpaw_ee.terrarium import weather

    client.post(
        f"/terrarium/universes/{uni['id']}/weather/pledge",
        json={"kind": "rain", "tokens": weather.POWER_COSTS["rain"]},
    )
    u = await UniverseDoc.get(uni["id"])
    assert len(u.weather_marks) == 1
    mark = u.weather_marks[0]
    assert set(mark) == {"kind", "x", "y", "expires_day"}
    assert mark["kind"] == "rain" and mark["expires_day"] == u.day + 2
    citizen_llm.set_mock_decision({"thought": "", "acts": []})
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    assert len((await UniverseDoc.get(uni["id"])).weather_marks) == 1, "day 3 still inside"
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    assert (await UniverseDoc.get(uni["id"])).weather_marks == [], "day 4: expired, pruned"


async def test_the_robber_halves_a_hoard_over_the_cap_only_when_raids_is_on(client):
    citizen_llm.set_mock_decision({"thought": "", "acts": []})
    quiet = create_universe(
        client, founders=1, time=ONE_TICK_DAYS, founder_cards=[_card(stock={"grain": 20})]
    )
    client.post(f"/terrarium/universes/{quiet['id']}/tick")
    assert (await _only_citizen(quiet["id"])).stock == {"grain": 20}, "raids off: no robber"
    capless = create_universe(
        client,
        founders=1,
        time=ONE_TICK_DAYS,
        raids=True,
        founder_cards=[_card(stock={"grain": 20})],
    )
    client.post(f"/terrarium/universes/{capless['id']}/tick")
    assert (await _only_citizen(capless["id"])).stock == {"grain": 20}, "no cap: the strong hoard"

    uni = create_universe(
        client,
        founders=1,
        time=ONE_TICK_DAYS,
        raids=True,
        stock_cap=8,
        founder_cards=[_card(stock={"grain": 20, "water": 8})],
    )
    client.post(f"/terrarium/universes/{uni['id']}/tick")
    assert (await _only_citizen(uni["id"])).stock == {"grain": 10, "water": 8}, "at the cap is fine"
    rows = await _events(uni["id"], "raid")
    assert [(r.actor, r.cost, r.data) for r in rows] == [("Ada", 0, {"took": {"grain": 10}})]


async def test_the_batched_day_harvests_what_the_watched_day_harvests(client, fake_batch):  # noqa: F811
    """Both paths reach _new_day through _land_tick; the Journals must match
    row for row, harvest rows included."""

    async def journal(universe_id: str, since: int):
        docs = (
            await EventDoc.find(EventDoc.universe_id == universe_id, EventDoc.seq > since)
            .sort("+seq")
            .to_list()
        )
        return [(d.kind, d.actor, d.cost, d.data) for d in docs]

    watched = create_universe(client, founders=2, time=ONE_TICK_DAYS)
    first = (await UniverseDoc.get(watched["id"])).seq
    client.post(f"/terrarium/universes/{watched['id']}/tick?n=3")
    sync_rows = await journal(watched["id"], first)
    assert any(k == "harvest" for k, *_ in sync_rows), "the well was built and harvested"
    doc = await UniverseDoc.get(watched["id"])
    doc.status = "archived"
    await doc.save()

    sleeping = create_universe(client, founders=2, time=ONE_TICK_DAYS)
    since = (await UniverseDoc.get(sleeping["id"])).seq
    now = datetime.now(UTC)
    for i in range(3):
        n = now + timedelta(hours=2 * i)
        d = await UniverseDoc.get(sleeping["id"])
        d.last_viewed_at = n - timedelta(days=2)
        d.last_tick_at = n - timedelta(seconds=7200)
        await d.save()
        await scheduler.run_scheduler_tick(now=lambda n=n: n)  # file
        await scheduler.run_scheduler_tick(now=lambda n=n: n)  # apply
    assert await journal(sleeping["id"], since) == sync_rows


async def test_a_rung_change_writes_exactly_one_era_row(client, monkeypatch):
    uni = create_universe(client, founders=1)
    citizen_llm.set_mock_decision({"thought": "", "acts": []})
    monkeypatch.setattr(world, "rung_for", lambda pop, unlocked: "town")
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    rows = await _events(uni["id"], "era")
    assert [(r.actor, r.cost, r.origin, r.data) for r in rows] == [
        ("GATE", 0, "system", {"from": "camp", "to": "town"})
    ]
    assert client.get(f"/terrarium/universes/{uni['id']}").json()["universe"]["rung"] == "town"


async def test_no_rung_change_writes_no_era_row(client):
    uni = create_universe(client, founders=1)
    client.post(f"/terrarium/universes/{uni['id']}/tick?n=2")
    assert await _events(uni["id"], "era") == []


# ---------------------------------------------------------------------------
# prompt: the prefix is unchanged for a world without resources
# ---------------------------------------------------------------------------


def _digest(snap: world.CitizenSnapshot) -> world.SenseDigest:
    return world.build_digest(
        day=3,
        tick=9,
        pool=100,
        citizen=snap,
        ledger=[],
        nearby_speech=[],
        new_artifacts=[],
        weather=[],
        viewer_messages=[],
        memories=[],
        constitution=["no theft"],
    )


def test_the_prefix_is_byte_identical_to_the_pre_resource_format_without_resources():
    nim = citizen(name="Nim", stock={"water": 3})
    prefix, suffix = citizen_llm.build_prompt_parts(bare_physics(), nim, _digest(nim))
    assert "== RESOURCES ==" not in prefix
    assert "makes" not in prefix and "also costs" not in prefix
    assert '"to": "spring"' not in prefix
    assert "- well: cost 20, needs nothing\n- farm: cost 40, needs ['well']\n" in prefix
    assert 'your stock: {"water": 3}' in suffix, "stock is volatile: it rides the suffix"

    rich, _ = citizen_llm.build_prompt_parts(physics(), nim, _digest(nim))
    assert "== RESOURCES ==" in rich
    assert "- well: cost 20, needs nothing, makes water\n" in rich
    assert "- farm: cost 40, needs ['well'], makes grain, also costs {\"water\": 2}\n" in rich
    assert '"to": "spring"' in rich and '{"write": {"ink": 1}' in rich


def test_the_mock_citizen_builds_only_what_its_stock_covers():
    p = physics()
    dry = citizen(unlocked=("well",), balance=500)
    wet = citizen(unlocked=("well",), balance=500, stock={"water": 2})
    import asyncio
    import json

    dry_acts = json.loads(
        asyncio.run(
            citizen_llm.MockLlm().decide(prompt="", physics=p, citizen=dry, digest=_digest(dry))
        )
    )["acts"]
    wet_acts = json.loads(
        asyncio.run(
            citizen_llm.MockLlm().decide(prompt="", physics=p, citizen=wet, digest=_digest(wet))
        )
    )["acts"]
    assert all(a["verb"] != "build" for a in dry_acts)
    assert any(a["verb"] == "build" and a["node"] == "farm" for a in wet_acts)


def test_service_snapshot_carries_the_stock():
    doc = CitizenDoc(workspace="w", universe_id="u", name="Nim", stock={"ink": 1})
    assert service._snapshot(doc).stock == {"ink": 1}
    assert service.citizen_wire(doc)["stock"] == {"ink": 1}
