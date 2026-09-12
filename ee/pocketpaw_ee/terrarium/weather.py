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
#
# WEATHER HAPPENS SOMEWHERE. ``weather_place`` picks a cell of the 120x80 tile
# map for a fired power from plain positions the service hands over (rain on
# the lowest-yield farm, drought at the well, storm on the densest houses,
# revive on the woken citizen, omen nowhere). The body names the same region
# the cell sits in, so text and ``data.at`` never disagree.

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

# The map, in tiles, and how far each power reaches from its centre.
MAP_W = 120.0
MAP_H = 80.0
RADIUS: dict[str, float] = {"rain": 18.0, "storm": 28.0, "drought": 22.0, "revive": 6.0}

# Which structures count as what, by name. Citizens name their own buildings.
_FARM_WORDS = ("farm", "field", "orchard", "garden")
_HOUSE_WORDS = ("house", "home", "hut", "cabin", "hall")
# Houses within this many tiles of each other are one cluster.
_CLUSTER_TILES = 20.0


class WeatherError(ValueError):
    """An unknown power, or one this universe's physics forbids."""


@dataclass(frozen=True)
class Spot:
    """A named point in tile space. ``yield_`` is a farm's recent take."""

    name: str
    x: float
    y: float
    yield_: int = 0


@dataclass(frozen=True)
class Snapshot:
    """The plain-value view of a world that ``weather_place`` reads.

    The service reads the docs and hands over positions only — no ids, no
    souls. ``revived`` is the Spot of the citizen a revive wakes, if any.
    """

    citizens: list[Spot] = field(default_factory=list)
    structures: list[Spot] = field(default_factory=list)
    revived: Spot | None = None


@dataclass(frozen=True)
class Place:
    """Where a power lands: a cell of the map, plus the region word for the body."""

    x: float
    y: float
    radius: float
    label: str


def region(x: float, y: float) -> str:
    """The region a tile sits in, in the words the map and the body share.

    Thirds of the map: north and south by height first, then east and the
    shore (west) by width; what is left is the commons around the well.
    """
    if y < MAP_H / 3:
        return "the north field"
    if y >= MAP_H * 2 / 3:
        return "the south field"
    if x >= MAP_W * 2 / 3:
        return "the east towns"
    if x < MAP_W / 3:
        return "the shore"
    return "the commons"


def _centroid(spots: list[Spot]) -> tuple[float, float]:
    if not spots:
        return MAP_W / 2, MAP_H / 2
    return (
        sum(s.x for s in spots) / len(spots),
        sum(s.y for s in spots) / len(spots),
    )


def _named(spots: list[Spot], words: tuple[str, ...]) -> list[Spot]:
    return [s for s in spots if any(w in s.name.lower() for w in words)]


def weather_place(kind: str, snapshot: Snapshot) -> Place | None:
    """Pick the cell a fired power lands on. Pure and deterministic.

    rain: the farm with the lowest recent yield, else the well. drought: the
    well. storm: the densest cluster of houses. revive: the woken citizen.
    omen: nowhere. Every fallback is the centre of the named citizens.
    """
    if kind not in WEATHER_KINDS:
        raise WeatherError(f"unknown weather kind {kind!r}; known: {list(WEATHER_KINDS)}")
    if kind == "omen":
        return None
    wells = _named(snapshot.structures, ("well",))
    well = min(wells, key=lambda s: s.name) if wells else None
    label: str | None = None
    if kind == "rain":
        farms = _named(snapshot.structures, _FARM_WORDS)
        if farms:
            target = min(farms, key=lambda s: (s.yield_, s.name))
            x, y = target.x, target.y
        elif well is not None:
            x, y, label = well.x, well.y, "the well"
        else:
            x, y = _centroid(snapshot.citizens)
    elif kind == "drought":
        if well is not None:
            x, y, label = well.x, well.y, "the well"
        else:
            x, y = _centroid(snapshot.citizens)
    elif kind == "storm":
        houses = _named(snapshot.structures, _HOUSE_WORDS)
        if houses:
            # ponytail: O(n^2) neighbour count; a grid if towns pass ~1k houses.
            def near(h: Spot) -> list[Spot]:
                return [o for o in houses if abs(o.x - h.x) + abs(o.y - h.y) <= _CLUSTER_TILES]

            seed = max(houses, key=lambda h: (len(near(h)), h.name))
            x, y = _centroid(near(seed))
        else:
            x, y = _centroid(snapshot.citizens)
    else:  # revive
        if snapshot.revived is not None:
            x, y = snapshot.revived.x, snapshot.revived.y
        else:
            x, y = _centroid(snapshot.citizens)
    return Place(float(x), float(y), RADIUS[kind], label or region(x, y))


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
    place: Place | None = None,
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

    where = place.label if place is not None else "the world"
    if kind == "rain":
        return WeatherEffect(kind, f"RAIN · {where} drinks", pool_delta=RAIN_POOL_DELTA)
    if kind == "drought":
        body = "the well drops a hand" if where == "the well" else f"{where} runs dry"
        return WeatherEffect(kind, f"DROUGHT · {body}", pool_delta=DROUGHT_POOL_DELTA)
    if kind == "storm":
        return WeatherEffect(
            kind,
            f"STORM · a storm rolls over {where} — thinking costs double",
            storm_ticks=STORM_TICKS,
        )
    if kind == "omen":
        text = " ".join(str(line or "").split())[:280] or "something is coming"
        return WeatherEffect(kind, "OMEN · an omen was spoken", broadcast_line=text)
    # revive
    ids = list(hibernating_ids or [])
    return WeatherEffect(
        kind,
        f"REVIVE · {len(ids)} hibernating soul(s) wake at {where}, debt cleared",
        clear_debt_for=ids,
    )


__all__ = [
    "DROUGHT_POOL_DELTA",
    "MAP_H",
    "MAP_W",
    "POWER_COSTS",
    "RADIUS",
    "RAIN_POOL_DELTA",
    "STORM_TICKS",
    "WEATHER_KINDS",
    "Place",
    "Snapshot",
    "Spot",
    "WeatherEffect",
    "WeatherError",
    "effect",
    "pledge",
    "powers",
    "region",
    "weather_place",
]
