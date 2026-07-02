# tests/ee/game/test_router.py — HTTP-layer tests for the /api/v1/game run
# surface (ee/pocketpaw_ee/game/router.py). Created: 2026-07-02
# (feat/game-surface, PE-A).
#
# Auth wiring mirrors tests/ee/sites/test_router.py exactly: the game router
# gates on require_plan_feature("game") (router level) +
# require_action_any_workspace("game.write"/"game.read"), which resolve the
# caller via current_active_user / current_workspace_id, while handler bodies
# read ctx off request_context. The app overrides all three plus
# require_license, stubs get_workspace_plan -> "go" (game is a go+ feature),
# and add_error_handler maps CloudError to the JSON envelope (404/400/503).
#
# Layers:
#   1. THE REAL PERSISTED PATH (skipped without the game profile): the pocket
#      is seeded through the REAL game.service.create_game_world against
#      mongomock, and POST /game/worlds reads it back through the REAL
#      pockets.service.get — so the test proves the world blocks
#      (cast/zones/dials) survive the persist → promote → wire round-trip,
#      not just a mocked dict. Then the full HTTP loop: beat (theft
#      auto-classified, grudge moves), events?since cursor, snapshot,
#      reputation after wrongs, 404s, cross-tenant 404, non-game pocket 400.
#   2. RUNTIME-INDEPENDENT ROUTES (run everywhere, incl. profile-less CI):
#      seed_example (creates the canonical Butcher pocket; second call
#      returns the SAME pocket — the idempotent-ish lookup is by
#      name+type over the caller-visible workspace pockets) and the 503
#      path (GAME_RUNTIME_AVAILABLE monkeypatched False).
#
# soul-protocol note: the PACKAGE is a base dep (always importable); only the
# GAME PROFILE is experiment-branch — so live-world tests use a skipif marker
# on find_spec("soul_protocol.profiles.game") instead of a module-level
# importorskip, keeping the 503/seed tests running on profile-less installs.

"""Tests for the game-worlds REST router."""

from __future__ import annotations

import importlib.util
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

pytest.importorskip("pocketpaw_ee")


def _game_profile_available() -> bool:
    try:
        return importlib.util.find_spec("soul_protocol.profiles.game") is not None
    except ModuleNotFoundError:  # no soul_protocol at all
        return False


requires_game_profile = pytest.mark.skipif(
    not _game_profile_available(),
    reason="soul_protocol.profiles.game not installed (experiment-branch dep)",
)

