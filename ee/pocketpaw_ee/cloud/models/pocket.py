"""Pocket and Widget documents.

Updated: 2026-05-21 — documented the ``type="home"`` pocket type (the
per-user pocket that backs the home page) and the ``type="native"``
widget type (rendered by the frontend as a built-in Svelte component
keyed on ``name``). Both reuse the free-form ``type`` field — no schema
change, just recognized values.
Updated: 2026-05-22 — added the optional ``Widget.spec`` field: a Ripple
rippleSpec subtree for a single tile (e.g. a ``chart`` node with a real
``data`` series). The home grid renders a ``widgets[]`` entry from its
``spec``; the home agent's ``add_widget`` MCP tool populates it. Native
widgets leave it ``None``.
Updated: 2026-05-28 (feat/wave-3e-template-slug) — added the optional
``Pocket.template_slug`` field: the kebab-case slug of the RFC 03 v2
:class:`PocketTemplate` this pocket was instantiated from. Optional so
legacy pockets (no template) read back as ``None`` without a Mongo
migration. ``pockets.service.resolve_pocket_template`` reads this field
and feeds the resolved template to the bulk dispatcher + temporal
scheduler.
Updated: 2026-06-03 (feat/sites-landing-brain) — added the optional
``Pocket.pattern`` field: the create-pocket layout pattern this pocket
was built as (``dashboard`` | ``app`` | ``viewer`` | ... | ``landing``).
Records site/landing intent as first-class metadata so a published Paw
Site renders as a marketing landing page rather than a dashboard.
Optional (default ``None``) so legacy pockets read back as ``None`` with
no Mongo migration.
Updated: 2026-06-04 (feat/sites-svelte-engine) — added the Paw Sites
"Svelte track" fields: ``Pocket.engine`` (``"ripple"`` default |
``"svelte"``) selects the site-generation track, and ``Pocket.source``
(``{relative_path: file_contents}`` | ``None``) holds the hand-written
SvelteKit source map a svelte-engine site materializes from (the svelte
analog of ``rippleSpec``). ``engine`` defaults to ``"ripple"`` and
``source`` to ``None`` so every existing pocket reads back as a ripple
pocket with no source map — additive, no Mongo migration.
Updated: 2026-06-05 (feat/entity-pocket-profile-field, entity-rooms
chunk ②) — added the optional ``Pocket.surface_profile`` field: a per-entity
override that MIRRORS the surface-domain ``SurfaceProfile`` (``ripple_mode`` /
``allowed_sdk_tools`` / ``deny_mcp_tool_ids`` / ``skill_names`` /
``system_message_override``) with JSON-friendly types (lists, not frozensets)
for Mongo. ALL sub-fields optional; the whole field defaults to ``None`` →
zero behaviour change for existing pockets, no Mongo migration. Consumed by
the entity-aware ``resolve_profile`` (chunk ①), which hydrates a
``SurfaceProfile`` from it.
Updated: 2026-06-07 (feat/entity-pocket-profile-field) — the
``PocketSurfaceProfile`` sub-model now lives in ``surface/domain.py`` (the
leaf domain module) and is imported here. This lets ``pockets.dto`` import the
same class from ``surface.domain`` instead of from ``models.pocket``, which
the OSS-EE boundary contract forbids. No schema change — the embedded BSON
shape is identical, so no Mongo migration.
"""

from __future__ import annotations

from typing import Any

from beanie import Indexed
from bson import ObjectId
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument
from pocketpaw_ee.cloud.surface.domain import PocketSurfaceProfile

__all__ = ["Pocket", "PocketSurfaceProfile", "Widget", "WidgetPosition"]


class WidgetPosition(BaseModel):
    row: int = 0
    col: int = 0


