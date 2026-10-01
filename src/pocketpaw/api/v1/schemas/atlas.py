# Atlas read API response schemas.
# Created: 2026-10-01 (feat/atlas-canonical) — the frozen wire contract for
#   GET /api/v1/atlas/{surfaces,verbs,search}. paw-enterprise's composer builds
#   its slash commands, verb chips and command search from these, so shapes are
#   fixed: every key is always present (nullable ones as null).
# Review pass (same branch): ``score`` is validated to 0..1.

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AtlasSurface(BaseModel):
    id: str
    name: str
    summary: str
    route: str
    slash: str | None
    presentation: Literal["inline", "window"]
    agent_openable: bool
    keywords: list[str]


class AtlasSurfacesResponse(BaseModel):
    surfaces: list[AtlasSurface]


class AtlasVerb(BaseModel):
    id: str
    name: str
    summary: str
    slash: str | None
    applies_to: list[str]
    triggers: list[Literal["slash", "verb", "agent"]]
    risk: Literal["read", "safe", "risky"]
    undo: bool
    keywords: list[str]


class AtlasVerbsResponse(BaseModel):
    verbs: list[AtlasVerb]


class AtlasSearchResult(BaseModel):
    id: str
    kind: Literal["surface", "verb", "capability", "primitive"]
    name: str
    route: str | None
    slash: str | None
    score: float = Field(ge=0, le=1)


class AtlasSearchResponse(BaseModel):
    query: str
    results: list[AtlasSearchResult]
