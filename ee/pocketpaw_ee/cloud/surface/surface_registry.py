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
# Changes: 2026-07-22 (feat/code-surface-profile, CD-3) — the CODE row stopped
# addressing the wrong machine. It used to carry
# ``allowed_sdk_tools={"Bash","Read","Write","Edit","Glob","Grep"}``, which read
# like "scope the agent to the coding built-ins" but is ADDITIVE — those six are
# in the SDK's default set already, so it granted nothing and restricted nothing.
# Meanwhile the /code agent runs on the BACKEND SERVER: the user's project lives
# in a sandbox behind the ``code_mode`` tool, so every one of those built-ins
# would have read and written the server's own disk and reported success. The row
# now DENIES them (``_CODE_BUILTIN_DENY`` — deny is the only lever that removes a
# built-in) and scopes the MCP surface to the tools that reach the project
# (``_CODE_FILE_TOOL_IDS`` — originally the single ``code_mode`` tool, now the
# four per-call file verbs the main agent drives). ``Agent`` joins the deny set
# too: a spawned subagent
# is a second, unsupervised path to tools. ``skill_names`` drops to empty — the
# `code` skill teaches nothing BUT those built-ins, so keeping it would inject an
# instruction to call what the agent no longer has. Still a static ``profile``:
# both sets are module-level literals, nothing lazily loaded, not meta-aware. The
# matching preamble rewrite is in ``handlers/code.py``.
#
# Changes: 2026-07-22 (fix/code-surface-denies-pocket-authoring) — CD-3's deny set
# turned out to cover only HALF the surface's wrong turns. It removed the
# file/shell built-ins (the wrong MACHINE) but left every pocket-authoring tool
# reachable (the wrong DELIVERABLE), because ``allow_mcp_tool_ids`` cannot touch
# them: ``claude_sdk`` unions ``POCKET_CREATION_GRANT``, ``WIDGET_TOOL_IDS`` and
# every ``ALWAYS_ALLOWED_MCP_SERVERS`` tool back in AFTER a mode's allow-list is
# applied, on purpose, so "create a pocket" works from every chat mode. On /code
# that guarantee is the bug: a user with a React project open asked for "an
# employee management app, with components, nice design" and got a pocket and a
# ripple ui-spec. ``_CODE_POCKET_DENY`` closes it — deny runs BEFORE the grant is
# unioned in and is the only lever that reaches those families.
#
# The row then went further, on the reading that /code should not reach a pocket
# AT ALL. The pocket READ verbs join the deny set (a pocket the agent can inspect
# is a pocket it can propose), and so does ``Skill`` (``_CODE_SKILL_DENY``) —
# the bundled ``pocketpaw-create-pocket`` skill loads as a local PLUGIN, so
# ``skill_names`` never withheld it and denying the invoking tool is the only
# lever that does.
#
# And the row gained a ``system_message_override``: ``CODE_SYSTEM_PROMPT``. The
# deny set alone would have left the agent with a pocket-shaped behavioral stack
# it could no longer act on — the ripple LAW, the delegation rule and the
# artifact rule all name tools that are now gone. Worse, a prohibition does not
# create a DEFAULT: told only what not to build, the trained-in dashboard remains
# the sole concrete plan in context. The override states the surface's own
# deliverable instead. Both halves were needed; neither alone held.
#
# The CODE row stays STATIC — the prompt is a module constant like the tool ids.
#
# Changes: 2026-07-24 (feat/code-surface-cleanup, CX-4) — removed the
# ``_CODE_POCKET_DENY`` MCP deny-list from the CODE profile. Since CX-3, /code
# routes to a dedicated ``code`` agent whose ``tool_mode="exclusive"`` policy caps
# the run's ``mcp__*`` tools to exactly the four file ids at run time; every id
# ``_CODE_POCKET_DENY`` named was an ``mcp__*`` id that cap already strips, so the
# surface deny-list was dead weight. The division of labor is now explicit: the
# code AGENT enforces MCP tool restriction (structurally, via exclusivity), and the
# SURFACE profile denies only the BUILT-IN tools the MCP cap cannot reach —
# ``_CODE_BUILTIN_DENY`` (backend-disk tools + ``Agent``) and ``_CODE_SKILL_DENY``
# (``Skill``), both KEPT unchanged.

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
from pocketpaw_ee.cloud.surface.system_prompts import CODE_SYSTEM_PROMPT

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

