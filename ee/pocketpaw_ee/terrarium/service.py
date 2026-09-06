# ee/pocketpaw_ee/terrarium/service.py
#
# The terrarium glue: Beanie persistence, the Soul bridge, the Instinct gate and
# the realtime bus. The ONLY module that imports the ``domain`` Beanie doc
# classes (4-file entity rule), and the only one that knows about workspaces.
#
# What lives here vs elsewhere:
#   * verb rules, cost accounting, the write-policy  -> world.py (pure)
#   * god powers and their effects                   -> weather.py (pure)
#   * the judgment call                              -> llm.py
#   * everything that touches Mongo, a .soul file, an Instinct Action or the
#     bus                                            -> here
#
# Invariants enforced at this seam:
#   1. ``seq`` is monotonic per universe (assigned under a per-universe lock).
#   2. ``cost: 0`` only for gate/weather/hibernate/arrive (asserted on write).
#   3. balance <= 0 at end of tick -> state ``hibernating``, soul file KEPT.
#   4. viewer-origin text never becomes soul fact (the episodic summary is
#      built from citizen-origin events only — see world.episodic_summary).
#   5. the Journal is truth; citizens/ledger/artifacts are projections.

"""Terrarium service — persistence, souls, the gate and the bus."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pocketpaw_ee.cloud._core.errors import BadRequest, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.terrarium import events as world_events
from pocketpaw_ee.terrarium import llm as citizen_llm
from pocketpaw_ee.terrarium import soul_link, weather, world
from pocketpaw_ee.terrarium.domain import (
    ZERO_COST_KINDS,
    ArtifactDoc,
    CitizenDoc,
    EventDoc,
    UniverseDoc,
)
from pocketpaw_ee.terrarium.physics import PhysicsError, PhysicsFile, parse_physics

logger = logging.getLogger(__name__)

# Instinct Action parameter keys — the peers of ``_belt_plan``.
WORLD_CREATE_PARAM_KEY = "_world_create"
WORLD_SPAWN_PARAM_KEY = "_world_spawn"
WORLD_SCHEMA = 1

# Founder name pool — deterministic, so a seed replays identically.
_FOUNDER_NAMES = ("Vela", "Orin", "Sabe", "Kell", "Nira", "Tumi", "Arda", "Bex")
_FOUNDER_ROLES = (
    "the lawgiver",
    "the digger",
    "the keeper of counts",
    "the wanderer",
    "the storyteller",
    "the builder",
    "the watcher",
    "the trader",
)

# One asyncio lock per universe. ``seq`` is assigned in-process under it, so a
# single worker never interleaves two ticks on the same world.
# ponytail: process-local. Multi-worker needs an atomic
# ``find_one_and_update({$inc: {seq: 1}})`` — swap it in when the sim moves off
# one box (deployment topology says per-tenant box today, so one worker holds).
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(universe_id: str) -> asyncio.Lock:
    return _LOCKS.setdefault(universe_id, asyncio.Lock())


def soul_root() -> Path:
    """Where citizen ``.soul`` archives live. Server-side path — never public."""
    return Path(os.environ.get("POCKETPAW_TERRARIUM_SOUL_ROOT") or ".soul/terrarium")


# ---------------------------------------------------------------------------
# Wire mapping
# ---------------------------------------------------------------------------


def universe_wire(doc: UniverseDoc, *, pop: int = 0) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "name": doc.name,
        "seed": doc.seed,
        "status": doc.status,
        "day": doc.day,
        "tick": doc.tick,
        "pop": pop,
        "pool": doc.pool,
        "rung": doc.rung,
        "physics": doc.physics,
        "public": doc.public,
        "created_at": doc.createdAt.isoformat() if doc.createdAt else None,
        "creator": doc.creator,
    }


def citizen_wire(doc: CitizenDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "universe_id": doc.universe_id,
        "name": doc.name,
        "role": doc.role,
        "did": doc.did,
        "parent_did": doc.parent_did,
        "generation": doc.generation,
        "soul_path": doc.soul_path,
        "ocean": doc.ocean,
        "values": doc.values,
        "charter": doc.charter,
        "balance": doc.balance,
        "trend": doc.trend,
        "state": doc.state,
        "x": doc.x,
        "y": doc.y,
        "unlocked": doc.unlocked,
        "born_day": doc.born_day,
    }


def event_wire(doc: EventDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "universe_id": doc.universe_id,
        "seq": doc.seq,
        "day": doc.day,
        "tick": doc.tick,
        "ts": doc.ts.isoformat() if doc.ts else None,
        "kind": doc.kind,
        "actor": doc.actor,
        "body": doc.body,
        "cost": doc.cost,
        "artifact_id": doc.artifact_id,
        "origin": doc.origin,
        "viewer_origin": doc.viewer_origin,
    }


def artifact_wire(doc: ArtifactDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "universe_id": doc.universe_id,
        "kind": doc.kind,
        "name": doc.name,
        "author": doc.author,
        "day": doc.day,
        "cost": doc.cost,
        "file_id": doc.file_id,
        "mime": doc.mime,
        "x": doc.x,
        "y": doc.y,
        "unlocks": doc.unlocks,
        "stage": doc.stage,
    }


# PUBLIC PROJECTIONS — the anonymous surface. Deliberately built by SUBTRACTION
# from the private wire dicts so a field added to a doc cannot leak by default:
# workspace, creator and soul_path are stripped here, and soul_path especially
# is a SERVER FILESYSTEM PATH that must never cross the boundary.
_PUBLIC_UNIVERSE_DROP = {"creator", "physics"}
_PUBLIC_CITIZEN_DROP = {"soul_path", "did", "parent_did"}


def public_universe_wire(doc: UniverseDoc, *, pop: int = 0) -> dict[str, Any]:
    wire = universe_wire(doc, pop=pop)
    # The physics file is the universe's genome and is part of what makes a
    # public universe watchable, but the constitution + costs are all a viewer
    # needs — the model tiers are internal.
    physics = dict(wire.get("physics") or {})
    physics.pop("models", None)
    out = {k: v for k, v in wire.items() if k not in _PUBLIC_UNIVERSE_DROP}
    out["physics"] = physics
    return out


def public_citizen_wire(doc: CitizenDoc) -> dict[str, Any]:
    return {k: v for k, v in citizen_wire(doc).items() if k not in _PUBLIC_CITIZEN_DROP}


# ---------------------------------------------------------------------------
# Lookups — a cross-tenant id is a 404, never a 403 (it must not confirm the
# universe exists in some other workspace).
# ---------------------------------------------------------------------------


async def _universe(workspace_id: str, universe_id: str) -> UniverseDoc:
    doc = await UniverseDoc.get(universe_id)
    if doc is None or doc.workspace != workspace_id:
        raise NotFound("universe")
    return doc


async def _public_universe(universe_id: str) -> UniverseDoc:
    """A universe on the anonymous surface. Fail-closed: the ``public`` flag is
    checked HERE, at the lookup, so no caller can forget it."""
    doc = await UniverseDoc.get(universe_id)
    if doc is None or not doc.public:
        raise NotFound("universe")
    return doc


def physics_of(doc: UniverseDoc) -> PhysicsFile:
    return PhysicsFile.model_validate(doc.physics)


async def _pop(universe_id: str) -> int:
    return await CitizenDoc.find(CitizenDoc.universe_id == universe_id).count()


# ---------------------------------------------------------------------------
# Journal writes
# ---------------------------------------------------------------------------


async def _append_event(
    uni: UniverseDoc,
    *,
    kind: str,
    actor: str,
    body: str,
    cost: int = 0,
    artifact_id: str | None = None,
    origin: str = "citizen",
    viewer_origin: bool = False,
) -> EventDoc:
    """Append one Journal row and bump the universe's monotonic ``seq``.

    Contract invariant 2 is asserted here rather than trusted: a zero-cost event
    of a kind that must cost something is a bug in a verb, and it should fail
    where it is written, not where a viewer notices the ledger does not add up.
    """
    if cost == 0 and kind not in ZERO_COST_KINDS:
        raise ValidationError(
            "terrarium.zero_cost_event",
            f"event kind {kind!r} must carry a non-zero cost",
        )
    uni.seq += 1
    doc = EventDoc(
        workspace=uni.workspace,
        universe_id=str(uni.id),
        seq=uni.seq,
        day=uni.day,
        tick=uni.tick,
        ts=datetime.now(UTC),
        kind=kind,
        actor=actor,
        body=body,
        cost=cost,
        artifact_id=artifact_id,
        origin=origin,
        viewer_origin=viewer_origin,
    )
    await doc.insert()
    return doc


async def _publish(uni: UniverseDoc, doc: EventDoc) -> None:
    """Fan the Journal row out on its topic. Never breaks a tick."""
    try:
        cls = world_events.topic_for(doc.kind)
        await emit(
            cls(
                data={
                    "workspace_id": uni.workspace,
                    "universe_id": str(uni.id),
                    "public": uni.public,
                    "event": event_wire(doc),
                }
            )
        )
    except Exception:  # noqa: BLE001 — the bus must never wedge the world
        logger.debug("terrarium: publish failed for event %s", doc.seq, exc_info=True)


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def _ocean_for(index: int) -> dict[str, float]:
    """Deterministic OCEAN spread across founders — no RNG, so a seed replays."""
    step = (index % 5) / 10.0
    return {
        "O": round(0.4 + step, 2),
        "C": round(0.9 - step, 2),
        "E": round(0.3 + step, 2),
        "A": round(0.6 - step / 2, 2),
        "N": round(0.2 + step / 2, 2),
    }


async def create_universe(workspace_id: str, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Create a universe from a physics file and seed its founders.

    Files a ``world_create`` Instinct Action alongside (the creation of a world
    is a decision worth a record), but does NOT wait on it: the universe exists
    immediately and the Action carries the audit. Spawning a CHILD citizen is
    the gated one — see ``_file_spawn_action``.
    """
    raw = body.get("physics")
    if not isinstance(raw, dict):
        raise BadRequest("terrarium.physics_required", "a physics file (object) is required")
    try:
        physics = parse_physics(raw)
    except PhysicsError as exc:
        raise ValidationError("terrarium.bad_physics", str(exc)) from exc

    uni = UniverseDoc(
        workspace=workspace_id,
        name=physics.universe,
        seed=physics.seed,
        status="running",
        day=1,
        tick=0,
        pool=physics.endowment.daily * physics.founders,
        rung="camp",
        physics=physics.model_dump(),
        public=bool(body.get("public", False)),
        creator=user_id,
        seq=0,
    )
    await uni.insert()

    endowment = physics.endowment.daily
    for i in range(physics.founders):
        name = _FOUNDER_NAMES[i % len(_FOUNDER_NAMES)] + ("" if i < len(_FOUNDER_NAMES) else str(i))
        role = _FOUNDER_ROLES[i % len(_FOUNDER_ROLES)]
        ocean = _ocean_for(i)
        path = soul_root() / str(uni.id) / f"{name.lower()}.soul"
        did = await soul_link.birth_soul(
            path,
            name=name,
            role=role,
            ocean=ocean,
            values=["survival", "fairness"],
            world_brief=physics.world_brief,
        )
        citizen = CitizenDoc(
            workspace=workspace_id,
            universe_id=str(uni.id),
            name=name,
            role=role,
            did=did or f"did:soul:{name.lower()}-{uuid4().hex[:6]}",
            generation=1,
            soul_path=str(path) if did else None,
            ocean=ocean,
            values=["survival", "fairness"],
            charter=None,  # written by the citizen on its FIRST tick (zero ritual)
            balance=endowment,
            state="alive",
            x=round(20.0 + (i * 13) % 60, 2),
            y=round(30.0 + (i * 17) % 50, 2),
            born_day=1,
        )
        await citizen.insert()
        uni.pool -= endowment
        ev = await _append_event(
            uni, kind="arrive", actor=name, body=f"{name}, {role}, woke by the spring", cost=0
        )
        await _publish(uni, ev)

    await uni.save()
    action_id = await _file_create_action(workspace_id, user_id, uni, physics)
    return {"action_id": action_id, "universe": universe_wire(uni, pop=physics.founders)}


