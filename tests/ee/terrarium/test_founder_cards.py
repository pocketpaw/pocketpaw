# tests/ee/terrarium/test_founder_cards.py — a founder card becomes a founder.
#
# The create flow may post an optional ``founder_cards`` array on the physics
# file (name, role, charter, values) with ``founders == len(founder_cards)``.
# These pin that a named founder reaches its citizen AND its soul: the citizen
# carries the card's name/role/charter/values, the deterministic OCEAN spread
# still supplies personality, the count/shape rules raise ``PhysicsError``, a
# card whose text fails moderation rejects the whole creation (nothing written),
# and a universe with no cards seeds generically exactly as before.

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium.physics import PhysicsError, parse_physics  # noqa: E402
from pocketpaw_ee.terrarium.service import _FOUNDER_NAMES, _ocean_for  # noqa: E402

from .conftest import create_universe, dust_physics  # noqa: E402


def _cards(n: int) -> list[dict]:
    """``n`` well-formed founder cards with names outside the generic pool, so a
    card's name reaching the citizen proves the override rather than a coincidence."""
    base = [
        {
            "name": "Ada",
            "role": "the mathematician",
            "charter": "Compute the future.",
            "values": ["logic", "care"],
        },
        {
            "name": "Sol",
            "role": "the sun-keeper",
            "charter": "Warmth is a public good.",
            "values": ["light"],
        },
        {
            "name": "Nim",
            "role": "the gamewright",
            "charter": "Every rule can be played.",
            "values": ["play", "rigor"],
        },
        {
            "name": "Ora",
            "role": "the oracle",
            "charter": "Ask before you build.",
            "values": ["counsel"],
        },
    ]
    return [
        dict(
            base[i % len(base)],
            name=base[i % len(base)]["name"] + ("" if i < len(base) else str(i)),
        )
        for i in range(n)
    ]


async def _soul_recall(path: str, query: str) -> list[str]:
    """Search a citizen's soul the way the existing soul tests do."""
    from soul_protocol import Soul

    soul = await Soul.awaken(Path(path))
    return [str(e.content) for e in await soul.recall(query, limit=100)]


# --- validation (pure ``parse_physics``, no beanie) -----------------------


def test_founders_must_equal_the_card_count() -> None:
    with pytest.raises(PhysicsError, match="must match"):
        parse_physics(dust_physics(founders=2, founder_cards=_cards(3)))


def test_a_card_with_an_empty_name_is_rejected() -> None:
    cards = _cards(2)
    cards[1]["name"] = "   "
    with pytest.raises(PhysicsError, match="name"):
        parse_physics(dust_physics(founders=2, founder_cards=cards))


def test_a_thirteenth_card_is_rejected() -> None:
    with pytest.raises(PhysicsError, match="at most 12"):
        parse_physics(dust_physics(founders=13, founder_cards=_cards(13)))


def test_no_cards_is_valid_and_leaves_the_field_absent() -> None:
    physics = parse_physics(dust_physics())
    assert physics.founder_cards is None


# --- the full create path -------------------------------------------------


def test_three_cards_become_three_named_citizens(client) -> None:
    cards = _cards(3)
    uni = create_universe(client, founders=3, founder_cards=cards)
    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]
    assert len(citizens) == 3

    by_name = {c["name"]: c for c in citizens}
    for i, card in enumerate(cards):
        cit = by_name[card["name"]]
        assert cit["role"] == card["role"]
        assert cit["charter"] == card["charter"]
        assert cit["values"] == card["values"]
        # A card carries no OCEAN — the deterministic spread still supplies it.
        assert cit["ocean"] == _ocean_for(i)


def test_no_cards_seeds_generically_exactly_as_before(client) -> None:
    uni = create_universe(client)  # the bundled Dust seed, 5 founders, no cards
    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]
    assert len(citizens) == 5
    for c in citizens:
        assert c["charter"] is None, "a generated founder writes its OWN charter on tick 1"
        assert c["values"] == ["survival", "fairness"]
        assert c["name"] in _FOUNDER_NAMES


def test_a_card_charter_that_fails_moderation_rejects_and_writes_nothing(client) -> None:
    cards = _cards(2)
    cards[1]["charter"] = "the rule here is kill yourself"
    res = client.post(
        "/terrarium/universes",
        json={"physics": dust_physics(founders=2, founder_cards=cards)},
    )
    assert res.status_code == 422, res.text
    # Nothing was written: the universe never came into being.
    assert client.get("/terrarium/universes").json()["universes"] == []


async def test_the_soul_on_disk_carries_the_card_charter(client) -> None:
    cards = _cards(2)
    uni = create_universe(client, founders=2, founder_cards=cards)
    citizens = client.get(f"/terrarium/universes/{uni['id']}/citizens").json()["citizens"]

    for card in cards:
        cit = next(c for c in citizens if c["name"] == card["name"])
        assert cit["soul_path"] and Path(cit["soul_path"]).exists()
        memories = await _soul_recall(cit["soul_path"], card["charter"])
        assert any(card["charter"] in m for m in memories), (
            "a card founder's soul must carry the charter its creator gave it"
        )
