# System events SSE schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel


class SystemEventData(BaseModel):
    """A system event delivered over SSE."""

    event_type: str
    content: str = ""
    metadata: dict = {}
