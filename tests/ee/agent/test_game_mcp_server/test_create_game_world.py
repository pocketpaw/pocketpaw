# tests/ee/agent/test_game_mcp_server/test_create_game_world.py
# Created: 2026-07-02 (feat/game-surface, PW-2) — coverage for the /game
# deterministic create tool ``create_game_world`` on the in-process
# ``pocketpaw_game`` server, mirroring the layers of
# tests/ee/agent/test_sites_mcp_server/test_create_dynamic_site.py:
#   1. World-spec validation (``game.service.validate_world_spec``) — a spec
#      must declare a non-empty `cast` of Souls (each with a name) and a
#      non-empty `zones` list; `dials`, when given, must use only the seven
#      known keys with numeric 0-1 values. The create fails CLOSED otherwise.
#   2. The v0 vibe→dials preset table (``resolve_dials``) — cozy / tense /
#      mystery / sandbox match as substrings of the vibe; unknown vibe →
#      balanced default; the service fills omitted dials from it.
#   3. Registration — the tool id rides the ``pocketpaw_game`` allowlist, the
#      provider advertises it, and the /game SurfaceProfile scopes to it.
#   4. End-to-end handler — against a real (mongomock) Beanie DB it persists
#      the world via ``agent_create`` and reads the PERSISTED _PocketDoc back
#      to confirm type=="game", pattern=="living-world", and — the
#      load-bearing assertion — that the world blocks (cast/zones/dials/vibe)
#      SURVIVED normalization on the persisted rippleSpec (ground truth in
#      Mongo, NOT agent narration).
# The autouse ``_default_game_plan`` fixture (mirrors the sites tests'
# ``_default_sites_plan``) defaults the plan to "go" so the end-to-end create
# exercises the persist mechanics; one denial test flips it to "free" to prove
# the gate wiring (game has no separate plan-gate test file yet).
"""Tests for the game-world create tool (create_game_world)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _default_game_plan():
    """create_game_world runs the shared Game plan gate
    (game.service.require_game_plan) before persisting — the same posture as the
    sites create handlers. These tests use synthetic workspace ids with no seeded
    Workspace doc, so default the plan to one that unlocks Game ("go") to
    exercise the create mechanics. Denial is covered by
    ``test_free_plan_is_denied`` below. Mirrors the autouse fixture in
    test_sites_mcp_server/test_create_dynamic_site.py."""
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


# A representative cozy world spec: three Souls, three zones, no dials (the
# vibe preset fills them). Mirrors the example in the bundled game skill.
def _tea_town_spec(with_dials: bool = False) -> dict:
    spec: dict = {
        "cast": [
            {
                "name": "Mirren",
                "archetype": "keeper",
                "persona": "runs the tea house, forgets nothing",
                "ocean": {"openness": 0.7, "agreeableness": 0.9},
            },
            {"name": "Osk", "archetype": "rival", "persona": "undercuts everyone", "ocean": {}},
            {"name": "Petal", "archetype": "wanderer", "persona": "arrives with the fog"},
        ],
        "zones": ["the tea house", "the cliff stairs", "the fog market"],
    }
    if with_dials:
        spec["dials"] = {
            "challenge": 0.1,
            "progress": 0.5,
            "choice": 0.6,
            "bonds": 1.0,
            "mark": 0.7,
            "pulse": 0.2,
            "spark": 0.5,
        }
    return spec


COZY_VIBE = "a cozy cliffside tea town where the fog gossips"


# ---------------------------------------------------------------------------
# World-spec validation (pure — no identity / Mongo needed)
# ---------------------------------------------------------------------------


class TestWorldSpecValidation:
    def test_complete_tea_town_spec_is_valid(self) -> None:
        from pocketpaw_ee.game.service import validate_world_spec

        assert validate_world_spec(_tea_town_spec()) == []
        assert validate_world_spec(_tea_town_spec(with_dials=True)) == []

    def test_missing_cast_is_rejected(self) -> None:
        from pocketpaw_ee.game.service import validate_world_spec

        spec = {k: v for k, v in _tea_town_spec().items() if k != "cast"}
        problems = validate_world_spec(spec)
        assert any("`cast`" in p for p in problems)

    def test_cast_member_without_name_is_rejected(self) -> None:
        from pocketpaw_ee.game.service import validate_world_spec

        spec = _tea_town_spec()
        spec["cast"].append({"archetype": "ghost"})
        problems = validate_world_spec(spec)
        assert any("cast[3]" in p and "`name`" in p for p in problems)

    def test_missing_zones_is_rejected(self) -> None:
        from pocketpaw_ee.game.service import validate_world_spec

        spec = {k: v for k, v in _tea_town_spec().items() if k != "zones"}
        problems = validate_world_spec(spec)
        assert any("`zones`" in p for p in problems)

    def test_unknown_dial_key_is_rejected(self) -> None:
        """JUICE is platform-provided — it is NOT one of the seven dials."""
        from pocketpaw_ee.game.service import validate_world_spec

        spec = _tea_town_spec(with_dials=True)
        spec["dials"]["juice"] = 0.9
        problems = validate_world_spec(spec)
        assert any("unknown dial keys" in p and "juice" in p for p in problems)

    def test_out_of_range_dial_is_rejected(self) -> None:
        from pocketpaw_ee.game.service import validate_world_spec

        spec = _tea_town_spec(with_dials=True)
        spec["dials"]["challenge"] = 1.5
        problems = validate_world_spec(spec)
        assert any("`challenge`" in p and "between 0 and 1" in p for p in problems)


# ---------------------------------------------------------------------------
# The v0 vibe→dials preset table
# ---------------------------------------------------------------------------


class TestVibeDialPresets:
    def test_presets_cover_all_seven_dials(self) -> None:
        from pocketpaw_ee.game.service import (
            BALANCED_DIALS,
            VIBE_DIAL_PRESETS,
            WORLD_DIAL_KEYS,
        )

        assert set(VIBE_DIAL_PRESETS) == {"cozy", "tense", "mystery", "sandbox"}
        for name, preset in VIBE_DIAL_PRESETS.items():
            assert set(preset) == set(WORLD_DIAL_KEYS), f"{name} preset must set all seven dials"
        assert set(BALANCED_DIALS) == set(WORLD_DIAL_KEYS)

    def test_vibe_substring_picks_preset(self) -> None:
        from pocketpaw_ee.game.service import VIBE_DIAL_PRESETS, resolve_dials

        assert resolve_dials(COZY_VIBE) == VIBE_DIAL_PRESETS["cozy"]
        assert resolve_dials("a TENSE submarine standoff") == VIBE_DIAL_PRESETS["tense"]
        assert resolve_dials("a foggy mystery at the manor") == VIBE_DIAL_PRESETS["mystery"]
        assert resolve_dials("an open sandbox island") == VIBE_DIAL_PRESETS["sandbox"]

    def test_unknown_vibe_falls_back_to_balanced(self) -> None:
        from pocketpaw_ee.game.service import BALANCED_DIALS, resolve_dials

        assert resolve_dials("a chrome-plated heist opera") == BALANCED_DIALS

    def test_resolve_dials_returns_a_copy(self) -> None:
        """Callers overlay author dials on the result — the table must never
        be mutated through it."""
        from pocketpaw_ee.game.service import VIBE_DIAL_PRESETS, resolve_dials

        dials = resolve_dials("cozy")
        dials["challenge"] = 0.99
        assert VIBE_DIAL_PRESETS["cozy"]["challenge"] == 0.2


# ---------------------------------------------------------------------------
# Registration — the tool rides the pocketpaw_game allowlist + /game profile
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.game import (
            CREATE_GAME_WORLD_TOOL_ID,
            GAME_TOOL_IDS,
        )

        assert CREATE_GAME_WORLD_TOOL_ID == "mcp__pocketpaw_game__create_game_world"
        assert CREATE_GAME_WORLD_TOOL_ID in GAME_TOOL_IDS

    def test_provider_advertises_game_tool_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.game import CREATE_GAME_WORLD_TOOL_ID
        from pocketpaw_ee.extensions import CloudGameMcpProvider

        assert CREATE_GAME_WORLD_TOOL_ID in CloudGameMcpProvider().tool_ids()

    def test_game_surface_profile_scopes_to_the_tool(self) -> None:
        """The /game SurfaceProfile's MCP allow-list names exactly the game
        tool ids (the surface_registry lazy-load path)."""
        from pocketpaw_ee.agent.mcp_servers.game import GAME_TOOL_IDS
        from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

        profile = resolve_profile(SurfaceKind.GAME, SurfaceMeta())
        assert profile.allow_mcp_tool_ids == frozenset(GAME_TOOL_IDS)


# ---------------------------------------------------------------------------
# End-to-end handler — persist + read back from Mongo (ground truth)
# ---------------------------------------------------------------------------


@pytest.fixture()
def recording_bus():
    """Install a recording EventBus so ``agent_create``'s ``emit(PocketCreated)``
    doesn't raise (the real bus is only wired by ``init_realtime()`` at boot).
    Mirrors tests/ee/agent/test_sites_mcp_server/test_create_dynamic_site.py."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


