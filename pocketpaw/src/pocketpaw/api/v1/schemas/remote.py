# Remote tunnel schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel


class TunnelStatusResponse(BaseModel):
    """Tunnel status."""

    active: bool = False
    url: str | None = None


class TunnelStartResponse(BaseModel):
    """Tunnel start result."""

    active: bool
    url: str | None = None
    error: str | None = None
