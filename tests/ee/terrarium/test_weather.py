# tests/ee/terrarium/test_weather.py — god powers act on the WORLD, never on a
# MIND. Two kinds of proof here:
#
#   1. BEHAVIOUR — pledges accumulate per universe until the threshold, then the
#      power fires exactly once and the pledge resets.
#   2. STRUCTURE — weather.py cannot reach a soul. Asserted by inspecting the
#      module's actual imports and the WeatherEffect field set, so a future
#      "just let a god nudge a citizen" edit is a red test, not a review note.
#      (An import-linter contract in ee/pyproject.toml pins the same rule for
#      CI; this is the runnable half.)
#   3. PLACE — weather lands somewhere on the 120x80 map, deterministically,
#      and the body names the same region the cell sits in.

from __future__ import annotations

import dataclasses
import inspect

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.terrarium import weather  # noqa: E402

# --- behaviour ------------------------------------------------------------


def test_a_pledge_below_threshold_does_not_fire():
    pledges, fired = weather.pledge({}, "rain", 5, "u1")
    assert fired is False
    assert pledges["rain"]["tokens"] == 5
    assert pledges["rain"]["gods"] == ["u1"]


def test_pledges_accumulate_across_gods_then_fire_and_reset():
    pledges, fired = weather.pledge({}, "rain", 12, "u1")
    assert fired is False
    pledges, fired = weather.pledge(pledges, "rain", 8, "u2")
    assert fired is True
    assert pledges["rain"] == {"tokens": 0, "gods": []}


def test_pledging_one_power_does_not_move_another():
    pledges, _ = weather.pledge({}, "rain", 15, "u1")
    pledges, fired = weather.pledge(pledges, "storm", 5, "u1")
    assert fired is False
    assert pledges["rain"]["tokens"] == 15
    assert pledges["storm"]["tokens"] == 5


def test_the_same_god_pledging_twice_is_counted_once():
    pledges, _ = weather.pledge({}, "omen", 3, "u1")
    pledges, _ = weather.pledge(pledges, "omen", 3, "u1")
    assert pledges["omen"]["gods"] == ["u1"]
    assert pledges["omen"]["tokens"] == 6


def test_unknown_power_and_non_positive_pledge_are_rejected():
    with pytest.raises(weather.WeatherError, match="unknown weather kind"):
        weather.pledge({}, "earthquake", 5, "u1")
    with pytest.raises(weather.WeatherError, match="at least 1 token"):
        weather.pledge({}, "rain", 0, "u1")


def test_powers_report_cost_pledged_gods_and_readiness():
    pledges, _ = weather.pledge({}, "omen", weather.POWER_COSTS["omen"] - 1, "u1")
    rows = {row["kind"]: row for row in weather.powers(pledges)}
    assert set(rows) == set(weather.WEATHER_KINDS)
    assert rows["omen"]["pledged"] == weather.POWER_COSTS["omen"] - 1
    assert rows["omen"]["gods"] == 1
    assert rows["omen"]["ready"] is False
    assert rows["rain"]["cost"] == weather.POWER_COSTS["rain"]


def test_each_power_touches_only_what_it_is_allowed_to():
    assert weather.effect("rain").pool_delta > 0
    assert weather.effect("drought").pool_delta < 0
    assert weather.effect("storm").storm_ticks > 0
    assert (
        weather.effect("omen", line="a light in the east").broadcast_line == "a light in the east"
    )
    assert weather.effect("revive", hibernating_ids=["c1", "c2"]).clear_debt_for == ["c1", "c2"]


def test_an_empty_omen_still_says_something_and_is_length_capped():
    assert weather.effect("omen", line=None).broadcast_line
    long_line = "x" * 1000
    assert len(weather.effect("omen", line=long_line).broadcast_line) <= 280


# --- place: weather happens somewhere -------------------------------------


def _spot(name: str, x: float, y: float, yield_: int = 0) -> weather.Spot:
    return weather.Spot(name, x, y, yield_)


def _world() -> weather.Snapshot:
    return weather.Snapshot(
        citizens=[_spot("ada", 20, 20), _spot("bo", 100, 60)],
        structures=[
            _spot("north farm", 30, 10, yield_=5),
            _spot("south field", 30, 70, yield_=1),
            _spot("the well", 60, 40),
            _spot("house a", 100, 40),
            _spot("house b", 105, 42),
            _spot("house c", 110, 38),
            _spot("hut d", 10, 40),
        ],
        revived=_spot("bo", 100, 60),
    )


