# __init__.py — Public surface for the surface-context entity.
#
# Created: 2026-05-24 — Re-exports the resolver and the two domain
# value objects every consumer needs. Handlers stay private to the
# sub-package; callers don't import them directly.
#
# Changes: 2026-06-05 (feat/surface-profile-bias-kill) — also re-export the
# new ``SurfaceProfile`` descriptor and its ``resolve_profile`` resolver so
# the chat agent_service can gate the ripple block on the per-surface policy
# (the "ripple-default bias" fix) via the package's public surface.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import (
    SurfaceContext,
    SurfaceKind,
    SurfaceMeta,
    SurfaceProfile,
)
from pocketpaw_ee.cloud.surface.service import resolve_profile, resolve_surface_context

__all__ = [
    "SurfaceContext",
    "SurfaceKind",
    "SurfaceMeta",
    "SurfaceProfile",
    "resolve_surface_context",
    "resolve_profile",
]
