# tests/ee/terrarium/test_trade_offers.py — the resource layer, wave 2: offers.
#
# Pins: ``trade`` with give/want and no ``to`` posts an ``offer`` row at the
# trade fee (costs.trade, else speak) carrying give / want / expires_day and
# escrowing nothing; a giver short of ``give`` is a gate row; a malformed offer
# is dropped unpaid; ``trade`` with ``offer_seq`` is an ``accept`` checked in
# the engine against the open-offer map (acceptor must hold ``want``) and
# settled by the service under the lock — both bundles move and the offer is
# claimed with ``data.taken_by``; an expired, taken, own or short-giver offer
# lands as a zero-cost gate with ``data.reason`` and nothing moves, fee
# refunded; a failing second write un-claims the offer (atomicity); the digest
# lists open offers and the suffix prints them only when there are any; every
# trade body (offer, accept, spring) is engine-templated even when the model
# supplied text.
#
# Mutation anchors live in tests/mutations/terrarium_resources.json.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import llm as citizen_llm  # noqa: E402
from pocketpaw_ee.terrarium import service, world  # noqa: E402
from pocketpaw_ee.terrarium.domain import CitizenDoc, EventDoc, UniverseDoc  # noqa: E402

from .conftest import create_universe  # noqa: E402
from .test_resources import _card, _digest, _events, citizen, decide, physics  # noqa: E402

OFFER = {"verb": "trade", "give": {"grain": 3}, "want": {"stone": 1}}
OFFERS = {
    7: {"seq": 7, "who": "Mira", "give": {"grain": 3}, "want": {"stone": 1}, "expires_day": 3}
}
FEE = physics().costs.speak  # Dust sets no costs.trade


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


def test_an_offer_is_posted_at_the_fee_and_escrows_nothing():
    out = world.apply_acts(physics(), citizen(stock={"grain": 3}), decide(OFFER), day=5)
    assert [e.kind for e in out.events] == ["think", "offer"]
    row = out.events[1]
    assert row.cost == -FEE and row.body == "offers 3 grain for 1 stone"
    assert row.data == {"give": {"grain": 3}, "want": {"stone": 1}, "expires_day": 7}
    assert out.stock_delta == {}, "checked, not escrowed"
    assert out.pool_delta == 2 + FEE and out.transfers == []


def test_costs_trade_sets_the_fee_when_present():
    p = physics(costs={**physics().costs.model_dump(), "trade": 5})
    assert world.trade_fee(p) == 5
    out = world.apply_acts(p, citizen(stock={"grain": 3}), decide(OFFER))
    assert out.events[1].cost == -5
    spring = {"verb": "trade", "to": "spring", "give": {"water": 4}, "want": {"stone": 1}}
    assert world.apply_acts(p, citizen(stock={"water": 4}), decide(spring)).events[1].cost == -5


def test_an_offer_without_the_stock_is_a_gate_row():
    out = world.apply_acts(physics(), citizen(stock={"grain": 1}), decide(OFFER))
    assert [e.kind for e in out.events] == ["think", "gate"]
    assert out.events[1].data == {"short": {"grain": 2}} and out.balance_delta == -2


@pytest.mark.parametrize(
    "act",
    [
        {"verb": "trade", "give": {"grain": 3}},
        {"verb": "trade", "want": {"stone": 1}},
        {"verb": "trade", "give": {"gold": 3}, "want": {"stone": 1}},
        {"verb": "trade", "give": {"grain": 0}, "want": {"stone": 1}},
    ],
)
def test_a_malformed_offer_is_dropped_unpaid(act):
    out = world.apply_acts(physics(), citizen(stock={"grain": 3}), decide(act))
    assert [e.kind for e in out.events] == ["think"] and out.balance_delta == -2


def test_an_accept_is_checked_against_the_open_offer_map():
    accept = {"verb": "trade", "offer_seq": 7}
    unknown = world.apply_acts(physics(), citizen(stock={"stone": 1}), decide(accept))
    assert [e.kind for e in unknown.events] == ["think"], "no map: no such offer"
    short = world.apply_acts(physics(), citizen(), decide(accept), offers=OFFERS)
    assert [e.kind for e in short.events] == ["think", "gate"]
    assert short.events[1].data == {"short": {"stone": 1}}
    out = world.apply_acts(physics(), citizen(stock={"stone": 1}), decide(accept), offers=OFFERS)
    row = out.events[1]
    assert row.kind == "accept" and row.cost == -FEE and row.data == {"offer_seq": 7}
    assert row.body == "accepts Mira's offer of 3 grain for 1 stone"
    assert out.stock_delta == {"stone": -1}, "the acceptor's side; the giver's lands at the write"


