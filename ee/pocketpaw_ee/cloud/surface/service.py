# service.py — Surface context resolver and handler dispatch.
#
# Created: 2026-05-24 — ``resolve_surface_context(workspace_id, user_id,
# body)`` validates the client's ``{surface, meta}`` hint, maps the
# string to a ``SurfaceKind``, and dispatches to a per-kind handler that
# builds the preamble. Per Rule 5 of the cloud entity rules, this is a
# module-level async function (not a class) and validation runs at entry
# via ``SurfaceRequest.model_validate``.
#
# Failure stays inert: any handler error logs and returns a
# ``GENERIC`` context with empty preamble. The chat path is the consumer
# — never let a surface failure break a chat send.
#
# Changes: 2026-06-05 (feat/surface-profile-bias-kill) — added
# ``resolve_profile(surface_kind, meta) -> SurfaceProfile``, the resolver
# for the new per-surface policy descriptor (the data backbone of the
# "ripple-default bias" fix). It is a PURE lookup (sync — no I/O), so unlike
# ``resolve_surface_context`` it never touches Mongo or a handler.
# Changes: 2026-06-05 (feat/sites-svelte-engine) — make ``resolve_profile``
# META-AWARE on the /sites row. PR 1's static ``_PROFILES[SITES]`` entry turned
# ripple OFF for EVERY /sites meta, but /sites carries THREE modes and only the
# svelte-CREATE one should lose ripple. The resolver now special-cases SITES and
# branches on ``meta``:
#   * create + svelte  (``meta.engine == "svelte"``, no ``pocket_id``) → ripple
#     OFF, deny the two ripple-create tools, surface the create-svelte-site skill
#     (hand-authored SvelteKit — the "default to ui-spec" LAW is wrong here);
#   * create + ripple  (``engine`` None/"ripple", no ``pocket_id``) → DEFAULT
#     profile (ripple ON, no deny) — it AUTHORS a ripple landing page;
#   * refine           (``meta.pocket_id`` set, ANY engine) → DEFAULT profile —
#     it EDITS the existing ripple landing spec via ``pocket_specialist__edit``.
# Refine WINS over engine: a ``pocket_id`` present means refine even when
# ``engine="svelte"`` is also stamped. Every non-sites kind AND any unmapped kind
# still falls through to ``_DEFAULT_PROFILE`` (ripple on) — today's behavior, zero
# regression. The deny set on the svelte row is now ENFORCED end-to-end (it is
# threaded to the OSS backend's ``run`` via ``deny_mcp_tool_ids`` — see
# ``run_core._drive_agent_loop`` → ``AgentPool.run`` → ``ClaudeSDKBackend.run``).

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud.surface.domain import (
    SurfaceContext,
    SurfaceKind,
    SurfaceMeta,
    SurfaceProfile,
)
from pocketpaw_ee.cloud.surface.dto import SurfaceMetaRequest, SurfaceRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Surface profile resolution (the "ripple-default bias" policy table)
#
# A SurfaceProfile is the per-surface behavioral policy the chat agent
# applies. ``resolve_profile`` resolves it from the surface kind (+ meta) via
# a static table, special-casing /sites. Keep this a PURE function (no I/O):
# it runs once per request on the hot chat path and must not block on Mongo or
# a handler.
#
# Non-sites surfaces use the static table below; only ones whose policy DIFFERS
# from the default appear there. /sites is META-AWARE (three modes — see
# ``resolve_profile``). Everything else — and any unmapped/future kind — gets
# ``_DEFAULT_PROFILE`` (ripple on, no denies, no skills), which is exactly
# today's behavior. That is the zero-regression guarantee.
# ---------------------------------------------------------------------------

# Ripple on, no surface-specific policy. The behavior every surface had
# before this primitive existed. Also the profile for the /sites ripple-create
# and refine modes (both author/edit a ripple landing spec, so they KEEP ripple
# and deny nothing).
_DEFAULT_PROFILE = SurfaceProfile(ripple_mode="on")

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

# Per-mode MCP-tool allow-lists keep a chat mode's agent context lean: only the
# mode's SPECIALIZED tools are named here. The "general everywhere" set — ripple
# widgets, the pocket lifecycle (read/create/edit/plan), and connectors
# (composio) — is kept by the OSS backend itself (``ALWAYS_ALLOWED_MCP_SERVERS``
# + the pocket-creation grant + widget tools), so a scoped mode still renders
# UI, makes pockets, and uses connectors. Chat stays unmapped → unrestricted.
#
# Built lazily + memoized (not at import): pulling the EE mcp-server tool-id
# constants at module load could cycle with the agent layer, and ``resolve_profile``
# is on the hot path. Built once on first call; a failed import degrades to
# ripple-only profiles (unrestricted tools) so tool-scoping can never break chat.
_PROFILE_CACHE: dict[str, Any] | None = None


