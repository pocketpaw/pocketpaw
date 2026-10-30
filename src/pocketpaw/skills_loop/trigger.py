# skills_loop/trigger.py — session-finalize trigger for the skills-loop reviewer.
# Created: 2026-06-16 (feat/self-improving-skills) — fires the forked write-only
#   reviewer in the BACKGROUND when a workspace agent's session finalizes. Mirrors
#   the fire-and-return background-spawn pattern of
#   MCTaskExecutor.execute_task_background (mission_control/executor.py): create
#   an asyncio task, track it so it can't be GC'd mid-flight or double-dispatched,
#   and return immediately so session teardown never blocks on the review. The
#   reviewer itself runs under the soul-write-only whitelist (see whitelist.py).

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pocketpaw.skills_loop.reviewer import ProcedureExtractor, SkillsLoopReviewer

logger = logging.getLogger(__name__)


class SkillsLoopTrigger:
    """Spawns background reviewer runs at session-finalize.

    Holds strong references to in-flight review tasks (asyncio does not keep a
    task alive on its own) and guards against double-dispatch for the same
    session key — the same discipline ``execute_task_background`` applies.
    """

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task] = {}

    def on_session_finalize(
        self,
        session_key: str,
        *,
        soul: Any,
        extractor: ProcedureExtractor,
        transcript: str,
        importance: int = 6,
    ) -> bool:
        """Launch a background review for ``session_key``. Returns immediately.

        MUST be called from within a running asyncio event loop — it schedules
        the review via ``asyncio.create_task`` (the same requirement
        ``execute_task_background`` has). Calling it from synchronous,
        loop-less code raises ``RuntimeError: no running event loop``.

        Returns True if a review was launched, False if one is already in
        flight for this session (double-dispatch guard).
        """
        if session_key in self._running and not self._running[session_key].done():
            logger.warning("skills_loop: review already running for %s, skipping", session_key)
            return False

        reviewer = SkillsLoopReviewer(soul=soul, extractor=extractor, importance=importance)

        async def _run() -> None:
            try:
                await reviewer.review_session(transcript)
            except Exception:  # noqa: BLE001 — a failed review must never crash teardown
                logger.exception("skills_loop: background review failed for %s", session_key)
            finally:
                self._running.pop(session_key, None)

        self._running[session_key] = asyncio.create_task(_run())
        return True

    def is_running(self, session_key: str) -> bool:
        task = self._running.get(session_key)
        return task is not None and not task.done()


__all__ = ["SkillsLoopTrigger"]
