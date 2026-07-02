# ee/pocketpaw_ee/game/router.py — REST surface for RUNNING game worlds.
# Created: 2026-07-02 (feat/game-surface, PE-A).
#
# Mirrors the sites router's wiring exactly: router-level
# require_plan_feature("game") (the same PLAN_FEATURES flag the in-process
# create tool gates on — game is go+, like studio), per-route RequestContext
# via request_context, and require_action_any_workspace("game.write" /
# "game.read") — registered in guards/actions.py ACTIONS at MEMBER tier,
# mirroring fabric.read/fabric.write (the registry fails LOUD on unknown
# actions, so registration is mandatory, not optional).
# Registered in ee/pocketpaw_ee/cloud/__init__.py under the /api/v1 prefix,
# right beside sites_router.
#
# Error posture (CloudError → the standard JSON envelope, never
# HTTPException): unknown/cross-tenant world_id → 404 NotFound("game_world")
# (the runtime raises the SAME KeyError for both, so a guessed handle never
# confirms a foreign world exists); unknown player/npc or a bad kind → 400
# BadRequest; a malformed world spec → 400 BadRequest; runtime unavailable
# (soul_protocol.profiles.game not importable — see runtime.py's dependency
# note) → 503 via a bare CloudError(503, ...), since _core.errors has no
# 503 subclass yet.
#
# Contract (FIXED — the /game frontend is built against these field names):
#   POST /game/worlds                      {pocket_id} → {world_id, snapshot}
#   POST /game/worlds/{wid}/beat           {player, text, kind?, npc?} → beat summary
#   GET  /game/worlds/{wid}/events?since=N → {events: [...]}
#   GET  /game/worlds/{wid}/snapshot       → snapshot dict + "engine"
#   POST /game/worlds/{wid}/reputation     {npc, player} → {line, notoriety, ...}
#   POST /game/seed_example                {} → {pocket_id, created}

"""REST surface for running game worlds (start / beat / poll / reputation)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError, NotFound
from pocketpaw_ee.game import runtime
from pocketpaw_ee.game.dto import (
    BeatRequest,
    EventsResponse,
    ReputationRequest,
    ReputationResponse,
    SeedExampleResponse,
    StartWorldRequest,
    StartWorldResponse,
)

router = APIRouter(
    prefix="/game",
    tags=["Game"],
    dependencies=[Depends(require_plan_feature("game"))],
)

# The canonical seed world — "The Butcher Remembers" (the soul-protocol demo
# arc as a pocket). seed_example is idempotent-ish: an existing same-named
# game pocket in the workspace is returned instead of re-created.
_BUTCHER_NAME = "The Butcher Remembers"
_BUTCHER_VIBE = "tense"
_BUTCHER_SPEC: dict = {
    "cast": [
        {
            "name": "Bjorn",
            "archetype": "The Butcher",
            "persona": (
                "I am Bjorn, a proud, gruff medieval butcher. I keep an honest "
                "stall and a long memory."
            ),
            "zone": "stall",
        },
        {
            "name": "Astrid",
            "archetype": "The Innkeeper",
            "persona": (
                "I am Astrid, the wary, sharp-eyed innkeeper. I pour honest ale, "
                "keep clean rooms, and miss nothing that happens under my roof."
            ),
            "zone": "tables",
        },
    ],
    "zones": ["stall", "tavern", "tables", "door"],
    # No dials on purpose — create_game_world fills all seven from the
    # "tense" vibe preset (the canonical preset dials for this world).
}


def _require_runtime() -> None:
    """503 when soul_protocol.profiles.game isn't importable — the dependency
    is dev-only until the profile ships in a soul-protocol release."""
    if not runtime.GAME_RUNTIME_AVAILABLE:
        raise CloudError(
            503,
            "game.runtime_unavailable",
            "The game runtime is unavailable on this deployment: "
            "soul_protocol.profiles.game is not importable "
            f"({runtime.GAME_RUNTIME_UNAVAILABLE_REASON or 'not installed'}). "
            "Install the soul-protocol build that ships the Game Profile.",
        )


@router.post("/worlds", response_model=StartWorldResponse)
async def start_world(
    body: StartWorldRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("game.write")),
) -> StartWorldResponse:
    """Wake a persisted game pocket into a live in-memory GameWorld.

    The pocket read (``pockets_service.get``) raises NotFound / Forbidden
    itself → 404 / 403 via the standard envelope; a pocket living under
    another workspace is a 404 for this caller (tenant check against
    ``get_pocket_workspace``). A non-game pocket or a spec the runtime can't
    wake is a 400. Worlds are v0-ephemeral — they die with the process."""
    _require_runtime()

    # Local import — keep the game package importable without eagerly pulling
    # the cloud pockets module (the same posture as game/service.py).
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    pocket = await pockets_service.get(body.pocket_id, ctx.user_id)
    pocket_workspace = await pockets_service.get_pocket_workspace(body.pocket_id)
    if pocket_workspace != ctx.workspace_id:
        # Cross-tenant read → the same 404 a missing pocket gets.
        raise NotFound("pocket", body.pocket_id)
    if (pocket.get("type") or "") != "game":
        raise BadRequest(
            "game.not_a_game_pocket",
            f"pocket '{body.pocket_id}' is not a game pocket (type="
            f"{pocket.get('type')!r}) — only Pocket type='game' carries a world spec",
        )

    try:
        world_id, snap = await runtime.start_world(
            workspace_id=ctx.workspace_id, pocket_id=body.pocket_id, pocket=pocket
        )
    except ValueError as exc:
        raise BadRequest("game.invalid_world_spec", str(exc)) from exc
    return StartWorldResponse(world_id=world_id, snapshot=snap)


@router.post("/worlds/{world_id}/beat")
async def beat(
    world_id: str,
    body: BeatRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("game.write")),
) -> dict:
    """One player line into the world; returns the beat summary dict
    (reaction, grudge_level, bond, phase, pacing, spark, ...) verbatim from
    the engine. ``kind`` omitted → the deterministic keyword classifier
    decides; ``npc`` omitted → the first NPC."""
    _require_runtime()
    try:
        return await runtime.beat(
            world_id,
            workspace_id=ctx.workspace_id,
            player=body.player,
            text=body.text,
            kind=body.kind,
            npc=body.npc,
        )
    except KeyError as exc:
        raise NotFound("game_world", world_id) from exc
    except (LookupError, ValueError) as exc:
        # Unknown player/npc, or an explicit kind the profile doesn't know.
        raise BadRequest("game.invalid_beat", str(exc)) from exc


@router.get("/worlds/{world_id}/events", response_model=EventsResponse)
async def events(
    world_id: str,
    since: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("game.read")),
) -> EventsResponse:
    """The world's event stream with ``t > since`` — the client poll cursor."""
    _require_runtime()
    try:
        return EventsResponse(
            events=runtime.events_since(world_id, workspace_id=ctx.workspace_id, since=since)
        )
    except KeyError as exc:
        raise NotFound("game_world", world_id) from exc


