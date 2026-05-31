"""Cross-process bus bridge needs to reconstruct Event subclasses from their
EVENT_TYPE discriminator on the receiving side. A registry populated via
``Event.__init_subclass__`` keeps the mapping in lockstep with the class
hierarchy without manual maintenance."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.realtime.events import (
    EVENT_REGISTRY,
    Event,
    GroupCreated,
    MessageNew,
    PocketCreated,
    rebuild_event,
)


def test_registry_includes_every_subclass_with_event_type():
    # Spot-check a handful that we know exist; the full coverage is the
    # invariant that every subclass with EVENT_TYPE is present.
    assert EVENT_REGISTRY["group.created"] is GroupCreated
    assert EVENT_REGISTRY["message.new"] is MessageNew
    assert EVENT_REGISTRY["pocket.created"] is PocketCreated

    for cls in _all_event_subclasses(Event):
        evt_type = getattr(cls, "EVENT_TYPE", "")
        if not evt_type:
            continue
        assert EVENT_REGISTRY.get(evt_type) is cls, (
            f"{cls.__name__} (EVENT_TYPE={evt_type!r}) missing from EVENT_REGISTRY"
        )


def test_rebuild_event_round_trips_through_dict():
    original = GroupCreated(data={"id": "g1", "name": "team"})
    payload = {
        "type": original.type,
        "data": original.data,
        "ts": original.ts.isoformat(),
    }

    rebuilt = rebuild_event(payload)

    assert isinstance(rebuilt, GroupCreated)
    assert rebuilt.type == "group.created"
    assert rebuilt.data == {"id": "g1", "name": "team"}
    assert rebuilt.ts == original.ts


def test_rebuild_event_unknown_type_returns_generic_event():
    """A future event type shipped by a newer worker shouldn't crash an older
    web consumer — fall back to the base Event so listeners that subscribe
    by string type can still match."""
    now = datetime.now(UTC)
    payload = {"type": "future.event", "data": {"x": 1}, "ts": now.isoformat()}

    rebuilt = rebuild_event(payload)

    assert type(rebuilt) is Event
    assert rebuilt.type == "future.event"
    assert rebuilt.data == {"x": 1}
    assert rebuilt.ts == now


def test_rebuild_event_missing_ts_uses_current_time():
    payload = {"type": "group.created", "data": {}}

    rebuilt = rebuild_event(payload)

    assert isinstance(rebuilt, GroupCreated)
    assert isinstance(rebuilt.ts, datetime)


def test_rebuild_event_rejects_non_dict_data():
    with pytest.raises((TypeError, ValueError)):
        rebuild_event({"type": "group.created", "data": "not-a-dict"})


def _all_event_subclasses(root: type) -> list[type]:
    """All transitive subclasses, deduped."""
    seen: set[type] = set()
    stack = [root]
    out: list[type] = []
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            out.append(sub)
            stack.append(sub)
    return out
