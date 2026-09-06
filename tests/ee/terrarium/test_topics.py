# tests/ee/terrarium/test_topics.py — the eight ``world.*`` realtime topics.
#
# Registration happens at IMPORT time via ``Event.__init_subclass__``, so what
# actually has to hold is (a) every contract topic is defined and registered,
# and (b) the module is reachable from app boot — service.py imports it, and
# the router imports service, so mounting the router pulls it in.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud._core.realtime.events import EVENT_REGISTRY  # noqa: E402
from pocketpaw_ee.terrarium import events as world_events  # noqa: E402


def test_every_contract_topic_is_registered():
    assert world_events.TERRARIUM_TOPICS == (
        "world.tick",
        "world.act",
        "world.thought",
        "world.weather",
        "world.gate",
        "world.spawn",
        "world.hibernate",
        "world.ledger",
    )
    for topic in world_events.TERRARIUM_TOPICS:
        assert topic in EVENT_REGISTRY, f"{topic} never reached EVENT_REGISTRY"


def test_importing_the_router_registers_the_topics():
    """The reachability half — mounting the router is what pulls events.py in."""
    import importlib

    importlib.import_module("pocketpaw_ee.terrarium.router")
    assert "world.tick" in EVENT_REGISTRY


def test_journal_kinds_map_onto_the_right_topic():
    assert world_events.topic_for("think") is world_events.WorldThought
    assert world_events.topic_for("weather") is world_events.WorldWeather
    assert world_events.topic_for("gate") is world_events.WorldGate
    assert world_events.topic_for("spawn") is world_events.WorldSpawn
    assert world_events.topic_for("hibernate") is world_events.WorldHibernate
    # Everything a citizen DOES shares world.act.
    for kind in ("say", "write", "craft", "build", "explore", "vote", "trade", "arrive"):
        assert world_events.topic_for(kind) is world_events.WorldAct


def test_the_audience_resolver_fans_world_events_to_the_workspace():
    import asyncio

    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver

    async def members(wid: str) -> list[str]:
        return ["u1", "u2"] if wid == "ws1" else []

    resolver = AudienceResolver(workspace_members=members)
    evt = world_events.WorldTick(data={"workspace_id": "ws1", "universe_id": "u", "event": {}})
    assert asyncio.run(resolver.audience(evt)) == ["u1", "u2"]

    # No workspace on the payload = nobody. Fail-closed.
    orphan = world_events.WorldAct(data={"universe_id": "u", "event": {}})
    assert asyncio.run(resolver.audience(orphan)) == []