def test_every_trade_body_is_engine_templated_even_when_the_model_wrote_text():
    spring = {"verb": "trade", "to": "spring", "give": {"water": 4}, "want": {"stone": 1}}
    kinds = {}
    for act in (spring, OFFER, {"verb": "trade", "offer_seq": 7}):
        out = world.apply_acts(
            physics(),
            citizen(stock={"water": 4, "grain": 3, "stone": 1}),
            decide({**act, "text": "ALL HAIL THE SPRING"}),
            offers=OFFERS,
        )
        kinds[out.events[1].kind] = out.events[1].body
    assert kinds == {
        "trade": "swaps 4 water for 1 stone at the spring",
        "offer": "offers 3 grain for 1 stone",
        "accept": "accepts Mira's offer of 3 grain for 1 stone",
    }


# ---------------------------------------------------------------------------
# service: the swap lands under the lock
# ---------------------------------------------------------------------------


def _world(client):
    return create_universe(
        client,
        founders=2,
        founder_cards=[
            _card(name="Mira", stock={"grain": 3}),
            _card(name="Nim", stock={"stone": 2, "water": 1}),
        ],
    )


async def _land(universe_id: str, by_name: dict[str, list[dict]]):
    """One tick of ``_land_tick`` with a decision per citizen — the mock LLM is
    one decision for everyone, and a trade needs two voices."""
    uni = await UniverseDoc.get(universe_id)
    docs = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).sort("+name").to_list()
    pairs = [(d, service._snapshot(d)) for d in docs]
    decisions = [decide(*by_name.get(d.name, [])) for d in docs]
    return await service._land_tick(uni, service.physics_of(uni), pairs, decisions, "u1")


async def _stocks(universe_id: str) -> dict[str, dict[str, int]]:
    docs = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return {d.name: d.stock for d in docs}


async def _offer(universe_id: str) -> EventDoc:
    (row,) = await _events(universe_id, "offer")
    return row


async def _post(client) -> tuple[dict, EventDoc]:
    uni = _world(client)
    await _land(uni["id"], {"Mira": [OFFER]})
    return uni, await _offer(uni["id"])


async def test_an_accept_moves_both_bundles_and_claims_the_offer(client):
    uni, offer = await _post(client)
    assert offer.cost == -FEE and offer.data["expires_day"] == 3 and "taken_by" not in offer.data
    assert await _stocks(uni["id"]) == {"Mira": {"grain": 3}, "Nim": {"stone": 2, "water": 1}}

    await _land(uni["id"], {"Nim": [{"verb": "trade", "offer_seq": offer.seq}]})
    assert await _stocks(uni["id"]) == {
        "Mira": {"stone": 1},
        "Nim": {"grain": 3, "stone": 1, "water": 1},
    }
    assert (await _offer(uni["id"])).data["taken_by"] == "Nim"
    (row,) = await _events(uni["id"], "accept")
    assert row.actor == "Nim" and row.cost == -FEE
    assert row.body == "accepts Mira's offer of 3 grain for 1 stone"
    assert row.data == {
        "offer_seq": offer.seq,
        "from": "Mira",
        "give": {"grain": 3},
        "want": {"stone": 1},
    }
    assert await _events(uni["id"], "gate") == []
    # A second accept of the same offer is a gate: taken_by holds.
    await _land(uni["id"], {"Nim": [{"verb": "trade", "offer_seq": offer.seq}]})
    (gate,) = await _events(uni["id"], "gate")
    assert gate.cost == 0 and "already taken" in gate.data["reason"]
    assert await _stocks(uni["id"]) == {
        "Mira": {"stone": 1},
        "Nim": {"grain": 3, "stone": 1, "water": 1},
    }


@pytest.mark.parametrize(
    ("spoil", "needle"),
    [
        ("expired", "expired"),
        ("taken", "already taken"),
        ("short", "no longer holds 2 grain"),
        ("own", "your own offer"),
        ("asleep", "not here to trade"),
    ],
)
async def test_a_spoiled_accept_is_a_gate_row_and_nothing_moves(client, spoil, needle, monkeypatch):
    uni, offer = await _post(client)
    acceptor = "Nim"
    remembered: list[str] = []

    async def capture(path, summary):
        remembered.append(summary)

    monkeypatch.setattr(service.soul_link, "remember_tick", capture)
    if spoil == "expired":
        offer.data = {**offer.data, "expires_day": 0}
        await offer.save()
    elif spoil == "taken":
        offer.data = {**offer.data, "taken_by": "Orin"}
        await offer.save()
    elif spoil == "short":
        mira = await CitizenDoc.find_one(CitizenDoc.name == "Mira")
        mira.stock = {"grain": 1}
        await mira.save()
    elif spoil == "own":
        acceptor = "Mira"
        mira = await CitizenDoc.find_one(CitizenDoc.name == "Mira")
        mira.stock = {"grain": 3, "stone": 1}
        await mira.save()
    elif spoil == "asleep":
        mira = await CitizenDoc.find_one(CitizenDoc.name == "Mira")
        mira.state = "hibernating"
        await mira.save()
    before = await _stocks(uni["id"])
    balance = (await CitizenDoc.find_one(CitizenDoc.name == acceptor)).balance

    await _land(uni["id"], {acceptor: [{"verb": "trade", "offer_seq": offer.seq}]})
    assert await _events(uni["id"], "accept") == []
    (gate,) = await _events(uni["id"], "gate")
    assert gate.actor == acceptor and gate.cost == 0 and gate.data["offer_seq"] == offer.seq
    assert needle in gate.data["reason"]
    if spoil == "short":
        assert gate.data["short"] == {"grain": 2}
    assert await _stocks(uni["id"]) == before, "nothing moves"
    assert "taken_by" not in (await _offer(uni["id"])).data or spoil == "taken"
    after = (await CitizenDoc.find_one(CitizenDoc.name == acceptor)).balance
    assert balance - after == physics().costs.think, "the fee is refunded; only the think lands"
    # Write-policy: the soul remembers the gate, never a swap that did not happen.
    mine = [m for m in remembered if f": {acceptor} " in m]
    assert len(mine) == 1 and "gate: could not accept" in mine[0] and "accepts" not in mine[0]