def _build_profiles() -> dict[str, Any]:
    sites_allow: frozenset[str] | None
    foresight_allow: frozenset[str] | None
    try:
        from pocketpaw_ee.agent.mcp_servers.foresight import FORESIGHT_TOOL_IDS
        from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS

        foresight_allow = frozenset(FORESIGHT_TOOL_IDS)
        sites_allow = frozenset(SITES_TOOL_IDS)
    except Exception:  # noqa: BLE001 — degrade to unrestricted, never break chat
        logger.warning(
            "surface: could not load mcp tool ids; per-mode scoping disabled",
            exc_info=True,
        )
        foresight_allow = None
        sites_allow = None

    return {
        "by_kind": {
            # Foresight: its scenario tools + the general-everywhere set.
            SurfaceKind.FORESIGHT: SurfaceProfile(
                ripple_mode="on", allow_mcp_tool_ids=foresight_allow
            ),
            # Files: no specialized MCP tools — document scaffolding runs on the
            # built-in Read/Write/Edit tools (never filtered). Empty allow-list
            # = general-everywhere only (drops the other modes' tools). ``None``
            # when the import degraded, so chat-like breadth is the safe fallback.
            SurfaceKind.FILES: SurfaceProfile(
                ripple_mode="on",
                allow_mcp_tool_ids=frozenset() if sites_allow is not None else None,
            ),
        },
        # /sites is meta-aware (below). Both modes scope to the sites authoring
        # tools + general. svelte-create additionally denies the two ripple-create
        # tools (deny runs AFTER allow, so create_svelte survives and the ripple
        # variants don't) and drops ripple + surfaces the svelte skill.
        "sites_default": SurfaceProfile(ripple_mode="on", allow_mcp_tool_ids=sites_allow),
        "sites_svelte_create": SurfaceProfile(
            ripple_mode="off",
            deny_mcp_tool_ids=_SITES_SVELTE_CREATE_DENY,
            allow_mcp_tool_ids=sites_allow,
            skill_names=frozenset({"create-svelte-site"}),
        ),
    }


def _profiles() -> dict[str, Any]:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        _PROFILE_CACHE = _build_profiles()
    return _PROFILE_CACHE


def resolve_profile(surface_kind: SurfaceKind, meta: SurfaceMeta) -> SurfaceProfile:
    """Resolve a ``SurfaceKind`` (+ ``meta``) to its behavioral ``SurfaceProfile``.

    Pure lookup (memoized table; no I/O), safe to call once per request on the
    hot chat path.

    /sites is META-AWARE — it carries three modes, and only the svelte-CREATE
    one loses ripple:

      * refine (``meta.pocket_id`` set, ANY engine) edits the existing ripple
        landing spec → KEEP ripple (``sites_default``). Refine WINS over
        engine: a ``pocket_id`` present means refine even if ``engine="svelte"``.
      * create + svelte (``meta.engine == "svelte"``, no ``pocket_id``)
        hand-authors SvelteKit → DROP ripple, deny the two ripple-create tools,
        surface the create-svelte-site skill (``sites_svelte_create``).
      * create + ripple (``engine`` None/"ripple", no ``pocket_id``) authors a
        ripple landing page → KEEP ripple (``sites_default``).

    Foresight and Files carry a per-mode MCP allow-list (lean tool set). Every
    other kind (and any unmapped/future kind) returns ``_DEFAULT_PROFILE``
    (``ripple_mode="on"``, no tool restriction) — today's behavior, e.g. /chat.
    """
    profiles = _profiles()
    if surface_kind == SurfaceKind.SITES:
        # refine (pocket_id) edits the existing ripple landing spec → keep ripple.
        # It wins over engine, so check it FIRST.
        if meta.pocket_id is None and meta.engine == "svelte":
            return profiles["sites_svelte_create"]
        return profiles["sites_default"]
    return profiles["by_kind"].get(surface_kind, _DEFAULT_PROFILE)


# Handler registry: SurfaceKind -> async callable returning the preamble.
# Built lazily on first use so import-time failures in a handler module
# don't block the rest of the resolver.
_HANDLERS: dict[SurfaceKind, Any] | None = None


