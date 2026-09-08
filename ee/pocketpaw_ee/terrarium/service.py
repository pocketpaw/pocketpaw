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
#   * fanning those calls out across the citizens    -> foresight.ForesightWorld
#     (this module keeps the clock, the arithmetic and the event writing)
#   * everything that touches Mongo, a .soul file, an Instinct Action or the
#     bus                                            -> here
#
# Invariants enforced at this seam:
#   1. ``seq`` is monotonic per universe (assigned under a per-universe lock).
#   2. ``cost: 0`` only for ``domain.ZERO_COST_KINDS`` (asserted on write).
#   3. balance <= 0 at end of tick -> state ``hibernating``, soul file KEPT.
#   4. viewer-origin text never becomes soul fact (the episodic summary is
#      built from citizen-origin events only — see world.episodic_summary).
#   5. the Journal is truth; citizens/ledger/artifacts are projections.
#   6. say / write / moment text, charters and artifacts pass ``moderation`` at
#      the write; a failing one lands as ``[withheld]`` (seq and cost intact).
#      Viewer lines are checked BEFORE the write and rejected with a 422;
#      founder-card text is checked the same way at creation.
#   7. ``paused`` never ticks and is a flat 404 in public; anonymous readers
#      trail the edge by ``TERRARIUM_PUBLIC_DELAY_EVENTS`` (default 20) rows.
#   8. a DORMANT world thinks in a half-price Message Batch when
#      ``TERRARIUM_BATCH_DORMANT`` is on (DEFAULT OFF) — ``dormant_batch_step``.

