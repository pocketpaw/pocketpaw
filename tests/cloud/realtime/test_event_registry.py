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


def test_every_event_type_has_exactly_one_owning_class():
    """Two classes sharing an EVENT_TYPE make the registry order-dependent:
    ``__init_subclass__`` keeps whichever module imported last, so
    ``rebuild_event`` and the registry test change with test ordering.
    Import every module that declares events, then require one owner per type.
    """
    import importlib
    import re
    from collections import defaultdict
    from pathlib import Path

    import pocketpaw_ee

    # Explicit: the pair that collided on meeting.* before the fix.
    import pocketpaw_ee.cloud._core.realtime.events  # noqa: F401
    import pocketpaw_ee.cloud.meetings.events  # noqa: F401

    # Plus every other module that subclasses Event, found by a source scan so
    # we import only those (walking every package drags in the whole app).
    root = Path(pocketpaw_ee.__path__[0])
    declaring: set[str] = set()
    for path in root.rglob("*.py"):
        if re.search(r"^class \w+\((\w+\.)?Event\):", path.read_text(), re.M):
            rel = path.relative_to(root.parent).with_suffix("")
            declaring.add(".".join(rel.parts))
    for name in declaring:
        importlib.import_module(name)

    # Each domain module must be on rebuild_event's lazy-load list, or a
    # process that never imported it rebuilds its events as the base Event.
    from pocketpaw_ee.cloud._core.realtime.events import _DOMAIN_EVENT_MODULES

    unlisted = declaring - {Event.__module__} - set(_DOMAIN_EVENT_MODULES)
    assert not unlisted, f"add to _DOMAIN_EVENT_MODULES: {sorted(unlisted)}"

    owners: dict[str, list[str]] = defaultdict(list)
    for cls in _all_event_subclasses(Event):
        evt_type = getattr(cls, "EVENT_TYPE", "")
        if evt_type and cls.__module__.startswith("pocketpaw"):
            owners[evt_type].append(f"{cls.__module__}.{cls.__qualname__}")

    dupes = {t: names for t, names in owners.items() if len(names) > 1}
    assert not dupes, f"EVENT_TYPE declared by more than one class: {dupes}"


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


@pytest.mark.parametrize(
    ("evt_type", "owner"),
    [
        ("meeting.scheduled", "pocketpaw_ee.cloud.meetings.events.MeetingScheduled"),
        ("mandate.created", "pocketpaw_ee.cloud.mandates.events.MandateCreated"),
    ],
)
def test_rebuild_event_finds_owner_module_not_yet_imported(evt_type, owner):
    """An arq worker imports only ``_core.realtime`` and still receives
    envelopes for events whose class lives in a domain module. A fresh
    interpreter proves ``rebuild_event`` loads that module instead of
    falling back to the base Event."""
    import subprocess
    import sys

    code = (
        "from pocketpaw_ee.cloud._core.realtime.events import rebuild_event\n"
        f"e = rebuild_event({{'type': {evt_type!r}, 'data': {{'workspace_id': 'w1'}}}})\n"
        "print(f'{type(e).__module__}.{type(e).__qualname__}')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert out.stdout.strip().splitlines()[-1] == owner
