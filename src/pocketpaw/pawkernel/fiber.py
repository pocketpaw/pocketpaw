# pawkernel fiber — SEMANTICS.md §2, §3, §4.
# Created: 2026-08-24 (feat/pawkernel-compose) — a fiber is the runtime handle
#   for one mounted plugin instance. It owns the injection gate (PENDING until
#   every required service exists), the effect stack (LIFO, run-at-most-once
#   disposers), the child fibers (disposed BEFORE the parent's own effects),
#   and the dragons: dispose-during-LOADING awaits apply before cleaning up
#   everything apply collected, and a throw in apply rolls back every
#   collected effect before landing in FAILED.
# Updated: 2026-08-24 (feat/pawkernel-compose) — §3 fourth dragon: a throwing
#   disposer no longer aborts the LIFO chain. Errors are contained per
#   disposer, reported through DisposerErrorEvent as they happen, and raised
#   to dispose()'s caller as an ExceptionGroup only after the fiber has
#   reached its target state. CancelledError is never contained.
# Updated: 2026-08-25 (feat/pawkernel-compose) — §4: dispose() is total. A
#   FAILED fiber now retires to DISPOSED instead of staying FAILED forever;
#   fiber.error keeps the original cause.

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pocketpaw.pawkernel.errors import EffectRejected
from pocketpaw.pawkernel.observer import DisposerErrorEvent, FiberStateEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pocketpaw.pawkernel.context import Context
    from pocketpaw.pawkernel.kernel import Kernel


class FiberState:
    """The lifecycle states of SEMANTICS.md §4, plus an unemitted INIT."""

    INIT = "INIT"
    PENDING = "PENDING"
    LOADING = "LOADING"
    ACTIVE = "ACTIVE"
    UNLOADING = "UNLOADING"
    DISPOSED = "DISPOSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Inject:
    """A plugin's injection declaration (§2)."""

    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    @classmethod
    def of(cls, spec: Any) -> Inject:
        if spec is None:
            return cls()
        if isinstance(spec, Inject):
            return spec
        if isinstance(spec, dict):
            return cls(
                required=tuple(spec.get("required") or ()),
                optional=tuple(spec.get("optional") or ()),
            )
        if isinstance(spec, (list, tuple)):
            return cls(required=tuple(spec))
        raise TypeError(f"cannot read an inject declaration from {spec!r}")


class Plugin(Protocol):
    """A unit of composition: a name, an inject declaration, an apply body."""

    name: str
    inject: Inject

    async def apply(self, ctx: Context) -> None: ...


@dataclass
class SimplePlugin:
    """Callable-backed plugin, for callers that do not need a class."""

    name: str
    apply_fn: Callable[[Context], Any]
    inject: Inject = field(default_factory=Inject)

    async def apply(self, ctx: Context) -> None:
        result = self.apply_fn(ctx)
        if inspect.isawaitable(result):
            await result


class Disposer:
    """A collected disposer. Runs at most once; repeat disposal is a no-op."""

    __slots__ = ("_fn", "_done", "name")

    def __init__(self, fn: Any, name: str | None = None) -> None:
        self._fn = fn if callable(fn) else None
        self._done = False
        self.name = name

    @property
    def done(self) -> bool:
        return self._done

    async def run(self) -> None:
        if self._done:
            return
        self._done = True
        if self._fn is None:
            return
        result = self._fn()
        if inspect.isawaitable(result):
            # An async disposer MUST settle. Shield it so a cancellation of
            # the awaiting frame cannot abandon half-finished cleanup.
            await asyncio.shield(asyncio.ensure_future(result))