"""Terrarium service — persistence, souls, the gate and the bus."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pocketpaw.security.rate_limiter import RateLimiter  # type: ignore[import-untyped]
from pocketpaw_ee.cloud._core.errors import (
    BadRequest,
    Forbidden,
    NotFound,
    RateLimited,
    ValidationError,
)
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud.byok import service as byok_service
from pocketpaw_ee.foresight.persona import OceanDrift
from pocketpaw_ee.foresight.world import ForesightWorld
from pocketpaw_ee.terrarium import events as world_events
from pocketpaw_ee.terrarium import llm as citizen_llm
from pocketpaw_ee.terrarium import moderation, soul_link, weather, world
from pocketpaw_ee.terrarium import scheduler as clock
from pocketpaw_ee.terrarium.domain import (
    EVENT_KINDS,
    ZERO_COST_KINDS,
    ArtifactDoc,
    CitizenDoc,
    EventDoc,
    UniverseDoc,
)
from pocketpaw_ee.terrarium.persona import CitizenPersona
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


def cost_per_watched_hour(doc: UniverseDoc, pop: int) -> float:
    """USD an hour of watching this world costs, in cents, from ITS OWN clock.

    A world-day is ``time.world_day_seconds`` of wall clock and holds
    ``time.ticks_per_day`` ticks, and every living citizen spends one model call
    per tick — so an hour of watching is ``pop * ticks_per_day * 3600 /
    world_day_seconds`` calls. Zero until the world has actually ticked: an
    estimate off no measurement is a guess wearing a dollar sign.
    """
    per_call = float((doc.cost or {}).get("cost_per_call") or 0.0)
    time_block = (doc.physics or {}).get("time") or {}
    ticks_per_day = max(1, int(time_block.get("ticks_per_day") or 12))
    day_seconds = max(1, int(time_block.get("world_day_seconds") or 3600))
    calls_per_hour = max(0, pop) * ticks_per_day * 3600.0 / day_seconds
    return round(per_call * calls_per_hour, 2)


def caching_engaged(doc: UniverseDoc) -> bool:
    """Is the prompt cache actually doing anything for this world?

    True only when a prefix was MARKED and the provider then served a cache
    read. A prefix under the model's minimum cacheable length is not an error —
    it silently caches nothing — so a world that marks every prefix and never
    reads one back pays full input rate forever. This is that failure, on the
    wire, instead of buried in the bill.
    """
    cost = doc.cost or {}
    return bool(cost.get("cache_marked_calls")) and bool(cost.get("cache_read_tokens"))


def _accrue_meter(uni: UniverseDoc, meter: Any) -> None:
    """Fold this tick's metering into the universe's running total."""
    tick_cost = meter.drain()
    total = dict(uni.cost or {})
    for key in (
        "calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_marked_calls",
        "batch_calls",
    ):
        total[key] = int(total.get(key, 0)) + int(tick_cost.get(key, 0))
    total["cost_usd"] = round(float(total.get("cost_usd", 0.0)) + tick_cost["cost_usd"], 6)
    total["model"] = tick_cost["model"]
    total["cost_per_call"] = round(total["cost_usd"] / total["calls"], 8) if total["calls"] else 0.0
    uni.cost = total


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
        "cost_per_watched_hour": cost_per_watched_hour(doc, pop),
        "caching_engaged": caching_engaged(doc),
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
        "data": doc.data,
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
        "design_id": doc.design_id,
    }


# PUBLIC PROJECTIONS — the anonymous surface. Deliberately built by SUBTRACTION
# from the private wire dicts so a field added to a doc cannot leak by default:
# workspace, creator and soul_path are stripped here, and soul_path especially
# is a SERVER FILESYSTEM PATH that must never cross the boundary.
_PUBLIC_UNIVERSE_DROP = {"creator", "physics"}
_PUBLIC_CITIZEN_DROP = {"soul_path", "did", "parent_did"}


_DEFAULT_PUBLIC_DELAY = 20


def public_delay_events() -> int:
    """How many Journal rows an anonymous reader trails the live edge by.
    Read per request, so an operator can widen the gap without a restart."""
    try:
        return max(0, int(os.environ.get("TERRARIUM_PUBLIC_DELAY_EVENTS") or _DEFAULT_PUBLIC_DELAY))
    except ValueError:
        return _DEFAULT_PUBLIC_DELAY


def batch_dormant_enabled() -> bool:
    """Does a world nobody is watching think in a half-price batch? DEFAULT OFF.

    Read on every call, never at import, so an operator can flip it without a
    deploy — and fail-closed like ``router.public_enabled``: anything that is not
    an explicit truthy value leaves today's synchronous behaviour exactly as it
    was.
    """
    return (os.environ.get("TERRARIUM_BATCH_DORMANT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def public_universe_wire(doc: UniverseDoc, *, pop: int = 0) -> dict[str, Any]:
    wire = universe_wire(doc, pop=pop)
    wire["public_lag"] = min(public_delay_events(), doc.seq)
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


async def _get_universe_doc(universe_id: str) -> UniverseDoc | None:
    """Load by id, treating a MALFORMED id exactly like a missing one.

    ``Document.get`` raises ``bson.errors.InvalidId`` on a non-ObjectId string,
    which is not a CloudError — it would 500. On the ANONYMOUS surface a 500 on
    a garbage id is both a bad response and a fingerprint, so every lookup
    funnels through here.
    """
    try:
        return await UniverseDoc.get(universe_id)
    except Exception:  # noqa: BLE001 — a malformed id is a 404, not a 500
        return None


async def _universe(workspace_id: str, universe_id: str) -> UniverseDoc:
    doc = await _get_universe_doc(universe_id)
    if doc is None or doc.workspace != workspace_id:
        raise NotFound("universe")
    return doc


async def _public_universe(universe_id: str) -> UniverseDoc:
    """A universe on the anonymous surface. Fail-closed: the ``public`` flag is
    checked HERE, at the lookup, so no caller can forget it."""
    doc = await _get_universe_doc(universe_id)
    if doc is None or not doc.public or doc.status == "paused":
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
    data: dict[str, Any] | None = None,
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
    data = dict(data or {})
    # Invariant 6. Withheld, not dropped: the seq is spent and the cost lands.
    if kind in moderation.MODERATED_KINDS and not moderation.allowed(body):
        # ``data`` may echo the body (a moment's ``headline``); scrub it too.
        data = {k: (moderation.WITHHELD if v == body else v) for k, v in data.items()}
        body = moderation.WITHHELD
        data["withheld"] = True
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
        data=data,
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


def _reject_unmoderated_founder_cards(physics: PhysicsFile) -> None:
    """A named founder is creator-supplied text on the public citizen wire, so
    its name, role, charter and values pass the same inbound check a viewer line
    does — a founder must not be a moderation bypass. Checked BEFORE anything is
    written: the whole creation fails, exactly like a rejected viewer line."""
    for card in physics.founder_cards or []:
        for text in (card.name, card.role, card.charter, *card.values):
            if not moderation.allowed(text):
                raise ValidationError(
                    "terrarium.founder_card_rejected",
                    f"founder card {card.name!r} contains text that was not accepted",
                )


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
    _reject_unmoderated_founder_cards(physics)

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
    cards = physics.founder_cards
    for i in range(physics.founders):
        card = cards[i] if cards else None
        if card is None:
            name = _FOUNDER_NAMES[i % len(_FOUNDER_NAMES)] + (
                "" if i < len(_FOUNDER_NAMES) else str(i)
            )
            role = _FOUNDER_ROLES[i % len(_FOUNDER_ROLES)]
            values = ["survival", "fairness"]
            charter = ""
        else:
            name = card.name
            role = card.role
            values = list(card.values)
            charter = card.charter
        ocean = _ocean_for(i)
        path = soul_root() / str(uni.id) / f"{name.lower()}.soul"
        did = await soul_link.birth_soul(
            path,
            name=name,
            role=role,
            ocean=ocean,
            values=values,
            world_brief=physics.world_brief,
            charter=charter,
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
            values=values,
            # A card seeds the day-one charter; a generated founder writes its
            # own on tick 1 (the zero ritual gates on ``charter is None``).
            charter=charter or None,
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

    produced: list[dict[str, Any]] = []
    # The universe is LOADED INSIDE the lock, not before it. A tick can hold the
    # lock for minutes on the real transport; a concurrent /speak that loaded the
    # doc first would write back a stale ``seq`` and duplicate a sequence number,
    # which breaks ``?since=`` paging (contract invariant 1).
    async with _lock(universe_id):
        uni = await _universe(workspace_id, universe_id)
        if uni.status in ("archived", "paused"):
            raise BadRequest(f"terrarium.{uni.status}", f"a {uni.status} universe does not tick")
        physics = physics_of(uni)
        # Bring-your-own-key: the workspace's key, via the ONE decrypting reader
        # in cloud.byok (the same store the rest of the product uses). No second
        # copy of a key lives on the universe. Platform credentials otherwise.
        creds = await byok_service.resolve_turn_credentials(uni.workspace)
        # Metered, always: the meter is what makes ``cost_per_watched_hour`` a
        # measurement instead of a guess, and a universe that skipped it would
        # report zero rather than report nothing.
        llm = citizen_llm.MeteredLlm(
            citizen_llm.resolve_llm(api_key=creds.api_key, tier=physics.models.founders),
            citizen_llm.model_for_tier(physics.models.founders),
        )
        for _ in range(n):
            produced.extend(await _one_tick(uni, physics, llm, user_id))
        uni.last_tick_at = datetime.now(UTC)
        await uni.save()
    return {"events": produced, "universe": universe_wire(uni, pop=await _pop(universe_id))}


async def _lineage_drifts(universe_id: str, citizens: list[CitizenDoc]) -> dict[str, OceanDrift]:
    """Each descendant's OCEAN distance from its parent, keyed by citizen id.

    ``executor.child_ocean`` samples a child's ABSOLUTE traits once, at birth,
    and persists them — that is the replayable half. This reads the difference
    back at decide time and hands it to the prompt, so a lineage is spoken as
    well as stored. Units are drift widths, which is what ``OceanDrift`` renders.

    Parents are matched across every state: a hibernating parent is still the
    lineage. A founder, and a descendant whose parent has been purged, get no
    entry and therefore no prompt line.
    """
    wanted = {c.parent_did for c in citizens if c.parent_did}
    if not wanted:
        return {}
    # Imported here, not at module scope: executor imports this module.
    from pocketpaw_ee.terrarium.executor import DRIFT_WIDTH

    parents = {
        p.did: p.ocean
        for p in await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
        if p.did in wanted
    }
    drifts: dict[str, OceanDrift] = {}
    for c in citizens:
        parent = parents.get(c.parent_did or "")
        if not parent:
            continue
        deltas = {
            soul_link.OCEAN_FIELDS[letter]: round(
                (float(value) - float(parent[letter])) / DRIFT_WIDTH, 3
            )
            for letter, value in c.ocean.items()
            if letter in soul_link.OCEAN_FIELDS and letter in parent
        }
        if deltas:
            drifts[str(c.id)] = OceanDrift(**deltas)
    return drifts


# How many citizens may hold an in-flight model call at once. The physics
# ``models`` block carries the per-tier model names today and no concurrency
# key, so this reads one if a world ever declares it and otherwise caps at 8 —
# enough that a camp-sized universe finishes a tick in one round trip, low
# enough that a crowd does not open fifty sockets at a provider.
_DEFAULT_CONCURRENCY = 8


def _concurrency(physics: PhysicsFile) -> int:
    return int(getattr(physics.models, "concurrency", _DEFAULT_CONCURRENCY))


SensedRow = tuple[CitizenDoc, "world.CitizenSnapshot", "world.SenseDigest"]


async def _sense(uni: UniverseDoc, physics: PhysicsFile) -> list[SensedRow]:
    """Every living citizen with its snapshot and its sense digest.

    Sequential on purpose: building a digest reads the docs, the Journal and the
    soul file. Both paths start here — the watched tick fans these rows out
    through Foresight, the dormant one turns them into batch entries — so the
    two see byte-identical prompts.
    """
    universe_id = str(uni.id)
    citizens = await CitizenDoc.find(
        CitizenDoc.universe_id == universe_id, CitizenDoc.state == "alive"
    ).to_list()
    ledger = await _ledger(universe_id)

    # Two windows, on purpose. A citizen's own ``say`` is stamped with the tick
    # it was spoken in, so it is only audible on the NEXT one — read tick-1 as
    # well or nobody ever hears anybody. Viewer lines and weather are stamped
    # into the CURRENT tick (they land between ticks), so they stay filtered to
    # ``== uni.tick``; widening them would make a viewer heard twice.
    recent = await EventDoc.find(
        EventDoc.universe_id == universe_id, EventDoc.tick >= uni.tick - 1
    ).to_list()
    speech = [f"{e.actor}: {e.body}" for e in recent if e.kind == "say" and not e.viewer_origin]
    current = [e for e in recent if e.tick == uni.tick]
    weather_lines = [e.body for e in current if e.kind == "weather"]
    viewer_msgs = [
        world.ViewerMessage(voice=e.actor, text=e.body) for e in current if e.viewer_origin
    ]
    new_art = [
        f"{a.kind} '{a.name}' by {a.author}"
        for a in await ArtifactDoc.find(
            ArtifactDoc.universe_id == universe_id, ArtifactDoc.day == uni.day
        ).to_list()
    ]

    rows: list[SensedRow] = []
    for doc in citizens:
        snap = _snapshot(doc)
        memories = await soul_link.recall_for_tick(doc.soul_path, f"{doc.name} {physics.universe}")
        rows.append(
            (
                doc,
                snap,
                world.build_digest(
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
                ),
            )
        )
    return rows


async def _drift_lines(uni: UniverseDoc, rows: list[SensedRow]) -> dict[str, str]:
    """Each citizen's lineage line, keyed by citizen id. Empty for a founder."""
    drifts = await _lineage_drifts(str(uni.id), [doc for doc, _snap, _digest in rows])
    return {cid: drift.as_prompt_block() for cid, drift in drifts.items()}