def test_rain_lands_on_the_lowest_yield_farm_and_the_body_says_so():
    place = weather.weather_place("rain", _world())
    assert place is not None
    assert (place.x, place.y, place.radius) == (30.0, 70.0, 18.0)
    assert weather.effect("rain", place=place).body == "RAIN · the south field drinks"


def test_drought_starts_at_the_well():
    place = weather.weather_place("drought", _world())
    assert place is not None
    assert (place.x, place.y, place.radius) == (60.0, 40.0, 22.0)
    assert weather.effect("drought", place=place).body == "DROUGHT · the well drops a hand"


def test_storm_centres_on_the_densest_houses():
    place = weather.weather_place("storm", _world())
    assert place is not None
    assert (place.x, place.y, place.radius) == (105.0, 40.0, 28.0)
    body = weather.effect("storm", place=place).body
    assert body.startswith("STORM · ") and "the east towns" in body


def test_revive_centres_on_the_woken_citizen():
    place = weather.weather_place("revive", _world())
    assert place is not None
    assert (place.x, place.y, place.radius) == (100.0, 60.0, 6.0)
    body = weather.effect("revive", hibernating_ids=["c1"], place=place).body
    assert body.startswith("REVIVE · 1 hibernating soul(s)") and "the south field" in body


def test_an_omen_has_no_place():
    assert weather.weather_place("omen", _world()) is None
    assert weather.effect("omen", line="x").body == "OMEN · an omen was spoken"


@pytest.mark.parametrize("kind", ["rain", "storm", "revive"])
def test_the_body_names_the_third_of_the_map_the_cell_sits_in(kind):
    place = weather.weather_place(kind, _world())
    assert place is not None
    assert weather.region(place.x, place.y) in weather.effect(kind, place=place).body


def test_the_cardinal_split_is_thirds_of_the_map():
    assert weather.region(60, 0) == "the north field"
    assert weather.region(60, 26) == "the north field"
    assert weather.region(60, 27) == "the commons"
    assert weather.region(60, 53) == "the commons"
    assert weather.region(60, 54) == "the south field"
    assert weather.region(39, 40) == "the shore"
    assert weather.region(80, 40) == "the east towns"


def test_the_place_is_deterministic_for_a_fixed_world():
    for kind in weather.WEATHER_KINDS:
        assert weather.weather_place(kind, _world()) == weather.weather_place(kind, _world())


def test_radius_by_kind():
    got = {
        k: weather.weather_place(k, _world()).radius for k in ("rain", "storm", "drought", "revive")
    }
    assert got == {"rain": 18.0, "storm": 28.0, "drought": 22.0, "revive": 6.0}


def test_a_world_with_no_farms_and_no_well_still_gets_a_place():
    bare = weather.Snapshot(citizens=[_spot("ada", 20, 20), _spot("bo", 100, 60)])
    for kind in ("rain", "drought", "storm", "revive"):
        place = weather.weather_place(kind, bare)
        assert place is not None
        assert (place.x, place.y) == (60.0, 40.0), kind
        assert "the commons" in weather.effect(kind, place=place).body
    assert weather.effect("drought", place=weather.weather_place("drought", bare)).body == (
        "DROUGHT · the commons runs dry"
    )
    assert weather.weather_place("rain", weather.Snapshot()) is not None


# --- structure: a god power cannot reach a mind ---------------------------


def test_weather_module_imports_nothing_that_could_touch_a_soul():
    """The structural half of the never-edit-a-soul rule.

    weather.py is PURE. If someone adds ``from ... import soul_link`` (or the
    service, or soul_protocol itself) to give a god a way to nudge a citizen,
    this fails — before the code ships, not after a viewer edits a mind.
    """
    source = inspect.getsource(weather)
    for forbidden in ("soul_link", "soul_protocol", "terrarium.service", "domain import"):
        assert forbidden not in source, f"weather.py must not reference {forbidden!r}"
    # And nothing soul-shaped resolved into its namespace at import time.
    assert not [n for n in vars(weather) if "soul" in n.lower()]


def test_weather_effect_carries_no_field_that_could_edit_a_mind():
    """The effect object IS the full extent of a god's reach. Four world knobs,
    nothing per-citizen except a debt clear (which is credits, not a mind)."""
    fields = {f.name for f in dataclasses.fields(weather.WeatherEffect)}
    assert fields == {
        "kind",
        "body",
        "pool_delta",
        "storm_ticks",
        "broadcast_line",
        "clear_debt_for",
    }
    for forbidden in ("soul", "dna", "charter", "ocean", "memory", "verb", "act"):
        assert not any(forbidden in f for f in fields), (
            f"WeatherEffect must not carry {forbidden!r}"
        )
