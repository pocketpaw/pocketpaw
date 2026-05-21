"""Identity tests for the Phase 5 realtime shim.

The realtime subsystem moved from ``ee.cloud.realtime.*`` to
``ee.cloud._core.realtime.*``. The old paths remain as re-export shims.
These tests assert that imports through both paths resolve to the same
Python objects.
"""

from __future__ import annotations


def test_bus_module_identity() -> None:
    from pocketpaw_ee.cloud._core.realtime import bus as core_bus
    from pocketpaw_ee.cloud.realtime import bus as shim_bus

    assert shim_bus.EventBus is core_bus.EventBus
    assert shim_bus.InProcessBus is core_bus.InProcessBus
    assert shim_bus.get_bus is core_bus.get_bus
    assert shim_bus.set_bus is core_bus.set_bus
    assert shim_bus.get_resolver is core_bus.get_resolver
    assert shim_bus.set_resolver is core_bus.set_resolver


def test_emit_identity() -> None:
    from pocketpaw_ee.cloud._core.realtime.emit import emit as core_emit
    from pocketpaw_ee.cloud.realtime.emit import emit as shim_emit

    assert shim_emit is core_emit


def test_audience_identity() -> None:
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver as core_AR
    from pocketpaw_ee.cloud.realtime.audience import AudienceResolver as shim_AR

    assert shim_AR is core_AR


def test_events_identity() -> None:
    from pocketpaw_ee.cloud._core.realtime import events as core_events
    from pocketpaw_ee.cloud.realtime import events as shim_events

    # Sample several event classes
    for name in (
        "Event",
        "WorkspaceUpdated",
        "NotificationNew",
        "MessageNew",
        "GroupCreated",
    ):
        assert getattr(shim_events, name) is getattr(core_events, name), name


# The Phase 5 design re-exported EventBus through _core.ports as a
# "port" location, but no production code imports it from there — the
# canonical home is _core.realtime.bus. The cleanup pass dropped both
# _core.ports.py and _core.repository.py because they had zero callers
# in production code.
