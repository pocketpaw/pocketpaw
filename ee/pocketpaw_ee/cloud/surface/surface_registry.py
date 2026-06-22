# surface_registry.py — The declarative surface registry (SR-1).
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
# This module is imported LAZILY (from inside ``service._load_handlers``, not
# at package import) so a broken handler-module import still can't take the
# whole surface dispatch table down at import time — the same deferral the
# old hand-written ``_load_handlers`` body had. The handler imports below run
# when ``surface_registry`` is first imported, which is the first call to
# ``_load_handlers``.
#
# The ``profile`` / ``profile_resolver`` / ``mcp_provider`` fields are DECLARED
# now but left at their defaults — SR-2 owns profiles and populates them. Do
# NOT wire profile data through this registry in SR-1; ``_build_profiles`` /
# ``resolve_profile`` / ``compose_entity_profile`` remain the source of truth
# for surface profiles until SR-2 migrates them onto these rows.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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

# The shape every handler module exports: an async preamble builder taking the
# tenancy tuple + the validated client meta and returning the rendered block.
BuildPreamble = Callable[[str, str, SurfaceMeta], Awaitable[str]]

# A profile resolver (declared for SR-2): given the client meta, return the
# surface's behavioral profile. Left ``None`` on every row in SR-1.
ProfileResolver = Callable[[SurfaceMeta], SurfaceProfile]


@dataclass(frozen=True)
class SurfaceSpec:
    """One row of the declarative surface registry.

    ``kind`` and ``build_preamble`` are the SR-1 payload — together they
    replace the hand-written entry in ``service._load_handlers``. ``route`` is
    the surface's canonical route (the ``SurfaceKind`` value with a leading
    slash), the cross-repo contract paw-enterprise and the backend agree on.

    ``profile`` / ``profile_resolver`` / ``mcp_provider`` are DECLARED here so
    the row's shape is locked now; SR-2 populates them when it migrates
    profile resolution onto this registry. They stay ``None`` in SR-1 — the
    existing ``resolve_profile`` / ``_build_profiles`` path is untouched.
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


# One row per surface that currently has a handler registered in
# ``service._load_handlers``. ``kind`` + ``build_preamble`` mirror that dict
# exactly (zero behavior change is the SR-1 contract); ``route`` is derived
# from the kind value. Order matches the old literal dict for easy diffing.
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
    SurfaceSpec(SurfaceKind.FILES, _route_for(SurfaceKind.FILES), files.build_preamble),
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
    ),
    SurfaceSpec(SurfaceKind.SITES, _route_for(SurfaceKind.SITES), sites.build_preamble),
    SurfaceSpec(SurfaceKind.STUDIO, _route_for(SurfaceKind.STUDIO), studio.build_preamble),
    SurfaceSpec(SurfaceKind.CODE, _route_for(SurfaceKind.CODE), code.build_preamble),
    SurfaceSpec(SurfaceKind.BELT, _route_for(SurfaceKind.BELT), belt.build_preamble),
    SurfaceSpec(SurfaceKind.GENERIC, _route_for(SurfaceKind.GENERIC), generic.build_preamble),
]
