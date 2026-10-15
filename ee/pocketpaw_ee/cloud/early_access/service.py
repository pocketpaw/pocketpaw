# ee/pocketpaw_ee/cloud/early_access/service.py: the early-access waitlist write.
#
# One ``waitlist`` row per lowercased email; the unique index makes a repeat join
# a no-op, so the caller cannot tell a new email from a known one (no account or
# list enumeration through this public route).

from __future__ import annotations

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud.early_access.dto import WaitlistJoinRequest
from pocketpaw_ee.cloud.models.waitlist import WaitlistEntry


async def join_waitlist(body: WaitlistJoinRequest) -> None:
    """Add ``body.email`` to the waitlist, once."""
    body = WaitlistJoinRequest.model_validate(body)
    entry = WaitlistEntry(email=str(body.email).strip().lower(), source=body.source)
    try:
        await entry.insert()
    except DuplicateKeyError:
        pass
    # no-event: a public, unauthenticated capture; the launch dashboard reads the
    # collection directly and nothing else subscribes.


__all__ = ["join_waitlist"]
