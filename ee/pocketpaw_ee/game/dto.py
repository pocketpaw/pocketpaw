# ee/pocketpaw_ee/game/dto.py — request/response DTOs for the /api/v1/game
# REST surface. Created: 2026-07-02 (feat/game-surface, PE-A). Mirrors the
# sites/dto.py posture: distinct <Op>Request and <...>Response classes, never
# one model for both directions. The beat summary and the world snapshot stay
# UNMODELED dicts on purpose — their shape is owned by soul-protocol's
# GameWorld (engine-neutral event/summary contract) and a Pydantic mirror here
# would drift the moment the profile grows a field. Field names are FIXED
# contract — the /game frontend is built against them; do not rename.

"""DTOs for the game-worlds REST surface."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StartWorldRequest(BaseModel):
    """POST /game/worlds — wake a game pocket into a live world."""

    pocket_id: str = Field(min_length=1)


class StartWorldResponse(BaseModel):
    """The new world handle + the HUD bootstrap snapshot (engine-stamped)."""

    world_id: str
    snapshot: dict


class BeatRequest(BaseModel):
    """POST /game/worlds/{wid}/beat — one player line into the world.

    ``kind`` absent → the deterministic keyword classifier decides.
    ``npc`` absent → the beat routes to the world's first NPC.
    """

    player: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: str | None = None
    npc: str | None = None


class EventsResponse(BaseModel):
    """GET /game/worlds/{wid}/events?since=N — events with t > since."""

    events: list[dict]


class ReputationRequest(BaseModel):
    """POST /game/worlds/{wid}/reputation — an NPC reads a player's
    portable reputation and reacts."""

    npc: str = Field(min_length=1)
    player: str = Field(min_length=1)


class ReputationResponse(BaseModel):
    """The reaction line + the notoriety band it was driven by."""

    npc: str
    player: str
    line: str
    notoriety: str


class SeedExampleResponse(BaseModel):
    """POST /game/seed_example — the canonical Butcher pocket (created or
    found; ``created`` says which)."""

    pocket_id: str
    created: bool