class Fiber:
    """The runtime handle for one mounted plugin instance."""

    def __init__(
        self,
        kernel: Kernel,
        plugin: Plugin,
        ctx: Context,
        parent: Fiber | None = None,
    ) -> None:
        self.kernel = kernel
        self.plugin = plugin
        self.name = getattr(plugin, "name", plugin.__class__.__name__)
        self.inject = Inject.of(getattr(plugin, "inject", None))
        self.parent = parent
        self.ctx = ctx
        self.state = FiberState.INIT
        self.error: BaseException | None = None
        # Every disposer error this fiber has contained, across every teardown
        # it has been through. This is how the FAILED and back-to-PENDING
        # paths stay observable: they have no caller to raise at.
        self.teardown_errors: list[Exception] = []

        self._effects: list[Disposer] = []
        self._children: list[Fiber] = []
        self._tearing_down = False
        self._load_task: asyncio.Future | None = None
        self._dispose_future: asyncio.Future | None = None
        self._unload_requested = False
        self._watched: list[Any] = []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Fiber {self.name} {self.state}>"

    # -- state ------------------------------------------------------------
    def _enter(self, state: str) -> None:
        self.state = state
        self.kernel.notify(FiberStateEvent(fiber=self.name, state=state))

    @property
    def children(self) -> Sequence[Fiber]:
        return tuple(self._children)

    # -- injection --------------------------------------------------------
    def _watch_deps(self) -> None:
        # Only required keys gate activation, so only required keys are
        # watched: an optional service appearing MUST NOT force a reload.
        for key in self.inject.required:
            cell = self.ctx._cell(key)
            cell.watch(self)
            self._watched.append(cell)

    def _unwatch_deps(self) -> None:
        for cell in self._watched:
            cell.unwatch(self)
        self._watched.clear()

    def _deps_met(self) -> bool:
        return all(self.ctx.get(key) is not None for key in self.inject.required)

    # -- start ------------------------------------------------------------
    def begin(self) -> None:
        """Synchronously decide PENDING vs LOADING and start apply if ready.

        Creating the load task here (rather than inside a coroutine that has
        not run yet) is what makes ``dispose`` during LOADING deterministic:
        a dispose issued immediately after a no-wait mount always finds a load
        task to await.
        """
        self._watch_deps()
        if not self._deps_met():
            self._enter(FiberState.PENDING)
            return
        self._start_load()

    def _start_load(self) -> None:
        self._load_task = self.kernel.spawn(self._load())

    async def wait_ready(self) -> None:
        """Await the current load, if any. Never propagates apply's error."""
        if self._load_task is not None and not self._load_task.done():
            await asyncio.shield(self._load_task)

    async def _load(self) -> None:
        self._enter(FiberState.LOADING)
        try:
            await self.plugin.apply(self.ctx)
        except asyncio.CancelledError:  # pragma: no cover - defensive
            raise
        except BaseException as exc:
            # §3 dragon: roll every collected effect back, then FAILED.
            self.error = exc
            await self._rollback()
            self._enter(FiberState.FAILED)
            return
        if self._dispose_future is not None or self._unload_requested:
            # §4 dragon: a disposal was requested while we were LOADING. Do
            # NOT pass through ACTIVE; the disposer owns the cleanup and runs
            # it only after this coroutine has fully returned.
            return
        self._enter(FiberState.ACTIVE)

    # -- effects ----------------------------------------------------------
    def effect(self, setup: Callable[[], Any], name: str | None = None) -> Disposer:
        """Run ``setup`` and collect its disposer (§3).

        Creation while PENDING or LOADING is legal. Creation while the fiber
        is tearing down (UNLOADING, or rolling back a failed apply) is
        rejected, and creation on a settled fiber is rejected too.
        """
        if self._tearing_down or self.state in (
            FiberState.UNLOADING,
            FiberState.DISPOSED,
            FiberState.FAILED,
        ):
            raise EffectRejected(self.name, FiberState.UNLOADING)
        disposer = Disposer(setup(), name=name)
        self._effects.append(disposer)
        return disposer

    def adopt_child(self, child: Fiber) -> None:
        self._children.append(child)

    # -- teardown ---------------------------------------------------------
    async def _teardown(self) -> list[Exception]:
        """Dispose children first, then own effects LIFO. Never re-entrant.

        §3 (fourth dragon): a throwing disposer MUST NOT abort the chain.
        Each failure is contained, reported through the observer at the moment
        it happens, and collected — every remaining disposer still runs and the
        caller decides how to surface the collected errors once unwinding has
        finished.

        ``asyncio.CancelledError`` is deliberately NOT contained. It is not a
        disposer error, and swallowing it here would mask a cancellation.
        """
        errors: list[Exception] = []
        self._tearing_down = True
        try:
            for child in reversed(list(self._children)):
                try:
                    await child.dispose()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors.append(exc)
            self._children.clear()
            for disposer in reversed(list(self._effects)):
                try:
                    await disposer.run()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    errors.append(exc)
                    self.kernel.notify(
                        DisposerErrorEvent(owner=self.name, effect=disposer.name, error=exc)
                    )
            self._effects.clear()
        finally:
            self._tearing_down = False
        self.teardown_errors.extend(errors)
        return errors

    def _report(self, errors: list[Exception]) -> None:
        """Record teardown errors where no caller can receive them.

        The FAILED and back-to-PENDING paths are driven by the kernel, not by
        a caller awaiting cleanup, so raising would only break the transition
        that §3 requires to complete. They stay observable on the fiber and on
        ``kernel.errors``.
        """
        self.kernel.errors.extend(errors)

    async def _rollback(self) -> None:
        """Roll back a failed apply. Emits no UNLOADING — §4's FAILED edge
        goes straight from LOADING to FAILED, and the fixture's trace says so.
        """
        self._report(await self._teardown())

    # -- unload (dependency withdrawn) ------------------------------------
    async def _unload_to_pending(self) -> None:
        if self.state not in (FiberState.ACTIVE, FiberState.LOADING):
            return
        self._unload_requested = True
        try:
            if self._load_task is not None and not self._load_task.done():
                await asyncio.shield(self._load_task)
            if self.state in (FiberState.DISPOSED, FiberState.FAILED):
                return
            self._enter(FiberState.UNLOADING)
            errors = await self._teardown()
            self._enter(FiberState.PENDING)
            self._report(errors)
        finally:
            self._unload_requested = False

    async def recheck(self) -> None:
        """Re-evaluate the injection gate after a watched service changed."""
        if self.state in (FiberState.DISPOSED, FiberState.FAILED):
            return
        if self._dispose_future is not None:
            return
        met = self._deps_met()
        if met and self.state == FiberState.PENDING:
            self._start_load()
            await self.wait_ready()
        elif not met and self.state in (FiberState.ACTIVE, FiberState.LOADING):
            await self._unload_to_pending()

    # -- dispose ----------------------------------------------------------
    async def dispose(self) -> None:
        """Dispose this fiber. Resolves only once all cleanup has settled.

        Single-flight: every caller awaits the same shielded future, so
        repeated disposal — including disposal racing a cancellation — is
        idempotent rather than an error.

        If any disposer raised, cleanup still completed in full and the fiber
        still reached DISPOSED; the collected errors are then raised here as
        an ``ExceptionGroup``. §3 requires the error be observable, and this
        is the caller who asked for the cleanup, so it is reported — never
        swallowed. Silent success would tell a caller awaiting cleanup that
        it finished cleanly when it did not.
        """
        if self._dispose_future is None:
            self._dispose_future = self.kernel.spawn(self._do_dispose())
        await asyncio.shield(self._dispose_future)

    async def _do_dispose(self) -> None:
        # §4 dragon: if apply is still running, await it to completion first.
        # Cleanup must not run concurrently with the remainder of apply.
        if self._load_task is not None and not self._load_task.done():
            await asyncio.shield(self._load_task)

        if self.state == FiberState.DISPOSED:
            # Already retired. Repeat disposal is a no-op, not an error.
            return

        errors: list[Exception] = []
        if self.state in (FiberState.ACTIVE, FiberState.LOADING):
            self._enter(FiberState.UNLOADING)
            errors = await self._teardown()
        # PENDING, INIT and FAILED have nothing to unwind — PENDING and INIT
        # never collected anything, and a FAILED fiber was already rolled
        # back. §4: dispose() is total, so they still retire to DISPOSED
        # rather than sitting in a state with no outgoing edge. The
        # originating failure stays on ``fiber.error``.

        self._unwatch_deps()
        if self.parent is not None and self in self.parent._children:
            self.parent._children.remove(self)
        self._enter(FiberState.DISPOSED)

        # Reported only after unwinding has completed and the fiber has
        # reached its target state — never instead of them.
        if errors:
            raise ExceptionGroup(f"disposer(s) failed while unloading {self.name!r}", errors)
