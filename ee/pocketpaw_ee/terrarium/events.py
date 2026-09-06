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


# Journal event kind -> the topic it rides. ``think`` is the only kind that
# gets its own topic (thoughts are the cheap, high-volume stream a viewer can
# turn off); every other citizen act shares ``world.act``.
KIND_TOPIC: dict[str, type[Event]] = {
    "think": WorldThought,
    "weather": WorldWeather,
    "gate": WorldGate,
    "spawn": WorldSpawn,
    "hibernate": WorldHibernate,
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
)


def topic_for(kind: str) -> type[Event]:
    """The Event class a Journal row of this kind is published on."""
    return KIND_TOPIC.get(kind, WorldAct)


__all__ = [
    "KIND_TOPIC",
    "TERRARIUM_TOPICS",
    "WorldAct",
    "WorldGate",
    "WorldHibernate",
    "WorldLedger",
    "WorldSpawn",
    "WorldThought",
    "WorldTick",
    "WorldWeather",
    "topic_for",
]
