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
# Changes: 2026-06-06 (feat/entity-pocket-profile-field, entity-rooms chunk ①)
# — added ``compose_entity_profile(base, override)``, the PURE (no-I/O) helper
# that folds an entity pocket's ``PocketSurfaceProfile`` override (the
# JSON-shaped dict from ``Pocket.surface_profile``, lists not frozensets) OVER a
# base ``SurfaceProfile`` resolved from the surface kind. Precedence: ripple_mode
# entity-wins-if-set else base; deny_mcp_tool_ids UNION (hard cap grows);
# allowed_sdk_tools / skill_names UNION; system_message_override entity-wins-if-set.
# A ``None`` / empty override returns the base unchanged → zero regression for the
# no-pocket / legacy path. ``resolve_profile`` stays PURE/no-I/O (the hot lookup);
# the actual once-per-run async pocket load that supplies the override dict lives
# tenant-scoped in ``run_core.execute_run`` (entity I/O never enters this module).
# Changes: 2026-06-10 (feat/studio-code-migration) — registered the STUDIO and
# CODE surfaces: their handlers (``studio.build_preamble`` / ``code.build_preamble``)
# join ``_load_handlers``, and ``_build_profiles`` gives both a ripple-OFF
# ``SurfaceProfile`` so the agent generates media / edits code instead of
# defaulting to a ripple ui-spec dashboard. STUDIO scopes ``allow_mcp_tool_ids``
# to the media tools (``MEDIA_TOOL_IDS`` loaded as a plain frozenset[str] inside
# the existing try/except — no EE symbol crosses into OSS) and surfaces the
# ``studio`` skill; CODE sets an ``allowed_sdk_tools`` allowlist (Bash/Read/Write/
# Edit/Glob/Grep) and surfaces the ``code`` skill. Both are plain ``by_kind``
# entries (not meta-aware like /sites).
# Changes: 2026-06-10 (feat/belt-surface, BS-2 Belt & Pulley stations thin
# slice) — registered the BELT surface (the develop station). Its handler
# (``belt.build_preamble``) joins ``_load_handlers``, and ``_build_profiles``
# gives it a ripple-OFF ``SurfaceProfile`` so the agent runs the station loop
# instead of building a dashboard: ``allowed_sdk_tools`` = the coding built-ins
# (Bash/Read/Write/Edit/Glob/Grep), ``skill_names`` = {"belt"}, and
# ``allow_mcp_tool_ids`` = the loom orientation tools (``LOOM_TOOL_IDS``, loaded
# in the existing try/except as a plain frozenset[str]) UNION the Instinct gate
# tool (``_BELT_GATE_TOOL_IDS``). The gate tool id is a literal here because its
# constant lives in a SIBLING branch's ``agent/mcp_servers/belt.py`` — the
# import reconciles when both PRs land.

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
# applies. ``resolve_profile`` resolves it from the surface kind (+ meta) by
# reading the ``SurfaceSpec`` for that kind off the declarative ``SURFACES``
# registry (SR-2): ``spec.profile_resolver(meta)`` if the row carries one (the
# meta-aware /sites row and the rows that need the lazily-loaded per-mode MCP
# tool ids), else the static ``spec.profile``, else ``_DEFAULT_PROFILE``. Keep
# this a PURE function (no I/O): it runs once per request on the hot chat path
# and must not block on Mongo or a handler.
#
# Only rows whose policy DIFFERS from the default carry a ``profile`` /
# ``profile_resolver``; everything else — and any unmapped/future kind — gets
# ``_DEFAULT_PROFILE`` (ripple on, no denies, no skills), which is exactly
# today's behavior. That is the zero-regression guarantee. The lazy + memoized
# MCP-tool-id load and the per-row profile builders live in
# ``surface_registry`` now (the single source of truth for surfaces).
# ---------------------------------------------------------------------------