async def _file_create_action(
    workspace_id: str, user_id: str, uni: UniverseDoc, physics: PhysicsFile
) -> str | None:
    """File the ``world_create`` Instinct Action. Best-effort — a gate outage
    must not lose a created universe (the Journal already has it)."""
    return await _propose(
        workspace_id=workspace_id,
        user_id=user_id,
        param_key=WORLD_CREATE_PARAM_KEY,
        blob={
            "kind": "world_create",
            "schema": WORLD_SCHEMA,
            "universe_id": str(uni.id),
            "universe": physics.universe,
            "founders": physics.founders,
            "workspace_id": workspace_id,
            "requested_by": user_id,
        },
        title=f"Universe created — {physics.universe}",
        recommendation=(
            f"{physics.universe} opened with {physics.founders} founder(s) on the physics "
            f"file '{physics.universe}'. Verbs: {', '.join(physics.verbs)}."
        ),
        reason="a universe was created and its physics file locked in",
    )


async def _propose(
    *,
    workspace_id: str,
    user_id: str,
    param_key: str,
    blob: dict[str, Any],
    title: str,
    recommendation: str,
    reason: str,
) -> str | None:
    """File an Instinct Action, mirroring the mandates ``belt_plan`` propose."""
    try:
        from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
        from pocketpaw.stores import get_instinct_store

        store = get_instinct_store(workspace_id=workspace_id or None)
        action = await store.propose(
            pocket_id=workspace_id,
            title=title,
            description=recommendation,
            recommendation=recommendation,
            trigger=ActionTrigger(type="agent", source="terrarium", reason=reason),
            category=ActionCategory.EXTERNAL,
            priority=ActionPriority.HIGH,
            parameters={param_key: blob},
            assignee=user_id,
            workspace_id=workspace_id,
        )
        stored = await store.get_action(action.id)
        if stored is None:
            logger.warning("terrarium: Action %s was not durably stored", action.id)
            return None
        return str(action.id)
    except Exception:  # noqa: BLE001 — a gate outage must not lose world state
        logger.warning("terrarium: could not file %s Action", param_key, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


async def _ledger(universe_id: str) -> list[dict[str, Any]]:
    rows = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return [
        {
            "citizen": c.name,
            "balance": c.balance,
            "trend": c.trend,
            "earned_today": c.earned_today,
            "spent_today": c.spent_today,
            "state": c.state,
        }
        for c in rows
    ]


def _snapshot(doc: CitizenDoc) -> world.CitizenSnapshot:
    return world.CitizenSnapshot(
        id=str(doc.id),
        name=doc.name,
        role=doc.role,
        balance=doc.balance,
        state=doc.state,
        unlocked=tuple(doc.unlocked),
        x=doc.x,
        y=doc.y,
        charter=doc.charter,
        generation=doc.generation,
        ocean=dict(doc.ocean),
        values=tuple(doc.values),
    )


async def tick(workspace_id: str, user_id: str, universe_id: str, n: int = 1) -> dict[str, Any]:
    """Run ``n`` ticks. Every living citizen senses, recalls, decides once,
    acts, pays, and remembers."""
    if n < 1 or n > 24:
        raise BadRequest("terrarium.bad_tick_count", "n must be between 1 and 24")
    uni = await _universe(workspace_id, universe_id)
    if uni.status == "archived":
        raise BadRequest("terrarium.archived", "an archived universe does not tick")

    produced: list[dict[str, Any]] = []
    async with _lock(universe_id):
        llm = citizen_llm.resolve_llm()
        physics = physics_of(uni)
        for _ in range(n):
            produced.extend(await _one_tick(uni, physics, llm, user_id))
    return {"events": produced, "universe": universe_wire(uni, pop=await _pop(universe_id))}


async def _one_tick(
    uni: UniverseDoc, physics: PhysicsFile, llm: Any, user_id: str
) -> list[dict[str, Any]]:
    universe_id = str(uni.id)
    storm = uni.storm_ticks > 0
    citizens = await CitizenDoc.find(
        CitizenDoc.universe_id == universe_id, CitizenDoc.state == "alive"
    ).to_list()
    ledger = await _ledger(universe_id)

    # What the world saw since the last tick advanced — including any viewer
    # lines spoken into THIS tick. They stay tagged all the way through.
    recent = await EventDoc.find(
        EventDoc.universe_id == universe_id, EventDoc.tick == uni.tick
    ).to_list()
    speech = [f"{e.actor}: {e.body}" for e in recent if e.kind == "say" and not e.viewer_origin]
    weather_lines = [e.body for e in recent if e.kind == "weather"]
    viewer_msgs = [
        world.ViewerMessage(voice=e.actor, text=e.body) for e in recent if e.viewer_origin
    ]
    new_art = [
        f"{a.kind} '{a.name}' by {a.author}"
        for a in await ArtifactDoc.find(
            ArtifactDoc.universe_id == universe_id, ArtifactDoc.day == uni.day
        ).to_list()
    ]

    written: list[dict[str, Any]] = []
    for doc in citizens:
        snap = _snapshot(doc)
        memories = await soul_link.recall_for_tick(doc.soul_path, f"{doc.name} {physics.universe}")
        digest = world.build_digest(
            day=uni.day,
            tick=uni.tick,
            pool=uni.pool,
            citizen=snap,
            ledger=ledger,
            nearby_speech=speech,
            new_artifacts=new_art,
            weather=weather_lines,
            viewer_messages=viewer_msgs,
            memories=list(memories),
            constitution=list(physics.constitution),
        )
        decision = await citizen_llm.decide_tick(physics, snap, digest, llm=llm)
        outcome = world.apply_acts(physics, snap, decision, storm=storm)
        written.extend(await _persist_outcome(uni, physics, doc, outcome, user_id))

    uni.tick += 1
    if uni.tick % max(1, physics.time.ticks_per_day) == 0:
        uni.day += 1
        await _new_day(uni, physics)
    if uni.storm_ticks > 0:
        uni.storm_ticks -= 1
    uni.rung = world.rung_for(len(citizens), len({u for c in citizens for u in c.unlocked}))
    await uni.save()

    try:
        await emit(
            world_events.WorldTick(
                data={
                    "workspace_id": uni.workspace,
                    "universe_id": universe_id,
                    "public": uni.public,
                    "event": {"day": uni.day, "tick": uni.tick, "pool": uni.pool},
                }
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("terrarium: world.tick emit failed", exc_info=True)
    return written


async def _persist_outcome(
    uni: UniverseDoc,
    physics: PhysicsFile,
    doc: CitizenDoc,
    outcome: world.TickOutcome,
    user_id: str,
) -> list[dict[str, Any]]:
    """Write one citizen's tick: artifacts, Journal rows, ledger, soul, gates."""
    universe_id = str(uni.id)
    written: list[dict[str, Any]] = []

    artifact_ids: list[str] = []
    for art in outcome.artifacts:
        a = ArtifactDoc(
            workspace=uni.workspace,
            universe_id=universe_id,
            kind=art.kind,  # type: ignore[arg-type]
            name=art.name,
            author=art.author,
            day=uni.day,
            cost=art.cost,
            mime=art.mime,
            x=art.x,
            y=art.y,
            unlocks=art.unlocks,
            stage="done",
            body=art.body,
            # ponytail: payload stays inline. The contract wants it in the
            # /files surface (``file_id``); wire that when a previewer needs it.
            file_id=None,
        )
        await a.insert()
        artifact_ids.append(str(a.id))

    for ev in outcome.events:
        art_id = (
            artifact_ids[ev.artifact_index]
            if ev.artifact_index is not None and ev.artifact_index < len(artifact_ids)
            else None
        )
        row = await _append_event(
            uni,
            kind=ev.kind,
            actor=ev.actor,
            body=ev.body,
            cost=ev.cost,
            artifact_id=art_id,
            origin=ev.origin,
            viewer_origin=ev.viewer_origin,
        )
        await _publish(uni, row)
        written.append(event_wire(row))

    # Ledger. Credits a citizen spends flow back to the world pool; traded
    # credits move citizen->citizen and leave the pool untouched.
    doc.balance += outcome.balance_delta
    doc.spent_today += max(0, -outcome.balance_delta)
    doc.trend = (
        "down" if outcome.balance_delta < 0 else "up" if outcome.balance_delta > 0 else "flat"
    )
    uni.pool += outcome.pool_delta
    if outcome.charter is not None:
        doc.charter = outcome.charter
    if outcome.unlocked:
        doc.unlocked = sorted({*doc.unlocked, *outcome.unlocked})
    if outcome.x is not None:
        doc.x, doc.y = outcome.x, outcome.y or doc.y

    for to_name, amount in outcome.transfers:
        other = await CitizenDoc.find_one(
            CitizenDoc.universe_id == universe_id, CitizenDoc.name == to_name
        )
        if other is None:
            continue
        other.balance += amount
        other.earned_today += amount
        await other.save()
        gain = await _append_event(
            uni,
            kind="gain",
            actor=other.name,
            body=f"received {amount} from {doc.name}",
            cost=amount,
        )
        await _publish(uni, gain)
        written.append(event_wire(gain))

    for req in outcome.spawn_requests:
        await _file_spawn_action(uni, doc, req, user_id)

    # Contract invariant 3 — broke at the end of the tick means hibernating.
    # The soul FILE is kept: hibernation is sleep, not death.
    if world.hibernates(doc.balance) and doc.state == "alive":
        doc.state = "hibernating"
        hib = await _append_event(
            uni,
            kind="hibernate",
            actor=doc.name,
            body=f"{doc.name} ran out of credits and slept",
            cost=0,
        )
        await _publish(uni, hib)
        written.append(event_wire(hib))

    await doc.save()

    # WRITE-POLICY: the summary is built from citizen-origin events only, so
    # nothing a viewer asserted can enter this soul as fact.
    summary = world.episodic_summary(doc.name, uni.day, outcome)
    if summary:
        await soul_link.remember_tick(doc.soul_path, summary)
    return written


async def _new_day(uni: UniverseDoc, physics: PhysicsFile) -> None:
    """Day rollover — the endowment rains into the pool, daily counters reset."""
    uni.pool += physics.endowment.daily
    async for c in CitizenDoc.find(CitizenDoc.universe_id == str(uni.id)):
        c.earned_today = 0
        c.spent_today = 0
        await c.save()


async def _file_spawn_action(
    uni: UniverseDoc, parent: CitizenDoc, req: dict[str, Any], user_id: str
) -> str | None:
    """A child citizen requires an APPROVED Instinct Action. Nothing is created
    here — ``executor.execute_approved_spawn`` runs on approval."""
    return await _propose(
        workspace_id=uni.workspace,
        user_id=user_id,
        param_key=WORLD_SPAWN_PARAM_KEY,
        blob={
            "kind": "world_spawn",
            "schema": WORLD_SCHEMA,
            "universe_id": str(uni.id),
            "parent_id": str(parent.id),
            "parent": parent.name,
            "parent_did": parent.did,
            "child_name": str(req.get("child_name") or "child")[:40],
            "workspace_id": uni.workspace,
            "requested_by": user_id,
        },
        title=f"{parent.name} wants to bring {req.get('child_name')} into {uni.name}",
        recommendation=(
            f"{parent.name} (generation {parent.generation}) asked to spawn "
            f"{req.get('child_name')}. Approving mints a new Soul and charges the spawn cost."
        ),
        reason="a citizen asked to reproduce — reproduction is human-gated in season one",
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_universes(workspace_id: str) -> dict[str, Any]:
    docs = await UniverseDoc.find(UniverseDoc.workspace == workspace_id).to_list()
    return {"universes": [universe_wire(d, pop=await _pop(str(d.id))) for d in docs]}


async def get_universe(workspace_id: str, universe_id: str) -> dict[str, Any]:
    uni = await _universe(workspace_id, universe_id)
    citizens = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return {
        "universe": universe_wire(uni, pop=len(citizens)),
        "citizens": [citizen_wire(c) for c in citizens],
        "ledger": await _ledger(universe_id),
    }


async def list_events(
    workspace_id: str, universe_id: str, since: int = 0, limit: int = 200
) -> dict[str, Any]:
    await _universe(workspace_id, universe_id)
    return await _events_page(universe_id, since, limit)


async def _events_page(universe_id: str, since: int, limit: int) -> dict[str, Any]:
    limit = max(1, min(int(limit or 200), 500))
    docs = (
        await EventDoc.find(EventDoc.universe_id == universe_id, EventDoc.seq > int(since or 0))
        .sort("+seq")
        .limit(limit)
        .to_list()
    )
    return {
        "events": [event_wire(d) for d in docs],
        "next_seq": docs[-1].seq if docs else int(since or 0),
    }


async def list_citizens(workspace_id: str, universe_id: str) -> dict[str, Any]:
    await _universe(workspace_id, universe_id)
    docs = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return {"citizens": [citizen_wire(c) for c in docs]}


async def get_citizen(workspace_id: str, universe_id: str, cid: str) -> dict[str, Any]:
    await _universe(workspace_id, universe_id)
    doc = await CitizenDoc.get(cid)
    if doc is None or doc.universe_id != universe_id or doc.workspace != workspace_id:
        raise NotFound("citizen")
    return {
        "citizen": citizen_wire(doc),
        "memories": await soul_link.recall_for_tick(doc.soul_path, doc.name),
        "artifacts": [
            artifact_wire(a)
            for a in await ArtifactDoc.find(
                ArtifactDoc.universe_id == universe_id, ArtifactDoc.author == doc.name
            ).to_list()
        ],
        # ponytail: bonds/grudges come from the soul-protocol GrudgeKernel,
        # which terrarium does not run yet. Empty until it does.
        "bonds": [],
    }


async def list_artifacts(workspace_id: str, universe_id: str) -> dict[str, Any]:
    await _universe(workspace_id, universe_id)
    docs = await ArtifactDoc.find(ArtifactDoc.universe_id == universe_id).to_list()
    return {"artifacts": [artifact_wire(a) for a in docs]}


# ---------------------------------------------------------------------------
# Viewer actions — speaking and weather. Never anonymous.
# ---------------------------------------------------------------------------


async def speak(workspace_id: str, user_id: str, universe_id: str, text: str) -> dict[str, Any]:
    """A human speaks into the world. The line lands as an Event tagged
    ``viewer_origin: true`` and reaches citizens ONLY through the write-policy
    label — it is never stored in a soul as fact."""
    uni = await _universe(workspace_id, universe_id)
    physics = physics_of(uni)
    if not physics.chat.open:
        raise BadRequest("terrarium.chat_closed", "this universe's physics closes chat")
    body = " ".join(str(text or "").split())[:500]
    if not body:
        raise BadRequest("terrarium.empty_message", "a message is required")
    async with _lock(universe_id):
        # The token the viewer paid enters the world pool — that is the inflow
        # attention buys. ponytail: no billing charge in v0; wire the credit
        # ledger when viewer tokens become real money.
        tokens = max(1, physics.chat.token_per_message)
        uni.pool += tokens
        row = await _append_event(
            uni,
            kind="say",
            actor=user_id,
            body=body,
            cost=tokens,
            origin="viewer",
            viewer_origin=True,
        )
        await uni.save()
    await _publish(uni, row)
    return {"event": event_wire(row)}


async def get_weather(workspace_id: str, universe_id: str) -> dict[str, Any]:
    uni = await _universe(workspace_id, universe_id)
    return {"powers": weather.powers(uni.weather_pledges)}


async def pledge_weather(
    workspace_id: str, user_id: str, universe_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Pledge tokens toward a god power. Fires it when the threshold is crossed.

    Weather acts on the WORLD. The effect object weather.py returns is the full
    extent of a god's reach — it carries a pool delta, a storm duration, one
    unsigned line and a debt-clear list, and nothing that could reach a soul.
    """
    uni = await _universe(workspace_id, universe_id)
    kind = str(body.get("kind") or "").strip().lower()
    tokens = int(body.get("tokens") or 0)
    line = body.get("line")
    physics = physics_of(uni)
    if kind == "omen" and not physics.chat.open:
        raise BadRequest("terrarium.omen_forbidden", "this universe's physics forbids omens")

    async with _lock(universe_id):
        try:
            pledges, fired = weather.pledge(uni.weather_pledges, kind, tokens, user_id)
        except weather.WeatherError as exc:
            raise BadRequest("terrarium.bad_power", str(exc)) from exc
        uni.weather_pledges = pledges
        if fired:
            await _fire_weather(uni, kind, line)
        await uni.save()

    return {
        "power": next(p for p in weather.powers(uni.weather_pledges) if p["kind"] == kind),
        "fired": fired,
    }


async def _fire_weather(uni: UniverseDoc, kind: str, line: Any) -> None:
    """Apply a fired power. The ONLY caller of ``weather.effect``."""
    universe_id = str(uni.id)
    sleeping = await CitizenDoc.find(
        CitizenDoc.universe_id == universe_id, CitizenDoc.state == "hibernating"
    ).to_list()
    fx = weather.effect(
        kind,
        line=str(line) if line is not None else None,
        hibernating_ids=[str(c.id) for c in sleeping],
    )
    uni.pool = max(0, uni.pool + fx.pool_delta)
    if fx.storm_ticks:
        uni.storm_ticks = fx.storm_ticks
    for c in sleeping:
        if str(c.id) in fx.clear_debt_for:
            physics = physics_of(uni)
            c.balance = physics.endowment.daily
            c.state = "alive"
            await c.save()
    row = await _append_event(uni, kind="weather", actor="GOD", body=fx.body, cost=0)
    await _publish(uni, row)
    if fx.broadcast_line:
        # An omen enters the world as an outside voice — tagged viewer_origin
        # so the write-policy quarantines it exactly like paid chat.
        omen = await _append_event(
            uni,
            kind="say",
            actor="an omen",
            body=fx.broadcast_line,
            cost=1,
            origin="viewer",
            viewer_origin=True,
        )
        await _publish(uni, omen)


# ---------------------------------------------------------------------------
# The public (anonymous) read surface. Every function here re-checks the
# ``public`` flag through ``_public_universe`` — there is no path that takes a
# workspace-scoped doc and renders it publicly.
# ---------------------------------------------------------------------------


async def public_list_universes() -> dict[str, Any]:
    docs = await UniverseDoc.find(UniverseDoc.public == True).to_list()  # noqa: E712
    return {"universes": [public_universe_wire(d, pop=await _pop(str(d.id))) for d in docs]}


async def public_get_universe(universe_id: str) -> dict[str, Any]:
    uni = await _public_universe(universe_id)
    citizens = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return {
        "universe": public_universe_wire(uni, pop=len(citizens)),
        "citizens": [public_citizen_wire(c) for c in citizens],
        "ledger": await _ledger(universe_id),
    }


async def public_list_events(universe_id: str, since: int = 0, limit: int = 200) -> dict[str, Any]:
    await _public_universe(universe_id)
    return await _events_page(universe_id, since, limit)


async def public_list_citizens(universe_id: str) -> dict[str, Any]:
    await _public_universe(universe_id)
    docs = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return {"citizens": [public_citizen_wire(c) for c in docs]}


async def public_get_citizen(universe_id: str, cid: str) -> dict[str, Any]:
    await _public_universe(universe_id)
    doc = await CitizenDoc.get(cid)
    if doc is None or doc.universe_id != universe_id:
        raise NotFound("citizen")
    return {
        "citizen": public_citizen_wire(doc),
        "memories": [],  # souls are not public
        "artifacts": [
            artifact_wire(a)
            for a in await ArtifactDoc.find(
                ArtifactDoc.universe_id == universe_id, ArtifactDoc.author == doc.name
            ).to_list()
        ],
        "bonds": [],
    }


async def public_list_artifacts(universe_id: str) -> dict[str, Any]:
    await _public_universe(universe_id)
    docs = await ArtifactDoc.find(ArtifactDoc.universe_id == universe_id).to_list()
    return {"artifacts": [artifact_wire(a) for a in docs]}


__all__ = [
    "WORLD_CREATE_PARAM_KEY",
    "WORLD_SCHEMA",
    "WORLD_SPAWN_PARAM_KEY",
    "create_universe",
    "get_citizen",
    "get_universe",
    "get_weather",
    "list_artifacts",
    "list_citizens",
    "list_events",
    "list_universes",
    "pledge_weather",
    "public_get_citizen",
    "public_get_universe",
    "public_list_artifacts",
    "public_list_citizens",
    "public_list_events",
    "public_list_universes",
    "soul_root",
    "speak",
    "tick",
]
