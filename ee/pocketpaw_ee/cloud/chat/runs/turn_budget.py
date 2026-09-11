# ee/pocketpaw_ee/cloud/chat/runs/turn_budget.py — a hard daily ceiling on how
# many agent runs one workspace may start.
#
# Created 2026-09-11 (feat/abuse-budgets).
#
# The gap this closes: the credit quota already in ``run_core`` is the right
# long-term ceiling on spend, and it is gated on ``billing_enforced``, which
# defaults to False. A deployment that has not switched billing on therefore
# has NO bound on what one signed-up account can spend on models. Guests have
# had one since ``guest_budget`` (40 turns a day); a free signup had less
# protection than a guest, which is backwards — a guest cannot even upload.
#
# ONE counter, not six. Every model call the platform pays for happens inside
# a run: the reply itself, and any tool the run calls (image generation,
# speech, OCR, translate, research, web search). Bounding runs bounds all of
# them, and six separate counters would be six places for the bound to be
# missing.
#
# NOT gated on ``billing_enforced`` — it is an abuse ceiling, not a plan. A
# workspace that pays for more usage raises its credit allowance; this only
# stops a single day from being pathological, so the default sits far above a
# heavy human day and far below a script.
#
# ``0`` means NO CAP. This DIVERGES from ``comprehension_budget``, where ``0``
# disables the feature and so blocks everything. Deliberate: a comprehension is
# an extra a workspace can live without for a day, whereas the agent answering
# IS the product. An env typo that reads as ``0`` must not take chat off the
# air for every tenant at once.
#
# It fails CLOSED on a database error. That costs nothing here: a run persists
# its messages to the same Mongo, so a database that cannot serve this counter
# cannot serve the run either.
#
# Structure copied from ``uploads/comprehension_budget.py``, including the
# increment-then-compare ordering and the rollback of an over-cap claim.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from pymongo import ReturnDocument

from pocketpaw_ee.cloud.models.workspace_turn_usage import WorkspaceTurnUsage

logger = logging.getLogger(__name__)

_ENV_CAP = "POCKETPAW_WORKSPACE_TURNS_DAILY"

#: Agent runs per workspace per UTC day. 500 is well past a heavy day of human
#: conversation across a whole workspace, and well short of a loop. Guests get
#: 40; this is the signed-up equivalent.
_DEFAULT_CAP = 500


def daily_cap() -> int:
    """Runs per workspace per UTC day. ``0`` means uncapped.

    A non-integer value is a misconfiguration, not an instruction: warn and use
    the default rather than reading ``"five hundred"`` as ``0``.
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
    """Claim one agent run against today's budget for ``workspace_id``.

    Returns ``(allowed, spent, cap)``. ``spent`` INCLUDES this one when
    allowed, so a caller can log "3/500" honestly.

    Increments FIRST and compares after: a check-then-increment lets two
    concurrent sends both read 499 and both spend. An over-cap claim is rolled
    back so a refused run does not hold a slot a later one could have used.
    """
    cap = daily_cap()
    if cap <= 0:
        return True, 0, 0

    if not workspace_id:
        # No tenant means no counter to charge, and an uncharged run is exactly
        # what this file exists to prevent.
        logger.warning("agent run refused — no workspace to charge")
        return False, 0, cap

    day = _today()
    key = f"{workspace_id}:{day}"
    try:
        # ``get_pymongo_collection``, NOT ``get_motor_collection`` — see the
        # note in ``uploads/comprehension_budget.py``; the wrong name fails
        # closed and reads as "chat is down" rather than as a bug here.
        coll = WorkspaceTurnUsage.get_pymongo_collection()
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
        # reading it as "0 spent" would be a permanently open gate.
        spent = int((doc or {}).get("used", cap + 1))
    except Exception:
        logger.warning(
            "turn budget unavailable for workspace=%s; refusing", workspace_id, exc_info=True
        )
        return False, 0, cap

    if spent > cap:
        try:
            await coll.update_one({"key": key}, {"$inc": {"used": -1}})
        except Exception:
            logger.debug("could not roll back an over-cap run claim", exc_info=True)
        return False, cap, cap
    return True, spent, cap


__all__ = ["daily_cap", "try_spend"]