# Ripple on, no surface-specific policy. The behavior every surface had
# before this primitive existed. Also the profile for the /sites ripple-create
# and refine modes (both author/edit a ripple landing spec, so they KEEP ripple
# and deny nothing) — those are built by the registry's ``_sites_profile``.
# Changes: 2026-06-22 (feat/surface-registry-backend, SR-1) — ``_load_handlers``
# now builds its ``SurfaceKind -> build_preamble`` dispatch table by LOOPING over
# the declarative ``SURFACES`` registry (``surface.surface_registry``) instead of a
# hand-written literal dict.
# Changes: 2026-06-22 (feat/surface-registry-backend-profiles, SR-2) —
# ``resolve_profile`` now SOURCES profiles from the same ``SURFACES`` registry
# instead of a local ``_build_profiles`` table: it indexes the row for the kind
# and applies ``profile_resolver(meta)`` / ``profile`` / ``_DEFAULT_PROFILE`` in
# that order. Behavior is byte-identical for every ``(kind, meta)`` (the /sites
# meta-fork and the lazy + memoized MCP-tool-id load moved verbatim into
# ``surface_registry``). ``compose_entity_profile`` is unchanged.
_DEFAULT_PROFILE = SurfaceProfile(ripple_mode="on")

# Cached ``SurfaceKind -> SurfaceSpec`` index, built once on first
# ``resolve_profile`` from the lazily-imported registry (the same deferral
# ``_load_handlers`` uses so a broken handler import can't break import time).
_SPEC_BY_KIND: dict[SurfaceKind, Any] | None = None


def _spec_by_kind() -> dict[SurfaceKind, Any]:
    global _SPEC_BY_KIND
    if _SPEC_BY_KIND is None:
        from pocketpaw_ee.cloud.surface.surface_registry import SURFACES

        _SPEC_BY_KIND = {spec.kind: spec for spec in SURFACES}
    return _SPEC_BY_KIND


def resolve_profile(surface_kind: SurfaceKind, meta: SurfaceMeta) -> SurfaceProfile:
    """Resolve a ``SurfaceKind`` (+ ``meta``) to its behavioral ``SurfaceProfile``.

    Pure lookup — no I/O, safe to call once per request on the hot chat path.
    Sourced from the declarative ``SURFACES`` registry (SR-2): the row for the
    kind supplies the profile via ``profile_resolver(meta)`` (meta-aware rows
    and rows that need the lazily-loaded per-mode MCP tool ids), else its static
    ``profile``, else ``_DEFAULT_PROFILE``.

    /sites is META-AWARE — its row carries a ``profile_resolver`` that branches
    on ``meta`` across three modes, and only the svelte-CREATE one loses ripple:

      * refine (``meta.pocket_id`` set, ANY engine) edits the existing ripple
        landing spec → KEEP ripple. Refine WINS over engine: a ``pocket_id``
        present means refine even if ``engine="svelte"``.
      * create + svelte (``meta.engine == "svelte"``, no ``pocket_id``)
        hand-authors SvelteKit → DROP ripple, deny the two ripple-create tools,
        surface the create-svelte-site skill.
      * create + ripple (``engine`` None/"ripple", no ``pocket_id``) authors a
        ripple landing page → KEEP ripple.

    Every other kind (and any unmapped/future kind, and every kind whose row
    carries no profile) returns ``_DEFAULT_PROFILE`` (``ripple_mode="on"``), so
    the only surface that deviates from a plain ripple-on default is /sites
    svelte-create plus the explicitly-scoped FORESIGHT / FILES / STUDIO / CODE /
    BELT rows — exactly today's behavior.
    """
    spec = _spec_by_kind().get(surface_kind)
    if spec is None:
        return _DEFAULT_PROFILE
    if spec.profile_resolver is not None:
        return spec.profile_resolver(meta)
    if spec.profile is not None:
        return spec.profile
    return _DEFAULT_PROFILE