class TestCreateGameWorldEndToEnd:
    @pytest.mark.asyncio
    async def test_persists_game_pocket_with_world_intact(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Drive the handler against a real (mongomock) Beanie DB and read the
        persisted _PocketDoc back. Proves a pocket lands with type=="game",
        pattern=="living-world", and the world blocks (cast/zones/dials/vibe)
        SURVIVED normalization on the persisted rippleSpec — plus that the
        omitted dials were filled from the cozy vibe preset."""
        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import game as game_mcp
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
        from pocketpaw_ee.game.service import VIBE_DIAL_PRESETS

        workspace_id = str(ObjectId())
        user_id = str(ObjectId())

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=workspace_id,
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=user_id,
            ),
        ):
            out = await game_mcp._create_game_world_handler(
                {
                    "name": "Saltwind Terrace",
                    "vibe": COZY_VIBE,
                    "world_spec": _tea_town_spec(),  # no dials — preset fills
                }
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        pocket_id = body["pocket_id"]
        assert pocket_id
        assert body["pocket"]["type"] == "game"
        assert body["pocket"]["pattern"] == "living-world"

        # Ground truth: read the persisted doc straight from Mongo.
        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.type == "game"
        assert doc.pattern == "living-world"
        # The world blocks MUST survive normalization on the persisted
        # rippleSpec — the world runtime wakes the Souls off these keys.
        spec = doc.rippleSpec
        assert isinstance(spec, dict)
        assert [m["name"] for m in spec["cast"]] == ["Mirren", "Osk", "Petal"]
        assert spec["zones"] == ["the tea house", "the cliff stairs", "the fog market"]
        assert spec["vibe"] == COZY_VIBE
        # Omitted dials were filled from the cozy preset ("cozy" is a
        # substring of the vibe).
        assert spec["dials"] == VIBE_DIAL_PRESETS["cozy"]

    @pytest.mark.asyncio
    async def test_author_dials_win_over_preset(self, beanie_test_db, recording_bus) -> None:
        """Author-provided dials persist verbatim — the preset only fills."""
        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import game as game_mcp
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=str(ObjectId()),
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=str(ObjectId()),
            ),
        ):
            out = await game_mcp._create_game_world_handler(
                {
                    "name": "Saltwind Terrace",
                    "vibe": COZY_VIBE,
                    "world_spec": _tea_town_spec(with_dials=True),
                }
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        doc = await _PocketDoc.get(ObjectId(body["pocket_id"]))
        assert doc is not None
        assert doc.rippleSpec["dials"]["challenge"] == 0.1
        assert doc.rippleSpec["dials"]["bonds"] == 1.0

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import game as game_mcp

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=None,
            ),
        ):
            out = await game_mcp._create_game_world_handler(
                {"vibe": COZY_VIBE, "world_spec": _tea_town_spec()}
            )

        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_world_spec_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import game as game_mcp

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value="ws_1",
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value="u_1",
            ),
        ):
            out = await game_mcp._create_game_world_handler({"vibe": COZY_VIBE})

        assert out.get("is_error") is True
        assert "`world_spec`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_invalid_world_spec_fails_closed(self) -> None:
        """An empty cast fails closed with the actionable problems list — the
        agent fixes the spec instead of persisting a hollow world."""
        from pocketpaw_ee.agent.mcp_servers import game as game_mcp

        broken = {"cast": [], "zones": ["somewhere"]}
        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value="ws_1",
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value="u_1",
            ),
        ):
            out = await game_mcp._create_game_world_handler(
                {"vibe": COZY_VIBE, "world_spec": broken}
            )

        assert out.get("is_error") is True
        assert "`cast`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_free_plan_is_denied(self) -> None:
        """The plan gate mirrors the Sites posture: a free-plan workspace gets
        plan.feature_denied, not a phantom-created world."""
        from pocketpaw_ee.agent.mcp_servers import game as game_mcp

        with (
            patch(
                "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
                new=AsyncMock(return_value="free"),
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value="ws_1",
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value="u_1",
            ),
        ):
            out = await game_mcp._create_game_world_handler(
                {"vibe": COZY_VIBE, "world_spec": _tea_town_spec()}
            )

        assert out.get("is_error") is True
        assert "plan.feature_denied" in out["content"][0]["text"]
