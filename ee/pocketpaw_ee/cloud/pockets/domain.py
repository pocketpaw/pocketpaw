"""Domain value objects for the pockets module.

Pure-Python frozen dataclasses. ``pockets/service.py`` owns the
conversion between these and the Beanie ``Pocket`` / ``Widget``
documents — domain objects are what every consumer outside the service
sees on read paths.

Updated: 2026-05-28 (feat/wave-3e-template-slug) — added optional
``Pocket.template_slug`` so the wire layer + bulk dispatcher can read
the RFC 03 v2 template the pocket was instantiated from. ``None`` for
legacy pockets.
Updated: 2026-06-03 (feat/sites-landing-brain) — added optional
``Pocket.pattern`` so the wire layer + sites generator can read the
layout/conversion intent the pocket was authored as (``"landing"`` for
marketing sites). ``None`` for legacy pockets.
Updated: 2026-06-04 (feat/sites-svelte-engine) — added the Paw Sites
"Svelte track" fields ``Pocket.engine`` (``"ripple"`` | ``"svelte"``) and
``Pocket.source`` (the SvelteKit source map, or ``None``) so the wire
layer + generator can read which track the pocket was built on and, for
svelte sites, the hand-written source to materialize. Defaults
(``engine="ripple"``, ``source=None``) keep legacy pockets unchanged.
Updated: 2026-06-05 (feat/entity-pocket-profile-field, entity-rooms
chunk ②) — added optional ``Pocket.surface_profile`` (the JSON-shaped
per-entity surface-profile override dict, or ``None``) so the wire layer +
the entity-aware resolve_profile (chunk ①) can read an entity pocket's
ripple_mode / tools / skills override. ``None`` for legacy pockets.
Updated: 2026-06-19 (feat/typed-ripplespec-phase2) — widened
``Pocket.ripple_spec`` to ``RippleSpec | dict[str, Any] | None`` during the
typed-rippleSpec transition. ``service._pocket_to_domain`` promotes the stored
flat dict to a typed ``RippleSpec`` on read (promote-on-read; the Beanie field
stays ``dict``, no document migration), falling back to the raw value on a
corrupt/unpromotable spec. Every reader downstream is dual-path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pocketpaw.bundled_templates.schema import RippleSpec


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
    # Phase-2 (feat/typed-ripplespec-phase2): widened to ``RippleSpec | dict``
    # during the transition. ``service._pocket_to_domain`` PROMOTES the stored
    # flat dict to a typed ``RippleSpec`` on read (promote-on-read — the Beanie
    # ``Pocket.rippleSpec`` field stays ``dict``, no document migration). A
    # corrupt/unpromotable spec falls back to the raw stored value, so a bad
    # doc never breaks pocket load. Every downstream reader is dual-path, so it
    # accepts whichever shape it gets.
    ripple_spec: RippleSpec | dict[str, Any] | None
    share_link_token: str | None
    share_link_access: str  # view | comment | edit
    shared_with: tuple[str, ...]
    tool_specs: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    project_id: str | None = None
    # RFC 03 v2 (Wave 3e) — the bundled-template slug the pocket was
    # instantiated from (e.g. ``"todo-task-tracker"``). ``None`` for
    # cold-generated or legacy pockets.
    template_slug: str | None = None
    # The create-pocket layout pattern the pocket was authored as
    # (``"dashboard"`` | ``"viewer"`` | ``"app"`` | ``"landing"`` | ...).
    # ``"landing"`` marks a marketing site so the generator renders a
    # landing page. ``None`` for legacy pockets.
    pattern: str | None = None
    # Paw Sites generation track: ``"ripple"`` (default) compiles
    # ``ripple_spec``; ``"svelte"`` materializes ``source`` instead.
    engine: str = "ripple"
    # The svelte-track source map ``{relative_path: file_contents}`` — the
    # SvelteKit files the generator writes onto the skeleton. ``None`` for
    # ripple pockets.
    source: dict[str, str] | None = None
    # Optional per-entity surface-profile override (the JSON-shaped dict that
    # mirrors the surface-domain ``SurfaceProfile``). Consumed by the
    # entity-aware resolve_profile (entity-rooms chunk ①). ``None`` = use the
    # surface-kind default (legacy pockets).
    surface_profile: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["Pocket", "Widget", "WidgetPosition"]