def compose_entity_profile(base: SurfaceProfile, override: dict[str, Any] | None) -> SurfaceProfile:
    """Fold an entity pocket's ``surface_profile`` override OVER a base profile.

    PURE — no I/O. ``base`` is the surface-kind profile from ``resolve_profile``;
    ``override`` is the JSON-shaped dict persisted on ``Pocket.surface_profile``
    (the ``PocketSurfaceProfile`` mirror, with plain ``list``s where the
    descriptor wants ``frozenset``s, and ``ripple_mode``/``allowed_sdk_tools``/
    ``system_message_override`` optionally ``None`` = "no opinion").

    Compose precedence (the entity-rooms payoff):

      * ``ripple_mode`` — entity wins WHEN SET (non-``None``), else base. A
        ``None`` override means "no opinion — fall back to the surface default."
      * ``deny_mcp_tool_ids`` — UNION. The entity's denies ADD to the base's;
        the hard cap can only GROW, never shrink (an entity can't re-enable a
        tool a surface forbade).
      * ``allowed_sdk_tools`` — UNION across the two sides, treating ``None`` as
        "no contribution." If BOTH sides are ``None`` the result stays ``None``
        (no surface restriction); an empty frozenset would wrongly deny all.
      * ``skill_names`` — UNION (the entity adds its skills to the base set).
      * ``system_message_override`` — entity wins WHEN SET, else base.

    ``override`` of ``None`` (no per-entity profile) returns ``base`` unchanged —
    the no-pocket / legacy path, byte-identical to today's behavior.
    """
    if override is None:
        return base

    # ripple_mode: entity wins when set, else base.
    ripple_mode = override.get("ripple_mode") or base.ripple_mode

    # deny: union of base + entity (the hard cap only grows).
    deny = base.deny_mcp_tool_ids | frozenset(override.get("deny_mcp_tool_ids") or ())

    # skill_names: union.
    skills = base.skill_names | frozenset(override.get("skill_names") or ())

    # allowed_sdk_tools: union, but None on BOTH sides stays None (no
    # restriction). An empty frozenset would deny every SDK tool, so we only
    # produce a concrete set when at least one side contributes one.
    entity_allow = override.get("allowed_sdk_tools")
    if base.allowed_sdk_tools is None and entity_allow is None:
        allowed: frozenset[str] | None = None
    else:
        allowed = (base.allowed_sdk_tools or frozenset()) | frozenset(entity_allow or ())

    # allow_mcp_tool_ids: same None-aware UNION as allowed_sdk_tools — None on
    # BOTH sides stays None (no MCP restriction); otherwise the entity's allowed
    # MCP tools ADD to the mode's set. An empty frozenset would wrongly drop
    # every non-grant MCP tool, so only build a set when a side contributes.
    entity_allow_mcp = override.get("allow_mcp_tool_ids")
    if base.allow_mcp_tool_ids is None and entity_allow_mcp is None:
        allow_mcp: frozenset[str] | None = None
    else:
        allow_mcp = (base.allow_mcp_tool_ids or frozenset()) | frozenset(entity_allow_mcp or ())

    # system_message_override: entity wins when set, else base.
    sys_override = override.get("system_message_override") or base.system_message_override

    return SurfaceProfile(
        ripple_mode=ripple_mode,
        allowed_sdk_tools=allowed,
        allow_mcp_tool_ids=allow_mcp,
        deny_mcp_tool_ids=deny,
        skill_names=skills,
        system_message_override=sys_override,
    )


# Handler registry: SurfaceKind -> async callable returning the preamble.
# Built lazily on first use so import-time failures in a handler module
# don't block the rest of the resolver.
_HANDLERS: dict[SurfaceKind, Any] | None = None


def _load_handlers() -> dict[SurfaceKind, Any]:
    """Build the ``SurfaceKind -> build_preamble`` dispatch table.

    The table is derived by LOOPING over the declarative ``SURFACES``
    registry (SR-1) rather than a hand-maintained literal dict — one
    ``SurfaceSpec`` row per surface is the single source of truth, so adding
    a surface means adding a row, not editing two parallel structures.

    ``SURFACES`` is imported lazily here (not at module top) so a broken
    handler-module import still can't take the whole chat path down at
    import time. We tolerate missing handler modules instead of raising
    because the surface module ships independently of the surfaces it knows
    about — a fresh deploy that drops a handler module shouldn't break
    dispatch. A fresh mutable dict is returned on every call (tests
    monkeypatch entries on the returned dict), and any ``SurfaceKind``
    without a row still degrades to the ``GENERIC`` fall-back in
    ``resolve_surface_context``.
    """
    from pocketpaw_ee.cloud.surface.surface_registry import SURFACES

    return {spec.kind: spec.build_preamble for spec in SURFACES}


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
        repo=req.repo,
        base_branch=req.base_branch,
        current_dir=req.current_dir,
        project_name=req.project_name,
        storage_root=req.storage_root,
        is_cloud_storage=req.is_cloud_storage,
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


__all__ = ["resolve_surface_context", "resolve_profile", "compose_entity_profile"]
