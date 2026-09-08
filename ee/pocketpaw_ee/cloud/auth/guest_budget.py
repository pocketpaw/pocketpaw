# ee/pocketpaw_ee/cloud/auth/guest_budget.py — hard, fail-CLOSED caps for guest
# accounts (BYOK-first onboarding).
#
# Created 2026-09-01 (feat/byok-guest-backend).
#
# Guests are anonymous: clearing localStorage loses the guest, it must NOT
# reset the meter — so both caps live server-side, on the guest's own rows.
# Two caps, two shapes:
#
#   * SESSIONS (default 2) — a plain count of the guest's session rows,
#     checked at explicit session-create. Not atomic (a race could mint a 3rd
#     session), and the auto-create path (``ensure_for_agent_scope``) is NOT
#     gated — deliberate porosity: the TURNS counter below is the money
#     backstop, and a surplus empty session costs nothing.
#   * TURNS/DAY (default 40) — one atomic increment-then-compare row per guest
#     per UTC day, cloned from ``uploads/comprehension_budget.py`` including
#     the over-cap rollback and the beanie-2.x ``get_pymongo_collection``
#     accessor (NEVER ``get_motor_collection`` — that exact bug shipped twice;
#     see the long note in comprehension_budget).
#
# Everything here fails CLOSED for guests: a broken database must not become
# an unmetered free tier. Non-guest users are a NO-OP everywhere (the helpers
# answer "allowed" without touching the counter), and that no-op includes the
# lookup failing — an unreadable user row refuses the turn rather than
# guessing.

from __future__ import annotations

import logging
from datetime import UTC, datetime

from beanie import PydanticObjectId
from pymongo import ReturnDocument

from pocketpaw_ee.cloud.models.guest_turn_usage import GuestTurnUsage
from pocketpaw_ee.cloud.models.user import GuestLimits, User

logger = logging.getLogger(__name__)

DEFAULT_LIMITS = GuestLimits()  # sessions=2, turns_per_day=40


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def load_guest(user_id: str | None) -> User | None:
    """The guest user doc, or None when the user is absent or not a guest.

    Raises on an unreadable id/row ONLY via the caller's fail-closed wrapper —
    here a malformed id is "not a guest" (guests are always our own minted
    ids), but a DB error propagates so gates can refuse instead of waving
    through.
    """
    if not user_id:
        return None
    try:
        oid = PydanticObjectId(user_id)
    except Exception:  # noqa: BLE001 — not an ObjectId, so not a minted guest
        return None
    doc = await User.get(oid)
    if doc is None or not doc.is_guest:
        return None
    return doc


def limits_for(user: User) -> GuestLimits:
    return user.guest_limits or DEFAULT_LIMITS


async def session_count(user_id: str) -> int:
    """How many session rows this guest owns (all workspaces, all surfaces).

    Counting goes through the sessions SERVICE (cloud rule 2: only an
    entity's own service touches its Beanie documents). Lazy import — the
    sessions service must stay importable without auth and vice versa.
    """
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    return await sessions_service.count_owned(user_id)


async def turns_used_today(user_id: str) -> int:
    """Read-only view of today's spend. 0 when no row yet."""
    doc = await GuestTurnUsage.find_one(GuestTurnUsage.key == f"{user_id}:{_today()}")
    return int(doc.used) if doc else 0


async def try_spend_turn(user_id: str, cap: int) -> tuple[bool, int, int]:
    """Claim one turn against today's budget. Returns ``(allowed, spent, cap)``.

    Increments FIRST and compares after — the atomic part is the increment; a
    check-then-increment lets two concurrent turns both read 39 and both spend.
    An over-cap claim is rolled back. Any storage failure refuses (fail
    CLOSED — see the module header).
    """
    if cap <= 0:
        return False, 0, max(0, cap)

    day = _today()
    key = f"{user_id}:{day}"
    try:
        # ``get_pymongo_collection``, NOT ``get_motor_collection`` — beanie 2.x.
        # The wrong name lands as AttributeError in the except below and reads
        # as "guest turns are off", the workspace's signature failure mode.
        coll = GuestTurnUsage.get_pymongo_collection()
        doc = await coll.find_one_and_update(
            {"key": key},
            {
                "$inc": {"used": 1},
                "$setOnInsert": {
                    "user": user_id,
                    "day": day,
                    "createdAt": datetime.now(UTC),
                },
                "$set": {"updatedAt": datetime.now(UTC)},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        # A missing return doc must not read as "0 spent" — that is a
        # permanently open gate. Treat as over-cap.
        spent = int((doc or {}).get("used", cap + 1))
    except Exception:
        logger.warning(
            "guest turn budget unavailable for user=%s; refusing", user_id, exc_info=True
        )
        return False, 0, cap

    if spent > cap:
        try:
            await coll.update_one({"key": key}, {"$inc": {"used": -1}})
        except Exception:
            logger.debug("could not roll back an over-cap guest turn claim", exc_info=True)
        return False, cap, cap
    return True, spent, cap


__all__ = [
    "DEFAULT_LIMITS",
    "limits_for",
    "load_guest",
    "session_count",
    "try_spend_turn",
    "turns_used_today",
]
