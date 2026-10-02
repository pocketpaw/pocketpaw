# ee/pocketpaw_ee/terrarium/executor.py
#
# The Instinct approve-side hook for terrarium. Reproduction is HUMAN-GATED in
# season one: a citizen's ``spawn`` verb files a ``world_spawn`` Action and
# leaves a zero-cost ``gate`` Event — no child exists until a human approves.
# This module is what runs on that approval.
#
# ``execute_approved_spawn`` is deliberately callable on its own (it takes an
# Action-like object and reads its own blob), so it can be wired from the
# instinct router's approve path exactly like ``mandates.executor
# .execute_approved_plan``, and tested without one.
#
# Re-validation at approval time, not propose time: the parent may have gone
# broke or hibernated while the Action sat in the tray, so the spawn cost, the
# parent's state and the child's name (unique per universe — citizens resolve
# by name inside a tick) are checked HERE before anything is minted.

"""Approve-side executor for the gated ``world_spawn`` Action."""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from uuid import uuid4

from pocketpaw_ee.terrarium import service, soul_link
from pocketpaw_ee.terrarium.domain import CitizenDoc, UniverseDoc

logger = logging.getLogger(__name__)

WORLD_SPAWN_PARAM_KEY = service.WORLD_SPAWN_PARAM_KEY


# Default half-width of a child's per-trait drift. Matches soul-protocol's
# EvolutionConfig.mutation_rate default order of magnitude; small enough that a
# lineage wanders rather than lurches.
DRIFT_WIDTH = 0.08


def child_ocean(
    parent_ocean: dict[str, float],
    *,
    parent_did: str | None,
    child_name: str,
    width: float = DRIFT_WIDTH,
) -> dict[str, float]:
    """A child's OCEAN: the parent's, with each trait nudged INDEPENDENTLY.

    Each of the five traits gets its own signed delta in ``[-width, +width]``,
    clamped to 0..1. That independence is the whole point — a single shared
    delta is not drift but a translation: every child would be strictly more
    (or less) than its parent on all five axes at once, lineages would never
    diverge in shape, and a few generations of it saturates everyone at the
    ends of the scale. The first version of this added a flat +0.08 and did
    exactly that.

    DETERMINISTIC, seeded from the parent DID and the child's name, because the
    engine takes no RNG: a Journal replayed from the same seed has to produce
    the same world, and a birth is part of that world (see the explore verb in
    ``world.py`` for the same rule).
    """
    seed = f"{parent_did or ''}:{child_name}"
    out: dict[str, float] = {}
    for i, (trait, value) in enumerate(sorted(parent_ocean.items())):
        # One stable hash per (lineage, child, trait) -> a delta across the
        # whole [-width, +width] range, not a fixed step.
        h = int(hashlib.sha256(f"{seed}:{trait}:{i}".encode()).hexdigest()[:8], 16)
        delta = (h / 0xFFFFFFFF) * 2 * width - width
        out[trait] = round(min(1.0, max(0.0, float(value) + delta)), 4)
    return out


def world_spawn_blob(action: Any) -> dict[str, Any] | None:
    """The ``_world_spawn`` blob on an Action, or None. Peer of ``_belt_plan_blob``."""
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get(WORLD_SPAWN_PARAM_KEY)
    return blob if isinstance(blob, dict) else None


async def execute_approved_spawn(action: Any) -> dict[str, Any]:
    """Mint the child citizen an approved ``world_spawn`` Action asked for.

    Returns ``{"ok": bool, "reason"|"citizen_id": ...}``. Never raises: an
    approve response must not fail because a world moved on underneath it.
    """
    blob = world_spawn_blob(action)
    if blob is None:
        return {"ok": False, "reason": "not a world_spawn action"}

    try:
        uni = await UniverseDoc.get(blob["universe_id"])
        parent = await CitizenDoc.get(blob["parent_id"])
        if uni is None or parent is None:
            return {"ok": False, "reason": "universe or parent is gone"}
        if uni.workspace != blob.get("workspace_id"):
            return {"ok": False, "reason": "workspace mismatch"}

        physics = service.physics_of(uni)
        cost = physics.costs.spawn
        # Re-validated NOW, not at propose time — the parent may have gone broke
        # or fallen asleep while the Action waited in the tray.
        if parent.state != "alive":
            return {"ok": False, "reason": f"{parent.name} is {parent.state}"}
        if parent.balance < cost:
            return {"ok": False, "reason": f"{parent.name} cannot afford {cost}"}

        name = str(blob.get("child_name") or "child")[:40]
        # Names resolve citizens inside a tick (service invariant 12); the
        # filing side checked too, but a founder may have arrived since.
        if await service._name_taken(str(uni.id), name):
            return {"ok": False, "reason": f"{name} is already a citizen of {uni.name}"}
        ocean = child_ocean(parent.ocean or {}, parent_did=parent.did, child_name=name)
        path = service.soul_root() / str(uni.id) / f"{name.lower()}-{uuid4().hex[:6]}.soul"
        did = await soul_link.birth_soul(
            path,
            name=name,
            role="",
            ocean=ocean,
            values=list(parent.values),
            world_brief=physics.world_brief,
        )
        child = CitizenDoc(
            workspace=uni.workspace,
            universe_id=str(uni.id),
            name=name,
            role="",
            did=did or f"did:soul:{name.lower()}-{uuid4().hex[:6]}",
            parent_did=parent.did,
            generation=parent.generation + 1,
            soul_path=str(path) if did else None,
            ocean=ocean,
            values=list(parent.values),
            charter=None,  # the child rewrites it on its own first tick
            balance=cost // 2,
            state="alive",
            x=parent.x,
            y=parent.y,
            born_day=uni.day,
        )
        await child.insert()

        parent.balance -= cost
        parent.spent_today += cost
        await parent.save()

        row = await service._append_event(
            uni,
            kind="spawn",
            actor=parent.name,
            body=f"{parent.name} brought {name} into the world",
            cost=-cost,
        )
        uni.pool += cost - child.balance
        await uni.save()
        await service._publish(uni, row)
        return {"ok": True, "citizen_id": str(child.id), "event_seq": row.seq}
    except Exception as exc:  # noqa: BLE001 — never break an approve response
        logger.exception("terrarium: world_spawn execution failed")
        return {"ok": False, "reason": str(exc)}


__all__ = [
    "DRIFT_WIDTH",
    "WORLD_SPAWN_PARAM_KEY",
    "child_ocean",
    "execute_approved_spawn",
    "world_spawn_blob",
]