WS_OWNER = "ws_game_owner"
USER_ID = "user-test-1"


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "member") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Member of the test workspace — game.write/read carry no role minimum."""

    def __init__(self, workspace_id: str) -> None:
        self.id = USER_ID
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="member")]


def _build_app(workspace_id: str, monkeypatch) -> FastAPI:
    from datetime import UTC, datetime

    import pocketpaw_ee.cloud.workspace.service as ws_svc
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.game.router import router as game_router

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="go"))

    fake_user = _FakeUser(workspace_id)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(game_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=str(fake_user.id),
            workspace_id=workspace_id,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: fake_user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _seed_butcher_pocket(workspace_id: str = WS_OWNER, user_id: str = USER_ID) -> str:
    """Persist the canonical Butcher pocket through the REAL create service
    (mongomock Beanie) and return its pocket_id."""
    from pocketpaw_ee.game.service import create_game_world

    view = await create_game_world(
        workspace_id=workspace_id,
        user_id=user_id,
        name="The Butcher Remembers",
        vibe="tense",
        world_spec={
            "cast": [
                {
                    "name": "Bjorn",
                    "archetype": "The Butcher",
                    "persona": "I am Bjorn, a proud, gruff medieval butcher.",
                    "zone": "stall",
                },
                {
                    "name": "Astrid",
                    "archetype": "The Innkeeper",
                    "persona": "I am Astrid, the wary innkeeper.",
                    "zone": "tables",
                },
            ],
            "zones": ["stall", "tavern", "tables", "door"],
        },
    )
    return str(view.get("_id") or view.get("id") or "")


# ---------------------------------------------------------------------------
# The live loop — real persisted pocket → real pockets read → live world.
# ---------------------------------------------------------------------------


@requires_game_profile
@pytest.mark.asyncio
async def test_start_world_from_seeded_pocket(beanie_test_db, monkeypatch):
    """POST /game/worlds wakes the persisted pocket: the snapshot carries the
    cast + zones that survived the persist → wire round-trip."""
    pocket_id = await _seed_butcher_pocket()
    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        resp = await c.post("/api/v1/game/worlds", json={"pocket_id": pocket_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["world_id"]
    snap = body["snapshot"]
    assert [n["name"] for n in snap["npcs"]] == ["Bjorn", "Astrid"]
    assert snap["zones"]["Bjorn"] == "stall"
    assert snap["zones"]["Astrid"] == "tables"
    assert snap["zones"]["You"] == "door"  # default player at the last zone
    assert snap["engine"] == "templated"


@requires_game_profile
@pytest.mark.asyncio
async def test_beat_auto_classifies_and_moves_grudge(beanie_test_db, monkeypatch):
    """POST .../beat with theft text (no kind) auto-classifies; the summary
    carries reaction/grudge_level/bond/phase and the grudge moved."""
    pocket_id = await _seed_butcher_pocket()
    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        start = await c.post("/api/v1/game/worlds", json={"pocket_id": pocket_id})
        wid = start.json()["world_id"]
        resp = await c.post(
            f"/api/v1/game/worlds/{wid}/beat",
            json={
                "player": "You",
                "text": "While you argued with the guard, I pocketed a string of sausages.",
                "kind": None,
            },
        )
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert summary["kind"] == "theft"
    assert summary["grudge_level"] == "SLIGHTED"
    assert summary["bond"] < 50
    assert summary["reaction"].strip()
    assert summary["phase"]


@requires_game_profile
@pytest.mark.asyncio
async def test_events_since_filters(beanie_test_db, monkeypatch):
    """GET .../events?since=N returns only events with t > N."""
    pocket_id = await _seed_butcher_pocket()
    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        start = await c.post("/api/v1/game/worlds", json={"pocket_id": pocket_id})
        wid = start.json()["world_id"]
        await c.post(
            f"/api/v1/game/worlds/{wid}/beat",
            json={"player": "You", "text": "Morning, Bjorn!"},
        )
        all_events = (await c.get(f"/api/v1/game/worlds/{wid}/events?since=0")).json()["events"]
        assert all_events
        cursor = all_events[-1]["t"]

        await c.post(
            f"/api/v1/game/worlds/{wid}/beat",
            json={"player": "You", "text": "I stole your best knife."},
        )
        resp = await c.get(f"/api/v1/game/worlds/{wid}/events?since={cursor}")
    assert resp.status_code == 200, resp.text
    newer = resp.json()["events"]
    assert newer
    assert all(e["t"] > cursor for e in newer)
    assert any(e["type"] == "beat" for e in newer)


@requires_game_profile
@pytest.mark.asyncio
async def test_snapshot_route_carries_engine(beanie_test_db, monkeypatch):
    pocket_id = await _seed_butcher_pocket()
    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        start = await c.post("/api/v1/game/worlds", json={"pocket_id": pocket_id})
        wid = start.json()["world_id"]
        resp = await c.get(f"/api/v1/game/worlds/{wid}/snapshot")
    assert resp.status_code == 200, resp.text
    snap = resp.json()
    assert snap["engine"] == "templated"
    assert snap["phase"]
    assert {n["name"] for n in snap["npcs"]} == {"Bjorn", "Astrid"}


@requires_game_profile
@pytest.mark.asyncio
async def test_reputation_after_wrongs(beanie_test_db, monkeypatch):
    """POST .../reputation: a never-wronged NPC (Astrid) reads the player's
    portable record — NOTORIOUS after theft + betrayal against Bjorn."""
    pocket_id = await _seed_butcher_pocket()
    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        start = await c.post("/api/v1/game/worlds", json={"pocket_id": pocket_id})
        wid = start.json()["world_id"]
        await c.post(
            f"/api/v1/game/worlds/{wid}/beat",
            json={"player": "You", "text": "I pocketed your sausages.", "npc": "Bjorn"},
        )
        await c.post(
            f"/api/v1/game/worlds/{wid}/beat",
            json={
                "player": "You",
                "text": "I told the guard you water down the salt pork.",
                "npc": "Bjorn",
            },
        )
        resp = await c.post(
            f"/api/v1/game/worlds/{wid}/reputation",
            json={"npc": "Astrid", "player": "You"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["line"].strip()
    assert body["notoriety"] == "NOTORIOUS"
    assert body["npc"] == "Astrid"
    assert body["player"] == "You"


@requires_game_profile
@pytest.mark.asyncio
async def test_unknown_world_id_is_404(beanie_test_db, monkeypatch):
    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        beat = await c.post(
            "/api/v1/game/worlds/nope1234/beat", json={"player": "You", "text": "hi"}
        )
        snap = await c.get("/api/v1/game/worlds/nope1234/snapshot")
        events = await c.get("/api/v1/game/worlds/nope1234/events")
        rep = await c.post(
            "/api/v1/game/worlds/nope1234/reputation", json={"npc": "A", "player": "B"}
        )
    assert beat.status_code == 404
    assert snap.status_code == 404
    assert events.status_code == 404
    assert rep.status_code == 404


@requires_game_profile
@pytest.mark.asyncio
async def test_cross_tenant_world_is_404(beanie_test_db, monkeypatch):
    """A world started under ws_game_owner is a 404 for another workspace —
    the runtime treats a foreign handle exactly like an unknown one."""
    pocket_id = await _seed_butcher_pocket()
    owner_app = _build_app(WS_OWNER, monkeypatch)
    async with _client(owner_app) as c:
        wid = (await c.post("/api/v1/game/worlds", json={"pocket_id": pocket_id})).json()[
            "world_id"
        ]

    intruder_app = _build_app("ws_intruder", monkeypatch)
    async with _client(intruder_app) as c:
        resp = await c.get(f"/api/v1/game/worlds/{wid}/snapshot")
    assert resp.status_code == 404


@requires_game_profile
@pytest.mark.asyncio
async def test_start_world_rejects_non_game_pocket(beanie_test_db, monkeypatch):
    """A pocket that isn't type='game' is a 400 — the run surface only wakes
    living worlds."""
    from pocketpaw_ee.cloud.pockets.service import agent_create

    view, pocket_id, err = await agent_create(
        workspace_id=WS_OWNER,
        owner_id=USER_ID,
        name="Just a dashboard",
        ripple_spec={"type": "container"},
        trusted=True,
    )
    assert err is None and pocket_id

    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        resp = await c.post("/api/v1/game/worlds", json={"pocket_id": pocket_id})
    assert resp.status_code == 400
    assert "not a game pocket" in resp.text


@requires_game_profile
@pytest.mark.asyncio
async def test_start_world_missing_pocket_is_404(beanie_test_db, monkeypatch):
    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        resp = await c.post("/api/v1/game/worlds", json={"pocket_id": "64" + "0" * 22})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Runtime-independent routes — these run on profile-less installs too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_example_creates_then_returns_same_pocket(beanie_test_db, monkeypatch):
    """POST /game/seed_example persists the canonical Butcher pocket (tense
    preset dials filled by the service); a second call finds it by
    name+type in the caller's workspace and returns the SAME id."""
    from bson import ObjectId
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
    from pocketpaw_ee.game.service import VIBE_DIAL_PRESETS

    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        first = await c.post("/api/v1/game/seed_example", json={})
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["created"] is True
        pocket_id = body["pocket_id"]
        assert pocket_id

        second = await c.post("/api/v1/game/seed_example", json={})
        assert second.status_code == 200, second.text
        assert second.json() == {"pocket_id": pocket_id, "created": False}

    # Ground truth in Mongo: a real game pocket with the tense preset dials.
    doc = await _PocketDoc.get(ObjectId(pocket_id))
    assert doc is not None
    assert doc.type == "game"
    assert doc.pattern == "living-world"
    assert doc.name == "The Butcher Remembers"
    assert [m["name"] for m in doc.rippleSpec["cast"]] == ["Bjorn", "Astrid"]
    assert doc.rippleSpec["zones"] == ["stall", "tavern", "tables", "door"]
    assert doc.rippleSpec["dials"] == VIBE_DIAL_PRESETS["tense"]


