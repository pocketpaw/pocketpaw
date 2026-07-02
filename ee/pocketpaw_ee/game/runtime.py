# ee/pocketpaw_ee/game/runtime.py — In-process GameWorld runtime: wakes a
# persisted Pocket type="game" world-spec into a LIVE soul_protocol GameWorld
# and exposes beat / events / snapshot / reputation over an in-memory registry.
#
# Created: 2026-07-02 (feat/game-surface, PE-A) — pocketPaw RUNS games now, not
# just stamps game pockets. The engine is the sibling soul-protocol package's
# Game Profile (``soul_protocol.profiles.game``: GrudgeKernel npcs, PlayerSoul
# players, DirectorEngine pacing, the four runnable feel dials, one GameWorld
# composition root per scene). Design decisions:
#   * GUARDED DEPENDENCY. ``soul-protocol`` is already a base dep of the OSS
#     core (``soul-protocol[engine]>=0.3.1``), but ``profiles.game`` only
#     exists on soul-protocol's ``experiment/npc-soul-grudge-kernel`` branch —
#     no published release carries it. Everything here import-guards
#     (``GAME_RUNTIME_AVAILABLE``); the REST router 503s with a clear message
#     and the tests importorskip, so CI (which installs the published
#     soul-protocol) stays green. Dev loop: after ``uv sync --dev --group ee``
#     run ``uv pip install -e ../../../../soul-protocol`` (path from the
#     game-surface worktree; ``../soul-protocol`` from a main paw-workspace
#     checkout) to swap the registry wheel for the experiment checkout. A
#     plain ``uv run`` re-syncs and reverts that swap — use ``uv run
#     --no-sync`` while working on the game runtime. A ``[tool.uv.sources]``
#     path override was tried and REJECTED: soul-protocol being a BASE dep
#     means the override repoints every environment (including CI, which has
#     no sibling checkout) at the path, hard-failing all ``uv sync`` jobs.
#     A sys-path injection env var was also rejected: the registry
#     soul-protocol is always installed, so injecting a second copy ahead of
#     it would partially shadow an imported package (version-skew hazard).
#   * V0 EPHEMERAL REGISTRY. Worlds live in a process-local dict keyed by a
#     short world_id and DIE WITH THE PROCESS — no persistence, no cross-worker
#     sharing. Restarting the app forgets every running world (the pocket, the
#     durable artifact, survives; re-POST /worlds to wake it again).
#   * TENANT-SCOPED HANDLES. Every accessor requires the caller's
#     workspace_id and treats a cross-tenant world_id exactly like an unknown
#     one (KeyError → 404), so a guessed handle never leaks another tenant's
#     world.
#   * FASTAPI-NATIVE ASYNC. Unlike the Butcher demo server (threads + one
#     background loop), the app is already async — world calls are awaited
#     directly, serialized per-world with an asyncio.Lock so concurrent beats
#     can't interleave mid-beat.
#   * KIND CLASSIFIER. ``classify_kind`` is ported from the demo server
#     (examples/butcher_remembers/server.py): deterministic keyword rules,
#     checked theft → betrayal → insult so "I pocketed it while you argued
#     with the guard" reads as the theft it is. An explicit kind always wins.
#   * DIALS. The seven authored world dials persist on the pocket; the four
#     RUNNABLE ones (challenge / progress / choice / spark) wire into
#     ``Dials`` and drive the director + trackers. bonds / mark / pulse are
#     authored-but-not-yet-consumed by the engine (no runtime knob in the
#     profile yet) — carried in the spec, ignored here, documented so nobody
#     hunts for a phantom wiring.
#   * OCEAN. Cast ``ocean`` sketches are authoring metadata for now —
#     ``GrudgeKernel.birth`` seeds its own tuned OCEAN vector and takes no
#     override; per-NPC personality injection is a profile follow-up.

