# ee/pocketpaw_ee/cloud/models/other_hand_usage.py — the Otherhand illustration
# spend counter.
#
# One Beanie document, one row per workspace per UTC day, keyed by a compound
# natural key (``<workspace>:<YYYY-MM-DD>``) so the only operation it ever
# serves — "claim one illustration for this workspace today" — is a single
# atomic upsert rather than a read followed by a write.
#
# WHY IT LIVES HERE and not beside its service: the cloud rules keep Beanie
# documents in ``cloud.models`` and let exactly one service import each one.
# The first version of this put the class in the service file, which imported
# ``models.base`` while ``models.__init__`` imported the class back — a cycle
# that surfaced as an unrelated-looking import error in a test. The convention
# was load-bearing.
#
# Created 2026-08-28 (feat/other-hand-illustrate-tool). Registered in
# ``cloud.models.__init__`` so ``init_beanie`` wires the collection.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class IllustrationUsage(TimestampedDocument):
    """How many generated illustrations one workspace has spent today.

    ``used`` rather than ``count``: ``count`` shadows an attribute on the parent
    Document and Pydantic warns about it, which is a real hazard here — the
    field this budget depends on silently colliding with a query method is
    exactly the sort of bug that reads as "the cap does not work sometimes".
    """

    #: ``<workspace>:<YYYY-MM-DD>``. UNIQUE — the upsert keys on it.
    key: Indexed(str, unique=True)  # type: ignore[valid-type]
    workspace: str
    day: str
    used: int = 0

    class Settings:
        name = "other_hand_illustration_usage"