@router.get("/worlds/{world_id}/snapshot")
async def snapshot(
    world_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("game.read")),
) -> dict:
    """The HUD bootstrap: zones, director phase, per-NPC relationship state,
    plus the ``engine`` stamp."""
    _require_runtime()
    try:
        return runtime.snapshot(world_id, workspace_id=ctx.workspace_id)
    except KeyError as exc:
        raise NotFound("game_world", world_id) from exc


@router.post("/worlds/{world_id}/reputation", response_model=ReputationResponse)
async def reputation(
    world_id: str,
    body: ReputationRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("game.read")),
) -> ReputationResponse:
    """An NPC reads the player's PORTABLE reputation off their player.soul and
    reacts to it — works even when that NPC was never personally wronged."""
    _require_runtime()
    try:
        result = await runtime.reputation(
            world_id, workspace_id=ctx.workspace_id, npc=body.npc, player=body.player
        )
    except KeyError as exc:
        raise NotFound("game_world", world_id) from exc
    except LookupError as exc:
        raise BadRequest("game.invalid_reputation", str(exc)) from exc
    return ReputationResponse(**result)


@router.post("/seed_example", response_model=SeedExampleResponse)
async def seed_example(
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("game.write")),
) -> SeedExampleResponse:
    """Create (or return) the canonical Butcher pocket — the known-good world
    to demo the runtime against.

    Idempotent-ish: the workspace's pockets visible to the caller are listed
    (``pockets_service.list_pockets`` — the same owned/team/shared/workspace
    visibility scope every gallery read uses) and an existing game pocket
    named "The Butcher Remembers" is returned instead of re-created. Creation
    goes through ``game.service.create_game_world`` — the exact deterministic
    path the /game chat tool uses (dials filled from the "tense" preset).

    Available even when the run-runtime is missing: seeding only persists the
    pocket (no soul-protocol import), so no ``_require_runtime`` here."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.game.service import create_game_world

    existing = await pockets_service.list_pockets(ctx.workspace_id, ctx.user_id)
    for pocket in existing:
        if pocket.get("name") == _BUTCHER_NAME and (pocket.get("type") or "") == "game":
            pocket_id = str(pocket.get("_id") or pocket.get("id") or "")
            if pocket_id:
                return SeedExampleResponse(pocket_id=pocket_id, created=False)

    view = await create_game_world(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        name=_BUTCHER_NAME,
        vibe=_BUTCHER_VIBE,
        world_spec=_BUTCHER_SPEC,
    )
    pocket_id = str(view.get("_id") or view.get("id") or "")
    if not pocket_id:
        raise CloudError(500, "game.seed_failed", "seed create returned no pocket id")
    return SeedExampleResponse(pocket_id=pocket_id, created=True)
