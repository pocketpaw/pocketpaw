# __init__.py — Public surface for the surface-context entity.
#
# Created: 2026-05-24 — Re-exports the resolver and the two domain
# value objects every consumer needs. Handlers stay private to the
# sub-package; callers don't import them directly.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceContext, SurfaceKind, SurfaceMeta
from pocketpaw_ee.cloud.surface.service import resolve_surface_context

__all__ = [
    "SurfaceContext",
    "SurfaceKind",
    "SurfaceMeta",
    "resolve_surface_context",
]
