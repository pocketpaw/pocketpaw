# tests/ee/game/test_runtime.py — the in-process GameWorld runtime
# (ee/pocketpaw_ee/game/runtime.py). Created: 2026-07-02 (feat/game-surface,
# PE-A). Pure in-memory coverage — no Mongo, no HTTP: the runtime takes the
# pocket WIRE DICT as input, so a literal dict stands in for the pockets
# service here (the router test drives the real persisted path end to end).
# Layers:
#   1. classify_kind — the deterministic keyword classifier ported from the
#      Butcher demo server, including the load-bearing precedence rule (theft
#      is checked before betrayal so "I pocketed it while you argued with the
#      guard" reads as theft).
#   2. start_world — cast → GrudgeKernels, default player "You", zones
#      (explicit cast zone wins; round-robin fallback; players at the LAST
#      zone), engine stamp, and the fail-closed paths (empty spec, invalid
#      spec, player/NPC name collision).
#   3. beat — auto-classification moves the grudge + weakens the bond; an
#      explicit kind overrides the classifier; unknown player/kind raise.
#   4. events_since — strict t > since filtering.
#   5. reputation — two wrongs (theft + betrayal) push the player's portable
#      notoriety to NOTORIOUS (thresholds: 2 deeds OR total severity >= 1.0)
#      and a NEVER-wronged NPC reads it off the player.soul.
#   6. Tenancy — a cross-workspace world_id raises the SAME KeyError as an
#      unknown one.
#
# Skips: soul-protocol itself is a base dep (always importable); the GAME
# PROFILE only exists on the experiment branch, so the module skips when
# ``soul_protocol.profiles.game`` is missing — the exact CI condition.

"""Tests for the game-world runtime (start / beat / events / reputation)."""

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("soul_protocol")
pytest.importorskip("soul_protocol.profiles.game")

from pocketpaw_ee.game import runtime  # noqa: E402

WS = "ws_game_owner"


def _world_spec() -> dict:
    """A Butcher-shaped world spec, as it rides a game pocket's rippleSpec."""
    return {
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
                # no zone — exercises the round-robin fallback (index 1 → "tavern")
            },
        ],
        "zones": ["stall", "tavern", "tables", "door"],
        "dials": {
            "challenge": 0.8,
            "progress": 0.6,
            "choice": 0.5,
            "bonds": 0.4,
            "mark": 0.5,
            "pulse": 0.9,
            "spark": 0.4,
        },
        "vibe": "tense",
    }


def _pocket(spec: dict | None = None) -> dict:
    """The slice of the pockets wire dict the runtime consumes."""
    return {"name": "The Butcher Remembers", "type": "game", "rippleSpec": spec or _world_spec()}


async def _start(workspace_id: str = WS) -> tuple[str, dict]:
    return await runtime.start_world(
        workspace_id=workspace_id, pocket_id="pk_game_1", pocket=_pocket()
    )


# ---------------------------------------------------------------------------
# classify_kind — ported keyword classifier
# ---------------------------------------------------------------------------


class TestClassifyKind:
    def test_theft_wins_over_betrayal(self) -> None:
        """The demo's load-bearing precedence: 'pocketed ... guard' is theft."""
        line = "While you argued with the guard, I pocketed a string of sausages."
        assert runtime.classify_kind(line) == "theft"

    def test_betrayal(self) -> None:
        assert runtime.classify_kind("I told the town guard all about you.") == "betrayal"

    def test_insult(self) -> None:
        assert runtime.classify_kind("You are a worthless coward.") == "insult"

    def test_neutral(self) -> None:
        assert runtime.classify_kind("Good morning! Fine sausages today.") == "neutral"


# ---------------------------------------------------------------------------
# start_world — composition + snapshot
# ---------------------------------------------------------------------------


class TestStartWorld:
    @pytest.mark.asyncio
    async def test_snapshot_has_cast_and_zones(self) -> None:
        world_id, snap = await _start()
        assert world_id
        assert [n["name"] for n in snap["npcs"]] == ["Bjorn", "Astrid"]
        # Explicit cast zone honored; missing zone round-robins (index 1 →
        # "tavern"); the default player "You" stands at the LAST zone ("door").
        assert snap["zones"]["Bjorn"] == "stall"
        assert snap["zones"]["Astrid"] == "tavern"
        assert [p["name"] for p in snap["players"]] == ["You"]
        assert snap["zones"]["You"] == "door"
        assert snap["engine"] == "templated"
        # Fresh world: no grudges yet, phase reported.
        assert snap["phase"]
        for npc in snap["npcs"]:
            for rel in npc["players"].values():
                assert rel["grudge"] == "NONE"

    @pytest.mark.asyncio
    async def test_empty_ripple_spec_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no world spec"):
            await runtime.start_world(
                workspace_id=WS,
                pocket_id="pk_empty",
                pocket={"name": "x", "type": "game", "rippleSpec": {}},
            )

    @pytest.mark.asyncio
    async def test_invalid_spec_fails_closed_with_problems(self) -> None:
        spec = _world_spec()
        spec["cast"] = []
        with pytest.raises(ValueError, match="`cast`"):
            await runtime.start_world(workspace_id=WS, pocket_id="pk_bad", pocket=_pocket(spec))

    @pytest.mark.asyncio
    async def test_player_name_colliding_with_npc_is_rejected(self) -> None:
        spec = _world_spec()
        spec["players"] = [{"name": "bjorn"}]  # case-insensitive collision
        with pytest.raises(ValueError, match="collides"):
            await runtime.start_world(workspace_id=WS, pocket_id="pk_collide", pocket=_pocket(spec))


