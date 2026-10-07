# surface_registry.py — the declarative surface registry: one source of truth for
# what surfaces exist, how each dispatches, and what policy each runs under.
#
# Each surface is one frozen ``SurfaceSpec`` row: its ``SurfaceKind``, canonical
# ``route`` (``"/" + kind.value``, the cross-repo contract), ``build_preamble``
# handler, and either a static ``profile`` or a ``profile_resolver`` (rows that
# need lazily loaded MCP tool ids, or fork on meta like /sites). Adding a surface
# is ONE row; ``_assert_registry_complete`` (run at import) fails if a
# ``SurfaceKind`` has no row. Imported lazily from service.py, so a broken
# handler import cannot take dispatch down at package import.
#
# MCP tool ids load lazily and memoized (``_mcp_tool_ids``); a failed import
# degrades to no MCP restriction, so tool scoping never breaks chat. The craft
# studios (vector / photo / design) share ``_studio_craft_profile(app)``: ripple
# off, allow-list = that app's one ``edit_<app>`` tool.
#
# Levers, and which one reaches what: ``allow_mcp_tool_ids`` scopes MCP tools but
# the pocket-creation grant and always-allowed servers are unioned back after it;
# ``deny_mcp_tool_ids`` runs BEFORE that union and is the only way to remove a
# built-in or a granted id (/code denies the backend-disk built-ins, ``Agent`` and
# ``Skill``; /other-hand denies the two pocket-creation ids); ``exclusive_tools``
# (the public concierge) offers only the allow-listed tools. ``skill_names`` is an
# exact ALLOWLIST that suppresses the bundled plugin, so /sites create names its
# design skills explicitly (sourced from handlers/sites.py so advertised ==
# allowed). A deny-only profile still needs a ``system_message_override`` that
# states the surface's own deliverable, or the trained-in default wins.
# ``POCKETPAW_SITES_MCP_SERVERS`` grants external MCP servers to /sites alone.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NamedTuple

