# ee/pocketpaw_ee/cloud/mandates/soul_link.py
#
# The mandate ↔ soul bridge. When a mandate binds a ``soul_path`` (a .soul
# file or a soul directory; several lines may share one), the foreman RECALLS
# context from it before planning, every finished shift APPENDS an episodic
# summary, and the cloud station keeps the line's LESSONS there:
#   * ``recall_for_planning(soul_path, query)``  -> list[str]
#   * ``remember_shift(soul_path, summary)``     -> bool
#   * ``note_lesson(soul_path, text)``           -> bool (False: not stored)
#   * ``recall_lessons(soul_path, query)``       -> list[str]
# A lesson is a PROCEDURAL memory in the ``belt-lessons`` domain, so recall
# reads lessons only. ``note_lesson`` dedups the way ``soul note`` does: the
# same ``reconcile_fact`` (installed soul-protocol has no ``Soul.note``) over
# the nearest lessons; a near-duplicate (SKIP) is not stored, a MERGE or CREATE
# is.
#
# Implementation opens the soul through ``pocketpaw.soul.open_soul`` (the
# per-path awaken), then rides the soul-protocol API (``recall``/``remember``),
# saved back in the shape it was read from: a soul directory with
# ``save_local``, a ``.soul`` archive with ``export`` (keys kept so later
# shifts can still sign; written beside the file, then swapped in).
# ``save_local`` on an archive path would mkdir over the file and fail.
# Writes to one path are serialized in-process (``_LOCKS``): awaken → save is a
# read-modify-write, and concurrent trackers would drop each other's memories.
# EVERYTHING is best-effort: a missing file, a corrupt soul, or a protocol
# error logs and degrades to an empty recall / no-op write — a soul failure
# must never wedge a shift or a run.

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# How many memories a planning recall pulls.
_RECALL_LIMIT = 5
LESSON_DOMAIN = "belt-lessons"
_DEDUP_NEIGHBOURS = 10
_LOCKS: dict[str, asyncio.Lock] = {}


def _existing(soul_path: str | None, why: str) -> Path | None:
    if not soul_path:
        return None
    path = Path(soul_path).expanduser()
    if not path.exists():
        logger.warning("mandate soul: %s does not exist — %s skipped", soul_path, why)
        return None
    return path


def _lock(path: Path) -> asyncio.Lock:
    return _LOCKS.setdefault(str(path.resolve()), asyncio.Lock())


async def _save(soul: Any, path: Path) -> None:
    if path.is_dir():
        await soul.save_local(path)
    else:  # write beside it, then swap: a crash mid-write never truncates the soul
        tmp = path.with_name(path.name + ".tmp")
        await soul.export(tmp, include_keys=True)
        tmp.replace(path)


async def recall_for_planning(soul_path: str | None, query: str) -> list[str]:
    """Recall up to ``_RECALL_LIMIT`` memory lines relevant to ``query`` from
    the mandate's soul. Empty list when no soul is bound or anything fails."""
    path = _existing(soul_path, "recall")
    if path is None:
        return []
    try:
        from pocketpaw.soul import open_soul

        soul = await open_soul(path)
        entries = await soul.recall(query, limit=_RECALL_LIMIT)
        return [str(e.content) for e in entries]
    except Exception:  # noqa: BLE001 — soul failures must never wedge a shift
        logger.warning("mandate soul: recall failed for %s", soul_path, exc_info=True)
        return []


async def remember_shift(soul_path: str | None, summary: str) -> bool:
    """Append an episodic shift summary to the mandate's soul and save it
    back in place. Returns True on success; False (logged) on any failure."""
    path = _existing(soul_path, "remember") if summary.strip() else None
    if path is None:
        return False
    try:
        from soul_protocol import MemoryType

        from pocketpaw.soul import open_soul

        async with _lock(path):
            soul = await open_soul(path)
            await soul.remember(summary.strip(), type=MemoryType.EPISODIC, importance=7)
            await _save(soul, path)
        return True
    except Exception:  # noqa: BLE001 — soul failures must never wedge a shift
        logger.warning("mandate soul: remember failed for %s", soul_path, exc_info=True)
        return False


async def note_lesson(soul_path: str | None, text: str) -> bool:
    """Note one lesson unless a near-duplicate is already there (see the
    header). True when it was stored; False when skipped or on any failure."""
    path = _existing(soul_path, "lesson") if text.strip() else None
    if path is None:
        return False
    try:
        from soul_protocol import MemoryType
        from soul_protocol.runtime.memory.dedup import reconcile_fact

        from pocketpaw.soul import open_soul

        async with _lock(path):
            soul = await open_soul(path)
            near = await soul.recall(
                text,
                limit=_DEDUP_NEIGHBOURS,
                types=[MemoryType.PROCEDURAL],
                domain=LESSON_DOMAIN,
            )
            if reconcile_fact(text.strip(), near)[0] == "SKIP":
                return False
            await soul.remember(
                text.strip(), type=MemoryType.PROCEDURAL, importance=7, domain=LESSON_DOMAIN
            )
            await _save(soul, path)
        return True
    except Exception:  # noqa: BLE001 — a lost lesson never fails a run
        logger.warning("mandate soul: lesson failed for %s", soul_path, exc_info=True)
        return False


async def recall_lessons(soul_path: str | None, query: str, limit: int = 5) -> list[str]:
    """The ``limit`` lessons most relevant to ``query``; [] on any failure."""
    path = _existing(soul_path, "lesson recall")
    if path is None:
        return []
    try:
        from soul_protocol import MemoryType

        from pocketpaw.soul import open_soul

        soul = await open_soul(path)
        entries = await soul.recall(
            query, limit=limit, types=[MemoryType.PROCEDURAL], domain=LESSON_DOMAIN
        )
        return [str(e.content) for e in entries]
    except Exception:  # noqa: BLE001 — no lessons is a fine brief
        logger.warning("mandate soul: lesson recall failed for %s", soul_path, exc_info=True)
        return []


__all__ = [
    "LESSON_DOMAIN",
    "note_lesson",
    "recall_for_planning",
    "recall_lessons",
    "remember_shift",
]
