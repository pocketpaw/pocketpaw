# pawkernel event bus — SEMANTICS.md §5.
# Created: 2026-08-24 (feat/pawkernel-compose) — four dispatch modes:
#   emit (fire-and-forget), waterfall (synchronous around-middleware),
#   parallel (fan out, await all), serial (ordered, first non-absent wins).
#   An event name carries exactly one mode for its whole lifetime; a second
#   registration under a different mode raises DispatchModeConflict.

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pocketpaw.pawkernel.errors import DispatchModeConflict

EMIT = "emit"
WATERFALL = "waterfall"
PARALLEL = "parallel"
SERIAL = "serial"
MODES = (EMIT, WATERFALL, PARALLEL, SERIAL)


@dataclass
class _Listener:
    callback: Callable[..., Any]
    mode: str


@dataclass
class EventBus:
    """Name-keyed listener registry with one dispatch mode per event."""

    _listeners: dict[str, list[_Listener]] = field(default_factory=dict)
    _modes: dict[str, str] = field(default_factory=dict)
    _spawn: Callable[[Any], None] | None = None

    # -- registration -----------------------------------------------------
    def on(self, event: str, callback: Callable[..., Any], mode: str = EMIT) -> Callable[[], None]:
        """Register ``callback`` for ``event``. Returns a remover.

        Registration is an effect (§5) — callers wrap this in
        ``Context.effect`` so the listener is removed on unload.
        """
        if mode not in MODES:
            raise ValueError(f"unknown dispatch mode {mode!r}")
        declared = self._modes.setdefault(event, mode)
        if declared != mode:
            raise DispatchModeConflict(event, declared, mode)
        listener = _Listener(callback=callback, mode=mode)
        self._listeners.setdefault(event, []).append(listener)

        def remove() -> None:
            bucket = self._listeners.get(event)
            if bucket and listener in bucket:
                bucket.remove(listener)

        return remove

    def _bucket(self, event: str, mode: str) -> list[_Listener]:
        declared = self._modes.get(event)
        if declared is not None and declared != mode:
            raise DispatchModeConflict(event, declared, mode)
        return list(self._listeners.get(event, ()))

    # -- dispatch ---------------------------------------------------------
    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Fire-and-forget observation. Not awaited, returns nothing."""
        for listener in self._bucket(event, EMIT):
            result = listener.callback(*args, **kwargs)
            if inspect.isawaitable(result) and self._spawn is not None:
                self._spawn(result)

    def waterfall(self, event: str, *args: Any) -> Any:
        """Around-middleware. Synchronous, returns a value.

        Each listener is called as ``cb(*args, next=next)``. Calling ``next()``
        delegates to the remaining listeners and returns their result, which
        the listener MAY wrap. Returning without calling ``next()``
        short-circuits: downstream listeners never run. The innermost ``next``
        yields the first argument (the value threaded through the chain).
        """
        listeners = self._bucket(event, WATERFALL)

        def step(index: int, current: tuple[Any, ...]) -> Any:
            if index >= len(listeners):
                return current[0] if current else None

            def nxt(*override: Any) -> Any:
                return step(index + 1, override if override else current)

            return listeners[index].callback(*current, next=nxt)

        return step(0, args)

    async def parallel(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Fan out concurrently and await every listener."""
        listeners = self._bucket(event, PARALLEL)
        awaitables = []
        for listener in listeners:
            result = listener.callback(*args, **kwargs)
            if inspect.isawaitable(result):
                awaitables.append(result)
        if awaitables:
            await asyncio.gather(*awaitables)

    async def serial(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Ordered dispatch; the first non-``None`` result wins."""
        for listener in self._bucket(event, SERIAL):
            result = listener.callback(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                return result
        return None
