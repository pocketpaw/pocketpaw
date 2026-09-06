# ee/pocketpaw_ee/terrarium/router.py
#
# TWO routers with DELIBERATELY different auth boundaries — the same split the
# file share-links surface uses, and for the same reason: an ambient dependency
# added to a shared router would silently change the public one's posture.
#
#   router        (prefix /terrarium)        — workspace surface. License-gated,
#       RBAC mirroring the belt console: ``terrarium.read`` (MEMBER) on reads,
#       ``terrarium.manage`` (ADMIN) on create / tick. Speaking and pledging are
#       MEMBER: they cost the viewer tokens, not the workspace's safety.
#
#   public_router (prefix /terrarium/public) — ANONYMOUS, READ-ONLY. No auth, no
#       license. This is a SECURITY BOUNDARY, so it is conservative on purpose:
#         * it is dark unless ``TERRARIUM_PUBLIC_ENABLED`` is truthy. DEFAULT
#           OFF — an operator has to turn it on deliberately.
#         * every route ALSO requires ``universe.public == true``, checked in
#           the service at the lookup (``_public_universe``) so no handler can
#           forget it. BOTH gates, never one.
#         * a universe that fails either gate is a flat 404 — never a 403,
#           which would confirm the universe exists.
#         * it carries NO write route, and never will. Speaking and pledging
#           move credits and enter souls' context; they require an account.
#       Anything added here needs a security review, not just a code review.

"""Terrarium routers — the workspace surface and the anonymous public one."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.terrarium import service
from pocketpaw_ee.terrarium.dto import CreateUniverseRequest, PledgeRequest, SpeakRequest

router = APIRouter(prefix="/terrarium", tags=["Terrarium"], dependencies=[Depends(require_license)])

# No dependencies. Deliberate — see the module header.
public_router = APIRouter(prefix="/terrarium/public", tags=["Terrarium Public"])


def public_enabled() -> bool:
    """The server-wide kill switch for the anonymous surface. DEFAULT OFF.

    Fail-closed: anything other than an explicit truthy value keeps the whole
    public surface dark, including a malformed value.
    """
    return (os.environ.get("TERRARIUM_PUBLIC_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _require_public_surface() -> None:
    """404 (not 403) when the public surface is off — the routes do not exist
    as far as an anonymous caller can tell."""
    if not public_enabled():
        raise NotFound("universe")


# ---------------------------------------------------------------------------
# Workspace surface
# ---------------------------------------------------------------------------


@router.post("/universes")
async def create_universe(
    body: CreateUniverseRequest,
    _user: Any = Depends(require_action_any_workspace("terrarium.manage")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Create a universe from a physics file and seed its founders. Files a
    ``world_create`` Instinct Action; returns ``{action_id, universe}``."""
    return await service.create_universe(workspace_id, user_id, body.model_dump())


@router.get("/universes")
async def list_universes(
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The workspace's universes."""
    return await service.list_universes(workspace_id)


@router.get("/universes/{universe_id}")
async def get_universe(
    universe_id: str,
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """One universe with its citizens and ledger. Another workspace's is a 404."""
    return await service.get_universe(workspace_id, universe_id)


@router.post("/universes/{universe_id}/tick")
async def tick(
    universe_id: str,
    n: int = Query(1, ge=1, le=24),
    _user: Any = Depends(require_action_any_workspace("terrarium.manage")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Run N ticks. Manual trigger; a scheduler would use this same path."""
    return await service.tick(workspace_id, user_id, universe_id, n)


@router.get("/universes/{universe_id}/events")
async def list_events(
    universe_id: str,
    since: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The Journal, paged by the monotonic ``seq``."""
    return await service.list_events(workspace_id, universe_id, since, limit)


@router.get("/universes/{universe_id}/citizens")
async def list_citizens(
    universe_id: str,
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    return await service.list_citizens(workspace_id, universe_id)


@router.get("/universes/{universe_id}/citizens/{cid}")
async def get_citizen(
    universe_id: str,
    cid: str,
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The profile drawer: citizen, soul memories, artifacts, bonds."""
    return await service.get_citizen(workspace_id, universe_id, cid)


@router.get("/universes/{universe_id}/artifacts")
async def list_artifacts(
    universe_id: str,
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The Built tab."""
    return await service.list_artifacts(workspace_id, universe_id)


@router.post("/universes/{universe_id}/speak")
async def speak(
    universe_id: str,
    body: SpeakRequest,
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Say one line into the world. Lands tagged ``viewer_origin: true``."""
    return await service.speak(workspace_id, user_id, universe_id, body.text)


@router.get("/universes/{universe_id}/weather")
async def get_weather(
    universe_id: str,
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Every god power's pledge state."""
    return await service.get_weather(workspace_id, universe_id)


@router.post("/universes/{universe_id}/weather/pledge")
async def pledge_weather(
    universe_id: str,
    body: PledgeRequest,
    _user: Any = Depends(require_action_any_workspace("terrarium.read")),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Pledge tokens toward a power. Fires it when the threshold is crossed."""
    return await service.pledge_weather(workspace_id, user_id, universe_id, body.model_dump())


# ---------------------------------------------------------------------------
# Public surface — anonymous, read-only, doubly gated, fail-closed.
# ---------------------------------------------------------------------------


@public_router.get("/universes")
async def public_list_universes() -> dict[str, Any]:
    _require_public_surface()
    return await service.public_list_universes()


@public_router.get("/universes/{universe_id}")
async def public_get_universe(universe_id: str) -> dict[str, Any]:
    _require_public_surface()
    return await service.public_get_universe(universe_id)


@public_router.get("/universes/{universe_id}/events")
async def public_list_events(
    universe_id: str,
    since: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    _require_public_surface()
    return await service.public_list_events(universe_id, since, limit)


@public_router.get("/universes/{universe_id}/citizens")
async def public_list_citizens(universe_id: str) -> dict[str, Any]:
    _require_public_surface()
    return await service.public_list_citizens(universe_id)


@public_router.get("/universes/{universe_id}/citizens/{cid}")
async def public_get_citizen(universe_id: str, cid: str) -> dict[str, Any]:
    _require_public_surface()
    return await service.public_get_citizen(universe_id, cid)


@public_router.get("/universes/{universe_id}/artifacts")
async def public_list_artifacts(universe_id: str) -> dict[str, Any]:
    _require_public_surface()
    return await service.public_list_artifacts(universe_id)


__all__ = ["public_enabled", "public_router", "router"]