async def test_a_giver_still_holds_check_reads_the_ticks_own_doc(client):
    """Mira spends her grain earlier in the same tick; Nim's accept must see it."""
    uni, offer = await _post(client)
    spring = {"verb": "trade", "to": "spring", "give": {"grain": 4}, "want": {"stone": 1}}
    mira = await CitizenDoc.find_one(CitizenDoc.name == "Mira")
    mira.stock = {"grain": 4}
    await mira.save()
    await _land(uni["id"], {"Mira": [spring], "Nim": [{"verb": "trade", "offer_seq": offer.seq}]})
    (gate,) = await _events(uni["id"], "gate")
    assert "no longer holds" in gate.data["reason"]
    assert await _stocks(uni["id"]) == {"Mira": {"stone": 1}, "Nim": {"stone": 2, "water": 1}}


async def test_a_failing_second_write_un_claims_the_offer(client, monkeypatch):
    uni, offer = await _post(client)
    real_save = CitizenDoc.save

    async def boom(self, *a, **k):
        if self.name == "Mira":
            raise RuntimeError("disk full")
        return await real_save(self, *a, **k)

    monkeypatch.setattr(CitizenDoc, "save", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        await _land(uni["id"], {"Nim": [{"verb": "trade", "offer_seq": offer.seq}]})
    monkeypatch.undo()
    assert "taken_by" not in (await _offer(uni["id"])).data
    assert await _stocks(uni["id"]) == {"Mira": {"grain": 3}, "Nim": {"stone": 2, "water": 1}}


async def test_an_expired_offer_is_acceptable_through_its_last_day(client):
    uni, offer = await _post(client)
    u = await UniverseDoc.get(uni["id"])
    u.day = offer.data["expires_day"]
    await u.save()
    await _land(uni["id"], {"Nim": [{"verb": "trade", "offer_seq": offer.seq}]})
    assert (await _offer(uni["id"])).data["taken_by"] == "Nim"


# ---------------------------------------------------------------------------
# the digest and the prompt
# ---------------------------------------------------------------------------


async def test_open_offers_ride_the_digest_and_the_suffix_only_when_there_are_any(client):
    uni = _world(client)
    u = await UniverseDoc.get(uni["id"])
    p = service.physics_of(u)
    rows = await service._sense(u, p)
    assert all(d.open_offers == () for _doc, _snap, d in rows)
    _prefix, suffix = citizen_llm.build_prompt_parts(p, rows[0][1], rows[0][2])
    assert "== OPEN OFFERS ==" not in suffix

    await _land(uni["id"], {"Mira": [OFFER]})
    u = await UniverseDoc.get(uni["id"])
    rows = await service._sense(u, p)
    _doc, snap, digest = next(r for r in rows if r[1].name == "Nim")
    (offer,) = await _events(uni["id"], "offer")
    assert digest.open_offers == (
        {
            "seq": offer.seq,
            "who": "Mira",
            "give": {"grain": 3},
            "want": {"stone": 1},
            "expires_day": 3,
            "taken_by": None,
        },
    )
    _prefix, suffix = citizen_llm.build_prompt_parts(p, snap, digest)
    assert f'- #{offer.seq} Mira gives {{"grain": 3}} for {{"stone": 1}}, until day 3' in suffix
    assert suffix.count("== OPEN OFFERS ==") == 1

    # Taken or expired: gone from the digest without any row being deleted.
    offer.data = {**offer.data, "taken_by": "Nim"}
    await offer.save()
    assert await service._open_offers(u) == []
    offer.data = {"give": {"grain": 3}, "want": {"stone": 1}, "expires_day": 0}
    await offer.save()
    assert await service._open_offers(u) == []


def test_the_prefix_names_the_offer_shapes_only_with_resources():
    nim = citizen(name="Nim")
    prefix, _ = citizen_llm.build_prompt_parts(physics(), nim, _digest(nim))
    assert '"offer_seq": <seq>' in prefix and '"give": {"<a>": n}, "want": {"<b>": m}' in prefix
    assert '"trade": null' not in prefix, "an unset costs.trade never reaches the prompt"
