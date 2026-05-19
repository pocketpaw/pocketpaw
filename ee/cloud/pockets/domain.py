"""Domain value objects for the pockets module.

Pure-Python frozen dataclasses. ``pockets/service.py`` owns the
conversion between these and the Beanie ``Pocket`` / ``Widget``
documents — domain objects are what every consumer outside the service
sees on read paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class WidgetPosition:
    row: int = 0
    col: int = 0


@dataclass(frozen=True)
class Widget:
    """Widget subdocument inside a Pocket.

    Pocket field uses tuples for hashability. ``config``, ``props``, and
    ``data`` carry arbitrary JSON which we keep as ``Any`` (frozen
    dataclasses don't enforce immutability at deeper nesting).
    """

    id: str
    name: str
    type: str
    icon: str
    color: str
    span: str
    data_source_type: str
    config: tuple[tuple[str, Any], ...]
    props: tuple[tuple[str, Any], ...]
    data: Any
    assigned_agent: str | None
    position: WidgetPosition


@dataclass(frozen=True)
class Pocket:
    """Pocket workspace value object.

    Updated: 2026-05-16 — added optional ``project_id`` so pockets can be
    grouped under a Mission Control Project. Optional (default None) so
    existing pocket records — and callers that don't care about projects —
    keep working unchanged.
    """

    id: str
    workspace_id: str
    name: str
    description: str
    type: str
    icon: str
    color: str
    owner: str
    visibility: str  # private | workspace | public
    team: tuple[str, ...]
    agents: tuple[str, ...]
    widgets: tuple[Widget, ...]
    ripple_spec: dict[str, Any] | None
    share_link_token: str | None
    share_link_access: str  # view | comment | edit
    shared_with: tuple[str, ...]
    tool_specs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    project_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["Pocket", "Widget", "WidgetPosition"]
