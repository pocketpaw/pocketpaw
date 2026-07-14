# surface_registry.py — The declarative surface registry (SR-1 + SR-2).
#
# Created: 2026-06-22 (feat/surface-registry-backend, SR-1) — the single
# declarative source of truth for "what surfaces exist and how each one
# dispatches." Each surface is one frozen ``SurfaceSpec`` row carrying its
# ``SurfaceKind``, its canonical ``route`` (the cross-repo contract — the
# ``SurfaceKind`` value with a leading slash, e.g. STUDIO -> "/studio"), and
# its ``build_preamble`` handler callable. ``service._load_handlers`` builds
# its ``dict[SurfaceKind, callable]`` dispatch table by LOOPING over
# ``SURFACES`` instead of hand-maintaining a parallel literal dict, so adding a
# surface means adding ONE row here.
#
# This module is imported LAZILY (from inside ``service._load_handlers`` and
# ``service._registry``, not at package import) so a broken handler-module
# import still can't take the whole surface dispatch table down at import time
# — the same deferral the old hand-written ``_load_handlers`` body had. The
# handler imports below run when ``surface_registry`` is first imported, which
# is the first call to ``_load_handlers`` / ``resolve_profile``.
#
# Changes: 2026-06-22 (feat/surface-registry-backend-profiles, SR-2) — PROFILES
# now DERIVE from these rows too, with ZERO behavior change. Every surface whose
# policy differs from the ripple-on default carries either a static ``profile``
# (CODE — needs no lazily-loaded MCP tool ids and is not meta-aware) or a
# ``profile_resolver`` (FORESIGHT / FILES / STUDIO / BELT / SITES — they need the
# lazily-imported per-mode MCP tool-id sets, and SITES additionally forks on
# ``meta``). ``service.resolve_profile`` reads ``spec.profile_resolver(meta)`` if
# present, else ``spec.profile`` if set, else the shared ripple-on default —
# producing IDENTICAL results to the pre-SR-2 ``_build_profiles`` table for every
# ``(kind, meta)``. The lazy + memoized MCP-tool-id load lives here now
# (``_mcp_tool_ids`` / ``_MCP_TOOL_IDS_CACHE``); a failed import degrades to no
# MCP restriction exactly as before, so tool-scoping can never break chat. A
# startup assertion (``_assert_registry_complete``, run at module import)
# guarantees every ``SurfaceKind`` has exactly one row and no row names a bogus
# kind — resolving the design's open question (keep the enum + assert).
#
# Changes: 2026-07-14 (Paw Bar concierge seam, T2) — registered the CONCIERGE
# surface (/paw-bar — the public concierge widget). Its handler
# (``concierge.build_preamble``) joins the row list and ``_concierge_profile``
# gives it a ripple-OFF, PUBLIC-SAFE policy: ``_CONCIERGE_DENY`` hard-strips the
# web + code/write/subagent + pocket-write tools (the real lockdown lever for a
# public surface — ``allow_mcp_tool_ids`` alone can't, since the universal grant
# + always-allowed servers survive it), and ``_concierge_allow_mcp`` is the
# site-kind-parameterized MCP allow-list (foreign = site-scoped KB only; a hook
# for dynamic sites' D1-read). The completeness assertion forced this row the
# moment ``SurfaceKind.CONCIERGE`` was added.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NamedTuple

from pocketpaw_ee.cloud.surface.domain import (
    SurfaceKind,
    SurfaceMeta,
    SurfaceProfile,
)
from pocketpaw_ee.cloud.surface.handlers import (
    activity,
    audit,
    belt,
    calendar,
    code,
    concierge,
    files,
    generic,
    home,
    knowledge,
    mission_control,
    pocket,
    pocket_widget,
    pockets_list,
    quickask,
    settings,
    sidepanel,
    sites,
    studio,
)
from pocketpaw_ee.cloud.surface.handlers import (
    agent as agent_handler,
)
from pocketpaw_ee.cloud.surface.handlers import (
    agents as agents_handler,
)
from pocketpaw_ee.cloud.surface.handlers import (
    chat as chat_handler,
)
from pocketpaw_ee.cloud.surface.handlers import (
    foresight as foresight_handler,
)

