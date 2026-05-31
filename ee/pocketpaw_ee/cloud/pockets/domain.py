"""Domain value objects for the pockets module.

Pure-Python frozen dataclasses. ``pockets/service.py`` owns the
conversion between these and the Beanie ``Pocket`` / ``Widget``
documents — domain objects are what every consumer outside the service
sees on read paths.

Updated: 2026-05-28 (feat/wave-3e-template-slug) — added optional
``Pocket.template_slug`` so the wire layer + bulk dispatcher can read
the RFC 03 v2 template the pocket was instantiated from. ``None`` for
legacy pockets.
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

    Pocket field uses tuples for hashability. ``config``, ``props``,
    ``data``, and ``spec`` carry arbitrary JSON which we keep as ``Any`` /
    ``dict`` (frozen dataclasses don't enforce immutability at deeper
    nesting).

    ``type`` is free-form. A widget with ``type="native"`` is a "native"
    widget — the frontend renders it as a built-in Svelte component looked
    up by ``name``, rather than from a rippleSpec. Native widgets carry no
    spec, so they are never manifest-validated.

    ``spec`` is an optional Ripple rippleSpec subtree for this single tile
    (e.g. a ``chart`` node with a real ``data`` series). The home grid
    renders a tile from its ``spec`` when present; ``None`` for native
    widgets.
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
    spec: dict[str, Any] | None = None


@dataclass(frozen=True)
class Pocket:
    """Pocket workspace value object.

    Updated: 2026-05-16 — added optional ``project_id`` so pockets can be
    grouped under a Mission Control Project. Optional (default None) so
    existing pocket records — and callers that don't care about projects —
    keep working unchanged.

    ``type`` is free-form. ``type="home"`` marks the per-user pocket that
    backs the home page — it behaves like an ordinary private pocket; the
    type is just a marker the home route and frontend key on.
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
    # RFC 03 v2 (Wave 3e) — the bundled-template slug the pocket was
    # instantiated from (e.g. ``"todo-task-tracker"``). ``None`` for
    # cold-generated or legacy pockets.
    template_slug: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["Pocket", "Widget", "WidgetPosition"]