async def _one_tick(
    uni: UniverseDoc, physics: PhysicsFile, llm: Any, user_id: str
) -> list[dict[str, Any]]:
    """The watched tick: every citizen decides now, and the world lands now."""
    rows = await _sense(uni, physics)

    # THE JUDGMENT CALLS FAN OUT; THE ARITHMETIC DOES NOT. Foresight gathers the
    # decides under its own semaphore and returns them in ``active_ids`` order,
    # so ``apply_acts`` and ``_persist_outcome`` still run one citizen at a time
    # in the order the docs came back — pool, ledger and ``seq`` keep exactly the
    # ordering the serial loop gave them.
    drifts = await _lineage_drifts(str(uni.id), [doc for doc, _snap, _digest in rows])
    fw = ForesightWorld(max_concurrent=_concurrency(physics))
    ids = [
        fw.add_agent(CitizenPersona(doc, snap, digest, physics, llm, drift=drifts.get(str(doc.id))))
        for doc, snap, digest in rows
    ]
    fanned = (await fw.tick(active_ids=ids)).last_tick_actions

    decisions: list[world.Decision] = []
    for (doc, snap, _digest), action in zip(rows, fanned):
        # ``decide_tick`` already degrades a bad transport to an empty Decision,
        # so ``ok: False`` only appears if the adapter itself blew up. Same
        # degrade either way: the citizen thinks, does nothing, and still pays.
        decision = world.Decision.model_validate(action) if action.get("ok") else world.Decision()
        decisions.append(decision)
    pairs = [(doc, snap) for doc, snap, _digest in rows]
    return await _land_tick(uni, physics, pairs, decisions, user_id, getattr(llm, "meter", None))