from pocketpaw.agents.pydantic_ai import _TENANT_SAFE_TOOLS
from pocketpaw_ee.agent.mcp_servers.other_hand import ILLUSTRATE_TOOL_ID
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
    browser,
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
    studio_craft,
    studio_editor,
)
from pocketpaw_ee.cloud.surface.handlers import (
    agent as agent_handler,
)
from pocketpaw_ee.cloud.surface.handlers import (
    agent_health as agent_health_handler,
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

# Every BRIDGED builtin the Otherhand agent is never told about (2026-09-10).
#
# The notebook prompt names exactly two tools, ``illustrate`` and
# ``image_generate``. The pydantic_ai backend nonetheless offered every
# tenant-safe builtin the bridge could build — 34 on a dev box, ~26.5k chars of
# JSON schema, about 6.6k tokens — on every turn. Tool schemas are the one part
# of the request the upstream prompt cache has been measured NOT to cover
# (``agents/pydantic_ai.py``, module header: same prompt, tools attached,
# ``cache_read_tokens`` of zero on every turn). So this was the largest
# uncacheable block in an Otherhand request, and every byte of it was a tool
# the prompt never mentions.
#
# DERIVED from the backend's own classification rather than written out, so
# a newly classified tool is denied here by default instead of leaking in
# until someone notices. The two names kept are the two the prompt commands.
#
# Bare names, not ``mcp__`` ids, on purpose: the backend's deny filter
# subtracts ANY matching name from the bridged list before the agent is built
# (the route ``_SITES_BUILTIN_DENY`` takes to remove Bash). An allow-list would
# be the cleaner shape, but the backend applies its allow to MCP toolsets only
# and says why; a deny is the lever that exists.
_OTHER_HAND_TOOLS: frozenset[str] = frozenset({"illustrate", "image_generate"})
_OTHER_HAND_BRIDGED_DENY: frozenset[str] = _TENANT_SAFE_TOOLS - _OTHER_HAND_TOOLS

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
    # /browser — the agentic-browser tools, used as the BROWSER surface's ALLOW.
    # The matching DENY on every other surface does NOT read this field; it goes
    # through ``browser_tool_ids()``, which loads the ids on its own. See that
    # function for why the two must not share an import fate.
    browser_allow: frozenset[str] | None = None
    # /studio/editor — the timeline edit + export verbs, plus open_surface.
    timeline_allow: frozenset[str] | None = None
    # /studio/vector|photo|design — that app's edit_<app> verb, keyed by app.
    craft_allow: dict[str, frozenset[str]] | None = None


# Built lazily + memoized: pulling the EE mcp-server tool-id constants at module
# import could cycle with the agent layer, and ``resolve_profile`` is on the hot
# path. A failed import degrades to no MCP restriction so tool-scoping can never
# break chat.
_MCP_TOOL_IDS_CACHE: _McpToolIds | None = None


def _external_sites_mcp_grants() -> frozenset[str]:
    """Bare ``mcp__<server>`` grants for the servers /sites is allowed to call.

    Reads ``POCKETPAW_SITES_MCP_SERVERS`` (comma-separated). Empty by default,
    so this is a no-op unless a deploy opts in.

    The BARE token is the point. An external server's tool names are unknown
    until the client connects, so ``claude_sdk._collect_mcp_tool_ids``
    allow-lists such a server wholesale as ``mcp__<server>`` with no tool
    segment — and the /sites allow-list is compared against ``allowed_tools`` by
    exact string, so emitting the same token here is what lets it through, and
    lets it through on THIS surface only. The alternative,
    ``ALWAYS_ALLOWED_MCP_SERVERS``, would grant the server to every surface.

    A name that matches no configured server is deliberately NOT validated
    against ``load_mcp_config()``: an unmatched grant is already inert (the
    token never appears in ``allowed_tools``, so nothing passes), and
    validating here would couple surface resolution to the MCP config file and
    give an optional integration a way to break profile resolution.

    Read once per process — the caller memoizes into ``_MCP_TOOL_IDS_CACHE`` —
    so changing the setting needs a restart, like the rest of this table.
    """
    try:
        from pocketpaw.config import get_settings

        raw = getattr(get_settings(), "sites_mcp_servers", "") or ""
    except Exception:  # noqa: BLE001 — never let config break profile resolution
        return frozenset()
    return frozenset(f"mcp__{name.strip()}" for name in raw.split(",") if name.strip())


def _load_mcp_tool_ids() -> _McpToolIds:
    """Load (or memoize) the per-mode MCP allow-lists from the EE agent layer.

    Degrades to all-``None`` (no MCP restriction, ``loaded=False``) if the
    import fails — identical to the pre-SR-2 ``_build_profiles`` try/except.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from pocketpaw_ee.agent.mcp_servers.ask import ASK_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.browser import BROWSER_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.craft import TOOL_IDS as CRAFT_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.files import FILES_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.foresight import FORESIGHT_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.fx import FX_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.icons import ICON_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.inspo import INSPO_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.loom import LOOM_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.media import MEDIA_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.palette import PALETTE_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.refero import REFERO_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.ship import SHIP_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.site_media import SITE_MEDIA_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.stock_images import STOCK_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.surfaces import SURFACES_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.timeline import TIMELINE_TOOL_IDS

        # /sites scopes to the sites-manager tools PLUS the authoring TOOLBELT the
        # crew (and the create-svelte-site skill) needs on-surface: stock photos,
        # icons, and palette derivation. These live on ambient in-process
        # servers, but the per-surface allow-list is a hard
        # whitelist (claude_sdk `allow_mcp_tool_ids`), so an id absent here is
        # FILTERED OUT on /sites — the tool would be silently unreachable. Named
        # here so authoring can actually call them.
        #
        # MEDIA (``MEDIA_TOOL_IDS``) still does NOT belong here, and adding it would
        # not work anyway: media.py sinks to the PRIVATE adapter and returns a
        # backend-relative /api/v1/media/<name>, which resolves against the PUBLISHED
        # site's own domain and 404s. SITE_MEDIA is the sibling with the right sink
        # (sites.public_assets — absolute, unsigned, tenant-scoped, immutable) and no
        # chat-canvas gallery pocket. Stock stays FIRST in the sourcing ladder because
        # it is free and instant; generation is for what stock cannot supply.
        sites_allow = (
            frozenset(SITES_TOOL_IDS)
            | frozenset(STOCK_TOOL_IDS)
            # Design research over real shipped pages. Named here because the
            # create preamble's PHASE 1b commands these tools unconditionally —
            # this list is a hard whitelist, so an id absent from it is silently
            # unreachable and the instruction would command nothing.
            | frozenset(INSPO_TOOL_IDS)
            | frozenset(SITE_MEDIA_TOOL_IDS)
            | frozenset(ICON_TOOL_IDS)
            | frozenset(FX_TOOL_IDS)
            | frozenset(PALETTE_TOOL_IDS)
            # ask_user: interactive question chips. Needed most on svelte-create
            # (ripple OFF) where the agent otherwise can only ask in plain text.
            | frozenset(ASK_TOOL_IDS)
            # files: /sites is where people upload a BRIEF, and an attachment is
            # inlined only for the turn it arrived on. Without these ids the
            # follow-up turn ("use the brief I sent") has no way back to it —
            # this list is a hard whitelist, so ambient is not enough here.
            | frozenset(FILES_TOOL_IDS)
            # refero: design research. /sites is the surface that chooses a
            # VISUAL DIRECTION, and this list is a hard whitelist, so the
            # ambient server is not enough — an id absent here is unreachable.
            # The tools return empty when no Refero token is configured, so
            # naming them costs an unconfigured deploy nothing.
            | frozenset(REFERO_TOOL_IDS)
            # Opt-in EXTERNAL servers (``POCKETPAW_SITES_MCP_SERVERS``), for a
            # server the operator configured themselves. Empty by default.
            | _external_sites_mcp_grants()
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
            # /browser scopes to the agentic-browser verbs. Crossed over as a
            # plain frozenset[str] — no pocketpaw_ee symbol leaks into OSS.
            browser_allow=frozenset(BROWSER_TOOL_IDS),
            # open_surface rides along: "pick another clip" is a trip to /files,
            # and this list is a hard whitelist, so the ambient server alone
            # would be filtered out here.
            timeline_allow=frozenset(TIMELINE_TOOL_IDS) | frozenset(SURFACES_TOOL_IDS),
            craft_allow={app: frozenset({tid}) for app, tid in CRAFT_TOOL_IDS.items()},
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
            browser_allow=None,
        )


def _mcp_tool_ids() -> _McpToolIds:
    global _MCP_TOOL_IDS_CACHE
    if _MCP_TOOL_IDS_CACHE is None:
        _MCP_TOOL_IDS_CACHE = _load_mcp_tool_ids()
    return _MCP_TOOL_IDS_CACHE


# Loaded and memoized SEPARATELY from ``_mcp_tool_ids()`` — see
# ``browser_tool_ids``. ``False`` is not a valid cached value, so a plain
# ``None``-means-unset sentinel is enough.
_BROWSER_TOOL_IDS_CACHE: frozenset[str] | None = None


def browser_tool_ids() -> frozenset[str]:
    """The agentic-browser MCP tool ids, or an empty set if THEIR import failed.

    Read by ``service.resolve_profile`` to DENY these ids on every non-browser
    surface, which makes this the one tool-id loader whose degrade path opens a
    hole instead of closing one. It therefore imports on its OWN, and is NOT
    served from ``_mcp_tool_ids()``.

    Why that matters: ``_load_mcp_tool_ids`` wraps ten sibling imports in a
    single ``try/except Exception``. Sharing it would mean an unrelated module
    (palette, stock_images, icons, ...) failing to import empties this set and
    silently turns the deny into a no-op — while ``CloudBrowserMcpProvider``,
    which imports ``mcp_servers.browser`` on its own independent path, still
    registers the server. Net effect: the browser becomes reachable from /chat,
    beside the send-capable connector tools, behind nothing but a warning log.
    That was a real fail-open (verified by simulating a palette ImportError),
    not a theoretical one.

    Loading here means the ONLY failure that empties this set is a failure of
    ``mcp_servers.browser`` itself — which also breaks ``build_browser_server``,
    so no browser tools exist to reach and the empty deny is genuinely safe.
    """
    global _BROWSER_TOOL_IDS_CACHE
    if _BROWSER_TOOL_IDS_CACHE is None:
        try:
            from pocketpaw_ee.agent.mcp_servers.browser import BROWSER_TOOL_IDS

            _BROWSER_TOOL_IDS_CACHE = frozenset(BROWSER_TOOL_IDS)
        except Exception:  # noqa: BLE001 — the server cannot have loaded either
            import logging

            logging.getLogger(__name__).warning(
                "surface: could not load browser tool ids; browser deny disabled "
                "(the browser MCP server cannot have loaded either)",
                exc_info=True,
            )
            _BROWSER_TOOL_IDS_CACHE = frozenset()
    return _BROWSER_TOOL_IDS_CACHE


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


def _studio_editor_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    # Studio editor: arrange an existing timeline. Ripple OFF for the same
    # reason /studio is — the deliverable is a cut, not a dashboard. Scoped to
    # the timeline verbs, which deliberately EXCLUDES the media-generation tools:
    # this surface arranges what exists, and generating here would answer
    # "arrange these clips" with a new clip. The one thing it creates is a
    # HyperFrames motion graphic (add_motion_graphic): hyperframes-core is the
    # composition contract, studio-motion the house styles and presets.
    return SurfaceProfile(
        ripple_mode="off",
        allow_mcp_tool_ids=_mcp_tool_ids().timeline_allow,
        skill_names=frozenset({"studio-editor", "hyperframes-core", "studio-motion"}),
    )


def _studio_craft_profile(app: str) -> ProfileResolver:
    # Craft studios (vector / photo / design): edit print work. Ripple OFF (the
    # deliverable is a design, not a dashboard) and scoped to that app's
    # edit_<app> verb alone.
    def resolve(_meta: SurfaceMeta) -> SurfaceProfile:
        allow = _mcp_tool_ids().craft_allow
        return SurfaceProfile(
            ripple_mode="off",
            allow_mcp_tool_ids=allow.get(app) if allow is not None else None,
        )

    return resolve


def _browser_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    # Browser: drive a real browser from chat. Ripple OFF, same as /studio.
    # It shipped as "trim", but "trim" is declared and never consumed
    # (``agent_service`` only checks ``== "off"``), so the agent received the
    # FULL inline ripple LAW — whose first rule is "default to ui-spec" — and
    # answered a comparison as an inline ```ui-spec``` block in the chat rail.
    # No pocket was created, no ``pocket_created`` fired, and the canvas stayed
    # on its empty state (live smoke, 2026-09-06). With the LAW off, the
    # /browser preamble owns the output shape: prose in chat, and a REAL pocket
    # via ``mcp__pocketpaw_pocket_specialist__create`` (still granted — the
    # pocket-creation grant is a tool grant, independent of the prompt) whose
    # persist path pushes ``pocket_created``, which is what the route listens
    # for. The MCP allow-list is the browser verbs; every OTHER surface gets
    # these same ids as a DENY (applied centrally in ``service.resolve_profile``),
    # which is the half that makes the scoping a boundary rather than a
    # preference.
    return SurfaceProfile(
        ripple_mode="off",
        allow_mcp_tool_ids=_mcp_tool_ids().browser_allow,
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


def _agent_health_profile(_meta: SurfaceMeta) -> SurfaceProfile:
    # Agent health: ripple stays ON (a cost or failure chart is a fair answer).
    # ``allowed_sdk_tools`` carries the lens tool ids, which is what GRANTS the
    # surface-scoped ``pocketpaw_lens`` server: core registers it only on a run
    # whose allow set names its tools, so no other surface loads it. Lazy import,
    # degrading to no grant (lens tools absent) rather than breaking chat.
    try:
        from pocketpaw_ee.agent.mcp_servers.lens import LENS_TOOL_IDS
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("surface: could not load lens tool ids", exc_info=True)
        return SurfaceProfile(ripple_mode="on")
    return SurfaceProfile(ripple_mode="on", allowed_sdk_tools=frozenset(LENS_TOOL_IDS))


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
# The deny set alone could not close the MCP side: composio CONNECTOR tool ids
# are dynamic/per-workspace, survive the always-allowed ``composio`` server, and
# the pydantic_ai backend bridges ~60 PocketPaw builtins no deny set named. Since
# 2026-09-27 the profile is ``exclusive_tools=True`` (deny-by-default), which is
# the "public/untrusted" lockdown this note used to ask for; the deny set below
# now does the one job exclusivity cannot, removing base SDK built-ins.
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
        # Deny-by-default: ONLY ``allow`` above. The deny set covers the base SDK
        # built-ins; this is what removes everything else — the universal
        # pocket grant and always-allowed servers on claude_sdk, and every
        # bridged PocketPaw builtin on pydantic_ai.
        exclusive_tools=True,
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
    # A whole repo from a base template (engine ``project``); edits files, not widgets.
    "project": "pocketpaw-create-project-site",
}

# 2026-09-08 — the create-scoped design skills the svelte/react branch must ALSO
# name. ``skill_names`` is an exact allowlist, not an addition: a non-empty set
# suppresses the wholesale bundled plugin (``claude_sdk._should_load_bundled_plugin``
# returns ``enabled and not skill_names``), so this branch has always run with
# exactly ONE skill and every other bundled skill withheld. That suppression is
# the feature — it is what stops ``pocketpaw-create-pocket`` firing on "build an
# app with components and nice design" mid-site-build — so widening it back to the
# full bundled set is NOT the fix. Naming the four create-scoped design skills
# takes this branch from one skill to five and leaves the other bundled skills
# withheld, which is the intent.
#
# It is sourced from ``handlers/sites.py`` rather than re-listed here because the
# create preamble ADVERTISES these same names, and a preamble naming a skill this
# set omits would point the agent at something it cannot load. One list, two
# readers, pinned by
# ``test_sites_create_skill_names_cover_the_advertised_skills``.
_SITES_CREATE_DESIGN_SKILLS: frozenset[str] = sites.create_design_skill_names()

#: Engines whose refine edits a ``source`` map (no widget spec), so refine drops ripple.
_SITES_SOURCE_REFINE_ENGINES: frozenset[str] = frozenset({"html", "svelte", "react", "project"})


def _sites_profile(meta: SurfaceMeta) -> SurfaceProfile:
    """/sites is META-AWARE — inline ripple stays ON only where the site IS a ripple
    spec (ripple create, ripple refine, or a refine whose engine is unknown).

      * refine (``meta.pocket_id`` set) of an html / svelte / react site edits a
        ``source`` map, not a widget spec → DROP ripple (about 10k tokens of widget
        catalog + delegation rule per turn the edit never uses). It asks through
        ``ask_user``, exactly like html create. On refine ``meta.engine`` is the
        SOURCE POCKET's engine, stamped server-side by
        ``run_core._resolve_entity_profile``; refine of a ripple site, or one whose
        engine could not be read, KEEPS ripple (sites default). Refine wins over the
        create branches below: a ``pocket_id`` present is never a create.
      * create + svelte/react (``meta.engine`` in ``_SITES_AUTHORING_SKILL``, no
        ``pocket_id``) hand-authors components → DROP ripple, deny the two
        ripple-create tools, surface that engine's authoring skill.
      * create + html (``engine`` None/"html", no ``pocket_id``) → DROP ripple
        (it asks through ``ask_user``); create + ripple → KEEP ripple.

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
    if meta.pocket_id is not None and meta.engine in _SITES_SOURCE_REFINE_ENGINES:
        return SurfaceProfile(
            ripple_mode="off",
            allow_mcp_tool_ids=sites_allow,
            deny_mcp_tool_ids=_SITES_BUILTIN_DENY,
        )
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
            #
            # UNION the create-scoped design skills (2026-09-08): because the set
            # is an exact allowlist, the authoring skill alone would leave the
            # create preamble's <design-skills> block naming four skills this
            # branch cannot load. The other bundled skills stay withheld.
            skill_names=frozenset({authoring_skill}) | _SITES_CREATE_DESIGN_SKILLS,
        )
    # html create (the default engine) hand-authors a static page, so it has no use
    # for inline ripple: the widget catalog rode every html create only to render one
    # ask widget. It asks through ``ask_user`` like svelte/react (2026-09-27,
    # feat/sites-lean-prompt). Skills stay wholesale (no ``skill_names``).
    if meta.pocket_id is None and (meta.engine or "html") == "html":
        return SurfaceProfile(
            ripple_mode="off",
            allow_mcp_tool_ids=sites_allow,
            deny_mcp_tool_ids=_SITES_BUILTIN_DENY,
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
        SurfaceKind.STUDIO_EDITOR,
        _route_for(SurfaceKind.STUDIO_EDITOR),
        studio_editor.build_preamble,
        profile_resolver=_studio_editor_profile,
    ),
    SurfaceSpec(
        SurfaceKind.STUDIO_VECTOR,
        _route_for(SurfaceKind.STUDIO_VECTOR),
        studio_craft.build_vector_preamble,
        profile_resolver=_studio_craft_profile("vector"),
    ),
    SurfaceSpec(
        SurfaceKind.STUDIO_PHOTO,
        _route_for(SurfaceKind.STUDIO_PHOTO),
        studio_craft.build_photo_preamble,
        profile_resolver=_studio_craft_profile("photo"),
    ),
    SurfaceSpec(
        SurfaceKind.STUDIO_DESIGN,
        _route_for(SurfaceKind.STUDIO_DESIGN),
        studio_craft.build_design_preamble,
        profile_resolver=_studio_craft_profile("design"),
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
        # ``allow_mcp_tool_ids`` carries exactly one id (2026-08-28): the
        # illustration tool. It was true that this surface had no server-side
        # tools — the page-ops block is parsed client-side — until generated
        # vector illustrations needed a generator call the client cannot make.
        # The allow-list is how the tool stays reachable HERE and nowhere else:
        # it costs money per call, and no other surface has a page to draw on.
        # No ``allowed_sdk_tools``
        # either — that field is ADDITIVE and ``Read`` is already in the default
        # set, which matters a lot on this surface: ``Read`` IS the vision path.
        # Static profile: no lazily-loaded ids, not meta-aware.
        profile=SurfaceProfile(
            ripple_mode="off",
            # Two deny sets, one field: the pocket MCP ids that make a pocket
            # unreachable, and the bridged builtins the prompt never names
            # (``_OTHER_HAND_BRIDGED_DENY`` — the token cut).
            deny_mcp_tool_ids=_OTHER_HAND_POCKET_DENY | _OTHER_HAND_BRIDGED_DENY,
            allow_mcp_tool_ids=frozenset({ILLUSTRATE_TOOL_ID}),
            system_message_override=OTHER_HAND_SYSTEM_PROMPT,
        ),
    ),
    SurfaceSpec(
        SurfaceKind.CONCIERGE,
        _route_for(SurfaceKind.CONCIERGE),
        concierge.build_preamble,
        profile_resolver=_concierge_profile,
    ),
    SurfaceSpec(
        SurfaceKind.BROWSER,
        _route_for(SurfaceKind.BROWSER),
        browser.build_preamble,
        profile_resolver=_browser_profile,
    ),
    SurfaceSpec(
        SurfaceKind.AGENT_HEALTH,
        _route_for(SurfaceKind.AGENT_HEALTH),
        agent_health_handler.build_preamble,
        profile_resolver=_agent_health_profile,
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
