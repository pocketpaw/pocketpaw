"""Re-export shim. Canonical home moved to ``ee.cloud._core.realtime.emit``
in Phase 5 of the cloud-restructure (2026-04-27)."""

from pocketpaw_ee.cloud._core.realtime.emit import emit  # noqa: F401

__all__ = ["emit"]