async def _land_tick(
    uni: UniverseDoc,
    physics: PhysicsFile,
    pairs: list[tuple[CitizenDoc, world.CitizenSnapshot]],
    decisions: list[world.Decision],
    user_id: str,
    meter: Any = None,
) -> list[dict[str, Any]]:
    """Everything after the judgment: apply, persist, cluster the moments, move
    the clock. The watched fan-out and the dormant batch BOTH land here, which
    is what makes the two paths write the same Journal for the same decisions.
    """
    universe_id = str(uni.id)
    storm = uni.storm_ticks > 0
    written: list[dict[str, Any]] = []
    placed: list[world.PlacedAct] = []
    for (doc, snap), decision in zip(pairs, decisions):
        outcome = world.apply_acts(physics, snap, decision, storm=storm)
        mine = await _persist_outcome(uni, physics, doc, outcome, user_id)
        written.extend(mine)
        # ``_persist_outcome`` writes ``outcome.events`` FIRST and in order, so
        # the head of what it returns lines up with them one for one (the gain
        # and hibernate rows it appends after are not acts). ``doc`` already
        # carries any move this tick made, so the position is where the citizen
        # ended up, not where it started.
        for ev, wire in zip(outcome.events, mine):
            if ev.kind in world.MOMENT_KIND_RANK:
                placed.append(
                    world.PlacedAct(
                        seq=int(wire["seq"]),
                        actor=ev.actor,
                        kind=ev.kind,
                        x=doc.x,
                        y=doc.y,
                        day=uni.day,
                        tick=uni.tick,
                        node=ev.node,
                    )
                )

    # A MOMENT is the story layer over the acts just written: two or more
    # citizens in one place on one day. It rides the same Journal, so ``seq``
    # paging, the realtime topic and the public events route carry it for free.
    for moment in world.cluster_moments(placed):
        row = await _append_event(
            uni,
            kind="moment",
            actor=moment.actors[0],
            body=moment.headline,
            cost=0,
            origin="system",
            data=asdict(moment),
        )
        await _publish(uni, row)
        written.append(event_wire(row))

    uni.tick += 1
    if uni.tick % max(1, physics.time.ticks_per_day) == 0:
        uni.day += 1
        await _new_day(uni, physics)
    if uni.storm_ticks > 0:
        uni.storm_ticks -= 1
    uni.rung = world.rung_for(len(pairs), len({u for doc, _snap in pairs for u in doc.unlocked}))
    if meter is not None:
        _accrue_meter(uni, meter)
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


