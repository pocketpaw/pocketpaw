# ee/pocketpaw_ee/terrarium/events.py
#
# The realtime topics the terrarium service emits. Thin ``Event`` subclasses
# keyed by an ``EVENT_TYPE`` discriminator — the base class auto-registers each
# one into ``EVENT_REGISTRY`` on definition, which is what makes them show up
# for the frontend's generated topic list.
#
# The payload for EVERY topic here is ``{universe_id, event}`` where ``event``
# is the contract's one Event shape, plus ``workspace_id`` for the audience
# resolver's workspace fan-out (mirroring ``belt_plan``).
#
# ``world.era`` carries the rung change (camp -> town ...), the one system row
# a viewer wants a banner for rather than a feed line. Resource rows (harvest,
# raid) and trade rows (offer, accept) ride ``world.act`` explicitly: offers are
# public like balances.
#
# NOTE: registration happens at IMPORT time, so this module must be reachable
# from app boot — it is, via service.py ← router.py ← cloud/__init__.

"""Terrarium realtime topics (``world.*``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pocketpaw_ee.cloud._core.realtime.events import Event


@dataclass
class WorldTick(Event):
    EVENT_TYPE: ClassVar[str] = "world.tick"


@dataclass
class WorldAct(Event):
    EVENT_TYPE: ClassVar[str] = "world.act"


@dataclass
class WorldThought(Event):
    EVENT_TYPE: ClassVar[str] = "world.thought"


@dataclass
class WorldWeather(Event):
    EVENT_TYPE: ClassVar[str] = "world.weather"


@dataclass
class WorldGate(Event):
    EVENT_TYPE: ClassVar[str] = "world.gate"


@dataclass
class WorldSpawn(Event):
    EVENT_TYPE: ClassVar[str] = "world.spawn"


@dataclass
class WorldHibernate(Event):
    EVENT_TYPE: ClassVar[str] = "world.hibernate"


@dataclass
class WorldLedger(Event):
    EVENT_TYPE: ClassVar[str] = "world.ledger"


@dataclass
class WorldMoment(Event):
    EVENT_TYPE: ClassVar[str] = "world.moment"


@dataclass
class WorldEra(Event):
    EVENT_TYPE: ClassVar[str] = "world.era"


# Journal event kind -> the topic it rides. ``think`` is the only kind that
# gets its own topic (thoughts are the cheap, high-volume stream a viewer can
# turn off) and ``moment`` gets one because it is the stream a stranger watches
# instead of the raw feed; every other citizen act shares ``world.act``.
KIND_TOPIC: dict[str, type[Event]] = {
    "think": WorldThought,
    "moment": WorldMoment,
    "weather": WorldWeather,
    "gate": WorldGate,
    "spawn": WorldSpawn,
    "hibernate": WorldHibernate,
    "era": WorldEra,
    # Resource rows are things that happened to a citizen; they ride the act feed.
    "harvest": WorldAct,
    "raid": WorldAct,
    # Trade offers are public like balances: they ride the act feed too.
    "offer": WorldAct,
    "accept": WorldAct,
}

TERRARIUM_TOPICS: tuple[str, ...] = (
    "world.tick",
    "world.act",
    "world.thought",
    "world.weather",
    "world.gate",
    "world.spawn",
    "world.hibernate",
    "world.ledger",
    "world.moment",
    "world.era",
)


def topic_for(kind: str) -> type[Event]:
    """The Event class a Journal row of this kind is published on."""
    return KIND_TOPIC.get(kind, WorldAct)


__all__ = [
    "KIND_TOPIC",
    "TERRARIUM_TOPICS",
    "WorldAct",
    "WorldEra",
    "WorldGate",
    "WorldHibernate",
    "WorldLedger",
    "WorldMoment",
    "WorldSpawn",
    "WorldThought",
    "WorldTick",
    "WorldWeather",
    "topic_for",
]
