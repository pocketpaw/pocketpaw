# Atlas read API — GET /api/v1/atlas/{surfaces,verbs,search}.
# Created: 2026-10-01 (feat/atlas-canonical). Atlas is the one place that says
#   which surfaces and composer verbs exist, where they open, who can trigger
#   them and how risky they are; the paw-enterprise composer reads it here and
#   the agent reads the same entries through atlas_search.
#
# Read-only and signed-in: any user whose JWT the EE auth bridge verified
#   (request.state.user_id), or a caller holding the "chat" scope (API key,
#   OAuth token, local dashboard session). Anonymous callers get 403.
#
# Workspace overlay: answers go through the atlas EntitlementProvider for the
#   caller's workspace scope (ws:<id>, "default" outside the cloud). The read
#   API has no chat-run identity to resolve a workspace role from, so role-gated
#   entries (the admin capability cards) are always hidden here, fail-closed.
#   Every capability card is role-gated today, so search returns no capability
#   results; surfaces, verbs and primitives are ungated.
#
# Score: atlas ranks by weighted token overlap (name > keyword > summary >
#   narrative). ``score`` = raw score / AtlasStore.max_score(q), the score of a
#   name hit on every distinct query word, clamped to 0..1 and rounded to 3 dp.

from __future__ import annotations

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
from pocketpaw.atlas.overlay import DEFAULT_SCOPE_KEY, AtlasOverlay, DefaultEntitlementProvider
from pocketpaw.atlas.store import get_atlas_store

MAX_LIMIT = 20
MAX_QUERY_CHARS = 200
SEARCH_KINDS = ("surface", "verb", "capability", "primitive")

_chat_scope = require_scope("chat")


async def _require_signed_in(request: Request) -> None:
    """A verified cloud user, or a caller holding the chat scope."""
    if getattr(request.state, "user_id", None):
        return
    await _chat_scope(request)


router = APIRouter(prefix="/atlas", tags=["Atlas"], dependencies=[Depends(_require_signed_in)])


def _visible(request: Request, entries: list[AtlasEntry]) -> list[AtlasEntry]:
    """Entries the caller's workspace overlay grants, in the given order."""
    workspace_id = getattr(request.state, "workspace_id", None)
    scope = f"ws:{workspace_id}" if workspace_id else DEFAULT_SCOPE_KEY
    provider = DefaultEntitlementProvider(scope_key=scope)
    granted = set(AtlasOverlay.visible_ids(get_atlas_store(), provider))
    return [e for e in entries if e.id in granted]


def _of_kind(request: Request, kind: str) -> list[AtlasEntry]:
    return _visible(request, [e for e in get_atlas_store().entries if e.kind == kind])


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
            for e in _of_kind(request, "surface")
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
            for e in _of_kind(request, "verb")
        ]
    )


@router.get("/search", response_model=AtlasSearchResponse)
async def search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_CHARS),
    kinds: str | None = Query(
        None, description="Comma-separated: surface,verb,capability,primitive"
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
    visible = {e.id for e in _visible(request, [e for _, e in scored])}
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
