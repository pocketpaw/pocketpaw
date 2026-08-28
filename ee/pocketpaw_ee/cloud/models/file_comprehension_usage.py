# ee/pocketpaw_ee/cloud/models/file_comprehension_usage.py — the file-comprehension
# spend counter.
#
# Created 2026-08-28 (FC-3 "File comprehension"). Registered in
# ``cloud.models.__init__`` so ``init_beanie`` wires the collection.
#
# One row per workspace per UTC day, keyed by a compound natural key
# (``<workspace>:<YYYY-MM-DD>``) so the only operation it ever serves — "claim
# one comprehension for this workspace today" — is a single atomic upsert
# rather than a read followed by a write. Two uploads landing in the same
# second must not both read 499 and both decide they are under the cap.
#
# WHY IT LIVES HERE and not beside its service: the cloud rules keep Beanie
# documents in ``cloud.models`` and let exactly one module import each one.
# ``models.other_hand_usage`` records what happens otherwise — a document
# declared in its own service file imports ``models.base`` while
# ``models.__init__`` imports the class back, and the cycle surfaces as an
# unrelated-looking import error somewhere else entirely. The convention is
# load-bearing, not decorative.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class FileComprehensionUsage(TimestampedDocument):
    """How many uploads one workspace has had comprehended today.

    ``used`` rather than ``count``: ``count`` shadows a query method on the
    parent Document and Pydantic warns about the collision. That matters more
    than tidiness here — the field the whole cap depends on silently resolving
    to a method is exactly the shape of bug that reads as "the ceiling works
    most of the time".
    """

    #: ``<workspace>:<YYYY-MM-DD>``. UNIQUE — the upsert keys on it.
    key: Indexed(str, unique=True)  # type: ignore[valid-type]
    workspace: str
    day: str
    used: int = 0

    class Settings:
        name = "file_comprehension_usage"