"""Run persisted game pockets as live soul-protocol GameWorlds (v0, in-memory).

The flow: a Pocket stamped ``type="game"`` carries the world spec (cast /
zones / dials / vibe) on its ``rippleSpec``. :func:`start_world` births a
:class:`GrudgeKernel` per cast entry and a :class:`PlayerSoul` per player
(default: one player named "You"), zones everyone via ``move()``, wires the
runnable dials, and registers the composed :class:`GameWorld` under a short
``world_id``. :func:`beat` routes one player line into the world (kind
auto-classified when absent); :func:`events_since` / :func:`snapshot` /
:func:`reputation` are the readbacks the /game HUD polls.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Guarded engine import — the game profile only exists on soul-protocol's
# experiment branch. Absence (or a published, profile-less soul-protocol)
# flips GAME_RUNTIME_AVAILABLE False; the router 503s, tests importorskip.
# ---------------------------------------------------------------------------
try:
    from soul_protocol.profiles.game import Dials, GameWorld, GrudgeKernel, PlayerSoul

    GAME_RUNTIME_AVAILABLE = True
    GAME_RUNTIME_UNAVAILABLE_REASON: str | None = None
except ImportError as _exc:  # pragma: no cover — exercised via monkeypatch in tests
    GAME_RUNTIME_AVAILABLE = False
    GAME_RUNTIME_UNAVAILABLE_REASON = str(_exc)
    Dials = GameWorld = GrudgeKernel = PlayerSoul = None  # type: ignore[assignment]

# The dialogue engine is the deterministic TemplatedDialogueEngine (kernel
# default) — free, offline, replay-safe. Snapshot responses stamp it so the
# client knows cost overlays are meaningless.
ENGINE_NAME = "templated"

# ---------------------------------------------------------------------------
# Free-play kind classifier — ported verbatim from the Butcher demo server
# (soul-protocol examples/butcher_remembers/server.py). First matching rule
# wins, checked in this order — theft before betrayal so "I pocketed it while
# you argued with the guard" reads as the theft it is. An explicit "kind" in
# the request always overrides the classifier.
# ---------------------------------------------------------------------------
_KIND_RULES: list[tuple[str, str]] = [
    (
        "theft",
        r"\b(steal|stole|stolen|pocket\w*|rob|robbed|robbing|pilfer\w*|swipe[ds]?|thief|theft)\b",
    ),
    (
        "betrayal",
        r"\b(betray\w*|lie[ds]?|lying|frame[ds]?|framing|guard|snitch\w*|traitor|sold\s+you\s+out)\b",
    ),
    (
        "insult",
        r"\b(insult\w*|maggot\w*|fool|idiot|coward|stink\w*|reek\w*|ugly|worthless|scum|swine|pig|dog|liar|wretch\w*)\b",
    ),
]


def classify_kind(text: str) -> str:
    """Classify a free-play line into a transgression kind. Deterministic, no LLM."""
    lowered = str(text).lower()
    for kind, pattern in _KIND_RULES:
        if re.search(pattern, lowered):
            return kind
    return "neutral"


# ---------------------------------------------------------------------------
# The in-memory world registry (v0 — worlds die with the process).
# ---------------------------------------------------------------------------


@dataclass
class _WorldHandle:
    """One running world + its meta, serialized by a per-world lock."""

    world: Any  # GameWorld — Any so the module imports engine-less
    meta: dict[str, Any]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_WORLDS: dict[str, _WorldHandle] = {}


def reset_worlds() -> None:
    """Drop every running world — test isolation helper."""
    _WORLDS.clear()


def _handle(world_id: str, workspace_id: str) -> _WorldHandle:
    """The caller's world, or KeyError. A world under ANOTHER workspace raises
    the same KeyError as an unknown id — a guessed handle must not confirm
    another tenant's world exists."""
    handle = _WORLDS.get(world_id)
    if handle is None or handle.meta.get("workspace_id") != workspace_id:
        raise KeyError(world_id)
    return handle


def _kernel_named(world: Any, npc_name: str | None) -> Any:
    """The named NPC kernel (case-insensitive), or the first when unnamed.
    Mirrors the demo server's resolver."""
    if not npc_name:
        return world.npcs[0]
    for kernel in world.npcs:
        if kernel.npc_name.lower() == str(npc_name).lower():
            return kernel
    known = sorted(k.npc_name for k in world.npcs)
    raise LookupError(f"unknown npc {npc_name!r}; known: {known}")


