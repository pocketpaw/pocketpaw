"""Re-export shim. Canonical home moved to ``ee.cloud._core.errors``
in Phase 1 of the cloud-restructure (2026-04-27). This module remains
so existing imports keep working; new code should import from ``_core``.
"""

from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
    ValidationError,
)

__all__ = [
    "CloudError",
    "ConflictError",
    "Forbidden",
    "NotFound",
    "SeatLimitError",
    "ValidationError",
]
