# ee/pocketpaw_ee/cloud/uploads/transcription_budget.py — a hard daily ceiling
# on media transcription.
#
# Created 2026-08-29 (T2 "Audio/video transcription at ingest").
#
# Transcription is triggered by an UPLOAD, not by a request — nobody asks for
# it, and each one costs real money on the PLATFORM's account (a user's own
# BYOK key pays for their turns and deliberately not for this). Dropping a
# folder of recordings into the Files box would otherwise be an unbounded,
# unrequested bill that the person doing it experiences as "the upload worked".
# That is the hole this closes.
#
# Deliberately coarse: one counter per workspace per UTC day, checked and
# incremented in ONE atomic update. It is a cost ceiling, not billing — it does
# not need to be exact under concurrency, it needs to be impossible to blow
# past by an order of magnitude.
#
# It counts FILES, not minutes, and that is only half a ceiling on its own —
# which is why ``transcription.py`` refuses anything over a duration/size limit
# BEFORE it claims here. Files x per-file-ceiling is the actual bound. Claiming
# after the length check also means an over-long file never burns a slot a
# transcribable one could have used.
#
# It fails CLOSED, which is the opposite of how the rest of the transcription
# path behaves, and the asymmetry is the point. Everything downstream fails
# OPEN, because the cost of skipping a transcript is a media file with no
# transcript. THIS gate fails closed, because the cost of being wrong here is
# an unbounded bill. A degraded database must not become an open tab.
#
# Structure cloned from ``uploads/comprehension_budget.py``, including the
# increment-then-compare ordering and the rollback of an over-cap claim.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from pymongo import ReturnDocument

from pocketpaw_ee.cloud.models.file_transcription_usage import FileTranscriptionUsage

logger = logging.getLogger(__name__)

_ENV_CAP = "POCKETPAW_FILE_TRANSCRIPTION_DAILY"
#: Lower than the comprehension cap (500) on purpose: a transcription is the
#: more expensive call of the two, and 100 media files a workspace a day is
#: already far past any honest use of a Files box.
_DEFAULT_CAP = 100


def daily_cap() -> int:
    """Transcriptions per workspace per UTC day. 0 disables the feature.

    A non-integer value is a misconfiguration, not an instruction: we warn and
    use the default rather than reading ``"one hundred"`` as zero and silently
    switching transcription off for the whole deployment.
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
    """Claim one transcription against today's budget for ``workspace_id``.

    Returns ``(allowed, spent, cap)``. ``spent`` INCLUDES this one when
    allowed, so a caller can log "3/100" honestly.

    Increments FIRST and compares after. The increment is the atomic part; a
    check-then-increment would let two concurrent uploads both read 99, both
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
        # No tenant means no counter to charge, and an uncharged transcription
        # is exactly what this file exists to prevent.
        logger.warning("media transcription refused — no workspace on the event")
        return False, 0, cap

    day = _today()
    key = f"{workspace_id}:{day}"
    try:
        # ``get_pymongo_collection``, NOT ``get_motor_collection`` — the latter
        # is beanie 1.x and this repo is on 2.1.0, where the attribute does not
        # exist. Getting it wrong here is invisible in the worst way: the
        # AttributeError lands in the fail-closed ``except`` below, every claim
        # is refused, and the feature reads as "transcription is off" rather
        # than as a bug. ``comprehension_budget.py`` and ``pockets/service.py``
        # carry the same note; a budget on this branch shipped with the wrong
        # name and refused every claim, silently.
        coll = FileTranscriptionUsage.get_pymongo_collection()
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
            "media transcription budget unavailable for workspace=%s; refusing",
            workspace_id,
            exc_info=True,
        )
        return False, 0, cap

    if spent > cap:
        try:
            await coll.update_one({"key": key}, {"$inc": {"used": -1}})
        except Exception:
            logger.debug("could not roll back an over-cap transcription claim", exc_info=True)
        return False, cap, cap
    return True, spent, cap


__all__ = ["daily_cap", "try_spend"]
