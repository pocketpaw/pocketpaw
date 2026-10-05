# service.py — Surface context resolver and handler dispatch.
#
# ``resolve_surface_context(workspace_id, user_id, body)`` validates the client's
# ``{surface, meta}`` hint (``SurfaceRequest.model_validate`` at entry), maps it
# to a ``SurfaceKind`` and dispatches to that kind's handler from the
# surface_registry ``SURFACES`` table, carrying the handler's own cache key out
# on ``SurfaceContext.preamble_cache_key``. Failure stays inert: any handler
# error logs and returns a GENERIC context with an empty preamble and a ``None``
# key, so a surface failure never breaks a chat send.
#
# ``resolve_profile(kind, meta)`` is a PURE, sync lookup of the per-surface
# policy from the registry rows (incl. the meta-aware /sites resolver). It folds
# the agentic-browser tool ids into ``deny_mcp_tool_ids`` for every surface but
# BROWSER at this one chokepoint, so the unmapped default case is covered too.
#
# ``compose_entity_profile(base, override)`` folds a pocket's
# ``PocketSurfaceProfile`` over the base: ripple_mode and the system-message
# override entity-wins-if-set, deny / allowed-tools / skills UNION,
# ``exclusive_tools`` ORed. The async pocket load lives in run_core.
#
# ``_meta_from_request`` passes ``SurfaceMeta`` hints through FIELD BY FIELD: a
# new hint not listed there validates in the DTO and is then dropped silently.

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from pocketpaw_ee.cloud.surface.domain import (
    SurfaceContext,
    SurfaceKind,
    SurfaceMeta,
    SurfacePreamble,
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
    BELT / SHIP rows — exactly today's behavior.
    """
    spec = _spec_by_kind().get(surface_kind)
    if spec is None:
        profile = _DEFAULT_PROFILE
    elif spec.profile_resolver is not None:
        profile = spec.profile_resolver(meta)
    elif spec.profile is not None:
        profile = spec.profile
    else:
        profile = _DEFAULT_PROFILE
    return _deny_browser_off_surface(surface_kind, profile)


def _deny_browser_off_surface(surface_kind: SurfaceKind, profile: SurfaceProfile) -> SurfaceProfile:
    """Add the agentic-browser tool ids to the deny set of every NON-browser surface.

    The browser drives a real Chromium as the tenant. It belongs on /browser and
    nowhere else — least of all /chat, which is otherwise unrestricted
    (``allow_mcp_tool_ids=None``) and carries the send-capable connector tools.
    An allow-list alone cannot express that: the surfaces that need blocking are
    exactly the ones that declare no allow-list.

    Applied HERE rather than row-by-row deliberately. This is the one chokepoint
    every ``(kind, meta)`` passes through, so it also covers ``_DEFAULT_PROFILE``,
    an unmapped/future kind, and any row added later without a thought for the
    browser. Deny is subtracted from ``allowed_tools`` before the SDK launches
    (``claude_sdk``), so the tools are physically unreachable, not discouraged.
    """
    if surface_kind is SurfaceKind.BROWSER:
        return profile
    from pocketpaw_ee.cloud.surface.surface_registry import browser_tool_ids

    ids = browser_tool_ids()
    if not ids:
        # The tool-id import degraded; there is nothing to deny (and nothing to
        # allow either — the BROWSER profile's allow is None on the same path).
        return profile
    return dataclasses.replace(profile, deny_mcp_tool_ids=profile.deny_mcp_tool_ids | ids)


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
      * ``exclusive_tools`` — OR. Like deny, a cap that can only tighten.

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
        # A hard cap, like deny: OR, so an entity can lock a surface down but
        # never unlock one. Dropping it here would hand a concierge whose site
        # pocket carries any override every builtin back.
        exclusive_tools=base.exclusive_tools or bool(override.get("exclusive_tools")),
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
        mode=req.mode,
        brief_id=req.brief_id,
        repo=req.repo,
        base_branch=req.base_branch,
        current_dir=req.current_dir,
        project_name=req.project_name,
        storage_root=req.storage_root,
        is_cloud_storage=req.is_cloud_storage,
        workspace_vm=req.workspace_vm,
        pawbar_actions=req.pawbar_actions,
        pawbar_catalog=req.pawbar_catalog,
        timeline=req.timeline,
        vector=req.vector,
        photo=req.photo,
        design=req.design,
        snapshot_path=req.snapshot_path,
        free_y=req.free_y,
        book_path=req.book_path,
        mark_box=req.mark_box,
        mark_image_path=req.mark_image_path,
        mark_text=req.mark_text,
        scene=req.scene,
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

    PA-2: it also carries the handler's ``cache_key`` out on the context. It
    does NOT compute one. The dispatcher has ``kind``, ``pocket_id`` and the
    rest of the meta in hand, and a key built from those would need no handler
    changes at all — and would be wrong for every handler that reads live data,
    because a pocket edited between two turns keeps all three. Every absorbed
    error above yields a key of ``None``, matching the empty preamble those
    paths render.
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
            preamble_cache_key=None,
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
            preamble_cache_key=None,
        )

    # Step 3: render the preamble. Handler exceptions are absorbed —
    # we'd rather ship a chat with no surface context than fail the send.
    try:
        rendered = await handler(workspace_id, user_id, meta)
    except Exception:
        logger.exception("surface handler %s failed; using GENERIC preamble", kind.value)
        return SurfaceContext(
            workspace_id=workspace_id,
            user_id=user_id,
            kind=SurfaceKind.GENERIC,
            meta=meta,
            preamble="",
            preamble_cache_key=None,
        )

    # Handlers return a ``SurfacePreamble``. A bare ``str`` is still accepted —
    # the same liberal-in-what-we-accept rule ``_resolve_kind`` follows for an
    # unknown surface name, and tests monkeypatch the dispatch table with plain
    # coroutines — but it carries NO key: text with an unknown provenance is
    # exactly the case that must not be allowed to claim stability, so it
    # renders as volatile rather than being handed a key we would have to
    # invent for it.
    if isinstance(rendered, SurfacePreamble):
        preamble, cache_key = rendered.text, rendered.cache_key
        images = rendered.images
    else:
        preamble, cache_key, images = (rendered or ""), None, ()

    return SurfaceContext(
        workspace_id=workspace_id,
        user_id=user_id,
        kind=kind,
        meta=meta,
        preamble=preamble or "",
        preamble_cache_key=cache_key,
        preamble_images=images,
    )


__all__ = ["resolve_surface_context", "resolve_profile", "compose_entity_profile"]
