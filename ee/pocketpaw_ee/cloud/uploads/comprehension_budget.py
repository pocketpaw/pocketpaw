# ee/pocketpaw_ee/cloud/uploads/comprehension_budget.py — a hard daily ceiling on
# file comprehension.
#
# Created 2026-08-28 (FC-3 "File comprehension").
#
# Comprehension is triggered by an UPLOAD, not by a request — nobody asks for
# it, and each one costs real money on the PLATFORM's account (a user's own
# BYOK key pays for their turns and deliberately not for this; see the header
# of ``comprehension.py``). A bulk import of ten thousand files would therefore
# be ten thousand unrequested model calls charged to us, and the person doing
# it would experience it as "the upload worked". That is the hole this closes.
#
# Deliberately coarse: one counter per workspace per UTC day, checked and
# incremented in ONE atomic update. It is a cost ceiling, not billing — it does
# not need to be exact under concurrency, it needs to be impossible to blow
# past by an order of magnitude.
#
# It fails CLOSED, which is the opposite of how the rest of the comprehension
# path behaves, and the asymmetry is the point. Everything downstream of this
# gate fails OPEN, because the cost of skipping a summary is a file with no
# summary. THIS gate fails closed, because the cost of being wrong here is an
# unbounded bill. A degraded database must not become an open tab.
#
# Structure copied from ``cloud/other_hand/illustration_budget.py``, including
# the increment-then-compare ordering and the rollback of an over-cap claim.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from pymongo import ReturnDocument

from pocketpaw_ee.cloud.models.file_comprehension_usage import FileComprehensionUsage

logger = logging.getLogger(__name__)

_ENV_CAP = "POCKETPAW_FILE_COMPREHENSION_DAILY"
_DEFAULT_CAP = 500


def daily_cap() -> int:
    """Comprehensions per workspace per UTC day. 0 disables the feature.

    A non-integer value is a misconfiguration, not an instruction: we warn and
    use the default rather than reading ``"five hundred"`` as zero and silently
    switching comprehension off for the whole deployment.
    """
    raw = (os.environ.get(_ENV_CAP) or "").strip()
    if not raw:
        return _DEFAULT_CAP
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s is not an integer (%r) — using the default", _ENV_CAP, raw)
        return _DEFAULT_CAP


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def try_spend(workspace_id: str | None) -> tuple[bool, int, int]:
    """Claim one comprehension against today's budget for ``workspace_id``.

    Returns ``(allowed, spent, cap)``. ``spent`` INCLUDES this one when
    allowed, so a caller can log "3/500" honestly.

    Increments FIRST and compares after. The increment is the atomic part; a
    check-then-increment would let two concurrent uploads both read 499, both
    conclude they are under the cap, and both spend. An over-cap claim is
    rolled back so a refused upload does not hold a slot a later one could
    have used.

    ``workspace_id`` is passed in rather than resolved from context: the
    FileReady listener already has it, and a budget that silently charges "no
    tenant" is a budget with a hole in it.
    """
    cap = daily_cap()
    if cap <= 0:
        return False, 0, 0

    if not workspace_id:
        # No tenant means no counter to charge, and an uncharged comprehension
        # is exactly what this file exists to prevent.
        logger.warning("file comprehension refused — no workspace on the event")
        return False, 0, cap

    day = _today()
    key = f"{workspace_id}:{day}"
    try:
        coll = FileComprehensionUsage.get_motor_collection()
        doc = await coll.find_one_and_update(
            {"key": key},
            {
                "$inc": {"used": 1},
                "$setOnInsert": {
                    "workspace": workspace_id,
                    "day": day,
                    "createdAt": datetime.now(UTC),
                },
                "$set": {"updatedAt": datetime.now(UTC)},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        # A successful update with no returned document should not happen, but
        # if it does we must not read it as "0 spent" — that is a permanently
        # open gate. Treat it as over-cap.
        spent = int((doc or {}).get("used", cap + 1))
    except Exception:
        # Fail closed — see the module note.
        logger.warning(
            "file comprehension budget unavailable for workspace=%s; refusing",
            workspace_id,
            exc_info=True,
        )
        return False, 0, cap

    if spent > cap:
        try:
            await coll.update_one({"key": key}, {"$inc": {"used": -1}})
        except Exception:
            logger.debug("could not roll back an over-cap comprehension claim", exc_info=True)
        return False, cap, cap
    return True, spent, cap


__all__ = ["daily_cap", "try_spend"]
