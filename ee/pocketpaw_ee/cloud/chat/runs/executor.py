"""RunExecutor — decides where an agent run executes. Selected by
``POCKETPAW_CLOUD_RUN_EXECUTOR`` (``inprocess`` | ``arq``)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Protocol

from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.run_core import execute_run

logger = logging.getLogger(__name__)


class RunExecutor(Protocol):
    async def submit(self, spec: RunSpec) -> None: ...


class InProcessExecutor:
    """Runs ``execute_run()`` in a tracked asyncio task in the web process."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def submit(self, spec: RunSpec) -> None:
        task = asyncio.create_task(self._guarded(spec))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guarded(self, spec: RunSpec) -> None:
        try:
            await execute_run(spec)
        except Exception:
            logger.exception("in-process run %s crashed", spec.run_id)

    async def drain(self) -> None:
        """Await all outstanding run tasks."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


_executor: RunExecutor | None = None


def get_executor() -> RunExecutor:
    global _executor
    if _executor is None:
        mode = os.environ.get("POCKETPAW_CLOUD_RUN_EXECUTOR", "inprocess").lower()
        if mode == "arq":
            from pocketpaw_ee.cloud.chat.runs.arq_executor import ArqExecutor

            _executor = ArqExecutor()
        else:
            _executor = InProcessExecutor()
    return _executor


def _reset_for_tests() -> None:
    global _executor
    _executor = None
