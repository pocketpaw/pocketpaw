"""Re-export shim. Canonical home moved to ``ee.cloud._core.realtime.bus``
in Phase 5 of the cloud-restructure (2026-04-27).

The ``EventBus`` Protocol is also re-exported via ``_core.ports`` for
consumers that want to import it as a port.
"""

from pocketpaw_ee.cloud._core.realtime.bus import (  # noqa: F401
    EventBus,
    InProcessBus,
    get_bus,
    get_resolver,
    set_bus,
    set_resolver,
)
