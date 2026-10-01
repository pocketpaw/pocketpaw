# Atlas read API — GET /api/v1/atlas/{surfaces,verbs,search}.
# Created: 2026-10-01 (feat/atlas-canonical). Atlas is the one place that says
#   which surfaces and composer verbs exist, where they open, who can trigger
#   them and how risky they are; the paw-enterprise composer reads it here and
#   the agent reads the same entries through atlas_search.
#
# Read-only. Callers: an ACTIVE signed-in cloud user (the EE auth bridge sets
#   request.state.user_id after the JWT verifies and ee_user_authenticated only
#   for active users), or a caller holding the "chat" scope (API key, OAuth
#   token, local dashboard session). Anyone else gets 403.
#
# Workspace overlay: answers go through the atlas EntitlementProvider for the
#   caller's workspace (ws:<id>, "default" outside the cloud). With a signed-in
#   user and workspace, the EE role-aware provider (overlay.build_role_aware_
#   provider, bound to that user) resolves the caller's workspace role, so an
#   owner sees owner-gated entries such as surface:security and a member
#   doesn't. Without one, or when the role can't be resolved, role-gated
#   entries stay hidden (fail-closed).
#
# Score: atlas ranks by weighted token overlap (name > keyword > summary >
#   narrative). ``score`` = raw score / AtlasStore.max_score(q), the score of a
#   name hit on every distinct query word, clamped to 0..1 and rounded to 3 dp.
#
# Review pass (same branch): active-user requirement, role-aware overlay,
#   ``kinds`` capped at 64 chars.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pocketpaw.api.deps import require_scope
from pocketpaw.api.v1.schemas.atlas import (
    AtlasSearchResponse,
    AtlasSearchResult,
    AtlasSurface,
    AtlasSurfacesResponse,
    AtlasVerb,
    AtlasVerbsResponse,
)
from pocketpaw.atlas.model import AtlasEntry
from pocketpaw.atlas.overlay import (
    DEFAULT_SCOPE_KEY,
    AtlasOverlay,
    DefaultEntitlementProvider,
    EntitlementProvider,
    build_role_aware_provider,
)
from pocketpaw.atlas.store import get_atlas_store

logger = logging.getLogger(__name__)

MAX_LIMIT = 20
MAX_QUERY_CHARS = 200
MAX_KINDS_CHARS = 64
SEARCH_KINDS = ("surface", "verb", "capability", "primitive")

_chat_scope = require_scope("chat")


async def _require_signed_in(request: Request) -> None:
    """An active verified cloud user, or a caller holding the chat scope."""
    state = request.state
    if getattr(state, "user_id", None) and getattr(state, "ee_user_authenticated", False):
        return
    await _chat_scope(request)


router = APIRouter(prefix="/atlas", tags=["Atlas"], dependencies=[Depends(_require_signed_in)])


async def _provider(request: Request) -> EntitlementProvider:
    """The caller's workspace overlay: role-aware when a user + workspace exist."""
    workspace_id = getattr(request.state, "workspace_id", None)
    user_id = getattr(request.state, "user_id", None)
    scope = f"ws:{workspace_id}" if workspace_id else DEFAULT_SCOPE_KEY
    if workspace_id and user_id:
        role_aware = build_role_aware_provider(scope, user_id=str(user_id))
        if role_aware is not None:
            prime = getattr(role_aware, "prime", None)
            if prime is not None:
                try:
                    await prime()
                except Exception:  # noqa: BLE001 — unresolved role hides gated entries
                    logger.debug("atlas api: role resolution failed", exc_info=True)
            return role_aware
    return DefaultEntitlementProvider(scope_key=scope)


async def _visible(request: Request, entries: list[AtlasEntry]) -> list[AtlasEntry]:
    """Entries the caller's workspace overlay grants, in the given order."""
    granted = set(AtlasOverlay.visible_ids(get_atlas_store(), await _provider(request)))
    return [e for e in entries if e.id in granted]


async def _of_kind(request: Request, kind: str) -> list[AtlasEntry]:
    return await _visible(request, [e for e in get_atlas_store().entries if e.kind == kind])


@router.get("/surfaces", response_model=AtlasSurfacesResponse)
async def list_surfaces(request: Request) -> AtlasSurfacesResponse:
    return AtlasSurfacesResponse(
        surfaces=[
            AtlasSurface(
                id=e.id,
                name=e.name,
                summary=e.summary,
                route=e.surface,
                slash=e.slash,
                presentation=e.presentation,
                agent_openable=bool(e.agent_openable),
                keywords=e.keywords,
            )
            for e in await _of_kind(request, "surface")
        ]
    )


@router.get("/verbs", response_model=AtlasVerbsResponse)
async def list_verbs(request: Request) -> AtlasVerbsResponse:
    return AtlasVerbsResponse(
        verbs=[
            AtlasVerb(
                id=e.id,
                name=e.name,
                summary=e.summary,
                slash=e.slash,
                applies_to=e.applies_to or [],
                triggers=e.triggers or [],
                risk=e.risk,
                undo=bool(e.undo),
                keywords=e.keywords,
            )
            for e in await _of_kind(request, "verb")
        ]
    )


@router.get("/search", response_model=AtlasSearchResponse)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_CHARS),
    kinds: str | None = Query(
        None,
        max_length=MAX_KINDS_CHARS,
        description="Comma-separated: surface,verb,capability,primitive",
    ),
    limit: int = Query(5, ge=1, description=f"Capped at {MAX_LIMIT}."),
) -> AtlasSearchResponse:
    wanted = {k.strip() for k in kinds.split(",") if k.strip()} if kinds else set(SEARCH_KINDS)
    unknown = wanted - set(SEARCH_KINDS)
    if unknown or not wanted:
        raise HTTPException(
            status_code=422,
            detail=f"kinds must be drawn from {', '.join(SEARCH_KINDS)}",
        )
    limit = min(limit, MAX_LIMIT)

    store = get_atlas_store()
    ceiling = store.max_score(q)
    scored = [(s, e) for s, e in store.search_scored(q) if e.kind in wanted]
    visible = {e.id for e in await _visible(request, [e for _, e in scored])}
    results = [
        AtlasSearchResult(
            id=e.id,
            kind=e.kind,
            name=e.name,
            route=e.surface or None,
            slash=e.slash,
            score=round(min(1.0, s / ceiling), 3) if ceiling else 0.0,
        )
        for s, e in scored
        if e.id in visible
    ][:limit]
    return AtlasSearchResponse(query=q, results=results)
