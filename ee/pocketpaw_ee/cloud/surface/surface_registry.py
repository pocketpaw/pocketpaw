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
# Changes: 2026-07-23 (feat/ship-surface-kind, SHIP-8a) — registered the SHIP
# surface (/ship — the managed-deploy control plane). Its handler
# (``ship.build_preamble``) joins the handler-import list + the ``SURFACES`` row,
# and a new ``_ship_profile`` resolver gives it a ripple-OFF ``SurfaceProfile``
# scoped to the ship verb tools: ``_McpToolIds`` grows a ``ship_allow`` field,
# loaded from ``SHIP_TOOL_IDS`` (importable on this branch, so it rides the same
# try/except None-degrade path as the loom/media ids) and surfaced via the
# ``ship`` skill. Ripple OFF so the agent drives managed deploys instead of
# building a dashboard.
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
#
# Changes: 2026-08-25 (feat/other-hand-surface, Otherhand v1) — added the
# OTHER_HAND row (/other-hand — the notebook page the agent writes back on). A
# STATIC ripple-OFF profile carrying ``_OTHER_HAND_POCKET_DENY`` (the two
# pocket-creation tool ids, the only ids an allow-list provably cannot strip) and
# ``OTHER_HAND_SYSTEM_PROMPT`` (the page-ops output contract: op vocabulary,
# 1240x1754 coordinate space, margins, the free_y rule). Both halves are
# required — /code proved a closed deny plus a forbidding preamble still loses to
# a trained-in default when nothing positive replaces it.
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
    SurfacePreamble,
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
    other_hand,
    pocket,
    pocket_widget,
    pockets_list,
    quickask,
    settings,
    ship,
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
from pocketpaw_ee.cloud.surface.system_prompts import (
    CODE_SYSTEM_PROMPT,
    OTHER_HAND_SYSTEM_PROMPT,
)

# The shape every handler module exports: an async preamble builder taking the
# tenancy tuple + the validated client meta and returning the rendered block
# WITH the cache key that says what the handler read to render it (PA-2). The
# key is part of the handler contract rather than something the dispatcher
# derives, because only the handler knows what it read — see ``SurfacePreamble``.
BuildPreamble = Callable[[str, str, SurfaceMeta], Awaitable[SurfacePreamble]]

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

# The two ripple-authoring MCP tool ids the /sites hand-authored-component CREATE
# modes forbid. Named for svelte because it was the only such mode until RX-2
# added react; both share the set (see ``_SITES_AUTHORING_SKILL``), and the name
# is kept rather than churned because it crosses into two test modules.
# Spelled out here (the EE layer is the source of truth); they cross to the OSS
# backend as a plain ``frozenset[str]`` via ``deny_mcp_tool_ids`` — never as an
# imported ``pocketpaw_ee`` symbol (import-linter forbids EE→OSS imports).
_SITES_SVELTE_CREATE_DENY: frozenset[str] = frozenset(
    {
        "mcp__pocketpaw_sites_manager__create_landing_site",
        "mcp__pocketpaw_pocket_specialist__create",
    }
)