def _load_handlers() -> dict[SurfaceKind, Any]:
    """Lazy import every per-kind handler. Missing handlers are skipped.

    We tolerate missing handler modules instead of raising at import time
    because the surface module ships independently of the surfaces it
    knows about — a fresh deploy that drops a handler module shouldn't
    take the whole chat path down.
    """
    from pocketpaw_ee.cloud.surface.handlers import (
        activity,
        audit,
        calendar,
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

    return {
        SurfaceKind.HOME: home.build_preamble,
        SurfaceKind.POCKETS_LIST: pockets_list.build_preamble,
        SurfaceKind.POCKET: pocket.build_preamble,
        SurfaceKind.POCKET_WIDGET: pocket_widget.build_preamble,
        SurfaceKind.MISSION_CONTROL: mission_control.build_preamble,
        SurfaceKind.FILES: files.build_preamble,
        SurfaceKind.AUDIT: audit.build_preamble,
        SurfaceKind.ACTIVITY: activity.build_preamble,
        SurfaceKind.AGENTS: agents_handler.build_preamble,
        SurfaceKind.AGENT: agent_handler.build_preamble,
        SurfaceKind.KNOWLEDGE: knowledge.build_preamble,
        SurfaceKind.CALENDAR: calendar.build_preamble,
        SurfaceKind.CHAT: chat_handler.build_preamble,
        SurfaceKind.QUICKASK: quickask.build_preamble,
        SurfaceKind.SETTINGS: settings.build_preamble,
        SurfaceKind.SIDEPANEL: sidepanel.build_preamble,
        SurfaceKind.FORESIGHT: foresight_handler.build_preamble,
        SurfaceKind.SITES: sites.build_preamble,
        SurfaceKind.GENERIC: generic.build_preamble,
    }


def _resolve_kind(value: str | None) -> SurfaceKind:
    """Map an inbound string to a ``SurfaceKind``. Unknown -> ``GENERIC``.

    Stay liberal in what we accept (clients can ship a new surface name
    before the backend ships its handler) and conservative in what we
    emit (the agent always gets a usable preamble).
    """
    if value is None:
        return SurfaceKind.GENERIC
    try:
        return SurfaceKind(value)
    except ValueError:
        logger.debug("unknown surface kind %r — falling back to GENERIC", value)
        return SurfaceKind.GENERIC


def _meta_from_request(req: SurfaceMetaRequest) -> SurfaceMeta:
    """Pydantic -> domain meta. Trivial pass-through."""
    return SurfaceMeta(
        pocket_id=req.pocket_id,
        widget_id=req.widget_id,
        focus_node_id=req.focus_node_id,
        agent_id=req.agent_id,
        file_id=req.file_id,
        route_path=req.route_path,
        run_id=req.run_id,
        scenario_id=req.scenario_id,
        panel=req.panel,
        site_id=req.site_id,
        engine=req.engine,
    )


async def resolve_surface_context(
    workspace_id: str, user_id: str, body: dict[str, Any] | SurfaceRequest | None
) -> SurfaceContext:
    """Resolve a client's surface hint into a rendered ``SurfaceContext``.

    Always returns a context — never raises. Errors are absorbed:

      * Invalid body shape (wrong fields, bad types) -> ``GENERIC`` with
        empty preamble.
      * Unknown surface kind -> ``GENERIC`` (still gets a tiny preamble).
      * Handler raised -> ``GENERIC`` with empty preamble; the error is
        logged at ``exception`` so it's discoverable but doesn't break
        the chat send.

    The dispatcher passes the validated meta and the tenancy tuple to
    every handler so individual handlers don't have to re-derive them.
    """
    global _HANDLERS

    # Step 1: validate the body. Bad input is logged at debug and the
    # caller gets a GENERIC context with empty preamble.
    try:
        validated = SurfaceRequest.model_validate(body or {})
    except Exception:
        logger.debug("surface body failed validation; using GENERIC", exc_info=True)
        return SurfaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            kind=SurfaceKind.GENERIC,
            meta=SurfaceMeta(),
            preamble="",
        )

    kind = _resolve_kind(validated.surface)
    meta = _meta_from_request(validated.meta)

    # Step 2: lazy-load the handler registry. Import errors here propagate
    # because they indicate a broken deploy — surface a clear failure
    # rather than silently swallowing every surface preamble.
    if _HANDLERS is None:
        _HANDLERS = _load_handlers()
    handler = _HANDLERS.get(kind)
    if handler is None:
        # Resolver has a SurfaceKind without a handler. Treat the same as
        # an unknown surface — graceful GENERIC fall-back, no crash.
        logger.warning("no handler registered for surface kind %s", kind.value)
        return SurfaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            kind=SurfaceKind.GENERIC,
            meta=meta,
            preamble="",
        )

    # Step 3: render the preamble. Handler exceptions are absorbed —
    # we'd rather ship a chat with no surface context than fail the send.
    try:
        preamble = await handler(workspace_id, user_id, meta)
    except Exception:
        logger.exception("surface handler %s failed; using GENERIC preamble", kind.value)
        return SurfaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            kind=SurfaceKind.GENERIC,
            meta=meta,
            preamble="",
        )

    return SurfaceContext(
        workspace_id=workspace_id,
        user_id=user_id,
        kind=kind,
        meta=meta,
        preamble=preamble or "",
    )


__all__ = ["resolve_surface_context", "resolve_profile"]