# Artifact kinds that carry a readable payload. Structures and tools are things
# on the map, not documents.
_FILE_BEARING_KINDS = frozenset({"book", "law", "map", "design"})


async def _land_artifact_file(uni: UniverseDoc, user_id: str, art: Any) -> str | None:
    """Write a payload-bearing artifact into the /files surface; return its id.

    Returns None for structures, for empty bodies, and on ANY failure — the
    Journal is the truth and the file is how a human reads it, so a storage
    hiccup degrades to "inline only", never to a failed tick. The upload
    service is imported lazily so the terrarium package stays import-light.
    """
    if art.kind not in _FILE_BEARING_KINDS or not (art.body or "").strip():
        return None
    try:
        from pocketpaw_ee.cloud.uploads.service import write_text_file

        safe = (
            "".join(ch if ch.isalnum() or ch in "-_ " else "-" for ch in art.name).strip()
            or "artifact"
        )
        if art.kind == "design":
            # The canonical JSON itself: the frontend's renderer reads it as-is.
            filename, content, mime = f"{safe}.json", art.body, "application/json"
        else:
            filename = f"{safe}.md"
            content = f"# {art.name}\n\n_{art.kind}, by {art.author}, day {uni.day}_\n\n{art.body}"
            mime = "text/markdown"
        rec = await write_text_file(
            workspace_id=uni.workspace,
            owner_id=user_id,
            folder_path=f"/terrarium/{uni.name}",
            filename=filename,
            content=content,
            mime=mime,
        )
        return str(rec.id)
    except Exception:  # noqa: BLE001 — best-effort, the doc keeps the body inline
        logger.warning("terrarium: could not land artifact %r in /files", art.name, exc_info=True)
        return None