# The shape every handler module exports: an async preamble builder taking the
# tenancy tuple + the validated client meta and returning the rendered block.
BuildPreamble = Callable[[str, str, SurfaceMeta], Awaitable[str]]

# A profile resolver: given the client meta, return the surface's behavioral
# profile. Set on the rows whose profile depends on the lazily-loaded per-mode
# MCP tool-id sets (FORESIGHT / FILES / STUDIO / BELT) or on ``meta`` (SITES).
ProfileResolver = Callable[[SurfaceMeta], SurfaceProfile]


@dataclass(frozen=True)
class SurfaceSpec:
    """One row of the declarative surface registry.

    ``kind`` and ``build_preamble`` are the SR-1 payload — together they
    replace the hand-written entry in ``service._load_handlers``. ``route`` is
    the surface's canonical route (the ``SurfaceKind`` value with a leading
    slash), the cross-repo contract paw-enterprise and the backend agree on.

    ``profile`` / ``profile_resolver`` (SR-2) carry the surface's behavioral
    ``SurfaceProfile``. ``service.resolve_profile`` reads ``profile_resolver``
    first (meta-aware or lazy-tool-id rows), then the static ``profile`` (rows
    that need neither), then falls back to the shared ripple-on default for
    rows that leave both ``None``. At most one of ``profile`` /
    ``profile_resolver`` is set on any row.

    ``mcp_provider`` is reserved for a later pass that maps a surface to its
    owning MCP server; unused today.
    """

    kind: SurfaceKind
    route: str
    build_preamble: BuildPreamble
    profile: SurfaceProfile | None = None
    profile_resolver: ProfileResolver | None = None
    mcp_provider: str | None = None


def _route_for(kind: SurfaceKind) -> str:
    """Canonical route for a kind: the enum value with a leading slash.

    This is the cross-repo contract (e.g. ``SurfaceKind.STUDIO`` -> ``"/studio"``).
    A few kinds' literal frontend routes differ (POCKET renders at
    ``/pockets/[id]``, MISSION_CONTROL at ``/mission-control``), but the
    registry's ``route`` is the VALUE-derived contract, matching the SR-1 spec
    (POCKET -> "/pocket", HOME -> "/home", SITES -> "/sites", ...).
    """
    return f"/{kind.value}"


# ---------------------------------------------------------------------------
# Profile data (SR-2 — migrated verbatim from ``service._build_profiles``).
#
# A SurfaceProfile is the per-surface behavioral policy the chat agent applies.
# Only surfaces whose policy DIFFERS from the ripple-on default carry one here;
# everything else (and any unmapped/future kind) resolves to ``_DEFAULT_PROFILE``
# in ``service.resolve_profile`` — exactly today's behavior (the zero-regression
# guarantee). The per-mode MCP allow-lists need EE agent-layer tool-id constants
# that could cycle at import; they are loaded LAZILY + memoized below, and a
# failed import degrades to no MCP restriction so tool-scoping can never break
# chat. Resolvers (not static profiles) carry these rows so the lazy load stays
# off the module-import path.
# ---------------------------------------------------------------------------

# The two ripple-authoring MCP tool ids the /sites SVELTE-CREATE mode forbids.
# Spelled out here (the EE layer is the source of truth); they cross to the OSS
# backend as a plain ``frozenset[str]`` via ``deny_mcp_tool_ids`` — never as an
# imported ``pocketpaw_ee`` symbol (import-linter forbids EE→OSS imports).
_SITES_SVELTE_CREATE_DENY: frozenset[str] = frozenset(
    {
        "mcp__pocketpaw_sites_manager__create_landing_site",
        "mcp__pocketpaw_pocket_specialist__create",
    }
)

# The Instinct gate tool the /belt develop station proposes its diff through.
# Spelled as a LITERAL because its canonical constant
# (``BELT_PROPOSE_CHANGE_TOOL_ID`` / ``BELT_TOOL_IDS``) lives in a SIBLING
# branch's ``ee/pocketpaw_ee/agent/mcp_servers/belt.py`` — not importable on this
# base. When both PRs land, swap this literal for the imported constant (same
# None-degrade path as the loom/media imports below). Do NOT drift the id.
_BELT_GATE_TOOL_IDS: frozenset[str] = frozenset({"mcp__pocketpaw_belt__belt_propose_change"})