# Built-in SDK tools the /sites agent never needs. It authors sites through the
# sites_manager + design MCP tools (the source map / copy is a tool ARGUMENT), so
# it never touches the file system or a shell — a dedicated sites agent should not
# carry Bash / file R-W / subagent spawning. These bare tool NAMES cross to the OSS
# backend in the SAME ``deny_mcp_tool_ids`` set as the mcp__ ids: the backend's deny
# filter subtracts ANY matching id from the launch allow-list, built-ins included, so
# naming them here physically removes them before the SDK starts. WebSearch / WebFetch
# / Skill are deliberately NOT denied (the agent still researches a business for real
# copy and loads the create-site skills).
_SITES_BUILTIN_DENY: frozenset[str] = frozenset(
    {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent"}
)

# The Instinct gate tool the /belt develop station proposes its diff through.
# Spelled as a LITERAL because its canonical constant
# (``BELT_PROPOSE_CHANGE_TOOL_ID`` / ``BELT_TOOL_IDS``) lives in a SIBLING
# branch's ``ee/pocketpaw_ee/agent/mcp_servers/belt.py`` — not importable on this
# base. When both PRs land, swap this literal for the imported constant (same
# None-degrade path as the loom/media imports below). Do NOT drift the id.
_BELT_GATE_TOOL_IDS: frozenset[str] = frozenset({"mcp__pocketpaw_belt__belt_propose_change"})

# The file tools the /code agent reaches the user's project through. The main
# agent drives the /code work in its own tool loop and reaches the project ONLY
# through these four verbs — each one delegates a single call to the browser,
# which owns the file session (the project runs in the tab, not on the backend).
# ``writeFile`` does not write: it stages a proposal for the user's per-hunk
# review. Spelled as LITERALS for the same reason ``_BELT_GATE_TOOL_IDS`` above
# is — their canonical constants live in the in-process MCP server (server
# ``pocketpaw_code``), which the profile layer must not import. The id format is
# the SDK's ``mcp__<server>__<tool>`` namespacing. Do NOT drift these ids;
# ``test_code_mcp_server`` pins them against the server's own constants.
_CODE_FILE_TOOL_IDS: frozenset[str] = frozenset(
    {
        "mcp__pocketpaw_code__readFile",
        "mcp__pocketpaw_code__search",
        "mcp__pocketpaw_code__listDir",
        "mcp__pocketpaw_code__writeFile",
    }
)

# Built-in SDK tools the /code agent must NOT have. Same mechanism and same
# reasoning as ``_SITES_BUILTIN_DENY`` above: these bare tool NAMES ride in
# ``deny_mcp_tool_ids``, and the OSS backend's deny filter subtracts ANY matching
# id from the launch allow-list — built-ins included — so naming them here
# physically removes them before the SDK starts.
#
# This is load-bearing, not tidiness. The /code agent runs on the BACKEND SERVER,
# not in the user's project: its cwd is the per-tenant scratch jail, and the
# user's code is only ever reachable through the file tools (which delegate to the
# browser). Left in place, the built-ins let the agent read and write the SERVER's
# filesystem and then report success — a silent wrong-machine failure with no
# error to notice.
#
# ``allowed_sdk_tools`` cannot do this job: it is ADDITIVE (unioned INTO the
# allow-list, ``effective = (agent_tools ∪ allow) − deny``), and the file/shell
# built-ins are in the SDK's default set already, so listing them there was a
# no-op that merely read like a restriction. ``allow_mcp_tool_ids`` cannot either
# — it filters only ``mcp__*`` ids and never touches built-ins. Deny is the only
# lever.
#
# ``Agent`` is denied for a reason beyond parity with SITES. Under this design
# the file tools are the ONLY path to the user's files; a spawned subagent is a
# SECOND path, with its own tool resolution and no supervision from this profile.
# Denying the six file/shell tools while leaving the tool that spawns a fresh
# tool-user would just move the hole one level down.
#
# WebSearch / WebFetch / Skill are deliberately NOT denied, the same reasoning
# SITES gives: researching an API or an error message to write against is
# legitimate work, and neither one reaches a filesystem.
_CODE_BUILTIN_DENY: frozenset[str] = frozenset(
    {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "Agent"}
)

# The pocket-AUTHORING tools the /code agent must not hold used to live here as
# ``_CODE_POCKET_DENY`` — an MCP deny-list spelling out the pocket / planner /
# widget ids so they could not survive back into the allow-list via
# ``POCKET_CREATION_GRANT`` / ``ALWAYS_ALLOWED_MCP_SERVERS`` / ``WIDGET_TOOL_IDS``.
# It was REMOVED 2026-07-24 (CX-4). MCP tool restriction for /code is now enforced
# STRUCTURALLY, one layer up: /code routes to a dedicated ``code`` agent whose
# config is ``tool_mode="exclusive"`` + ``tools=_CODE_FILE_TOOL_IDS``, and at run
# time that exclusive policy CAPS the run's ``mcp__*`` surface to exactly those
# four file tools — no pocket / widget / atlas / planner id can reach the allow-list
# regardless of any grant (proven by
# ``tests/cloud/agents/test_code_agent_seed.py::
# test_seeded_code_agent_config_drives_exclusive_allowlist``). With the cap moved to
# the agent, an MCP deny-list on the surface is dead weight — every id it named is an
# ``mcp__*`` id the exclusivity already strips.
#
# The exclusivity cap covers ONLY ``mcp__*`` ids, though. It does NOT and
# structurally CANNOT touch the SDK's built-in tools, so the surface's remaining
# denies below stay: ``_CODE_BUILTIN_DENY`` (Bash/Read/Write/… + Agent — the
# backend-disk tools) and ``_CODE_SKILL_DENY`` (``Skill``) both deny BUILT-INS that
# no MCP allow-list — exclusive or not — can reach.

# ``Skill`` is denied, and it is the last door.
#
# The bundled skills ship as a Claude Code LOCAL PLUGIN, which is loaded from the
# SDK ``plugins=`` option independently of ``skill_names`` — so a surface CANNOT
# withhold ``pocketpaw-create-pocket`` by naming a narrower skill set, and CD-3's
# empty ``skill_names`` never did. Its description ("create / build a pocket,
# dashboard, tracker, tool, viewer ... with enterprise-quality design") matches a
# request like "build an employee management app with components and nice design"
# almost word for word, which is how the reported bug started.
#
# With the code agent's exclusivity capping the pocket MCP tools out of reach,
# invoking that skill can no longer BUILD anything — but it would still cost the
# user a turn: the agent loads a long instruction telling it to call
# ``get_widget_spec`` and ``pocket_specialist__create``, attempts them, takes hard
# errors, and only then finds its file tools. ``Skill`` is a BUILT-IN, so the MCP
# cap does not reach it — this surface deny is what keeps the skill from firing on
# the exact repro prompt at all. CD-3 made the same argument when it dropped the
# `code` skill from the profile ("absence is recoverable; contradiction is not");
# it applies equally to a skill that teaches the wrong deliverable.
#
# /code needs no skill: ``CODE_SYSTEM_PROMPT`` is now the agent's whole guidance
# here, and it is not a document the agent has to go and fetch. If a
# code-targeted skill is ever written, removing ``Skill`` from this set is the
# one-line change that admits it.
_CODE_SKILL_DENY: frozenset[str] = frozenset({"Skill"})


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
        from pocketpaw_ee.agent.mcp_servers.ask import ASK_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.design_systems import DESIGN_SYSTEM_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.foresight import FORESIGHT_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.icons import ICON_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.loom import LOOM_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.media import MEDIA_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.palette import PALETTE_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.stock_images import STOCK_TOOL_IDS

        # /sites scopes to the sites-manager tools PLUS the authoring TOOLBELT the
        # crew (and the create-svelte-site skill) needs on-surface: stock photos,
        # icons, palette derivation, and the design-system library. These live on
        # ambient in-process servers, but the per-surface allow-list is a hard
        # whitelist (claude_sdk `allow_mcp_tool_ids`), so an id absent here is
        # FILTERED OUT on /sites — the tool would be silently unreachable. Named
        # here so authoring can actually call them. MEDIA (image/video gen) stays
        # scoped to /studio, not added here (site imagery leans on stock first).
        sites_allow = (
            frozenset(SITES_TOOL_IDS)
            | frozenset(STOCK_TOOL_IDS)
            | frozenset(ICON_TOOL_IDS)
            | frozenset(PALETTE_TOOL_IDS)
            | frozenset(DESIGN_SYSTEM_TOOL_IDS)
            # ask_user: interactive question chips. Needed most on svelte-create
            # (ripple OFF) where the agent otherwise can only ask in plain text.
            | frozenset(ASK_TOOL_IDS)
        )

        return _McpToolIds(
            loaded=True,
            foresight_allow=frozenset(FORESIGHT_TOOL_IDS),
            sites_allow=sites_allow,
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
            deny_mcp_tool_ids=_SITES_SVELTE_CREATE_DENY | _SITES_BUILTIN_DENY,
            allow_mcp_tool_ids=sites_allow,
            skill_names=frozenset({"create-svelte-site"}),
        )
    # Ripple-create + refine: keep ripple + the sites tool scope, but still drop the
    # file/shell built-ins — no /sites mode authors on disk (refine edits the ripple
    # spec through the pocket MCP tools, not the file system).
    return SurfaceProfile(
        ripple_mode="on",
        allow_mcp_tool_ids=sites_allow,
        deny_mcp_tool_ids=_SITES_BUILTIN_DENY,
    )


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
        # Code: edit + run code, but NOT on this machine. Ripple OFF so the
        # agent edits code instead of building a dashboard. The user's project
        # is reachable ONLY through the file tools ``_CODE_FILE_TOOL_IDS``
        # (allow) — readFile / search / listDir / writeFile, each delegated one
        # call at a time to the browser that holds the file session — and the
        # file/shell built-ins are stripped (deny) because they address the
        # backend server's own disk, not the project — see ``_CODE_BUILTIN_DENY``.
        # Both sets are module-level literals, so this stays a STATIC profile (no
        # lazily-loaded ids, not meta-aware, no resolver needed).
        #
        # The deny set covers only BUILT-IN tools now. Restricting the MCP surface
        # for /code is no longer this profile's job: /code routes to the dedicated
        # ``code`` agent, whose ``tool_mode="exclusive"`` policy caps the run's
        # ``mcp__*`` tools to exactly the four file ids at run time — so the old
        # ``_CODE_POCKET_DENY`` MCP deny-list became dead weight and was removed
        # (CX-4). What the exclusivity cap CANNOT reach are the built-ins, which is
        # exactly what ``_CODE_BUILTIN_DENY`` (backend-disk tools + ``Agent``) and
        # ``_CODE_SKILL_DENY`` (``Skill`` — the create-pocket plugin's invoker)
        # still deny here.
        #
        # ``skill_names`` is deliberately EMPTY, where it used to carry the
        # `code` skill. That skill is not incidentally about the built-ins — it
        # is entirely about them ("you use the built-in Bash / Read / Write /
        # Edit / Glob / Grep tools", then a five-step loop built on them), so
        # under the deny above it would be an injected instruction to call tools
        # the agent no longer has: the agent attempts them, takes hard errors,
        # and burns turns before finding the path the preamble already gave it.
        # Absence is recoverable; contradiction is not. The edit→run→verify
        # DISCIPLINE that skill carried now lives in ``CODE_SYSTEM_PROMPT``,
        # retargeted onto the file tools above.
        profile=SurfaceProfile(
            ripple_mode="off",
            allow_mcp_tool_ids=_CODE_FILE_TOOL_IDS,
            deny_mcp_tool_ids=_CODE_BUILTIN_DENY | _CODE_SKILL_DENY,
            # The surface's own system prompt, replacing the pocket-shaped
            # behavioral stack the shared builder would otherwise assemble. See
            # ``system_prompts.py`` for why a prohibition alone did not hold.
            system_message_override=CODE_SYSTEM_PROMPT,
        ),
    ),
    SurfaceSpec(
        SurfaceKind.BELT,
        _route_for(SurfaceKind.BELT),
        belt.build_preamble,
        profile_resolver=_belt_profile,
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
