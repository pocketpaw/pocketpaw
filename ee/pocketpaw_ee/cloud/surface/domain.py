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
# Changes: 2026-06-05 (feat/surface-profile-bias-kill) — added the typed
# ``SurfaceProfile`` descriptor, the per-surface POLICY object (the data
# backbone of the "ripple-default bias" fix). Its ``ripple_mode`` drives
# whether ``build_behavior_instructions`` includes the ~20k-char
# INLINE_RIPPLE_SYSTEM_PROMPT ("default to ui-spec" LAW); ``off`` omits it
# so the /sites svelte-create surface stops defaulting to a ripple ui-spec
# instead of hand-authored Svelte.
# Changes: 2026-06-05 (feat/sites-svelte-engine) — ``deny_mcp_tool_ids`` is now
# ENFORCED end-to-end: the resolver's set is threaded to the OSS backend's
# ``run`` as a plain ``frozenset[str]`` and subtracted from the SDK allowlist
# (replacing the deleted prompt-sniffing tool gate in ``claude_sdk.py``).
# ``skill_names`` / ``allowed_sdk_tools`` / ``system_message_override`` remain
# DECLARED-but-inert tested DATA for later passes. The resolver lives in
# ``service.py`` and is META-AWARE on /sites (three modes).

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


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


@dataclass(frozen=True)
class SurfaceProfile:
    """The behavioral policy a surface imposes on the chat agent.

    Resolved once per request from ``SurfaceKind`` via ``resolve_profile``.
    The single source of truth for surface-specific agent shaping, so
    consumers branch on a typed field rather than re-deriving rules from a
    bare ``kind ==`` check scattered across the codebase.

    Fields:
      * ``ripple_mode`` — whether the ripple LAW applies. ``"on"`` (the default
        — every surface, plus the /sites ripple-create and refine modes) keeps
        INLINE_RIPPLE_SYSTEM_PROMPT; ``"off"`` omits it (the /sites *svelte
        create* mode only — the agent hand-authors SvelteKit, so the "default to
        ui-spec" LAW is actively wrong; the ripple-create and refine /sites modes
        still author/edit a ripple spec and KEEP it); ``"trim"`` is reserved for
        a future slimmed variant (declared, not yet used).
      * ``allowed_sdk_tools`` — optional SDK-tool allowlist (``None`` = no
        surface restriction). Declared for PR 2; not consumed in PR 1.
      * ``allow_mcp_tool_ids`` — optional per-surface MCP-tool ALLOW-list
        (``None`` = no restriction, the agent keeps every MCP tool). ENFORCED:
        when set, the OSS backend keeps only the MCP tools in this set PLUS the
        universal pocket-creation grant, dropping the rest before the SDK
        launches. This is how a mode keeps its agent context lean (Files,
        Foresight, etc. carry only their own tools). Pocket creation stays
        available everywhere via the grant. ``None`` on every surface today.
      * ``deny_mcp_tool_ids`` — MCP tool ids this surface forbids. ENFORCED: the
        resolved set is threaded to the OSS backend's ``run`` as a plain
        ``frozenset[str]`` (``deny_mcp_tool_ids``) and subtracted from
        ``allowed_tools`` before the SDK launches. Non-empty only on the /sites
        svelte-create row. Applied AFTER the allow-list, so a mode can allow a
        group and still deny a specific id.
      * ``skill_names`` — skills this surface surfaces to the agent. Tested DATA;
        skill-surfacing consumption lands in a later pass.
      * ``system_message_override`` — optional full system-message swap for a
        surface. Declared for a future PR; not consumed yet.

    ``ripple_mode`` and ``deny_mcp_tool_ids`` are CONSUMED today; the remaining
    fields are intentionally populated-but-inert so the descriptor's shape is
    locked now and later passes add enforcement without re-designing the
    primitive.
    """

    ripple_mode: Literal["on", "off", "trim"]
    allowed_sdk_tools: frozenset[str] | None = None
    allow_mcp_tool_ids: frozenset[str] | None = None
    deny_mcp_tool_ids: frozenset[str] = field(default_factory=frozenset)
    skill_names: frozenset[str] = field(default_factory=frozenset)
    system_message_override: str | None = None


__all__ = ["SurfaceKind", "SurfaceMeta", "SurfaceContext", "SurfaceProfile"]
