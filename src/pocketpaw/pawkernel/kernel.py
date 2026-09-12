# pawkernel kernel — the composition runtime's entry point.
# Created: 2026-08-24 (feat/pawkernel-compose) — owns the root context, the
#   shared event bus, the deterministic deferred-work queue (dependent
#   re-checks run AFTER the provider's fiber has settled, never mid-apply),
#   the background task set, and settle(): quiescence for the whole runtime.

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from pocketpaw.pawkernel.context import Context
from pocketpaw.pawkernel.events import EventBus
from pocketpaw.pawkernel.fiber import Fiber, Plugin
from pocketpaw.pawkernel.observer import KernelEvent, Observer, null_observer


class Kernel:
    """The composition kernel: one root context and its lifecycle scheduler."""

    def __init__(self, observer: Observer | None = None) -> None:
        self._observer: Observer = observer or null_observer
        self._queue: deque[Callable[[], Awaitable[None]]] = deque()
        self._tasks: list[asyncio.Task] = []
        self.errors: list[BaseException] = []
        self.bus = EventBus(_spawn=self._spawn_awaitable)
        self.root = Context(kernel=self, parent=None, label="root", fiber=None)

    # -- observability ----------------------------------------------------
    def notify(self, event: KernelEvent) -> None:
        self._observer(event)

    # -- scheduling -------------------------------------------------------
    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        """Run ``coro`` as a tracked background task."""
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        task.add_done_callback(self._collect)
        return task

    def _spawn_awaitable(self, awaitable: Awaitable[Any]) -> None:
        self.spawn(awaitable)

    def _collect(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.errors.append(exc)

    def defer(self, fn: Callable[[], Awaitable[None]]) -> None:
        """Queue lifecycle work to run once the current fiber has settled.

        §2: load order is derived from injection. A dependent activated by a
        service published mid-apply must not start loading until the provider
        has finished — so the re-check goes on this queue, not straight into
        the event loop.
        """
        self._queue.append(fn)

    async def settle(self, max_turns: int = 100_000) -> None:
        """Await quiescence: no queued work and no unfinished tasks."""
        for _ in range(max_turns):
            if self._queue:
                await self._queue.popleft()()
                continue
            pending = [t for t in self._tasks if not t.done()]
            if pending:
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                continue
            await asyncio.sleep(0)
            if not self._queue and all(t.done() for t in self._tasks):
                return
        raise RuntimeError("kernel did not reach quiescence")

    # -- composition ------------------------------------------------------
    async def mount(
        self, plugin: Plugin, ctx: Context | None = None, nowait: bool = False
    ) -> Fiber:
        """Mount ``plugin`` into ``ctx`` (the root context by default)."""
        parent_ctx = ctx if ctx is not None else self.root
        parent_fiber = parent_ctx.fiber
        fiber_ctx = parent_ctx.child()
        fiber = Fiber(kernel=self, plugin=plugin, ctx=fiber_ctx, parent=parent_fiber)
        fiber_ctx._fiber = fiber
        if parent_fiber is not None:
            # Mounting a child plugin is an effect of the parent (§3): it is
            # torn down with the parent, and before the parent's own effects.
            parent_fiber.adopt_child(fiber)
        fiber.begin()
        if not nowait:
            await fiber.wait_ready()
        return fiber