# ---------------------------------------------------------------------------
# beat — the one verb
# ---------------------------------------------------------------------------


class TestBeat:
    @pytest.mark.asyncio
    async def test_theft_text_auto_classifies_and_grudge_moves(self) -> None:
        world_id, snap = await _start()
        assert snap["npcs"][0]["players"] != {}

        summary = await runtime.beat(
            world_id,
            workspace_id=WS,
            player="You",
            text="While you argued with the guard, I pocketed a string of sausages.",
        )
        assert summary["kind"] == "theft"  # auto-classified, no kind sent
        assert summary["npc"] == "Bjorn"  # default routing: first NPC
        assert summary["grudge_level"] == "SLIGHTED"  # one theft: NONE → SLIGHTED
        assert summary["bond"] < 50  # theft weakened the seeded bond
        assert summary["reaction"].strip()
        assert summary["phase"]

        # The snapshot reflects the moved grudge.
        after = runtime.snapshot(world_id, workspace_id=WS)
        bjorn = next(n for n in after["npcs"] if n["name"] == "Bjorn")
        (rel,) = bjorn["players"].values()
        assert rel["grudge"] == "SLIGHTED"
        assert "pocketed" in (rel["last_grievance"] or "")

    @pytest.mark.asyncio
    async def test_explicit_kind_overrides_classifier(self) -> None:
        world_id, _ = await _start()
        summary = await runtime.beat(
            world_id,
            workspace_id=WS,
            player="You",
            text="I pocketed a sausage (just kidding, paying in full).",
            kind="neutral",
        )
        assert summary["kind"] == "neutral"
        assert summary["grudge_level"] == "NONE"

    @pytest.mark.asyncio
    async def test_named_npc_routing_is_case_insensitive(self) -> None:
        world_id, _ = await _start()
        summary = await runtime.beat(
            world_id, workspace_id=WS, player="You", text="A mug of ale, please.", npc="astrid"
        )
        assert summary["npc"] == "Astrid"

    @pytest.mark.asyncio
    async def test_unknown_player_raises_lookup(self) -> None:
        world_id, _ = await _start()
        with pytest.raises(LookupError, match="unknown player"):
            await runtime.beat(world_id, workspace_id=WS, player="Nobody", text="hi")

    @pytest.mark.asyncio
    async def test_unknown_explicit_kind_raises_value_error(self) -> None:
        world_id, _ = await _start()
        with pytest.raises(ValueError, match="unknown kind"):
            await runtime.beat(world_id, workspace_id=WS, player="You", text="hi", kind="arson")


# ---------------------------------------------------------------------------
# events_since — poll cursor
# ---------------------------------------------------------------------------


class TestEvents:
    @pytest.mark.asyncio
    async def test_since_filters_strictly(self) -> None:
        world_id, _ = await _start()
        await runtime.beat(world_id, workspace_id=WS, player="You", text="Morning, Bjorn!")
        first_batch = runtime.events_since(world_id, workspace_id=WS, since=0)
        assert first_batch  # move events from zoning + the first beat run
        cursor = first_batch[-1]["t"]

        await runtime.beat(
            world_id, workspace_id=WS, player="You", text="I stole your best knife.", npc="Bjorn"
        )
        newer = runtime.events_since(world_id, workspace_id=WS, since=cursor)
        assert newer
        assert all(e["t"] > cursor for e in newer)
        # The new run narrates the beat itself.
        assert any(e["type"] == "beat" for e in newer)
        # And nothing older leaked through the cursor.
        assert {e["t"] for e in newer}.isdisjoint({e["t"] for e in first_batch})


# ---------------------------------------------------------------------------
# reputation — portable notoriety read by a never-wronged NPC
# ---------------------------------------------------------------------------


class TestReputation:
    @pytest.mark.asyncio
    async def test_notoriety_after_wrongs(self) -> None:
        world_id, _ = await _start()
        # Two heavy wrongs against Bjorn only (theft 0.7 + betrayal 0.9 —
        # both the 2-deed count gate and the 1.0 severity gate → NOTORIOUS).
        await runtime.beat(
            world_id, workspace_id=WS, player="You", text="I pocketed your sausages."
        )
        await runtime.beat(
            world_id,
            workspace_id=WS,
            player="You",
            text="I told the guard you water down the salt pork.",
        )

        # Astrid was never wronged — she reads the PORTABLE record.
        result = await runtime.reputation(world_id, workspace_id=WS, npc="Astrid", player="You")
        assert result["npc"] == "Astrid"
        assert result["player"] == "You"
        assert result["line"].strip()
        assert result["notoriety"] == "NOTORIOUS"

    @pytest.mark.asyncio
    async def test_clean_player_reads_unknown(self) -> None:
        world_id, _ = await _start()
        result = await runtime.reputation(world_id, workspace_id=WS, npc="Astrid", player="You")
        assert result["notoriety"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Tenancy + registry
# ---------------------------------------------------------------------------


class TestTenancyAndRegistry:
    @pytest.mark.asyncio
    async def test_cross_workspace_handle_is_unknown(self) -> None:
        """A world under another workspace raises the SAME KeyError as an
        unknown id — a guessed handle must not confirm the world exists."""
        world_id, _ = await _start(workspace_id=WS)
        with pytest.raises(KeyError):
            runtime.snapshot(world_id, workspace_id="ws_intruder")
        with pytest.raises(KeyError):
            runtime.events_since(world_id, workspace_id="ws_intruder", since=0)

    @pytest.mark.asyncio
    async def test_unknown_world_id_is_key_error(self) -> None:
        with pytest.raises(KeyError):
            runtime.snapshot("nope1234", workspace_id=WS)