async def _owned_design(universe_id: str, author: str, design_id: str | None) -> str | None:
    """The ``design_id`` a structure may carry: only a ``design`` artifact in
    this universe authored by this citizen. A foreign, missing or malformed id
    is ignored and the structure is built plain — the reference is a courtesy
    to the renderer, never a claim the model gets to make about another
    citizen's work."""
    if not design_id:
        return None
    try:
        art = await ArtifactDoc.get(design_id)
    except Exception:  # noqa: BLE001 — a non-ObjectId string is "no such design"
        return None
    if art is None or art.universe_id != universe_id or art.kind != "design":
        return None
    return design_id if art.author == author else None


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
        # Invariant 6: the name is on the public artifact wire and the body is
        # a file a viewer opens. Withheld together, never trimmed.
        if not (moderation.clean(art.name) and moderation.clean(art.body)):
            art = replace(art, name=moderation.WITHHELD, body=moderation.WITHHELD)
        # A book, a law or a map is a FILE in the workspace's /files surface, so
        # a viewer opens it in the same inline viewer as any other file. The
        # body stays on the doc too (the contract keeps it); the file is how
        # humans read it. Best-effort: a files failure must never wedge a tick.
        file_id = await _land_artifact_file(uni, user_id, art)
        design_id = await _owned_design(universe_id, doc.name, art.design_id)
        a = ArtifactDoc(
            workspace=uni.workspace,
            universe_id=universe_id,
            kind=art.kind,  # type: ignore[arg-type]
            name=art.name,
            author=art.author,
            day=uni.day,
            cost=art.cost,
            mime=art.mime if file_id is None else (art.mime or "text/markdown"),
            x=art.x,
            y=art.y,
            unlocks=art.unlocks,
            stage="done",
            body=art.body,
            file_id=file_id,
            design_id=design_id,
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
            data={"design_id": art_id} if ev.kind == "design" and art_id else None,
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
        # Invariant 6: the charter is on the citizen wire, public included.
        doc.charter = outcome.charter if moderation.clean(outcome.charter) else moderation.WITHHELD
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
# The sleeping tick — one batch, half price, two sweeps
# ---------------------------------------------------------------------------

# How long an open batch is waited on before it is written off. The provider
# expires a batch at 24 hours, so past that there is nothing left to collect.
_BATCH_MAX_AGE_SECONDS = 24 * 3600


async def dormant_batch_step(workspace_id: str, user_id: str, universe_id: str) -> str:
    """One sweep's move on a world nobody is watching.

    Two phases, so a restart between them costs nothing: the first builds every
    citizen's prompt exactly as the watched tick does, files ONE Message Batch
    and writes its id on the universe; a later sweep polls that id and, once the
    batch has ended, lands the results through the same ``apply_acts`` and
    ``_persist_outcome`` path — so the Journal, the ledger, moderation and the
    moments step behave identically, at half the model bill.

    Returns what it did: ``submitted``, ``waiting``, ``applied``, or ``sync``
    when the caller should run the ordinary synchronous tick instead.
    """
    async with _lock(universe_id):
        uni = await _universe(workspace_id, universe_id)
        if uni.status == "paused":
            # A paused world does not tick, and its open batch is left exactly
            # where it is — resuming picks it back up.
            return "waiting"
        physics = physics_of(uni)
        creds = await byok_service.resolve_turn_credentials(uni.workspace)
        batch = citizen_llm.resolve_batch_llm(api_key=creds.api_key, tier=physics.models.founders)
        if batch is None:
            return "sync"
        # An open batch is always resolved, even after the flag was turned off or
        # somebody started watching — otherwise the id would strand the world.
        if uni.batch_id:
            return await _apply_batch(uni, physics, batch, user_id)
        if not batch_dormant_enabled():
            return "sync"
        return await _submit_batch(uni, physics, batch)


async def _submit_batch(uni: UniverseDoc, physics: PhysicsFile, batch: Any) -> str:
    """Phase one: file the batch and stop. Nothing is written to the Journal —
    the world has not thought yet, it has only asked."""
    rows = await _sense(uni, physics)
    if not rows:
        return "sync"  # an empty batch is a 400; let the clock tick it normally
    lines = await _drift_lines(uni, rows)
    entries: list[citizen_llm.BatchEntry] = []
    for doc, snap, digest in rows:
        prefix, suffix = citizen_llm.build_prompt_parts(
            physics, snap, digest, drift_line=lines.get(str(doc.id), "")
        )
        entries.append(
            citizen_llm.BatchEntry(
                custom_id=str(doc.id),
                prefix=prefix,
                suffix=suffix,
                physics=physics,
                citizen=snap,
                digest=digest,
            )
        )
    uni.batch_id = await batch.submit(entries)
    uni.batch_tick = uni.tick
    uni.batch_at = datetime.now(UTC)
    await uni.save()
    return "submitted"


async def _apply_batch(uni: UniverseDoc, physics: PhysicsFile, batch: Any, user_id: str) -> str:
    """Phase two: collect an ended batch and land the tick it belongs to."""
    batch_id = uni.batch_id or ""
    if uni.tick != uni.batch_tick or _batch_too_old(uni):
        return await _abandon_batch(uni, batch_id)
    if not await batch.ended(batch_id):
        return "waiting"
    landed = await batch.results(batch_id)
    citizens = await CitizenDoc.find(
        CitizenDoc.universe_id == str(uni.id), CitizenDoc.state == "alive"
    ).to_list()
    pairs = [(doc, _snapshot(doc)) for doc in citizens]
    # Results come back in ANY order the provider likes, so every one is matched
    # on the custom_id it was filed under. Position here means nothing.
    ordered = [landed.get(str(doc.id)) for doc, _snap in pairs]
    meter = citizen_llm.CostMeter(citizen_llm.model_for_tier(physics.models.founders))
    decisions = [_batch_decision(res, meter) for res in ordered]
    uni.batch_id = None
    uni.batch_at = None
    await _land_tick(uni, physics, pairs, decisions, user_id, meter)
    uni.last_tick_at = datetime.now(UTC)
    await uni.save()
    return "applied"


def _batch_decision(res: citizen_llm.BatchResult | None, meter: Any) -> world.Decision:
    """One entry's judgment, and what it cost.

    ONLY a ``succeeded`` entry is parsed and metered. An errored, canceled,
    expired or missing one degrades to the empty Decision a failed synchronous
    decide gives — the citizen thinks, does nothing, and still pays the in-world
    think — and the world is NOT billed for a think the provider never ran. The
    status decides that, never whether text happens to be attached.
    """
    if res is None or res.status != "succeeded":
        return world.Decision(thought="(the thought did not form)", acts=[])
    meter.record("", res.text, res.usage, batch=True)
    try:
        return citizen_llm.parse_decision(res.text)
    except Exception:  # noqa: BLE001 — one bad citizen must not stop the world
        logger.warning("terrarium: a batched decision did not parse", exc_info=True)
        return world.Decision(thought="(the thought did not form)", acts=[])


def _batch_too_old(uni: UniverseDoc) -> bool:
    at = clock._aware(uni.batch_at)
    if at is None:
        return True
    return (datetime.now(UTC) - at).total_seconds() > _BATCH_MAX_AGE_SECONDS


async def _abandon_batch(uni: UniverseDoc, batch_id: str) -> str:
    """A batch that outlived its window, or the tick it was built for, is written
    off — said out loud in the Journal, because a world that quietly skipped a
    day would read as a bug to whoever comes back to watch it."""
    row = await _append_event(
        uni,
        kind="batch",
        actor="the clock",
        body=(
            f"the batched thoughts for day {uni.day} never came back — "
            "this world is thinking live again"
        ),
        cost=0,
        origin="system",
        data={"batch_id": batch_id},
    )
    await _publish(uni, row)
    uni.batch_id = None
    uni.batch_at = None
    await uni.save()
    return "sync"


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
    await _touch_viewed(await _universe(workspace_id, universe_id))
    return await _events_page(universe_id, since, limit)


async def _touch_viewed(uni: UniverseDoc) -> None:
    """Somebody is watching — the clock (scheduler.py) reads this to decide
    between the running and the dormant cadence."""
    uni.last_viewed_at = datetime.now(UTC)
    await uni.save()


async def scheduler_sweep(now: datetime) -> list[dict[str, Any]]:
    """The clock's per-interval read: flip running/dormant on every live
    universe from its own physics, and return the rows whose next tick is due.
    Called by ``scheduler.run_scheduler_tick``; one universe failing is logged
    and skipped so the sweep always sees the rest."""
    due_rows: list[dict[str, Any]] = []
    docs = await UniverseDoc.find({"status": {"$in": ["running", "dormant"]}}).to_list()
    for uni in docs:
        try:
            due, status = clock.due_state(
                (uni.physics or {}).get("time") or {},
                now=now,
                last_tick_at=uni.last_tick_at,
                last_viewed_at=uni.last_viewed_at or uni.createdAt,
            )
            if status != uni.status:
                uni.status = status
                await uni.save()
            if due:
                due_rows.append(
                    {
                        "workspace_id": uni.workspace,
                        "universe_id": str(uni.id),
                        "user_id": uni.creator or "system:scheduler",
                        # A world nobody is watching thinks in a batch when the
                        # flag is on; one already holding a batch is drained
                        # whatever the flag says, so no id is ever stranded.
                        "batch": bool(uni.batch_id)
                        or (status == "dormant" and batch_dormant_enabled()),
                    }
                )
        except Exception:  # noqa: BLE001 — one bad universe never sinks the sweep
            logger.warning("terrarium clock: sweep failed for universe %s", uni.id, exc_info=True)
    return due_rows


async def _events_page(
    universe_id: str,
    since: int,
    limit: int,
    kind: str | None = None,
    max_seq: int | None = None,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 200), 500))
    query = [EventDoc.universe_id == universe_id, EventDoc.seq > int(since or 0)]
    if max_seq is not None:
        query.append(EventDoc.seq <= max_seq)
    if kind:
        query.append(EventDoc.kind == kind)
    docs = await EventDoc.find(*query).sort("+seq").limit(limit).to_list()
    return {
        "events": [event_wire(d) for d in docs],
        "next_seq": docs[-1].seq if docs else int(since or 0),
    }


