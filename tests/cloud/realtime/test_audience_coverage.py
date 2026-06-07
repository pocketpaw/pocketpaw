"""Coverage test: every Event subclass must resolve to a list without raising.

This catches a whole class of drift: add a new Event subclass, forget to add its
branch to AudienceResolver, and the coverage test fails. Room-scoped events
(typing.*) intentionally return [] — they're routed by ConnectionManager, not
the bus — so they pass too.
"""

from __future__ import annotations

import inspect

import pytest
from pocketpaw_ee.cloud._core.realtime import events as ev_mod
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.events import Event


async def _members(_key: str) -> list[str]:
    return ["u1"]


def _all_event_subclasses() -> list[type[Event]]:
    return [
        cls
        for _name, cls in inspect.getmembers(ev_mod, inspect.isclass)
        if issubclass(cls, Event) and cls is not Event
    ]


@pytest.mark.asyncio
async def test_every_event_subclass_resolves_to_list():
    """Every typed Event must be handleable by AudienceResolver without raising."""
    resolver = AudienceResolver(
        group_members=_members,
        workspace_members=_members,
        workspace_admins=_members,
        workspace_peers=_members,
    )
    subclasses = _all_event_subclasses()
    assert subclasses, "no Event subclasses discovered"

    # Payload fields the resolver touches across all branches.
    common_payload = {
        "group_id": "g1",
        "user_id": "u1",
        "sender_id": "u1",
        "peer_id": "p1",
        "workspace_id": "w1",
        "invite_id": "i1",
        "member_ids": ["u1", "u2"],
        "message_id": "m1",
        "emoji": "x",
        "file_id": "f1",
        "id": "n1",
        "kind": "mention",
    }

    for cls in subclasses:
        ev = cls(data=dict(common_payload))
        result = await resolver.audience(ev)
        assert isinstance(result, list), f"{cls.__name__}: expected list, got {type(result)}"


def test_subclasses_have_unique_wire_types():
    """No two Event subclasses may share the same EVENT_TYPE string."""
    seen: dict[str, str] = {}
    for cls in _all_event_subclasses():
        wire = getattr(cls, "EVENT_TYPE", None)
        assert wire, f"{cls.__name__} missing EVENT_TYPE"
        assert wire not in seen, f"duplicate wire type {wire!r}: {cls.__name__} and {seen[wire]}"
        seen[wire] = cls.__name__
