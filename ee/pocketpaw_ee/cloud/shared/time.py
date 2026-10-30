"""Re-export shim. Canonical home moved to ``ee.cloud._core.time``
in Phase 1 of the cloud-restructure (2026-04-27). This module remains
so existing imports keep working; new code should import from ``_core``.
"""

from pocketpaw_ee.cloud._core.time import iso_utc

__all__ = ["iso_utc"]