async def list_citizens(workspace_id: str, universe_id: str) -> dict[str, Any]:
    await _universe(workspace_id, universe_id)
    docs = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return {"citizens": [citizen_wire(c) for c in docs]}


async def _get_citizen_doc(cid: str) -> CitizenDoc | None:
    """Same malformed-id-is-a-404 rule as ``_get_universe_doc``."""
    try:
        return await CitizenDoc.get(cid)
    except Exception:  # noqa: BLE001
        return None


async def get_citizen(workspace_id: str, universe_id: str, cid: str) -> dict[str, Any]:
    await _universe(workspace_id, universe_id)
    doc = await _get_citizen_doc(cid)
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


async def list_gates(workspace_id: str, universe_id: str) -> dict[str, Any]:
    """Pending human decisions for this universe — today, only ``world_spawn``.

    READ ONLY, on purpose. Approving still goes through
    ``POST /instinct/actions/{id}/approve``, because the Instinct gate is the
    single chain authority and a second approve path here would be a second
    place for the chain to close. The observatory needs this route because a
    citizen asking for a child is otherwise invisible from the world it
    happened in — the Action exists, but only the tray knows about it.
    """
    await _universe(workspace_id, universe_id)
    try:
        from pocketpaw.instinct.models import ActionStatus
        from pocketpaw.stores import get_instinct_store

        store = get_instinct_store(workspace_id=workspace_id or None)
        actions = await store.list_actions()
    except Exception:  # noqa: BLE001 — a gate-read failure must not break the page
        logger.warning("terrarium: could not read the gate list", exc_info=True)
        return {"gates": []}

    gates: list[dict[str, Any]] = []
    for action in actions:
        if getattr(action, "status", None) != ActionStatus.PENDING:
            continue
        blob = (getattr(action, "parameters", None) or {}).get(WORLD_SPAWN_PARAM_KEY)
        if not isinstance(blob, dict) or blob.get("universe_id") != universe_id:
            continue
        gates.append(
            {
                "action_id": str(getattr(action, "id", "")),
                "kind": "world_spawn",
                "title": getattr(action, "title", ""),
                "parent": blob.get("parent"),
                "child_name": blob.get("child_name"),
                # No DIDs and no requester id: this is a room a viewer reads.
            }
        )
    return {"gates": gates}


# ---------------------------------------------------------------------------
# Viewer actions — speaking and weather. Never anonymous.
# ---------------------------------------------------------------------------


# Ten viewer lines a minute per person, in memory. The same limiter the rest of
# cloud uses; per-process, like every other bucket in ``_core.rate_limit``.
_speak_limiter = RateLimiter(rate=10.0 / 60.0, capacity=10)