class Widget(BaseModel):
    """Widget subdocument embedded in a Pocket.

    Has its own _id so the frontend can address widgets by ID (not index).
    Field aliases match the frontend camelCase convention.
    """

    id: str = Field(default_factory=lambda: str(ObjectId()), alias="_id")
    name: str
    # Free-form. ``type="native"`` marks a widget the frontend renders as a
    # built-in Svelte component keyed on ``name`` (no rippleSpec).
    type: str = "custom"
    icon: str = ""
    color: str = ""
    span: str = "col-span-1"
    dataSourceType: str = Field(default="static", alias="dataSourceType")
    config: dict[str, Any] = Field(default_factory=dict)
    props: dict[str, Any] = Field(default_factory=dict)
    data: Any = None
    # Optional Ripple rippleSpec subtree for this single tile (e.g. a
    # ``chart`` node carrying a real ``data`` series). The home grid
    # renders the tile from ``spec`` when present. ``None`` for native
    # widgets, which have no rippleSpec.
    spec: dict[str, Any] | None = None
    assignedAgent: str | None = Field(default=None, alias="assignedAgent")
    position: WidgetPosition = Field(default_factory=WidgetPosition)

    model_config = {"populate_by_name": True}


class Pocket(TimestampedDocument):
    """Pocket workspace with widgets, team, and ripple spec.

    Updated: 2026-05-16 — added optional ``project_id`` so pockets can be
    grouped under a Mission Control Project. Optional everywhere
    (default None) to keep the migration backwards-compatible — existing
    pockets read back as "no project assigned".
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    project_id: str | None = None
    name: str
    description: str = ""
    # No pattern restriction — frontend sends data, deep-work, etc.
    # ``type="home"`` marks the per-user pocket that backs the home page.
    type: str = "custom"
    # Optional RFC 03 v2 template slug — the bundled-template this pocket
    # was instantiated from (e.g. ``"todo-task-tracker"``). When set,
    # ``pockets.service.resolve_pocket_template`` loads + validates the
    # template so the bulk dispatcher / temporal scheduler can fan out
    # actions against it. Legacy pockets (no template) read as ``None``
    # — no Mongo migration needed for adding an optional field.
    template_slug: str | None = None
    # Optional create-pocket layout pattern (e.g. ``"dashboard"``,
    # ``"viewer"``, ``"app"``, ``"landing"``). Records the conversion /
    # layout intent the pocket was authored as. ``pattern="landing"``
    # (set by the marketing-site brain) tells the sites generator to
    # render a marketing landing page, not a dashboard. Legacy pockets
    # read back as ``None`` — no Mongo migration for an optional field.
    pattern: str | None = None
    icon: str = ""
    color: str = ""
    owner: str
    team: list[Any] = Field(default_factory=list)  # User IDs or populated objects
    agents: list[Any] = Field(default_factory=list)  # Agent IDs or populated objects
    widgets: list[Widget] = Field(default_factory=list)
    rippleSpec: dict[str, Any] | None = Field(default=None, alias="rippleSpec")
    # Paw Sites generation track. ``"ripple"`` (the default) compiles
    # ``rippleSpec`` into the site; ``"svelte"`` materializes ``source``
    # (hand-written SvelteKit files) instead. The toggle is persisted on
    # the pocket so the generator + any later refine pick the same track.
    # Legacy pockets default to ``"ripple"`` — additive, no migration.
    engine: str = "ripple"
    # The svelte-track source map: ``{relative_path: file_contents}`` for a
    # SvelteKit project (e.g. ``"src/routes/+page.svelte"`` → contents). The
    # svelte analog of ``rippleSpec`` — the generator writes these files onto
    # the paw-sites skeleton and prerenders. ``None`` for ripple pockets.
    source: dict[str, str] | None = None
    # Default "workspace": new pockets are visible to every workspace member.
    # Owner can tighten to "private" (owner-only + explicit shared_with) via
    # the visibility toggle in the pocket UI.
    visibility: str = Field(default="workspace", pattern="^(private|workspace|public)$")
    share_link_token: str | None = None
    share_link_access: str = Field(default="view", pattern="^(view|comment|edit)$")
    shared_with: list[str] = Field(default_factory=list)  # User IDs with explicit access
    # Pocket-scoped tool specs merged into the base toolset for agent runs
    # performed inside this pocket. Each entry is free-form so built-in IDs,
    # workspace MCP refs, and inline declarative tools can coexist.
    tool_specs: list[dict[str, Any]] = Field(default_factory=list)
    # Optional per-entity surface-profile override. Consumed by the
    # entity-aware resolve_profile (entity-rooms chunk ①); None = use the
    # surface-kind default.
    surface_profile: PocketSurfaceProfile | None = None

    model_config = {"populate_by_name": True}

    class Settings:
        name = "pockets"
