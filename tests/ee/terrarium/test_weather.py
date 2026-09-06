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
