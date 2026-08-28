# ee/pocketpaw_ee/cloud/other_hand/illustration_budget.py — a hard daily ceiling
# on generated illustrations.
#
# Created 2026-08-28.
#
# The illustration tool is agent-driven: it fires when the agent judges a
# picture would help, which is the right product behaviour and also the reason
# this file exists. Each generation costs real money on the PLATFORM's account
# — a user's own BYOK key pays for their tokens and not for this — so "the
# agent decides" without a ceiling is an unbounded bill attached to a signup
# form.
#
# Deliberately coarse: one counter per workspace per UTC day, checked and
# incremented in one atomic update. It is a cost ceiling, not billing — it does
# not need to be exact under concurrency, it needs to be impossible to blow
# past by an order of magnitude.
#
# It fails CLOSED. If the counter cannot be read or written, the answer is no.
# A degraded database must not become an open tab at the illustrator: the cost
# of being wrong in that direction is money, and in the other direction is a
# turn that explains in words.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from pymongo import ReturnDocument

from pocketpaw_ee.cloud.models.other_hand_usage import IllustrationUsage

logger = logging.getLogger(__name__)

_ENV_CAP = "POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS"
_DEFAULT_CAP = 20


def daily_cap() -> int:
    """Illustrations per workspace per UTC day. 0 disables the feature."""
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


async def try_spend(workspace_id: str | None = None) -> tuple[bool, int, int]:
    """Claim one illustration against today's budget.

    Returns ``(allowed, spent, cap)``. ``spent`` is the count INCLUDING this
    one when allowed, so a caller can report "3/20" honestly.

    Increments first and compares after — the increment is the atomic part, and
    a check-then-increment would let two concurrent turns both pass the check.
    An over-cap claim is rolled back so a rejected turn does not consume budget
    it never spent.
    """
    cap = daily_cap()
    if cap <= 0:
        return False, 0, 0

    if workspace_id is None:
        # The same public accessor every other in-process MCP tool uses to
        # resolve tenancy (see mcp_servers/media.py::_identity).
        try:
            from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id

            workspace_id = current_workspace_id()
        except Exception:  # noqa: BLE001 — no tenancy resolves to a refusal below
            workspace_id = None
    if not workspace_id:
        # No tenant means no budget to charge, and an uncharged generation is
        # exactly the hole this file exists to close.
        logger.warning("other-hand: illustration refused — no workspace in context")
        return False, 0, cap

    key = f"{workspace_id}:{_today()}"
    try:
        coll = IllustrationUsage.get_motor_collection()
        doc = await coll.find_one_and_update(
            {"key": key},
            {
                "$inc": {"used": 1},
                "$setOnInsert": {
                    "workspace": workspace_id,
                    "day": _today(),
                    "created_at": datetime.now(UTC),
                },
                "$set": {"updated_at": datetime.now(UTC)},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        spent = int((doc or {}).get("used", cap + 1))
    except Exception:
        # Fail closed — see the module note.
        logger.warning("other-hand: illustration budget unavailable; refusing", exc_info=True)
        return False, 0, cap

    if spent > cap:
        # Give it back: this turn is not getting a picture, so it should not
        # hold a slot that a later turn could have used.
        try:
            await coll.update_one({"key": key}, {"$inc": {"used": -1}})
        except Exception:
            logger.debug("other-hand: could not roll back an over-cap claim", exc_info=True)
        return False, cap, cap
    return True, spent, cap
