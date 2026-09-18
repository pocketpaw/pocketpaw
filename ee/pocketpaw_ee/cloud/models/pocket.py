"""Pocket and Widget documents — the Beanie models behind the ``pockets``
collection.

A Pocket is the workspace canvas: widgets, team, and ONE authored artifact
whose shape depends on ``engine``. ``engine="ripple"`` (the default, and what
every legacy document reads back as) authors a ``rippleSpec``;
``engine="svelte"``/``react``/``html`` author a ``source`` file map instead.
``type="home"`` marks the per-user pocket behind the home page, and
``Widget.type="native"`` a widget the frontend renders as a built-in Svelte
component keyed on its ``name`` rather than from a spec.

INVARIANTS a reader must not break:

* EVERY FIELD ADDED HERE IS OPTIONAL WITH A DEFAULT, and the default must be
  what a document written before the field existed should mean. That is what
  lets this collection grow without a Mongo migration, and it is why
  ``keeps_client_bundle`` is tri-state (``None`` = the author declared nothing,
  which publish resolves from ``sites_keep_client_bundle_default``; ``True`` and
  ``False`` are authorial choices that win in BOTH directions). A two-state
  ``bool`` could not express "undeclared", so a default could only ever have
  been one-way.
* ``source`` IS ``dict[str, Any]``, NOT ``dict[str, str]``. A static source-track
  site carries only ``{relative_path: file_contents}``, but a DYNAMIC svelte site
  carries its live-data bindings (``objects``, ``sources``, ``actions``, ``auth``)
  as SIBLING keys on the SAME dict, so the values are lists and bools as well as
  strings. They live inside ``source`` so they ride the versioned content —
  draft/publish/revert capture the full dynamic spec in one snapshot.
* ``source_gated`` IS A COHORT STAMP, NOT A GATE. It records that the pocket was
  born after the site-source gate was switched on; the decision to withhold
  ``source`` from the wire is made in ``pockets.service``, which ANDs it with the
  live setting and the workspace entitlement. It defaults ``False``, so every
  pocket predating the field is permanently outside the cohort.
* ``PocketSurfaceProfile`` IS IMPORTED FROM ``surface.domain``, not defined here.
  ``pockets.dto`` needs the same class, and importing it from this module would
  break the OSS-EE boundary contract. The embedded BSON shape is identical.
"""

from __future__ import annotations

from typing import Any

from beanie import Indexed
from bson import ObjectId
from pydantic import BaseModel, Field
from pymongo import IndexModel

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
    # The svelte-track source CONTENT ENVELOPE. Carries the
    # ``{relative_path: file_contents}`` SvelteKit files (e.g.
    # ``"src/routes/+page.svelte"`` → contents) the generator writes onto the
    # paw-sites skeleton and prerenders — the svelte analog of ``rippleSpec``.
    # For a DYNAMIC svelte site it ALSO carries the live-data bindings as
    # SIBLING keys on the same dict: ``objects`` (a list of D1 table defs),
    # ``sources`` / ``actions`` (lists), and ``auth`` (a bool). Hence
    # ``dict[str, Any]`` (DSV-5): the values are file-content strings for the
    # path entries and lists/bools for the binding entries. ``None`` for ripple
    # pockets; a STATIC svelte pocket carries only the str->str file map (a
    # subset of the looser type — no behaviour change).
    source: dict[str, Any] | None = None
    # SF-2 — this pocket was created while the site-source gate was switched on,
    # so its ``source`` may be withheld from the wire when the workspace is not
    # entitled to read it. A COHORT STAMP, not the gate itself: it records which
    # side of the flip the pocket was born on, and the gate ANDs it with the
    # live ``sites_source_gate_enabled`` setting and the workspace entitlement.
    #
    # A STORED FLAG RATHER THAN ``created_at < <ship date>``. A date comparison
    # needs a magic constant nothing owns, silently re-classifies every row if
    # anyone edits the timestamp, and leaves an owner's future opt-in nowhere to
    # live. This field is that somewhere.
    #
    # Defaults ``False``, which is what every pocket written before this field
    # existed reads back as — so the entire existing population is permanently
    # outside the cohort with no Mongo migration, which is the whole of D3.
    source_gated: bool = False
    # MT-1 — this site's own client JavaScript is load-bearing (an onMount, a
    # ``use:`` action, an IntersectionObserver scroll-reveal, a WebGL canvas). A
    # site generated with ``csr = false`` has its emitted hydration bundle pruned
    # after the build (on ripple), so that code never runs. Lives on the pocket
    # (beside ``engine``) because it describes the AUTHORED artifact, not the
    # deployment.
    # TRI-STATE since feat/sites-js-by-default: ``None`` (the default, and what
    # every legacy pocket reads back as) means the author declared NOTHING, so
    # publish resolves it from ``sites_keep_client_bundle_default``. ``True`` /
    # ``False`` are explicit authorial choices and BOTH win over that config
    # default — declaring ``False`` is how a pure-static page opts out. Same
    # "None = no declaration, use the default" shape as ``surface_profile``
    # below. Still additive with no Mongo migration: the key is simply absent on
    # legacy docs, which is exactly the undeclared state.
    keeps_client_bundle: bool | None = None
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
    # Per-pocket connector allowlist. `None` (default) = inherit all workspace
    # connectors (backward-compatible). An explicit list restricts the pocket to
    # only those named connectors. Empty list = no connectors allowed.
    # Workspace-level connector permissions (member → connector) still apply
    # on top — a pocket cannot grant a connector the member doesn't have.
    allowed_connectors: list[str] | None = None

    model_config = {"populate_by_name": True}

    class Settings:
        name = "pockets"
        indexes = [
            # The list query is a $or over owner / shared_with / visibility,
            # always anchored on ``workspace``. The inline ``Indexed`` on the
            # workspace field alone left Mongo scanning every pocket in the
            # workspace and filtering the $or in memory; these let it use the
            # index for each branch of the union instead.
            #
            # shared_with is an array, so that one is multikey.
            IndexModel([("workspace", 1), ("owner", 1)], name="workspace_owner_1"),
            IndexModel([("workspace", 1), ("visibility", 1)], name="workspace_visibility_1"),
            IndexModel([("workspace", 1), ("shared_with", 1)], name="workspace_shared_with_1"),
        ]