def _player_named(world: Any, player_name: str | None) -> Any:
    """The named player soul (case-insensitive), or the first when unnamed."""
    if not player_name:
        return world.players[0]
    for player in world.players:
        if player.name.lower() == str(player_name).lower():
            return player
    known = sorted(p.name for p in world.players)
    raise LookupError(f"unknown player {player_name!r}; known: {known}")


def _require_runtime() -> None:
    if not GAME_RUNTIME_AVAILABLE:  # pragma: no cover — router 503s first
        raise RuntimeError(
            "game runtime unavailable: soul_protocol.profiles.game is not "
            f"importable ({GAME_RUNTIME_UNAVAILABLE_REASON})"
        )


# ---------------------------------------------------------------------------
# start_world — wake a persisted world spec into a live GameWorld.
# ---------------------------------------------------------------------------


async def start_world(*, workspace_id: str, pocket_id: str, pocket: dict) -> tuple[str, dict]:
    """Compose a live :class:`GameWorld` from a game pocket's world spec.

    ``pocket`` is the wire dict ``pockets.service.get`` returns; the world
    spec rides its ``rippleSpec`` (cast / zones / dials / vibe as sibling
    keys — the same envelope ``create_game_world`` persists). Births one
    :class:`GrudgeKernel` per cast entry (name / archetype / persona;
    TemplatedDialogueEngine v0) and one :class:`PlayerSoul` per ``players``
    entry (default: a single player named "You"), applies zones via
    ``move()`` — a cast entry's explicit ``zone`` wins, otherwise cast
    round-robins the spec's zones and players stand at the LAST zone (the
    demo's "door" position) — and wires the runnable dials.

    Returns ``(world_id, snapshot)`` where the snapshot already carries the
    ``engine`` stamp. Raises ``ValueError`` on a spec the runtime can't wake
    (missing/invalid cast or zones, per ``service.validate_world_spec``, or a
    player name colliding with an NPC name — zones are keyed by soul name).
    """
    _require_runtime()

    spec = pocket.get("rippleSpec")
    if not isinstance(spec, dict) or not spec:
        raise ValueError("pocket has no world spec (empty rippleSpec)")

    # Same validator the create path runs — a hand-edited / legacy pocket
    # fails here with the actionable problem list instead of a mid-birth crash.
    from pocketpaw_ee.game.service import validate_world_spec

    problems = validate_world_spec(spec)
    if problems:
        raise ValueError("world spec is not a valid living world — it needs " + "; ".join(problems))

    cast: list[dict] = spec["cast"]
    zone_list: list[str] = [z.strip() for z in spec["zones"]]

    kernels = []
    for member in cast:
        name = str(member["name"]).strip()
        archetype = str(member.get("archetype") or "The Villager").strip()
        persona_raw = member.get("persona")
        persona = (
            str(persona_raw).strip()
            if isinstance(persona_raw, str) and persona_raw.strip()
            else f"I am {name}, {archetype}."
        )
        kernels.append(await GrudgeKernel.birth(name=name, archetype=archetype, persona=persona))

    npc_names = {k.npc_name.lower() for k in kernels}

    players_raw = spec.get("players")
    player_entries: list[dict] = (
        [p for p in players_raw if isinstance(p, dict) and str(p.get("name") or "").strip()]
        if isinstance(players_raw, list)
        else []
    )
    if not player_entries:
        player_entries = [{"name": "You"}]  # v0 default — one local player

    player_souls = []
    for entry in player_entries:
        player_name = str(entry["name"]).strip()
        if player_name.lower() in npc_names:
            # GameWorld zones are keyed by soul NAME — a collision would fuse
            # the player and the NPC on the zone map. Fail closed, clearly.
            raise ValueError(f"player name {player_name!r} collides with an NPC name")
        player_souls.append(await PlayerSoul.birth(name=player_name))

    # The four RUNNABLE dials wire into the engine; bonds/mark/pulse are
    # authored-but-not-yet-consumed (see module header).
    dial_spec = spec.get("dials") if isinstance(spec.get("dials"), dict) else {}
    dials = Dials(
        challenge=float(dial_spec.get("challenge", 0.5)),
        progress=float(dial_spec.get("progress", 0.5)),
        choice=float(dial_spec.get("choice", 0.5)),
        spark=float(dial_spec.get("spark", 0.5)),
    )

    world = GameWorld(kernels, player_souls, dials=dials)

    # Zones: explicit cast ``zone`` wins; otherwise deterministic round-robin
    # over the spec's zones. Players stand at the last zone (the demo's door).
    for i, (kernel, member) in enumerate(zip(kernels, cast)):
        zone_raw = member.get("zone")
        zone = (
            zone_raw.strip()
            if isinstance(zone_raw, str) and zone_raw.strip()
            else zone_list[i % len(zone_list)]
        )
        world.move(kernel.npc_name, zone)
    for player in player_souls:
        world.move(player.name, zone_list[-1])

    world_id = uuid.uuid4().hex[:8]
    meta = {
        "workspace_id": workspace_id,
        "pocket_id": pocket_id,
        "name": str(pocket.get("name") or ""),
        "vibe": str(spec.get("vibe") or ""),
        "engine": ENGINE_NAME,
        "created_at": datetime.now(UTC).isoformat(),
    }
    _WORLDS[world_id] = _WorldHandle(world=world, meta=meta)
    return world_id, snapshot(world_id, workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Readbacks + the one verb.
# ---------------------------------------------------------------------------


async def beat(
    world_id: str,
    *,
    workspace_id: str,
    player: str,
    text: str,
    kind: str | None = None,
    npc: str | None = None,
) -> dict:
    """One player line into the world; returns the world's beat summary
    (reaction, grudge_level, bond, phase, pacing, ...).

    ``kind`` absent/blank → :func:`classify_kind` decides; an explicit kind
    always wins (an unknown one raises ``ValueError`` from the world).
    ``npc`` routes the beat (case-insensitive); default is the first NPC.
    Serialized per-world so concurrent beats can't interleave mid-run.
    """
    handle = _handle(world_id, workspace_id)
    async with handle.lock:
        world = handle.world
        player_soul = _player_named(world, player)
        resolved_kind = (kind or "").strip() or classify_kind(text)
        npc_name = _kernel_named(world, npc).npc_name if npc else None
        return await world.beat(
            player_soul.did,
            player_soul.name,
            text,
            kind=resolved_kind,
            npc_name=npc_name,
        )


def events_since(world_id: str, *, workspace_id: str, since: int = 0) -> list[dict]:
    """World events with ``t > since`` — the client's poll cursor."""
    handle = _handle(world_id, workspace_id)
    return [e for e in handle.world.events() if e["t"] > since]


def snapshot(world_id: str, *, workspace_id: str) -> dict:
    """The HUD bootstrap: ``GameWorld.snapshot()`` + the ``engine`` stamp."""
    handle = _handle(world_id, workspace_id)
    snap = handle.world.snapshot()
    snap["engine"] = handle.meta["engine"]
    return snap


async def reputation(world_id: str, *, workspace_id: str, npc: str, player: str) -> dict:
    """A (possibly never-wronged) NPC reads the player's portable reputation
    off their player.soul and reacts to it — the cross-soul gut-punch beat."""
    handle = _handle(world_id, workspace_id)
    async with handle.lock:
        world = handle.world
        kernel = _kernel_named(world, npc)
        player_soul = _player_named(world, player)
        line, notoriety = await kernel.react_to_reputation(player_soul)
        return {
            "npc": kernel.npc_name,
            "player": player_soul.name,
            "line": line,
            "notoriety": notoriety,
        }


__all__ = [
    "ENGINE_NAME",
    "GAME_RUNTIME_AVAILABLE",
    "GAME_RUNTIME_UNAVAILABLE_REASON",
    "beat",
    "classify_kind",
    "events_since",
    "reputation",
    "reset_worlds",
    "snapshot",
    "start_world",
]
