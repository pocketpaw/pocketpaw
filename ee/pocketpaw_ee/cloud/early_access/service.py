# ee/pocketpaw_ee/cloud/early_access/service.py: the early-access writes.
#
# Waitlist: one ``waitlist`` row per lowercased email; the unique index makes a
# repeat join a no-op, so the caller cannot tell a new email from a known one (no
# account or list enumeration through this public route).
#
# Notify-me: ``User.notify_plans_at`` is set once by a conditional update, so a
# repeat or racing click keeps the first time and returns it.

from __future__ import annotations

from datetime import UTC, datetime

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.early_access.dto import WaitlistJoinRequest
from pocketpaw_ee.cloud.models.user import User
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


async def notify_plans(user_id: str) -> datetime:
    """Record that ``user_id`` wants to hear when plans open. Idempotent:
    returns the first stored time on every call."""
    oid = PydanticObjectId(user_id)
    await User.find_one({"_id": oid, "notify_plans_at": None}).update(
        {"$set": {"notify_plans_at": datetime.now(UTC)}}
    )
    doc = await User.get(oid)
    if doc is None:
        raise NotFound("user", user_id)
    # no-event: a stored interest flag; the launch stats read it directly and
    # nothing else subscribes.
    return doc.notify_plans_at


__all__ = ["join_waitlist", "notify_plans"]