class _McpToolIds(NamedTuple):
    """The lazily-loaded per-mode MCP allow-lists.

    ``loaded`` records whether the EE agent-layer import succeeded. When it
    FAILED every allow-list is ``None`` (no MCP restriction) — the degrade path
    that keeps tool-scoping from ever breaking chat. ``loaded`` distinguishes
    the FILES "general-everywhere only" case (``frozenset()`` when loaded) from
    the degraded ``None``.
    """

    loaded: bool
    foresight_allow: frozenset[str] | None
    sites_allow: frozenset[str] | None
    studio_allow: frozenset[str] | None
    belt_allow: frozenset[str] | None


# Built lazily + memoized: pulling the EE mcp-server tool-id constants at module
# import could cycle with the agent layer, and ``resolve_profile`` is on the hot
# path. A failed import degrades to no MCP restriction so tool-scoping can never
# break chat.
_MCP_TOOL_IDS_CACHE: _McpToolIds | None = None


def _load_mcp_tool_ids() -> _McpToolIds:
    """Load (or memoize) the per-mode MCP allow-lists from the EE agent layer.

    Degrades to all-``None`` (no MCP restriction, ``loaded=False``) if the
    import fails — identical to the pre-SR-2 ``_build_profiles`` try/except.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from pocketpaw_ee.agent.mcp_servers.foresight import FORESIGHT_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.loom import LOOM_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.media import MEDIA_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS

        return _McpToolIds(
            loaded=True,
            foresight_allow=frozenset(FORESIGHT_TOOL_IDS),
            sites_allow=frozenset(SITES_TOOL_IDS),
            # /studio scopes to the media-generation tools (image + video).
            # Crossed over from the EE mcp-server module as a plain
            # frozenset[str] — never an imported pocketpaw_ee symbol leaks into
            # the OSS surface service.
            studio_allow=frozenset(MEDIA_TOOL_IDS),
            # /belt (the develop station) scopes to the loom orientation tools
            # (so the agent grounds itself before coding) UNION the Instinct
            # gate tool (so it proposes the diff through the gate).
            belt_allow=frozenset(LOOM_TOOL_IDS) | _BELT_GATE_TOOL_IDS,
        )
    except Exception:  # noqa: BLE001 — degrade to no restriction, never break chat
        logger.warning(
            "surface: could not load mcp tool ids; per-mode MCP scoping disabled",
            exc_info=True,
        )
        return _McpToolIds(
            loaded=False,
            foresight_allow=None,
            sites_allow=None,
            studio_allow=None,
            belt_allow=None,
        )


def _mcp_tool_ids() -> _McpToolIds:
    global _MCP_TOOL_IDS_CACHE
    if _MCP_TOOL_IDS_CACHE is None:
        _MCP_TOOL_IDS_CACHE = _load_mcp_tool_ids()
    return _MCP_TOOL_IDS_CACHE


# --- Per-row profile resolvers -------------------------------------------------
#
# Each closes over the lazily-memoized ``_mcp_tool_ids()`` and reproduces the
# EXACT ``SurfaceProfile`` the old ``_build_profiles().by_kind`` (or the /sites
# special-case in ``resolve_profile``) returned for that surface.


def _foresight_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    # Foresight: its scenario tools + the general-everywhere set.
    return SurfaceProfile(ripple_mode="on", allow_mcp_tool_ids=_mcp_tool_ids().foresight_allow)


def _files_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    # Files names no specialized MCP tools — document scaffolding rides the
    # built-in Read/Write/Edit tools (never filtered). Empty allow =
    # general-everywhere only; None when the import degraded.
    ids = _mcp_tool_ids()
    return SurfaceProfile(
        ripple_mode="on",
        allow_mcp_tool_ids=frozenset() if ids.loaded else None,
    )


def _studio_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    # Studio: media generation (image + video). Ripple OFF so the agent
    # generates media instead of defaulting to a ripple ui-spec dashboard;
    # scoped to the media MCP tools (+ general-everywhere); the `studio` skill
    # carries the generate→gallery flow.
    return SurfaceProfile(
        ripple_mode="off",
        allow_mcp_tool_ids=_mcp_tool_ids().studio_allow,
        skill_names=frozenset({"studio"}),
    )


def _belt_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    # Belt: the develop station (orient→develop→propose via gate). Ripple OFF so
    # the agent runs the station loop instead of building a dashboard. SDK-tool
    # allowlist scopes it to the coding built-ins; the `belt` skill carries the
    # station playbook; the MCP allow-list is the loom orientation tools (ground
    # first) UNION the Instinct gate tool (propose the diff). belt_allow is None
    # when the import degraded.
    return SurfaceProfile(
        ripple_mode="off",
        skill_names=frozenset({"belt"}),
        allowed_sdk_tools=frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep"}),
        allow_mcp_tool_ids=_mcp_tool_ids().belt_allow,
    )


# --- Concierge (public Paw Bar widget) tool lockdown --------------------------
#
# The concierge is a PUBLIC, anonymous, prompt-injectable surface bound to a
# per-tenant agent, so its tool surface must be locked down HARD. The existing
# ``allow_mcp_tool_ids`` mechanism alone CANNOT do this: the OSS backend keeps
# the universal pocket-creation grant + the always-allowed servers (composio
# connectors, pocket lifecycle) through ANY allow-list, and ``allowed_sdk_tools``
# is ADDITIVE — it never strips the base SDK tools (Bash/Write/Edit/Agent/…). So
# the real lever for a public surface is ``deny_mcp_tool_ids``, which the backend
# subtracts from the FINAL tool list (SDK names included) BEFORE the allow-grant
# re-adds anything, so a denied id can't sneak back. This set is the public-safe
# deny:
#   * web tools (SSRF / exfil / unbounded browsing) — the T2 requirement;
#   * code / filesystem / subagent SDK tools (a public caller must not run code,
#     write in the tenant jail, or spawn subagents);
#   * skill loading (pulls arbitrary capabilities);
#   * the pocket write/create MCP tools that otherwise survive the universal
#     grant + always-allowed ``pocketpaw_pocket*`` servers.
# RESIDUAL GAP: composio CONNECTOR tool ids are dynamic/per-workspace and can't
# be enumerated in a static deny set, and they survive the always-allowed
# ``composio`` server — a concierge pocket with connectors bound could still
# reach them. A concierge pocket must therefore have NO connectors bound until
# the OSS backend grows a true "public/untrusted" lockdown mode (drop the
# universal grants). Tracked as the T2 follow-up.
_CONCIERGE_DENY: frozenset[str] = frozenset(
    {
        # Web (the explicit T2 requirement).
        "WebSearch",
        "WebFetch",
        # Code / filesystem / subagent — a public caller must not execute or write.
        "Bash",
        "Write",
        "Edit",
        "Agent",
        "Skill",
        # Pocket write/create tools that survive the universal grant + the
        # always-allowed pocket-lifecycle servers.
        "mcp__pocketpaw_pocket_specialist__create",
        "mcp__pocketpaw_pocket_specialist__edit",
        "mcp__pocketpaw_pocket_planner__plan_pocket",
        "mcp__pocketpaw_pocket__add_widget",
    }
)


def _concierge_allow_mcp(site_kind: str = "foreign") -> frozenset[str]:
    """Site-kind-parameterized MCP allow-list for the concierge (one guard, one
    param, per the design's seam-ownership).

    A FOREIGN site grounds on its site-scoped KB ALONE, which is injected into
    the prompt as ``pocket:<id>`` knowledge context (NOT an MCP tool), so it
    names NO specialized MCP tools — an empty allow keeps the surface lean
    (general read tools like ``get_pocket`` still survive via the always-allowed
    pocket-lifecycle server; the deny set above strips the dangerous ones). The
    hook for our own DYNAMIC sites lands here later: ``site_kind == "dynamic"``
    widens the returned set with the D1-read tool id(s).
    """
    if site_kind == "dynamic":
        # Placeholder for the dynamic-sites D1-read tool — wired when that lands.
        return frozenset()
    return frozenset()


def _concierge_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    """/paw-bar — the PUBLIC concierge widget. Ripple OFF (it answers questions,
    never builds a dashboard) + the public-safe tool lockdown (``_CONCIERGE_DENY``
    hard-strips web/code/write/subagent/pocket-write tools) + the site-scoped MCP
    allow-list. KB grounding is locked to ``pocket:<id>`` by
    ``ScopeKind.CONCIERGE`` in ``agent_service._kb_scopes_for_context`` — the
    profile governs tools, the scope governs KB.
    """
    return SurfaceProfile(
        ripple_mode="off",
        deny_mcp_tool_ids=_CONCIERGE_DENY,
        allow_mcp_tool_ids=_concierge_allow_mcp("foreign"),
    )


def _sites_profile(meta: SurfaceMeta) -> SurfaceProfile:
    """/sites is META-AWARE — three modes, only svelte-CREATE loses ripple.

      * refine (``meta.pocket_id`` set, ANY engine) edits the existing ripple
        landing spec → KEEP ripple (sites default). Refine WINS over engine: a
        ``pocket_id`` present means refine even if ``engine="svelte"``.
      * create + svelte (``meta.engine == "svelte"``, no ``pocket_id``)
        hand-authors SvelteKit → DROP ripple, deny the two ripple-create tools,
        surface the create-svelte-site skill.
      * create + ripple (``engine`` None/"ripple", no ``pocket_id``) authors a
        ripple landing page → KEEP ripple (sites default).

    Both modes scope to the sites authoring tools + general. svelte-create
    additionally denies the two ripple-create tools (deny runs AFTER allow).
    """
    sites_allow = _mcp_tool_ids().sites_allow
    if meta.pocket_id is None and meta.engine == "svelte":
        return SurfaceProfile(
            ripple_mode="off",
            deny_mcp_tool_ids=_SITES_SVELTE_CREATE_DENY,
            allow_mcp_tool_ids=sites_allow,
            skill_names=frozenset({"create-svelte-site"}),
        )
    return SurfaceProfile(ripple_mode="on", allow_mcp_tool_ids=sites_allow)


# One row per surface that currently has a handler registered in
# ``service._load_handlers``. ``kind`` + ``build_preamble`` mirror that dict
# exactly (zero behavior change is the SR-1 contract); ``route`` is derived
# from the kind value. ``profile`` / ``profile_resolver`` (SR-2) mirror the
# old ``_build_profiles`` table exactly — rows that need the lazily-loaded MCP
# tool ids or are meta-aware carry a ``profile_resolver``; CODE (no lazy data,
# not meta-aware) carries a static ``profile``; every other row leaves both
# ``None`` and resolves to the shared ripple-on default. Order matches the old
# literal dict for easy diffing.
SURFACES: list[SurfaceSpec] = [
    SurfaceSpec(SurfaceKind.HOME, _route_for(SurfaceKind.HOME), home.build_preamble),
    SurfaceSpec(
        SurfaceKind.POCKETS_LIST,
        _route_for(SurfaceKind.POCKETS_LIST),
        pockets_list.build_preamble,
    ),
    SurfaceSpec(SurfaceKind.POCKET, _route_for(SurfaceKind.POCKET), pocket.build_preamble),
    SurfaceSpec(
        SurfaceKind.POCKET_WIDGET,
        _route_for(SurfaceKind.POCKET_WIDGET),
        pocket_widget.build_preamble,
    ),
    SurfaceSpec(
        SurfaceKind.MISSION_CONTROL,
        _route_for(SurfaceKind.MISSION_CONTROL),
        mission_control.build_preamble,
    ),
    SurfaceSpec(
        SurfaceKind.FILES,
        _route_for(SurfaceKind.FILES),
        files.build_preamble,
        profile_resolver=_files_profile,
    ),
    SurfaceSpec(SurfaceKind.AUDIT, _route_for(SurfaceKind.AUDIT), audit.build_preamble),
    SurfaceSpec(SurfaceKind.ACTIVITY, _route_for(SurfaceKind.ACTIVITY), activity.build_preamble),
    SurfaceSpec(SurfaceKind.AGENTS, _route_for(SurfaceKind.AGENTS), agents_handler.build_preamble),
    SurfaceSpec(SurfaceKind.AGENT, _route_for(SurfaceKind.AGENT), agent_handler.build_preamble),
    SurfaceSpec(SurfaceKind.KNOWLEDGE, _route_for(SurfaceKind.KNOWLEDGE), knowledge.build_preamble),
    SurfaceSpec(SurfaceKind.CALENDAR, _route_for(SurfaceKind.CALENDAR), calendar.build_preamble),
    SurfaceSpec(SurfaceKind.CHAT, _route_for(SurfaceKind.CHAT), chat_handler.build_preamble),
    SurfaceSpec(SurfaceKind.QUICKASK, _route_for(SurfaceKind.QUICKASK), quickask.build_preamble),
    SurfaceSpec(SurfaceKind.SETTINGS, _route_for(SurfaceKind.SETTINGS), settings.build_preamble),
    SurfaceSpec(SurfaceKind.SIDEPANEL, _route_for(SurfaceKind.SIDEPANEL), sidepanel.build_preamble),
    SurfaceSpec(
        SurfaceKind.FORESIGHT,
        _route_for(SurfaceKind.FORESIGHT),
        foresight_handler.build_preamble,
        profile_resolver=_foresight_profile,
    ),
    SurfaceSpec(
        SurfaceKind.SITES,
        _route_for(SurfaceKind.SITES),
        sites.build_preamble,
        profile_resolver=_sites_profile,
    ),
    SurfaceSpec(
        SurfaceKind.STUDIO,
        _route_for(SurfaceKind.STUDIO),
        studio.build_preamble,
        profile_resolver=_studio_profile,
    ),
    SurfaceSpec(
        SurfaceKind.CODE,
        _route_for(SurfaceKind.CODE),
        code.build_preamble,
        # Code: edit + run code. Ripple OFF so the agent edits code instead of
        # building a dashboard; the SDK-tool allowlist scopes it to the coding
        # built-ins; the `code` skill carries the edit→run→verify loop. No
        # lazily-loaded MCP tool ids and not meta-aware, so this is a STATIC
        # profile (no resolver needed).
        profile=SurfaceProfile(
            ripple_mode="off",
            skill_names=frozenset({"code"}),
            allowed_sdk_tools=frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep"}),
        ),
    ),
    SurfaceSpec(
        SurfaceKind.BELT,
        _route_for(SurfaceKind.BELT),
        belt.build_preamble,
        profile_resolver=_belt_profile,
    ),
    SurfaceSpec(
        SurfaceKind.CONCIERGE,
        _route_for(SurfaceKind.CONCIERGE),
        concierge.build_preamble,
        profile_resolver=_concierge_profile,
    ),
    SurfaceSpec(SurfaceKind.GENERIC, _route_for(SurfaceKind.GENERIC), generic.build_preamble),
]


def _assert_registry_complete(surfaces: list[SurfaceSpec] | None = None) -> None:
    """Fail fast at import if the registry isn't a clean 1:1 with ``SurfaceKind``.

    Guarantees every ``SurfaceKind`` member has EXACTLY ONE ``SurfaceSpec`` and
    every ``SurfaceSpec.kind`` is a real ``SurfaceKind`` — no orphan rows, no
    duplicate rows, no missing kinds. Resolves the SR design's open question:
    keep the enum as the closed set of surfaces and ASSERT the registry covers
    it, rather than deriving the enum from the registry. Runs at module import
    (call at the bottom of this file) so a drift between the enum and the table
    surfaces as an ``ImportError`` on the first surface call, not as a silent
    wrong-profile / missing-handler at request time.

    ``surfaces`` defaults to the module ``SURFACES``; tests inject a mutated
    list to prove the assertion fires.
    """
    rows = SURFACES if surfaces is None else surfaces

    kinds = [spec.kind for spec in rows]

    # Every row names a real SurfaceKind (no bogus / orphan rows).
    orphans = [k for k in kinds if not isinstance(k, SurfaceKind)]
    if orphans:
        raise AssertionError(f"surface registry has rows with non-SurfaceKind kinds: {orphans!r}")

    # No duplicate rows for the same kind.
    seen: set[SurfaceKind] = set()
    dupes: set[SurfaceKind] = set()
    for k in kinds:
        if k in seen:
            dupes.add(k)
        seen.add(k)
    if dupes:
        raise AssertionError(f"surface registry has duplicate rows for kinds: {sorted(dupes)!r}")

    # Every SurfaceKind member is covered by exactly one row.
    missing = [k for k in SurfaceKind if k not in seen]
    if missing:
        raise AssertionError(f"surface registry is missing rows for kinds: {missing!r}")


# Run the completeness check at import so an enum/registry drift fails fast.
_assert_registry_complete()
