# domain.py — Surface context value objects.
#
# Created: 2026-05-24 — Surface-aware chat preamble entity. The cloud
# chat agent today only sees scope / participants / current-pocket-id
# (three lines of dynamic context). Paw-enterprise is chat-first and
# every route will eventually have a chat bar — the agent should know
# which SURFACE the user is on and what's actually visible there. This
# module owns the value objects ``SurfaceKind`` (enumerates every chat-
# bearing surface), ``SurfaceMeta`` (client-supplied hints) and
# ``SurfaceContext`` (resolved snapshot + rendered preamble). Per the
# 11 entity rules, tenancy is enforced at construction — ``workspace_id``
# and ``user_id`` are required on ``SurfaceContext``.
#
# Updated: 2026-06-04 (feat/sites-refine-surface) — ``SurfaceMeta`` grows a
# ``site_id`` hint. The /sites/[siteId] refine chat stamps the published
# site id (and the underlying pocket_id) so the sites handler can branch onto
# a LANDING-AWARE REFINE preamble instead of the create-a-new-site one.
# Updated: 2026-06-04 (feat/sites-svelte-engine) — ``SurfaceMeta`` grows an
# ``engine`` hint ("ripple" | "svelte"). The /sites create UI's "Use Svelte
# pages" toggle stamps it so the sites handler routes the CREATE preamble to
# the svelte-track authoring skill (engine="svelte") instead of the
# ripple/default marketing brain. Persisted setting is pocket.engine; this is
# only the per-turn routing signal.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SurfaceKind(StrEnum):
    """Every chat-bearing surface in paw-enterprise.

    The router stamps one of these on every chat send via the client's
    ``surface`` hint. Unknown values fall back to ``GENERIC`` so an older
    client (or a route we haven't classified yet) still gets a usable
    preamble instead of failing the chat send.
    """

    HOME = "home"
    POCKETS_LIST = "pockets"  # /pockets index
    POCKET = "pocket"  # /pockets/[id]
    POCKET_WIDGET = "pocket_widget"  # /pockets/[id] with widget-focus modal open
    MISSION_CONTROL = "mission_control"  # /mission-control
    FILES = "files"
    AUDIT = "audit"
    ACTIVITY = "activity"
    AGENTS = "agents"
    AGENT = "agent"  # /agents/[id]
    KNOWLEDGE = "knowledge"
    CALENDAR = "calendar"
    CHAT = "chat"
    QUICKASK = "quickask"
    SETTINGS = "settings"
    SIDEPANEL = "sidepanel"
    FORESIGHT = "foresight"  # /foresight + /foresight/scenarios/* routes
    SITES = "sites"  # /sites — describe-to-create + manage published Paw Sites
    GENERIC = "generic"  # any unknown surface — agent still gets a usable preamble


@dataclass(frozen=True)
class SurfaceMeta:
    """Client-supplied hints about the current surface.

    Every field optional. Stay small — anything heavy gets fetched
    server-side by the matching handler rather than serialized over the
    wire. ``route_path`` is for debugging only (the raw
    ``$page.route.id`` the client read), not for routing decisions.
    """

    pocket_id: str | None = None
    widget_id: str | None = None
    focus_node_id: str | None = None
    agent_id: str | None = None
    file_id: str | None = None
    route_path: str | None = None
    # Foresight surface hints — set by the paw-enterprise sidebar's
    # surface stamp on /foresight routes. ``run_id`` is the active
    # ScenarioRun being viewed; ``scenario_id`` is the custom scenario
    # being edited; ``panel`` is the active rail tab ("scenarios" |
    # "live" | "results" | "aggregate" | "insights" | "editor"). All
    # optional — the handler degrades gracefully when absent.
    run_id: str | None = None
    scenario_id: str | None = None
    panel: str | None = None
    # Sites surface hint — set by the /sites/[siteId] refine chat. The id of
    # the published Paw Site being refined; paired with ``pocket_id`` (the
    # site's source pocket) so the handler routes to the refine/edit path
    # instead of the create-a-new-site path. Absent on the /sites gallery.
    site_id: str | None = None
    # Sites create hint — set by the /sites create UI's "Use Svelte pages"
    # toggle: "ripple" (default) | "svelte". On the create branch the handler
    # branches on this to prefer the svelte-track authoring skill when
    # "svelte"; absent / "ripple" keeps the default marketing brain. Does not
    # affect the refine branch (keyed on ``pocket_id``).
    engine: str | None = None


@dataclass(frozen=True)
class SurfaceContext:
    """Resolved surface state, ready to be embedded in the agent's prompt.

    Multi-tenant — ``workspace_id`` and ``user_id`` are required at
    construction time per the entity rules' tenancy-at-construction
    contract. Constructing one without tenancy info is a type error.

    ``preamble`` is the rendered XML-ish block the chat router prepends
    to the dynamic context (before scope/participants). Empty when the
    handler failed or had nothing meaningful to say — the chat path keeps
    going regardless.
    """

    workspace_id: str
    user_id: str
    kind: SurfaceKind
    meta: SurfaceMeta
    preamble: str


__all__ = ["SurfaceKind", "SurfaceMeta", "SurfaceContext"]