@pytest.mark.asyncio
async def test_routes_503_when_runtime_unavailable(beanie_test_db, monkeypatch):
    """Every run route degrades to a clear 503 when the engine isn't
    installed — the exact posture a published-soul-protocol deployment hits."""
    from pocketpaw_ee.game import runtime

    monkeypatch.setattr(runtime, "GAME_RUNTIME_AVAILABLE", False)
    monkeypatch.setattr(
        runtime, "GAME_RUNTIME_UNAVAILABLE_REASON", "No module named 'soul_protocol.profiles'"
    )

    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        start = await c.post("/api/v1/game/worlds", json={"pocket_id": "whatever"})
        beat = await c.post("/api/v1/game/worlds/w1/beat", json={"player": "p", "text": "t"})
        snap = await c.get("/api/v1/game/worlds/w1/snapshot")
        events = await c.get("/api/v1/game/worlds/w1/events")
        rep = await c.post("/api/v1/game/worlds/w1/reputation", json={"npc": "a", "player": "b"})
    for resp in (start, beat, snap, events, rep):
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"]["code"] == "game.runtime_unavailable"


@pytest.mark.asyncio
async def test_seed_example_works_without_runtime(beanie_test_db, monkeypatch):
    """Seeding only PERSISTS the pocket — it must succeed even when the run
    engine is absent (the pocket is durable; the world can be woken later)."""
    from pocketpaw_ee.game import runtime

    monkeypatch.setattr(runtime, "GAME_RUNTIME_AVAILABLE", False)
    app = _build_app(WS_OWNER, monkeypatch)
    async with _client(app) as c:
        resp = await c.post("/api/v1/game/seed_example", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is True
