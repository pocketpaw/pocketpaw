# ee/pocketpaw_ee/cloud/mandates/soul_link.py
# Created: 2026-06-11 (feat/belt-mandates, slice 6 — soul wiring, demo bar).
#
# The mandate ↔ soul bridge. When a mandate binds a ``soul_path`` (a .soul
# file), the foreman RECALLS context from it before planning, and every
# finished shift APPENDS an episodic summary to it — the mandate accumulates
# long-lived judgment context across shifts.
#
# Clean, narrow interface (two functions) so the transport can be swapped
# without touching the foreman/service:
#   * ``recall_for_planning(soul_path, query)``  -> list[str]
#   * ``remember_shift(soul_path, summary)``     -> bool
#
# Implementation rides the real soul-protocol API (``Soul.awaken`` →
# ``recall``/``remember`` → ``save_local``). EVERYTHING is best-effort: a
# missing file, a corrupt soul, or a protocol error logs and degrades to an
# empty recall / no-op remember — a soul failure must never wedge a shift.

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# How many memories a planning recall pulls.
_RECALL_LIMIT = 5


async def recall_for_planning(soul_path: str | None, query: str) -> list[str]:
    """Recall up to ``_RECALL_LIMIT`` memory lines relevant to ``query`` from
    the mandate's soul. Empty list when no soul is bound or anything fails."""
    if not soul_path:
        return []
    path = Path(soul_path).expanduser()
    if not path.exists():
        logger.warning("mandate soul: %s does not exist — empty recall", soul_path)
        return []
    try:
        from soul_protocol import Soul

        soul = await Soul.awaken(path)
        entries = await soul.recall(query, limit=_RECALL_LIMIT)
        return [str(e.content) for e in entries]
    except Exception:  # noqa: BLE001 — soul failures must never wedge a shift
        logger.warning("mandate soul: recall failed for %s", soul_path, exc_info=True)
        return []


async def remember_shift(soul_path: str | None, summary: str) -> bool:
    """Append an episodic shift summary to the mandate's soul and save it
    back in place. Returns True on success; False (logged) on any failure."""
    if not soul_path or not summary.strip():
        return False
    path = Path(soul_path).expanduser()
    if not path.exists():
        logger.warning("mandate soul: %s does not exist — remember skipped", soul_path)
        return False
    try:
        from soul_protocol import MemoryType, Soul

        soul = await Soul.awaken(path)
        await soul.remember(summary.strip(), type=MemoryType.EPISODIC, importance=7)
        await soul.save_local(path)
        return True
    except Exception:  # noqa: BLE001 — soul failures must never wedge a shift
        logger.warning("mandate soul: remember failed for %s", soul_path, exc_info=True)
        return False


__all__ = ["recall_for_planning", "remember_shift"]