def _check_viewer_line(text: str) -> str:
    """The inbound gate. Returns the normalised line or raises; writes nothing."""
    body = " ".join(str(text or "").split())[: moderation.MAX_LEN]
    if not body:
        raise BadRequest("terrarium.empty_message", "a message is required")
    if not moderation.allowed(body):
        raise ValidationError("terrarium.line_rejected", "That line was not accepted")
    return body


async def speak(workspace_id: str, user_id: str, universe_id: str, text: str) -> dict[str, Any]:
    """A human speaks into the world. The line lands as an Event tagged
    ``viewer_origin: true`` and reaches citizens ONLY through the write-policy
    label — it is never stored in a soul as fact."""
    body = _check_viewer_line(text)
    if not _speak_limiter.allow(f"terrarium-speak:{user_id}"):
        raise RateLimited("terrarium.speak_rate_limited", "Too many lines — wait a minute.")
    # Loaded inside the lock — see the note in ``tick``.
    async with _lock(universe_id):
        uni = await _universe(workspace_id, universe_id)
        physics = physics_of(uni)
        if not physics.chat.open:
            raise BadRequest("terrarium.chat_closed", "this universe's physics closes chat")
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


async def set_paused(
    workspace_id: str, user_id: str, universe_id: str, *, paused: bool, is_admin: bool
) -> dict[str, Any]:
    """The per-world kill switch. Owner (creator) or workspace admin only.

    Paused: the sweep's status query never selects it, the manual tick
    refuses, and ``_public_universe`` 404s it exactly like a private world.
    Resume sets ``running``; the next sweep re-derives dormant if nobody is
    watching.
    """
    async with _lock(universe_id):
        uni = await _universe(workspace_id, universe_id)
        if not is_admin and uni.creator != user_id:
            raise Forbidden("terrarium.not_owner", "only the owner or an admin can do that")
        if uni.status == "archived":
            raise BadRequest("terrarium.archived", "an archived universe cannot be paused")
        uni.status = "paused" if paused else "running"
        await uni.save()
    return {"universe": universe_wire(uni, pop=await _pop(universe_id))}


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
    kind = str(body.get("kind") or "").strip().lower()
    tokens = int(body.get("tokens") or 0)
    line = body.get("line")
    if line:
        line = _check_viewer_line(line)

    # Loaded inside the lock — see the note in ``tick``.
    async with _lock(universe_id):
        uni = await _universe(workspace_id, universe_id)
        physics = physics_of(uni)
        if kind == "omen" and not physics.chat.open:
            raise BadRequest("terrarium.omen_forbidden", "this universe's physics forbids omens")
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
        # so the write-policy quarantines it exactly like paid chat. Its token
        # goes to the pool like any other spoken line, so the ledger adds up.
        uni.pool += 1
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
    docs = await UniverseDoc.find(
        UniverseDoc.public == True,  # noqa: E712
        UniverseDoc.status != "paused",
    ).to_list()
    return {"universes": [public_universe_wire(d, pop=await _pop(str(d.id))) for d in docs]}


async def public_get_universe(universe_id: str) -> dict[str, Any]:
    uni = await _public_universe(universe_id)
    citizens = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return {
        "universe": public_universe_wire(uni, pop=len(citizens)),
        "citizens": [public_citizen_wire(c) for c in citizens],
        "ledger": await _ledger(universe_id),
    }


async def public_list_events(
    universe_id: str, since: int = 0, limit: int = 200, kind: str | None = None
) -> dict[str, Any]:
    """The anonymous Journal page, optionally one kind only (``moment``).

    The kind is validated AFTER the double gate, never before: a bad kind on a
    universe that is private or on a server with the flag off must still be the
    same flat 404 every other read is, or the error itself would confirm the
    universe exists.
    """
    uni = await _public_universe(universe_id)
    await _touch_viewed(uni)
    if kind is not None and kind not in EVENT_KINDS:
        raise BadRequest("terrarium.bad_event_kind", f"no such event kind: {kind!r}")
    # The delay buffer: a stranger reads ``buffer`` rows behind the live edge,
    # which is the window an owner has to pause the world before a bad row is
    # ever served anonymously.
    return await _events_page(
        universe_id, since, limit, kind, max_seq=uni.seq - public_delay_events()
    )


async def public_list_citizens(universe_id: str) -> dict[str, Any]:
    await _public_universe(universe_id)
    docs = await CitizenDoc.find(CitizenDoc.universe_id == universe_id).to_list()
    return {"citizens": [public_citizen_wire(c) for c in docs]}


async def public_get_citizen(universe_id: str, cid: str) -> dict[str, Any]:
    await _public_universe(universe_id)
    doc = await _get_citizen_doc(cid)
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
    "public_delay_events",
    "public_get_citizen",
    "public_get_universe",
    "public_list_artifacts",
    "public_list_citizens",
    "public_list_events",
    "public_list_universes",
    "set_paused",
    "soul_root",
    "speak",
    "tick",
]
