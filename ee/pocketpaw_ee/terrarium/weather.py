# ee/pocketpaw_ee/terrarium/weather.py
#
# WEATHER — the only way a viewer touches a universe. Five named world events:
# rain, drought, storm, omen, revive. Powers are COLLECTIVE: viewers pledge
# tokens per kind until the threshold is crossed, then the event fires once,
# the pledge resets, and the firing is journalled as a ``weather`` Event.
#
# THE HARD BOUNDARY, enforced structurally rather than by review:
# weather acts on the WORLD, never on a MIND. This module is PURE — it imports
# no soul module, no Beanie document, no service. Its whole output is a
# ``WeatherEffect`` value object with four fields: a pool delta, a think-cost
# multiplier duration, one unsigned broadcast line, and a list of hibernating
# citizens whose debt is cleared. There is no field, and no import, through
# which a god could edit a soul, DNA, a charter, or force a citizen's action.
# ``tests/ee/terrarium/test_weather.py`` asserts the absence of those imports,
# so adding one is a test failure and not merely a review comment.

"""Weather — collective viewer powers that act on the world, never on a mind."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WEATHER_KINDS: tuple[str, ...] = ("rain", "drought", "storm", "omen", "revive")

# Tokens a power needs before it fires. Deliberately module-level rather than
# per-physics: the god surface is priced in viewer tokens, not world credits.
POWER_COSTS: dict[str, int] = {
    "rain": 20,
    "drought": 20,
    "storm": 30,
    "omen": 10,
    "revive": 50,
}

# What rain gives and drought takes from the pool, and how long a storm lasts.
RAIN_POOL_DELTA = 200
DROUGHT_POOL_DELTA = -200
STORM_TICKS = 6


class WeatherError(ValueError):
    """An unknown power, or one this universe's physics forbids."""


@dataclass(frozen=True)
class WeatherEffect:
    """Everything a fired power is allowed to change. Nothing else is reachable.

    ``pool_delta`` moves the world pool. ``storm_ticks`` is how many ticks the
    think cost stays doubled. ``broadcast_line`` is one unsigned line entering
    the world as an outside voice (it rides the write-policy like any viewer
    text). ``clear_debt_for`` names hibernating citizens whose debt is paid.
    """

    kind: str
    body: str
    pool_delta: int = 0
    storm_ticks: int = 0
    broadcast_line: str | None = None
    clear_debt_for: list[str] = field(default_factory=list)


def pledge(
    pledges: dict[str, dict[str, Any]],
    kind: str,
    tokens: int,
    god: str,
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Add a pledge. Returns the new pledge state and whether the power FIRED.

    Firing resets that power's pledge to zero — the next event has to be paid
    for again. Pledges are per-universe and per-kind.
    """
    if kind not in WEATHER_KINDS:
        raise WeatherError(f"unknown weather kind {kind!r}; known: {list(WEATHER_KINDS)}")
    if tokens <= 0:
        raise WeatherError("a pledge must be at least 1 token")

    new = {
        k: {"tokens": int(v.get("tokens", 0)), "gods": list(v.get("gods", []))}
        for k, v in pledges.items()
    }
    row = new.setdefault(kind, {"tokens": 0, "gods": []})
    row["tokens"] = int(row["tokens"]) + int(tokens)
    if god and god not in row["gods"]:
        row["gods"].append(god)

    if row["tokens"] >= POWER_COSTS[kind]:
        new[kind] = {"tokens": 0, "gods": []}
        return new, True
    return new, False


def powers(
    pledges: dict[str, dict[str, Any]], allowed: set[str] | None = None
) -> list[dict[str, Any]]:
    """The ``GET /weather`` rows: cost, pledged so far, how many gods, readiness."""
    rows = []
    for kind in WEATHER_KINDS:
        if allowed is not None and kind not in allowed:
            continue
        row = pledges.get(kind) or {}
        pledged = int(row.get("tokens", 0))
        rows.append(
            {
                "kind": kind,
                "cost": POWER_COSTS[kind],
                "pledged": pledged,
                "gods": len(row.get("gods", [])),
                "ready": pledged >= POWER_COSTS[kind],
            }
        )
    return rows


def effect(
    kind: str,
    *,
    line: str | None = None,
    hibernating_ids: list[str] | None = None,
) -> WeatherEffect:
    """Build the effect of a fired power. This is the FULL extent of a god's reach.

    Note what is NOT here and cannot be added without changing the value object
    every caller reads: no citizen id to re-personalise, no soul path, no
    charter text, no forced verb. ``omen`` gets exactly one unsigned line, and
    that line still enters citizens through the write-policy as an unverified
    outside voice.
    """
    if kind not in WEATHER_KINDS:
        raise WeatherError(f"unknown weather kind {kind!r}; known: {list(WEATHER_KINDS)}")

    if kind == "rain":
        return WeatherEffect(kind, "rain fell on the world", pool_delta=RAIN_POOL_DELTA)
    if kind == "drought":
        return WeatherEffect(kind, "the spring ran low", pool_delta=DROUGHT_POOL_DELTA)
    if kind == "storm":
        return WeatherEffect(
            kind, "a storm rolled in — thinking costs double", storm_ticks=STORM_TICKS
        )
    if kind == "omen":
        text = " ".join(str(line or "").split())[:280] or "something is coming"
        return WeatherEffect(kind, "an omen was spoken", broadcast_line=text)
    # revive
    ids = list(hibernating_ids or [])
    return WeatherEffect(
        kind,
        f"{len(ids)} hibernating soul(s) had their debt cleared",
        clear_debt_for=ids,
    )


__all__ = [
    "DROUGHT_POOL_DELTA",
    "POWER_COSTS",
    "RAIN_POOL_DELTA",
    "STORM_TICKS",
    "WEATHER_KINDS",
    "WeatherEffect",
    "WeatherError",
    "effect",
    "pledge",
    "powers",
]