# The pocket-creation tool ids the Otherhand (/other-hand) surface forbids.
#
# These are EXACTLY ``claude_sdk.POCKET_CREATION_GRANT``, and naming them here is
# not redundancy — it is the only thing that removes them. The grant is UNIONED
# into any ``allow_mcp_tool_ids`` the surface sets, and their two servers
# (``pocketpaw_pocket_specialist`` / ``pocketpaw_pocket_planner``) are in
# ``ALWAYS_ALLOWED_MCP_SERVERS``, so they survive every allow-list by
# construction. The deny is subtracted from the allow-list BEFORE the grant
# union runs (``claude_sdk._build_options``), and the grant branch only filters —
# it never re-adds — so a denied id cannot come back.
#
# Why this surface needs it at all: "draw me a mitosis diagram", "make me a
# study plan for this", "sketch the water cycle" are near-perfect lexical
# matches for the create-pocket path, and the correct answer to every one of
# them is ink on the page. An agent that reaches the pocket tool here produces a
# dashboard the user cannot see, on a surface with no way to show it.
#
# Spelled as literals (the EE layer is the source of truth) because these cross
# to the OSS backend as a bare ``frozenset[str]`` — import-linter forbids the
# EE→OSS import that would let us reference the constant.
_OTHER_HAND_POCKET_DENY: frozenset[str] = frozenset(
    {
        "mcp__pocketpaw_pocket_specialist__create",
        "mcp__pocketpaw_pocket_planner__plan_pocket",
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
# ``writeFile`` saves the file (it staged a proposal for per-hunk review until
# 2026-07-25). Spelled as LITERALS for the same reason ``_BELT_GATE_TOOL_IDS`` above
# is — their canonical constants live in the in-process MCP server (server
# ``pocketpaw_code``), which the profile layer must not import. The id format is
# the SDK's ``mcp__<server>__<tool>`` namespacing. Do NOT drift these ids;
# ``test_code_mcp_server`` pins them against the server's own constants.
#
# ``editFile`` joined the set 2026-07-28 (fix/code-truncated-read-destroys-file).
# It is not an optional extra: ``readFile`` caps at 30_000 characters, so on any
# larger file a whole-file ``writeFile`` means sending back invented text for the
# part never read — which is what a live session reported as the agent
# "fabricating things". ``editFile`` is the verb that makes a large file
# changeable without holding all of it, and the browser now refuses the lossy
# write. Adding the id HERE is not sufficient on its own: the seeded ``code``
# agent's ``tool_mode="exclusive"`` policy caps the run's ``mcp__*`` surface
# independently, so the same id has to reach that config too or the tool is
# defined, allowed here, and still stripped at run time.
_CODE_FILE_TOOL_IDS: frozenset[str] = frozenset(
    {
        "mcp__pocketpaw_code__readFile",
        "mcp__pocketpaw_code__search",
        "mcp__pocketpaw_code__listDir",
        "mcp__pocketpaw_code__editFile",
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
    # Defaulted, unlike the fields above: a test that pins the cache by naming
    # only the fields it knows (test_sites_handler builds one with five) must not
    # break when a NEW surface adds an allow-list. ``None`` is the degrade value
    # (no MCP restriction), which is the safe direction to default toward.
    ship_allow: frozenset[str] | None = None


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
        from pocketpaw_ee.agent.mcp_servers.ship import SHIP_TOOL_IDS
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
            # /ship (the managed-deploy control plane) scopes to the ship verb
            # tools (list/provision boxes, list/create/deploy apps, add domain,
            # create db, logs, metrics, request-destroy). Crossed over as a plain
            # frozenset[str] — no pocketpaw_ee symbol leaks into the OSS surface
            # service. Unlike belt's gate id, SHIP_TOOL_IDS IS importable here, so
            # it rides the same try/except None-degrade path as the others.
            ship_allow=frozenset(SHIP_TOOL_IDS),
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
            ship_allow=None,
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


def _ship_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    # Ship: the managed-deploy control plane. Ripple OFF so the agent drives
    # managed deploys through the ship verb tools instead of building a
    # dashboard; the `ship` skill carries the full playbook (the verb loop + the
    # safety rule); the MCP allow-list is scoped to the ship verb tools
    # (SHIP_TOOL_IDS — list/provision boxes, list/create/deploy apps, add domain,
    # create db, logs, metrics, request-destroy). No SDK-tool allowlist: ship
    # drives infra purely through MCP verbs, not code. ship_allow is None when the
    # import degraded (no MCP restriction — never break chat).
    return SurfaceProfile(
        ripple_mode="off",
        skill_names=frozenset({"ship"}),
        allow_mcp_tool_ids=_mcp_tool_ids().ship_allow,
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


def _concierge_profile(meta: SurfaceMeta) -> SurfaceProfile:
    """/paw-bar — the PUBLIC concierge widget. Ripple OFF (it answers questions,
    never builds a dashboard) + the public-safe tool lockdown (``_CONCIERGE_DENY``
    hard-strips web/code/write/subagent/pocket-write tools) + the site-scoped MCP
    allow-list. KB grounding is locked to ``pocket:<id>`` + ``agent:<id>`` (the
    site's own pocket and its own dedicated concierge agent, never ``workspace:``)
    by ``ScopeKind.CONCIERGE`` in ``agent_service._kb_scopes_for_context`` — the
    profile governs tools, the scope governs KB.

    C1 — action registry: when the widget declares actions (carried on
    ``meta.pawbar_actions`` by ``concierge_chat``), the restrictive MCP allow-list
    is WIDENED by EXACTLY this widget's per-verb tool ids
    (``mcp__pawbar_actions__pawbar_<verb>``) so those tools survive the lockdown —
    and nothing else does. A widget with no declared actions keeps the empty
    allow-list, so the concierge tool surface is byte-identical deny-all.

    Slice 3 — the escape hatch: a run bound to a widget (``meta.widget_id``, also
    stamped server-side by ``concierge_chat``) additionally allows the built-in
    ``pawbar_request_human`` tool, whether or not the widget declares actions. It
    is the one tool every site's concierge gets, because "let me talk to a person"
    must never depend on what the owner happened to configure. It executes no
    declared verb — see ``paw_bar.handoff`` — so the zero-authority posture is
    unchanged."""
    allow = _concierge_allow_mcp("foreign")
    actions = getattr(meta, "pawbar_actions", None)
    widget_id = getattr(meta, "widget_id", None)
    if actions or widget_id:
        from pocketpaw_ee.agent.mcp_servers.pawbar import handoff_tool_id, pawbar_tool_id

        verb_ids = frozenset(
            pawbar_tool_id(a["verb"])
            for a in (actions or [])
            if isinstance(a, dict) and a.get("verb")
        )
        allow = allow | verb_ids
        if widget_id:
            allow = allow | {handoff_tool_id()}
    return SurfaceProfile(
        ripple_mode="off",
        deny_mcp_tool_ids=_CONCIERGE_DENY,
        allow_mcp_tool_ids=allow,
    )


# The bundled authoring skill each hand-authored-component create engine needs.
# These two engines drop ripple (they write markup, not a widget spec), so the
# agent's whole authoring brain arrives as a skill — an entry that names nothing
# real leaves the surface with ZERO skills (see
# ``test_every_surface_skill_name_resolves_to_a_real_skill``).
#
# Each skill composes with ``pocketpaw-design-taste`` rather than restating it:
# design taste is engine-agnostic and reaches the agent EMBEDDED in the preamble
# (``handlers/sites.py::_design_taste_system``), so it is deliberately absent from
# this map — naming it here would ship the same bytes twice per turn.
_SITES_AUTHORING_SKILL: dict[str, str] = {
    "svelte": "pocketpaw-create-svelte-site",
    "react": "pocketpaw-create-react-site",
}


def _sites_profile(meta: SurfaceMeta) -> SurfaceProfile:
    """/sites is META-AWARE — three modes; only the hand-authored component
    CREATE engines (svelte, react) lose ripple.

      * refine (``meta.pocket_id`` set, ANY engine) edits the existing ripple
        landing spec → KEEP ripple (sites default). Refine WINS over engine: a
        ``pocket_id`` present means refine even if ``engine="svelte"``.
      * create + svelte/react (``meta.engine`` in ``_SITES_AUTHORING_SKILL``, no
        ``pocket_id``) hand-authors components → DROP ripple, deny the two
        ripple-create tools, surface that engine's authoring skill.
      * create + ripple/html (``engine`` None/"ripple"/"html", no ``pocket_id``)
        → KEEP ripple (sites default).

    All modes scope to the sites authoring tools + general. The component-create
    modes additionally deny the two ripple-create tools (deny runs AFTER allow).

    react (RX-2) joins svelte rather than getting its own branch: the two differ
    only in WHICH authoring skill they name. Sharing the branch is what keeps the
    ripple_mode and the deny set from drifting apart between them — and the
    ``ripple_on`` fork in ``handlers/sites.py::_create_preamble`` reads the same
    split, so the preamble's ask-mechanism instruction matches what the surface
    actually grants.
    """
    sites_allow = _mcp_tool_ids().sites_allow
    authoring_skill = _SITES_AUTHORING_SKILL.get(meta.engine or "")
    if meta.pocket_id is None and authoring_skill is not None:
        return SurfaceProfile(
            ripple_mode="off",
            deny_mcp_tool_ids=_SITES_SVELTE_CREATE_DENY | _SITES_BUILTIN_DENY,
            allow_mcp_tool_ids=sites_allow,
            # The BUNDLED skill's real name. svelte's was "create-svelte-site"
            # until 2026-07-31, which matched nothing — and a non-empty
            # skill_names suppresses the wholesale bundled plugin, so this
            # surface ran with ZERO skills and the agent authored sites by hand
            # instead of through the sites tools. Guarded by
            # test_every_surface_skill_name_resolves_to_a_real_skill.
            skill_names=frozenset({authoring_skill}),
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
    SurfaceSpec(
        SurfaceKind.SHIP,
        _route_for(SurfaceKind.SHIP),
        ship.build_preamble,
        profile_resolver=_ship_profile,
    ),
    SurfaceSpec(
        SurfaceKind.OTHER_HAND,
        _route_for(SurfaceKind.OTHER_HAND),
        other_hand.build_preamble,
        # Otherhand: the user's notebook page. The deliverable is INK — a fenced
        # ``page-ops`` block of vector primitives the frontend draws onto the same
        # page the user is writing on.
        #
        # ``ripple_mode="off"`` because the INLINE_RIPPLE_SYSTEM_PROMPT's "default
        # to ui-spec" LAW is actively wrong here, for the same reason the /sites
        # svelte-create mode turns it off: the surface authors something that is
        # not a ripple spec, so the LAW is a ~20k-char instruction to produce the
        # wrong artifact with tools this row denies.
        #
        # The deny set is the load-bearing half (see ``_OTHER_HAND_POCKET_DENY``);
        # the ``system_message_override`` is the other half, and neither works
        # alone. The deny makes a pocket unreachable; the override supplies the
        # thing to do instead, down to the op vocabulary and the 1240x1754
        # coordinate space. /code is the precedent for needing both: with ripple
        # off and a preamble forbidding pockets, it still authored a ui-spec,
        # because a prohibition does not create a default.
        #
        # No ``allow_mcp_tool_ids``: the surface has no specialized MCP tools of
        # its own (the page-ops block is parsed client-side — there is no server
        # plumbing to call), and an allow-list here would restrict the agent's
        # general capability without adding anything. No ``allowed_sdk_tools``
        # either — that field is ADDITIVE and ``Read`` is already in the default
        # set, which matters a lot on this surface: ``Read`` IS the vision path.
        # Static profile: no lazily-loaded ids, not meta-aware.
        profile=SurfaceProfile(
            ripple_mode="off",
            deny_mcp_tool_ids=_OTHER_HAND_POCKET_DENY,
            system_message_override=OTHER_HAND_SYSTEM_PROMPT,
        ),
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
