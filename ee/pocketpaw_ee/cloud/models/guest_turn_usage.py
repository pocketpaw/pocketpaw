# ee/pocketpaw_ee/cloud/models/guest_turn_usage.py — the guest daily-turn counter.
#
# Created 2026-09-01 (feat/byok-guest-backend, BYOK-first onboarding).
#
# One row per GUEST USER per UTC day, keyed by a compound natural key
# (``<user>:<YYYY-MM-DD>``), so "claim one turn for this guest today" is a
# single atomic upsert — the same increment-then-compare shape as
# ``FileComprehensionUsage``, and for the same reason: two turns landing in
# the same second must not both read 39 and both decide they are under the cap.
#
# Keyed on the USER, not the workspace: a guest is one anonymous user with one
# auto-provisioned workspace today, but the limit is a property of the guest
# account (it survives an upgrade decision, and a future multi-workspace guest
# should not multiply the cap).
#
# Lives in ``cloud.models`` per the one-importer convention — see the header of
# ``file_comprehension_usage.py`` for why that convention is load-bearing.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class GuestTurnUsage(TimestampedDocument):
    """How many agent turns one guest user has spent today.

    ``used`` rather than ``count`` — ``count`` shadows a query method on the
    parent Document (see ``FileComprehensionUsage`` for the full note).
    """

    #: ``<user>:<YYYY-MM-DD>``. UNIQUE — the upsert keys on it.
    key: Indexed(str, unique=True)  # type: ignore[valid-type]
    user: str
    day: str
    used: int = 0

    class Settings:
        name = "guest_turn_usage"
