# ee/pocketpaw_ee/cloud/auth/guest_budget.py — hard, fail-CLOSED caps for guest
# accounts (BYOK-first onboarding).
#
# Created 2026-09-01 (feat/byok-guest-backend).
# Updated 2026-10-01 (CN-3): the TURNS/DAY counter moved to the shared
# ``metering.service`` daily primitive (meter ``guest_turns``, subject the guest
# user); ``try_spend_turn`` / ``turns_used_today`` and the ``GuestTurnUsage``
# collection are gone. ``guest_gates`` claims and reads it directly. The
# limits (``limits_for`` and its env floors) stay here.
# Updated 2026-09-10 (feat/guest-cap-env-override) — ``limits_for`` now takes
# a deployment-wide FLOOR from POCKETPAW_GUEST_SESSIONS /
# POCKETPAW_GUEST_TURNS_PER_DAY, so a dev box can raise both caps out of the
# way without a migration and without a bypass path in the gate.
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
#     per UTC day in the shared ``metering`` daily counter, with the over-cap
#     rollback.
#
# Everything here fails CLOSED for guests: a broken database must not become
# an unmetered free tier. That is also why there is no "disable the limits"
# boolean: the two env knobs below can only RAISE a cap, never remove the
# counter, so a typo'd or mis-deployed value costs headroom rather than the
# whole gate. Dev "switches it off" by raising both to a number nobody reaches.
#
# Non-guest users are a NO-OP everywhere (the helpers
# answer "allowed" without touching the counter), and that no-op includes the
# lookup failing — an unreadable user row refuses the turn rather than
# guessing.

from __future__ import annotations

import logging
import os

from beanie import PydanticObjectId

from pocketpaw_ee.cloud.models.user import GuestLimits, User

logger = logging.getLogger(__name__)

DEFAULT_LIMITS = GuestLimits()  # sessions=2, turns_per_day=40


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


# Deployment-wide FLOORS under the per-user caps. Unset means "no opinion", so
# the stored numbers stand — the module's fail-closed default survives a
# missing env, a broken env, and a fresh container alike.
SESSIONS_ENV = "POCKETPAW_GUEST_SESSIONS"
TURNS_ENV = "POCKETPAW_GUEST_TURNS_PER_DAY"


def _floor(name: str, stored: int) -> int:
    """The larger of the stored cap and an env floor. Read per call, not at
    import, so a container's env and a test's monkeypatch both land.

    A non-integer or <= 0 value is IGNORED rather than applied. Zero is the one
    number that must never come from config: the turn counter treats cap <= 0
    as "refuse", so an operator who writes ``0`` meaning "unlimited" would turn
    every guest turn off — the opposite of what they typed.
    """
    raw = os.environ.get(name)
    if raw is None:
        return stored
    try:
        floor = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; ignoring", name, raw)
        return stored
    if floor <= 0:
        logger.warning("%s=%r must be > 0; ignoring", name, raw)
        return stored
    return max(stored, floor)


def limits_for(user: User) -> GuestLimits:
    stored = user.guest_limits or DEFAULT_LIMITS
    return GuestLimits(
        sessions=_floor(SESSIONS_ENV, stored.sessions),
        turns_per_day=_floor(TURNS_ENV, stored.turns_per_day),
    )


async def session_count(user_id: str) -> int:
    """How many session rows this guest owns (all workspaces, all surfaces).

    Counting goes through the sessions SERVICE (cloud rule 2: only an
    entity's own service touches its Beanie documents). Lazy import — the
    sessions service must stay importable without auth and vice versa.
    """
    from pocketpaw_ee.cloud.sessions import service as sessions_service

    return await sessions_service.count_owned(user_id)


__all__ = [
    "DEFAULT_LIMITS",
    "SESSIONS_ENV",
    "TURNS_ENV",
    "limits_for",
    "load_guest",
    "session_count",
]
